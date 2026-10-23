//! The HTTP side: routes, headers, rate limits and the sweeper.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{ConnectInfo, Path as UrlPath, Request, State};
use axum::http::header::{self, HeaderName, HeaderValue};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;
use tokio_util::io::ReaderStream;

use crate::clock::Clock;
use crate::config::Config;
use crate::error::ApiError;
use crate::limiter::Limiter;
use crate::store::{secrets_equal, sha256_hex, valid_id, valid_sha, Auth, Store};

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; \
media-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const IMMUTABLE: &str = "public, max-age=31536000, immutable";
const MAX_REFS: usize = 256;

pub struct App {
    pub cfg: Arc<Config>,
    pub clock: Clock,
    pub store: Store,
    reads: Limiter,
    writes: Limiter,
}

pub type Shared = Arc<App>;

impl App {
    pub async fn new(cfg: Config, clock: Clock) -> std::io::Result<Shared> {
        let cfg = Arc::new(cfg);
        Ok(Arc::new(App {
            store: Store::open(cfg.clone(), clock.clone()).await?,
            reads: Limiter::new(cfg.read_burst, cfg.read_per_minute),
            writes: Limiter::new(cfg.write_burst, cfg.write_per_minute),
            cfg,
            clock,
        }))
    }

    /// One sweep: expired publications, old tombstones, idle rate-limit entries.
    pub async fn sweep(&self) {
        let stats = self.store.sweep().await;
        self.reads.forget_idle(self.clock.now_ms());
        self.writes.forget_idle(self.clock.now_ms());
        if stats != Default::default() {
            tracing::info!("sweep: {stats:?}");
        }
    }
}

/// Sweeps now and then every `sweep_secs`, for as long as the process lives.
pub fn spawn_sweeper(app: Shared) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(app.cfg.sweep_secs.max(1)));
        loop {
            tick.tick().await;
            app.sweep().await;
        }
    });
}

pub fn router(app: Shared) -> Router {
    Router::new()
        .route("/health", get(|| async { "ok" }))
        .route("/p/{id}", get(viewer_index))
        .route("/p/{dir}/{*rest}", get(viewer_asset))
        .route(
            "/pub/{id}",
            get(get_manifest)
                .put(put_manifest)
                .delete(delete_publication),
        )
        .route("/pub/{id}/{sha}", get(get_blob).put(put_blob))
        .layer(middleware::from_fn_with_state(app.clone(), rate_limit))
        .layer(middleware::map_response(security_headers))
        .with_state(app)
}

// ---- middleware ----

async fn security_headers(mut res: Response) -> Response {
    let h = res.headers_mut();
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    res
}

fn client_ip(app: &App, req: &Request) -> IpAddr {
    if app.cfg.trust_proxy {
        // The proxy appends the address it saw: the last entry is the one we can trust.
        let forwarded = req
            .headers()
            .get("x-forwarded-for")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.rsplit(',').next())
            .and_then(|v| v.trim().parse().ok());
        if let Some(ip) = forwarded {
            return ip;
        }
    }
    req.extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map_or(IpAddr::V4(Ipv4Addr::UNSPECIFIED), |c| c.0.ip())
}

async fn rate_limit(State(app): State<Shared>, req: Request, next: Next) -> Response {
    if req.uri().path() == "/health" {
        return next.run(req).await;
    }
    let limiter = if matches!(*req.method(), Method::GET | Method::HEAD) {
        &app.reads
    } else {
        &app.writes
    };
    match limiter.take(client_ip(&app, &req), app.clock.now_ms()) {
        Ok(()) => next.run(req).await,
        Err(secs) => {
            let mut e = ApiError::new(StatusCode::TOO_MANY_REQUESTS, "rate_limited");
            e.retry_after = Some(secs);
            e.into_response()
        }
    }
}

// ---- helpers ----

fn header_str<'a>(h: &'a HeaderMap, name: &str) -> Option<&'a str> {
    h.get(name).and_then(|v| v.to_str().ok())
}

fn header_u64(h: &HeaderMap, name: &'static str, missing: &'static str) -> Result<u64, ApiError> {
    header_str(h, name)
        .and_then(|v| v.trim().parse().ok())
        .ok_or(ApiError::bad(missing))
}

fn check_id(id: &str) -> Result<(), ApiError> {
    valid_id(id).then_some(()).ok_or(ApiError::bad("bad_id"))
}

fn check_sha(sha: &str) -> Result<(), ApiError> {
    valid_sha(sha)
        .then_some(())
        .ok_or(ApiError::bad("bad_sha256"))
}

/// The publisher's token (as its hash) from `Authorization: Bearer`, and whether the host's
/// upload token, if it has one, came with it.
fn auth_of(app: &App, h: &HeaderMap) -> Result<Auth, ApiError> {
    let token = header_str(h, "authorization")
        .and_then(|v| v.split_once(' '))
        .filter(|(scheme, _)| scheme.eq_ignore_ascii_case("bearer"))
        .map(|(_, t)| t.trim())
        .ok_or(ApiError::new(StatusCode::UNAUTHORIZED, "token_required"))?;
    if token.is_empty() || token.len() > 256 {
        return Err(ApiError::bad("bad_token"));
    }
    let upload_ok = app
        .cfg
        .upload_token
        .as_deref()
        .is_none_or(|want| secrets_equal(want, header_str(h, "x-upload-token").unwrap_or("")));
    Ok(Auth {
        token_hash: sha256_hex(token.as_bytes()),
        upload_ok,
    })
}

/// A body of at most `max` bytes; the length is checked before and while reading.
async fn read_body(h: &HeaderMap, body: Body, max: u64) -> Result<Vec<u8>, ApiError> {
    let too_big = || ApiError::new(StatusCode::PAYLOAD_TOO_LARGE, "too_large");
    if header_u64(h, "content-length", "").is_ok_and(|n| n > max) {
        return Err(too_big());
    }
    let bytes = axum::body::to_bytes(body, max as usize)
        .await
        .map_err(|_| too_big())?;
    Ok(bytes.to_vec())
}

fn etag_matches(h: &HeaderMap, sha: &str) -> bool {
    header_str(h, "if-none-match").is_some_and(|v| {
        v.split(',').any(|t| {
            let t = t.trim();
            let t = t.strip_prefix("W/").unwrap_or(t).trim_matches('"');
            t == "*" || t == sha
        })
    })
}

fn set(res: &mut Response, name: HeaderName, value: &str) {
    if let Ok(v) = HeaderValue::from_str(value) {
        res.headers_mut().insert(name, v);
    }
}

// ---- publications ----

async fn get_manifest(
    State(app): State<Shared>,
    UrlPath(id): UrlPath<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    check_id(&id)?;
    let (meta, bytes) = app.store.get_manifest(&id).await?;
    let sha = meta.manifest_sha.unwrap_or_default();
    let mut res = if etag_matches(&headers, &sha) {
        StatusCode::NOT_MODIFIED.into_response()
    } else {
        let mut res = bytes.into_response();
        set(&mut res, header::CONTENT_TYPE, "application/octet-stream");
        res
    };
    set(&mut res, header::ETAG, &format!("\"{sha}\""));
    set(&mut res, header::CACHE_CONTROL, "no-cache");
    set(
        &mut res,
        HeaderName::from_static("x-pub-version"),
        &meta.version.to_string(),
    );
    set(
        &mut res,
        HeaderName::from_static("x-pub-expires"),
        &meta.expires.to_string(),
    );
    Ok(res)
}

async fn get_blob(
    State(app): State<Shared>,
    UrlPath((id, sha)): UrlPath<(String, String)>,
) -> Result<Response, ApiError> {
    check_id(&id)?;
    check_sha(&sha)?;
    let (file, len) = app.store.open_blob(&id, &sha).await?;
    let mut res = Response::new(Body::from_stream(ReaderStream::new(file)));
    set(&mut res, header::CONTENT_TYPE, "application/octet-stream");
    set(&mut res, header::CONTENT_LENGTH, &len.to_string());
    set(&mut res, header::CACHE_CONTROL, IMMUTABLE);
    set(&mut res, header::ETAG, &format!("\"{sha}\""));
    Ok(res)
}

async fn put_blob(
    State(app): State<Shared>,
    UrlPath((id, sha)): UrlPath<(String, String)>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, ApiError> {
    check_id(&id)?;
    check_sha(&sha)?;
    let auth = auth_of(&app, &headers)?;
    app.store.check_writer(&id, &auth).await?;
    let bytes = read_body(&headers, body, app.cfg.blob_max_bytes).await?;
    if sha256_hex(&bytes) != sha {
        return Err(ApiError::bad("sha256_mismatch"));
    }
    let created = app.store.put_blob(&id, &sha, &bytes, &auth).await?;
    Ok(if created {
        StatusCode::CREATED
    } else {
        StatusCode::NO_CONTENT
    }
    .into_response())
}

async fn put_manifest(
    State(app): State<Shared>,
    UrlPath(id): UrlPath<String>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, ApiError> {
    check_id(&id)?;
    let auth = auth_of(&app, &headers)?;
    app.store.check_writer(&id, &auth).await?;

    let version = header_u64(&headers, "x-pub-version", "bad_version")?;
    if version == 0 {
        return Err(ApiError::bad("bad_version"));
    }
    let expires = header_u64(&headers, "x-pub-expires", "bad_expires")?;
    let now = app.clock.now();
    if expires <= now || expires > now + app.cfg.max_days * 86_400 {
        return Err(ApiError::bad("expiry_out_of_range")
            .with(json!({ "now": now, "max_days": app.cfg.max_days })));
    }
    let mut refs: Vec<String> = Vec::new();
    for sha in header_str(&headers, "x-pub-blobs").unwrap_or("").split(',') {
        let sha = sha.trim();
        if sha.is_empty() {
            continue;
        }
        check_sha(sha)?;
        if !refs.iter().any(|r| r == sha) {
            refs.push(sha.to_string());
        }
    }
    if refs.len() > MAX_REFS {
        return Err(ApiError::bad("too_many_blobs"));
    }

    let manifest = read_body(&headers, body, app.cfg.manifest_max_bytes).await?;
    if manifest.is_empty() {
        return Err(ApiError::bad("empty_manifest"));
    }
    let meta = app
        .store
        .put_manifest(&id, &manifest, version, expires, &refs, &auth)
        .await?;
    Ok(Json(json!({
        "version": meta.version,
        "expires": meta.expires,
        "sha256": meta.manifest_sha,
    }))
    .into_response())
}

async fn delete_publication(
    State(app): State<Shared>,
    UrlPath(id): UrlPath<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    check_id(&id)?;
    let auth = auth_of(&app, &headers)?;
    app.store.delete(&id, &auth).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

// ---- the viewer ----

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn viewer_response(path: &Path, bytes: Vec<u8>, cache: &str) -> Response {
    let mut res = bytes.into_response();
    set(&mut res, header::CONTENT_TYPE, content_type(path));
    set(&mut res, header::CONTENT_SECURITY_POLICY, CSP);
    set(&mut res, header::CACHE_CONTROL, cache);
    res
}

/// Any `/p/<something>` is the viewer: it reads the id and the key from its own address and
/// shows "not found" itself, so the host never says which ids exist to someone without a link.
async fn viewer_index(State(app): State<Shared>) -> Result<Response, ApiError> {
    let path = app.cfg.viewer_dir.join("index.html");
    match tokio::fs::read(&path).await {
        Ok(bytes) => Ok(viewer_response(&path, bytes, "no-store")),
        Err(_) => Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "viewer_missing",
        )),
    }
}

/// `/p/<dir>/<path>` serves `<viewer-dir>/<dir>/<path>` (the build's `assets/` folder).
async fn viewer_asset(
    State(app): State<Shared>,
    UrlPath((dir, rest)): UrlPath<(String, String)>,
) -> Result<Response, ApiError> {
    let not_found = || ApiError::new(StatusCode::NOT_FOUND, "not_found");
    let mut path: PathBuf = app.cfg.viewer_dir.clone();
    for part in Path::new(&dir).join(&rest).components() {
        match part {
            Component::Normal(p)
                if !p.to_string_lossy().starts_with('.') && !p.to_string_lossy().contains('\\') =>
            {
                path.push(p)
            }
            _ => return Err(not_found()),
        }
    }
    let bytes = tokio::fs::read(&path).await.map_err(|_| not_found())?;
    Ok(viewer_response(&path, bytes, "no-cache"))
}
