//! The HTTP side: routes, headers, rate limits and the sweeper.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::ws::WebSocketUpgrade;
use axum::extract::{ConnectInfo, Path as UrlPath, Request, State};
use axum::http::header::{self, HeaderName, HeaderValue};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde_json::{json, Value};
use tokio_util::io::ReaderStream;

use crate::clock::Clock;
use crate::config::Config;
use crate::error::ApiError;
use crate::limiter::Limiter;
use crate::live::{self, Connection};
use crate::rooms::{RoomMeta, RoomStore};
use crate::store::{secrets_equal, sha256_hex, valid_id, valid_sha, Auth, Store};

const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; \
media-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
/// The viewer's policy, plus compiling the notes CRDT's WebAssembly. `connect-src 'self'` covers
/// the same-origin `wss:` room socket.
const GUEST_CSP: &str = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; \
style-src 'self'; connect-src 'self'; media-src blob:; img-src data: blob:; base-uri 'none'; \
form-action 'none'; frame-ancestors 'none'";
const IMMUTABLE: &str = "public, max-age=31536000, immutable";
const MAX_REFS: usize = 256;

pub struct App {
    pub cfg: Arc<Config>,
    pub clock: Clock,
    pub store: Store,
    /// Guest-notes rooms (live, sealed notes).
    pub rooms: RoomStore,
    reads: Limiter,
    writes: Limiter,
}

pub type Shared = Arc<App>;

impl App {
    pub async fn new(cfg: Config, clock: Clock) -> std::io::Result<Shared> {
        let cfg = Arc::new(cfg);
        Ok(Arc::new(App {
            store: Store::open(cfg.clone(), clock.clone()).await?,
            rooms: RoomStore::open(cfg.clone(), clock.clone()).await?,
            reads: Limiter::new(cfg.read_burst, cfg.read_per_minute),
            writes: Limiter::new(cfg.write_burst, cfg.write_per_minute),
            cfg,
            clock,
        }))
    }

    /// One sweep: expired publications, expired or idle rooms, old tombstones, idle
    /// rate-limit entries.
    pub async fn sweep(&self) {
        let stats = self.store.sweep().await;
        let rooms = self.rooms.sweep(self.clock.now()).await;
        if rooms != Default::default() {
            tracing::info!("room sweep: {rooms:?}");
        }
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
        .route("/n/{room}", get(guest_index))
        .route("/n/{dir}/{*rest}", get(guest_asset))
        .route(
            "/live/{room}",
            get(live_socket).put(create_room).delete(delete_room),
        )
        .route("/live/{room}/snapshot", put(put_snapshot))
        .route("/live/{room}/end", post(end_room))
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

// ---- guest-notes rooms ----

fn room_json(meta: &RoomMeta) -> Value {
    json!({
        "created": meta.created,
        "expires": meta.expires,
        "ended_at": meta.ended_at,
        "last_seq": meta.last_seq,
        "snapshot_seq": (meta.snapshot_bytes > 0).then_some(meta.snapshot_seq),
    })
}

/// Days since 1970-01-01 of a proleptic Gregorian date (Howard Hinnant's `days_from_civil`).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `X-Room-Expires`: unix seconds, or an ISO 8601 / RFC 3339 time with a zone
/// (`2026-10-31T12:00:00Z`, `2026-10-31T14:00:00.250+02:00`; fractions of a second are dropped).
fn parse_expires(s: &str) -> Option<u64> {
    let s = s.trim();
    if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) {
        return s.parse().ok();
    }
    let b = s.as_bytes();
    let num = |from: usize, len: usize| -> Option<i64> {
        let part = s.get(from..from + len)?;
        part.bytes()
            .all(|c| c.is_ascii_digit())
            .then(|| part.parse().ok())?
    };
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || !matches!(b[10], b'T' | b't' | b' ') {
        return None;
    }
    if b[13] != b':' || b[16] != b':' {
        return None;
    }
    let (y, mo, d) = (num(0, 4)?, num(5, 2)?, num(8, 2)?);
    let (h, mi, sec) = (num(11, 2)?, num(14, 2)?, num(17, 2)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let mut at = 19;
    if b[at] == b'.' {
        at += 1;
        let digits = b[at..].iter().take_while(|c| c.is_ascii_digit()).count();
        if digits == 0 {
            return None;
        }
        at += digits;
    }
    let offset = match &s[at..] {
        "Z" | "z" => 0,
        zone => {
            let sign = match zone.as_bytes().first()? {
                b'+' => 1,
                b'-' => -1,
                _ => return None,
            };
            let (oh, om) = match zone.len() {
                6 if zone.as_bytes()[3] == b':' => (num(at + 1, 2)?, num(at + 4, 2)?),
                5 => (num(at + 1, 2)?, num(at + 3, 2)?),
                _ => return None,
            };
            if oh > 23 || om > 59 {
                return None;
            }
            sign * (oh * 3600 + om * 60)
        }
    };
    let secs = days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + sec - offset;
    u64::try_from(secs).ok()
}

fn header_expires(h: &HeaderMap) -> Result<Option<u64>, ApiError> {
    match header_str(h, "x-room-expires") {
        None => Ok(None),
        Some(v) => parse_expires(v)
            .map(Some)
            .ok_or(ApiError::bad("bad_expires")),
    }
}

/// `PUT /live/<room>`: creates the room (201), or moves its expiry (200). `X-Room-Token` is the
/// room token guests will prove the link with (only its sha256 is kept); `X-Room-Expires` is
/// unix seconds or an ISO 8601 time, clamped to `room_max_days` from now.
async fn create_room(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    check_id(&room)?;
    let auth = auth_of(&app, &headers)?;
    let token = header_str(&headers, "x-room-token")
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .ok_or(ApiError::bad("room_token_required"))?;
    if token.len() > 256 {
        return Err(ApiError::bad("bad_room_token"));
    }
    let expires = header_expires(&headers)?.ok_or(ApiError::bad("bad_expires"))?;
    let (meta, created) = app
        .rooms
        .create(&room, &auth, &sha256_hex(token.as_bytes()), expires)
        .await?;
    let status = if created {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    Ok((status, Json(room_json(&meta))).into_response())
}

/// `PUT /live/<room>/snapshot`: the owner's sealed fold covering frames up to `X-Room-Since`.
async fn put_snapshot(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
    headers: HeaderMap,
    body: Body,
) -> Result<Response, ApiError> {
    check_id(&room)?;
    let auth = auth_of(&app, &headers)?;
    let since = header_u64(&headers, "x-room-since", "bad_since")?;
    app.rooms.check_publisher(&room, &auth).await?;
    let bytes = read_body(&headers, body, app.cfg.room_snapshot_max_bytes)
        .await
        .map_err(|mut e| {
            if e.status == StatusCode::PAYLOAD_TOO_LARGE {
                e.code = "snapshot_too_large";
            }
            e
        })?;
    app.rooms.put_snapshot(&room, &auth, &bytes, since).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `POST /live/<room>/end`: read-only from now on, until `X-Room-Expires` (default: the
/// current expiry).
async fn end_room(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    check_id(&room)?;
    let auth = auth_of(&app, &headers)?;
    let expires = match header_expires(&headers)? {
        Some(e) => e,
        None => app.rooms.check_publisher(&room, &auth).await?.expires,
    };
    let meta = app.rooms.end(&room, &auth, expires).await?;
    Ok(Json(room_json(&meta)).into_response())
}

async fn delete_room(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    check_id(&room)?;
    let auth = auth_of(&app, &headers)?;
    app.rooms.delete(&room, &auth).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `GET /live/<room>`: the room's WebSocket (see `live` for the protocol). The connection is
/// counted before the upgrade. A full room, or an unknown, deleted or expired one, still gets
/// the upgrade and is closed at once (4429 / 4410), since a browser WebSocket cannot read an HTTP
/// status; only a malformed room id is refused as HTTP 400.
async fn live_socket(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    check_id(&room)?;
    let max = usize::try_from(app.cfg.room_frame_max_bytes)
        .unwrap_or(usize::MAX)
        .saturating_add(64 * 1024);
    let (id, _) = match app.rooms.connect(&room).await {
        Ok(joined) => joined,
        Err(e) => {
            let (code, reason) = match e.status {
                StatusCode::NOT_FOUND | StatusCode::GONE => (live::CLOSE_GONE, "gone"),
                StatusCode::SERVICE_UNAVAILABLE => (live::CLOSE_FULL, "room_full"),
                _ => return Err(e),
            };
            return Ok(ws
                .max_message_size(max)
                .max_frame_size(max)
                .on_upgrade(move |socket| live::refuse(socket, code, reason)));
        }
    };
    let conn = Connection::new(app.clone(), room, id);
    // Frames a little over the cap still reach the store, which answers `frame_too_large`;
    // the socket itself only refuses what is far beyond it.
    Ok(ws
        .max_message_size(max)
        .max_frame_size(max)
        .on_upgrade(move |socket| live::run(socket, conn)))
}

// ---- static pages: the viewer and the guest page ----

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
        "wasm" => "application/wasm",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn static_response(path: &Path, bytes: Vec<u8>, csp: &str, cache: &str) -> Response {
    let mut res = bytes.into_response();
    set(&mut res, header::CONTENT_TYPE, content_type(path));
    set(&mut res, header::CONTENT_SECURITY_POLICY, csp);
    set(&mut res, header::CACHE_CONTROL, cache);
    res
}

async fn static_index(root: &Path, csp: &str, missing: &'static str) -> Result<Response, ApiError> {
    let path = root.join("index.html");
    match tokio::fs::read(&path).await {
        Ok(bytes) => Ok(static_response(&path, bytes, csp, "no-store")),
        Err(_) => Err(ApiError::new(StatusCode::SERVICE_UNAVAILABLE, missing)),
    }
}

/// `<root>/<dir>/<rest>`, refusing anything that climbs out or names a hidden file.
async fn static_asset(root: &Path, dir: &str, rest: &str, csp: &str) -> Result<Response, ApiError> {
    let not_found = || ApiError::new(StatusCode::NOT_FOUND, "not_found");
    let mut path: PathBuf = root.to_path_buf();
    for part in Path::new(dir).join(rest).components() {
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
    Ok(static_response(&path, bytes, csp, "no-cache"))
}

/// Any `/p/<something>` is the viewer: it reads the id and the key from its own address and
/// shows "not found" itself, so the host never says which ids exist to someone without a link.
async fn viewer_index(State(app): State<Shared>) -> Result<Response, ApiError> {
    static_index(&app.cfg.viewer_dir, CSP, "viewer_missing").await
}

/// `/p/<dir>/<path>` serves `<viewer-dir>/<dir>/<path>` (the build's `assets/` folder).
async fn viewer_asset(
    State(app): State<Shared>,
    UrlPath((dir, rest)): UrlPath<(String, String)>,
) -> Result<Response, ApiError> {
    static_asset(&app.cfg.viewer_dir, &dir, &rest, CSP).await
}

/// `/n/<room>` is the guest page for any well-formed room id; like the viewer, it never says
/// whether the room exists (the socket does, to those with the token).
async fn guest_index(
    State(app): State<Shared>,
    UrlPath(room): UrlPath<String>,
) -> Result<Response, ApiError> {
    if !valid_id(&room) {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "not_found"));
    }
    static_index(&app.cfg.guest_dir, GUEST_CSP, "guest_missing").await
}

/// `/n/<dir>/<path>` serves `<guest-dir>/<dir>/<path>` (the build's `assets/`, wasm included).
async fn guest_asset(
    State(app): State<Shared>,
    UrlPath((dir, rest)): UrlPath<(String, String)>,
) -> Result<Response, ApiError> {
    static_asset(&app.cfg.guest_dir, &dir, &rest, GUEST_CSP).await
}

#[cfg(test)]
mod tests {
    use super::parse_expires;

    #[test]
    fn expires_as_seconds_or_iso() {
        assert_eq!(parse_expires("1800000000"), Some(1_800_000_000));
        assert_eq!(parse_expires(" 1800000000 "), Some(1_800_000_000));
        assert_eq!(parse_expires("1970-01-01T00:00:00Z"), Some(0));
        // 1_800_000_000 is 2027-01-15T08:00:00Z.
        for s in [
            "2027-01-15T08:00:00Z",
            "2027-01-15T08:00:00+00:00",
            "2027-01-15T08:00:00.123456+00:00",
            "2027-01-15T10:00:00+02:00",
            "2027-01-15T03:30:00-0430",
            "2027-01-15 08:00:00z",
        ] {
            assert_eq!(parse_expires(s), Some(1_800_000_000), "{s}");
        }
        assert_eq!(parse_expires("2024-02-29T00:00:00Z"), Some(1_709_164_800));
        for s in [
            "",
            "soon",
            "-5",
            "2027-01-15T08:00:00",
            "2027-01-15T08:00Z",
            "2027-13-15T08:00:00Z",
            "2027-01-15T08:00:00.Z",
            "2027-01-15T08:00:00+2:00",
            "1969-12-31T23:59:59Z",
        ] {
            assert_eq!(parse_expires(s), None, "{s}");
        }
    }
}
