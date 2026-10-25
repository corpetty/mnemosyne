//! The whole contract, in process: requests go straight into the router, time is a hand-turned
//! clock, and the data directory is a temporary folder.

use axum::body::Body;
use axum::http::{HeaderMap, Method, Request, StatusCode};
use axum::Router;
use mnemosyne_publish_host::app::{router, App, Shared};
use mnemosyne_publish_host::clock::Clock;
use mnemosyne_publish_host::config::Config;
use mnemosyne_publish_host::store::sha256_hex;
use serde_json::Value;
use tempfile::TempDir;
use tower::ServiceExt;

const T0: u64 = 1_800_000_000;
const DAY: u64 = 86_400;
const ID: &str = "0123456789abcdef";
const OTHER_ID: &str = "fedcba9876543210";
const TOKEN: &str = "publisher-secret-token";
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; \
media-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

struct Host {
    app: Shared,
    router: Router,
    clock: Clock,
    tmp: TempDir,
}

struct Reply {
    status: StatusCode,
    headers: HeaderMap,
    body: Vec<u8>,
}

impl Reply {
    fn header(&self, name: &str) -> &str {
        self.headers.get(name).map_or("", |v| v.to_str().unwrap())
    }
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap()
    }
}

async fn host() -> Host {
    host_with(|_| {}).await
}

async fn host_with(tweak: impl FnOnce(&mut Config)) -> Host {
    let tmp = tempfile::tempdir().unwrap();
    std::fs::create_dir(tmp.path().join("viewer")).unwrap();
    std::fs::create_dir(tmp.path().join("viewer/assets")).unwrap();
    std::fs::write(
        tmp.path().join("viewer/index.html"),
        "<!doctype html><title>viewer</title>",
    )
    .unwrap();
    std::fs::write(tmp.path().join("viewer/assets/app.js"), "console.log(1)").unwrap();
    std::fs::write(tmp.path().join("secret.txt"), "not for the web").unwrap();
    let mut cfg = Config::defaults();
    cfg.data_dir = tmp.path().join("data");
    cfg.viewer_dir = tmp.path().join("viewer");
    // Rate limits are tested on their own; everything else runs without.
    cfg.write_per_minute = 0;
    cfg.read_per_minute = 0;
    tweak(&mut cfg);
    let clock = Clock::manual(T0);
    let app = App::new(cfg, clock.clone()).await.unwrap();
    Host {
        router: router(app.clone()),
        app,
        clock,
        tmp,
    }
}

impl Host {
    async fn send(&self, req: Request<Body>) -> Reply {
        let res = self.router.clone().oneshot(req).await.unwrap();
        let (parts, body) = res.into_parts();
        let body = axum::body::to_bytes(body, usize::MAX)
            .await
            .unwrap()
            .to_vec();
        Reply {
            status: parts.status,
            headers: parts.headers,
            body,
        }
    }

    async fn get(&self, uri: &str) -> Reply {
        self.send(Request::get(uri).body(Body::empty()).unwrap())
            .await
    }

    async fn get_with(&self, uri: &str, name: &str, value: &str) -> Reply {
        self.send(
            Request::get(uri)
                .header(name, value)
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    async fn put_blob_as(&self, id: &str, data: &[u8], token: &str) -> Reply {
        let sha = sha256_hex(data);
        self.send(
            Request::put(format!("/pub/{id}/{sha}"))
                .header("authorization", format!("Bearer {token}"))
                .body(Body::from(data.to_vec()))
                .unwrap(),
        )
        .await
    }

    async fn put_blob(&self, data: &[u8]) -> Reply {
        self.put_blob_as(ID, data, TOKEN).await
    }

    /// A manifest PUT; `headers` are (version, expires, blob hashes).
    async fn put_manifest_as(
        &self,
        id: &str,
        token: &str,
        body: &[u8],
        version: u64,
        expires: u64,
        blobs: &[&[u8]],
    ) -> Reply {
        let shas: Vec<String> = blobs.iter().map(|b| sha256_hex(b)).collect();
        self.send(
            Request::builder()
                .method(Method::PUT)
                .uri(format!("/pub/{id}"))
                .header("authorization", format!("Bearer {token}"))
                .header("x-pub-version", version.to_string())
                .header("x-pub-expires", expires.to_string())
                .header("x-pub-blobs", shas.join(","))
                .body(Body::from(body.to_vec()))
                .unwrap(),
        )
        .await
    }

    async fn put_manifest(&self, body: &[u8], version: u64, blobs: &[&[u8]]) -> Reply {
        self.put_manifest_as(ID, TOKEN, body, version, T0 + 90 * DAY, blobs)
            .await
    }

    async fn delete_as(&self, id: &str, token: Option<&str>) -> Reply {
        let mut req = Request::delete(format!("/pub/{id}"));
        if let Some(t) = token {
            req = req.header("authorization", format!("Bearer {t}"));
        }
        self.send(req.body(Body::empty()).unwrap()).await
    }

    fn blob_path(&self, id: &str, data: &[u8]) -> std::path::PathBuf {
        self.tmp
            .path()
            .join("data")
            .join(id)
            .join("blobs")
            .join(sha256_hex(data))
    }

    fn pub_dir(&self, id: &str) -> std::path::PathBuf {
        self.tmp.path().join("data").join(id)
    }
}

#[tokio::test]
async fn write_read_update_delete_flow() {
    let h = host().await;
    let (clip1, clip2) = (vec![1u8; 4000], vec![2u8; 5000]);

    assert_eq!(h.put_blob(&clip1).await.status, StatusCode::CREATED);
    // Nothing is readable until a manifest exists.
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        h.put_manifest(b"manifest v1", 1, &[&clip1]).await.status,
        StatusCode::OK
    );

    let m = h.get(&format!("/pub/{ID}")).await;
    assert_eq!(m.status, StatusCode::OK);
    assert_eq!(m.body, b"manifest v1");
    assert_eq!(m.header("content-type"), "application/octet-stream");
    assert_eq!(m.header("x-pub-version"), "1");
    assert_eq!(m.header("cache-control"), "no-cache");
    assert_eq!(
        m.header("etag"),
        format!("\"{}\"", sha256_hex(b"manifest v1"))
    );

    let b = h.get(&format!("/pub/{ID}/{}", sha256_hex(&clip1))).await;
    assert_eq!(b.status, StatusCode::OK);
    assert_eq!(b.body, clip1);
    assert_eq!(
        b.header("cache-control"),
        "public, max-age=31536000, immutable"
    );

    // On disk: the layout of the contract, ciphertext as sent.
    assert_eq!(
        std::fs::read(h.pub_dir(ID).join("manifest")).unwrap(),
        b"manifest v1"
    );
    assert_eq!(std::fs::read(h.blob_path(ID, &clip1)).unwrap(), clip1);

    // Update: a new clip, the old one dropped.
    assert_eq!(h.put_blob(&clip2).await.status, StatusCode::CREATED);
    assert_eq!(
        h.put_manifest(b"manifest v2", 2, &[&clip2]).await.status,
        StatusCode::OK
    );
    assert_eq!(h.get(&format!("/pub/{ID}")).await.body, b"manifest v2");
    assert!(!h.blob_path(ID, &clip1).exists());
    assert_eq!(
        h.get(&format!("/pub/{ID}/{}", sha256_hex(&clip1)))
            .await
            .status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        h.get(&format!("/pub/{ID}/{}", sha256_hex(&clip2)))
            .await
            .status,
        StatusCode::OK
    );
    assert_eq!(
        h.app.store.used_bytes().await,
        (b"manifest v2".len() + clip2.len()) as u64
    );

    // Unpublish.
    assert_eq!(
        h.delete_as(ID, Some(TOKEN)).await.status,
        StatusCode::NO_CONTENT
    );
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::GONE);
    assert_eq!(
        h.get(&format!("/pub/{ID}/{}", sha256_hex(&clip2)))
            .await
            .status,
        StatusCode::GONE
    );
    assert!(!h.pub_dir(ID).join("manifest").exists());
    assert!(!h.pub_dir(ID).join("blobs").exists());
    assert_eq!(h.app.store.used_bytes().await, 0);
    // Writing to a deleted publication is refused too.
    assert_eq!(
        h.put_manifest(b"again", 3, &[]).await.status,
        StatusCode::GONE
    );

    // 410 for the grace period, then 404.
    h.clock.advance(29 * DAY);
    h.app.sweep().await;
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::GONE);
    h.clock.advance(2 * DAY);
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.status,
        StatusCode::NOT_FOUND
    );
    h.app.sweep().await;
    assert!(!h.pub_dir(ID).exists());
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn first_write_sets_the_token_and_later_writes_need_it() {
    let h = host().await;
    assert_eq!(h.put_blob(b"clip").await.status, StatusCode::CREATED);

    // Only the hash is stored.
    let meta = std::fs::read_to_string(h.pub_dir(ID).join("meta.json")).unwrap();
    assert!(!meta.contains(TOKEN));
    assert!(meta.contains(&sha256_hex(TOKEN.as_bytes())));

    assert_eq!(
        h.put_blob_as(ID, b"more", "someone-else").await.status,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        h.put_manifest_as(ID, "someone-else", b"m", 1, T0 + DAY, &[])
            .await
            .status,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        h.delete_as(ID, Some("someone-else")).await.status,
        StatusCode::FORBIDDEN
    );
    assert_eq!(h.delete_as(ID, None).await.status, StatusCode::UNAUTHORIZED);
    let no_auth = h
        .send(
            Request::put(format!("/pub/{ID}/{}", sha256_hex(b"x")))
                .body(Body::from("x"))
                .unwrap(),
        )
        .await;
    assert_eq!(no_auth.status, StatusCode::UNAUTHORIZED);
    // The rightful owner still can.
    assert_eq!(
        h.put_manifest(b"m", 1, &[b"clip"]).await.status,
        StatusCode::OK
    );
    assert_eq!(
        h.delete_as(OTHER_ID, Some(TOKEN)).await.status,
        StatusCode::NOT_FOUND
    );
    // A wrong token changed nothing.
    assert_eq!(h.get(&format!("/pub/{ID}")).await.body, b"m");
}

#[tokio::test]
async fn a_blob_whose_hash_differs_from_the_path_is_refused() {
    let h = host().await;
    let wrong = sha256_hex(b"something else");
    let res = h
        .send(
            Request::put(format!("/pub/{ID}/{wrong}"))
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(Body::from("the real bytes"))
                .unwrap(),
        )
        .await;
    assert_eq!(res.status, StatusCode::BAD_REQUEST);
    assert_eq!(res.json()["error"], "sha256_mismatch");
    // Nothing was created, not even the token claim.
    assert!(!h.pub_dir(ID).exists());
}

#[tokio::test]
async fn blobs_the_new_manifest_does_not_reference_are_deleted_after_the_swap() {
    let h = host().await;
    let (a, b, c) = (b"aaaa".to_vec(), b"bbbb".to_vec(), b"cccc".to_vec());
    for blob in [&a, &b, &c] {
        h.put_blob(blob).await;
    }
    // Uploaded but unreferenced blobs are not served.
    assert_eq!(
        h.put_manifest(b"m1", 1, &[&a, &b]).await.status,
        StatusCode::OK
    );
    assert!(h.blob_path(ID, &a).exists() && h.blob_path(ID, &b).exists());
    assert!(!h.blob_path(ID, &c).exists(), "c was never referenced");
    assert_eq!(
        h.get(&format!("/pub/{ID}/{}", sha256_hex(&c))).await.status,
        StatusCode::NOT_FOUND
    );
    // Swap: b stays, a goes.
    assert_eq!(h.put_manifest(b"m2", 2, &[&b]).await.status, StatusCode::OK);
    assert!(!h.blob_path(ID, &a).exists());
    assert!(h.blob_path(ID, &b).exists());
}

#[tokio::test]
async fn a_manifest_referencing_a_missing_blob_is_a_conflict() {
    let h = host().await;
    h.put_blob(b"present").await;
    let res = h.put_manifest(b"m", 1, &[b"present", b"absent"]).await;
    assert_eq!(res.status, StatusCode::CONFLICT);
    assert_eq!(res.json()["error"], "missing_blobs");
    assert_eq!(res.json()["missing"][0], sha256_hex(b"absent"));
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn size_caps_and_the_store_quota() {
    let h = host_with(|c| {
        c.manifest_max_bytes = 100;
        c.blob_max_bytes = 1000;
        c.publication_max_bytes = 1499;
        c.quota_bytes = 2500;
    })
    .await;

    // Manifest and blob caps.
    assert_eq!(
        h.put_manifest(&[0u8; 101], 1, &[]).await.status,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(
        h.put_blob(&[0u8; 1001]).await.status,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert!(!h.pub_dir(ID).exists());

    // Per publication: 1000 + 600 > 1499, counted across uploads.
    assert_eq!(h.put_blob(&[1u8; 1000]).await.status, StatusCode::CREATED);
    let over = h.put_blob(&[2u8; 600]).await;
    assert_eq!(over.status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(over.json()["error"], "publication_too_large");
    // A manifest counts toward it as well: 100 + 1000 + 400 is over, 100 + 1000 is fine.
    assert_eq!(h.put_blob(&[3u8; 400]).await.status, StatusCode::CREATED);
    assert_eq!(
        h.put_manifest(&[9u8; 100], 1, &[&[1u8; 1000], &[3u8; 400]])
            .await
            .status,
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(
        h.put_manifest(&[9u8; 100], 1, &[&[1u8; 1000]]).await.status,
        StatusCode::OK
    );

    // The store as a whole: 1100 held (the unreferenced blob went with the swap), quota 2500.
    assert_eq!(
        h.put_blob_as(OTHER_ID, &[4u8; 1000], "t2").await.status,
        StatusCode::CREATED
    );
    assert_eq!(
        h.put_blob_as(OTHER_ID, &[5u8; 400], "t2").await.status,
        StatusCode::CREATED
    );
    let full = h.put_blob_as("aaaaaaaaaaaaaaaa", &[6u8; 100], "t3").await;
    assert_eq!(full.status, StatusCode::INSUFFICIENT_STORAGE);
    assert_eq!(full.json()["error"], "store_full");
    // Deleting frees the space again.
    assert_eq!(
        h.delete_as(OTHER_ID, Some("t2")).await.status,
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        h.put_blob_as("aaaaaaaaaaaaaaaa", &[6u8; 100], "t3")
            .await
            .status,
        StatusCode::CREATED
    );
}

#[tokio::test]
async fn bodies_over_the_cap_are_refused_while_reading_too() {
    // No Content-Length (chunked): the limit still holds.
    let h = host_with(|c| c.blob_max_bytes = 10).await;
    let data = vec![7u8; 11];
    let stream = futures_like_stream(data.clone());
    let res = h
        .send(
            Request::put(format!("/pub/{ID}/{}", sha256_hex(&data)))
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(stream)
                .unwrap(),
        )
        .await;
    assert_eq!(res.status, StatusCode::PAYLOAD_TOO_LARGE);
}

fn futures_like_stream(data: Vec<u8>) -> Body {
    // Chunks without a known length.
    let chunks: Vec<Result<Vec<u8>, std::io::Error>> =
        data.chunks(4).map(|c| Ok(c.to_vec())).collect();
    Body::from_stream(futures_util::stream::iter(chunks))
}

#[tokio::test]
async fn versions_must_increase() {
    let h = host().await;
    assert_eq!(h.put_manifest(b"v1", 1, &[]).await.status, StatusCode::OK);
    let same = h.put_manifest(b"v1 again", 1, &[]).await;
    assert_eq!(same.status, StatusCode::CONFLICT);
    assert_eq!(same.json()["error"], "version_not_increasing");
    assert_eq!(same.json()["current"], 1);
    assert_eq!(
        h.put_manifest(b"v0", 0, &[]).await.status,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(h.put_manifest(b"v5", 5, &[]).await.status, StatusCode::OK);
    assert_eq!(
        h.put_manifest(b"v3", 3, &[]).await.status,
        StatusCode::CONFLICT
    );
    let m = h.get(&format!("/pub/{ID}")).await;
    assert_eq!(
        (m.body.as_slice(), m.header("x-pub-version")),
        (&b"v5"[..], "5")
    );
}

#[tokio::test]
async fn expiry_must_be_in_the_future_and_within_the_maximum() {
    let h = host_with(|c| c.max_days = 30).await;
    let put = |expires| h.put_manifest_as(ID, TOKEN, b"m", 1, expires, &[]);
    for bad in [T0 - 10, T0, T0 + 30 * DAY + 1, T0 + 400 * DAY] {
        let res = put(bad).await;
        assert_eq!(res.status, StatusCode::BAD_REQUEST, "expires {bad}");
        assert_eq!(res.json()["error"], "expiry_out_of_range");
    }
    // Required, and a number.
    let no_header = h
        .send(
            Request::put(format!("/pub/{ID}"))
                .header("authorization", format!("Bearer {TOKEN}"))
                .header("x-pub-version", "1")
                .body(Body::from("m"))
                .unwrap(),
        )
        .await;
    assert_eq!(no_header.status, StatusCode::BAD_REQUEST);
    assert!(!h.pub_dir(ID).exists());
    assert_eq!(put(T0 + 30 * DAY).await.status, StatusCode::OK);
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.header("x-pub-expires"),
        (T0 + 30 * DAY).to_string()
    );
}

#[tokio::test]
async fn the_sweeper_removes_what_has_expired() {
    let h = host().await;
    let clip = b"clip".to_vec();
    h.put_blob(&clip).await;
    assert_eq!(
        h.put_manifest_as(ID, TOKEN, b"m", 1, T0 + 10 * DAY, &[&clip])
            .await
            .status,
        StatusCode::OK
    );
    assert_eq!(
        h.put_manifest_as(OTHER_ID, "t2", b"keep", 1, T0 + 100 * DAY, &[])
            .await
            .status,
        StatusCode::OK
    );
    // A publication that only ever got blobs.
    h.put_blob_as("1111111111111111", b"orphan", "t3").await;

    h.app.sweep().await;
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::OK);

    h.clock.advance(11 * DAY);
    // Expired answers 410 even before the sweeper has run.
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::GONE);
    assert_eq!(h.put_blob(b"late").await.status, StatusCode::GONE);
    assert!(h.blob_path(ID, &clip).exists());

    let stats = h.app.store.sweep().await;
    assert_eq!(
        (
            stats.expired,
            stats.pending_removed,
            stats.tombstones_removed
        ),
        (1, 1, 0)
    );
    assert!(!h.blob_path(ID, &clip).exists());
    assert!(!h.pub_dir(ID).join("manifest").exists());
    assert!(!h.pub_dir("1111111111111111").exists());
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::GONE);
    assert_eq!(
        h.get(&format!("/pub/{OTHER_ID}")).await.status,
        StatusCode::OK
    );
    assert_eq!(h.app.store.used_bytes().await, 4);

    // Its tombstone goes after the grace, counted from when it expired.
    h.clock.advance(30 * DAY);
    assert_eq!(h.app.store.sweep().await.tombstones_removed, 1);
    assert_eq!(
        h.get(&format!("/pub/{ID}")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn etag_and_conditional_requests() {
    let h = host().await;
    h.put_manifest(b"v1", 1, &[]).await;
    let uri = format!("/pub/{ID}");
    let etag = h.get(&uri).await.header("etag").to_string();

    for cond in [
        etag.clone(),
        format!("W/{etag}"),
        format!("\"x\", {etag}"),
        "*".to_string(),
    ] {
        let res = h.get_with(&uri, "if-none-match", &cond).await;
        assert_eq!(res.status, StatusCode::NOT_MODIFIED, "{cond}");
        assert!(res.body.is_empty());
        assert_eq!(res.header("etag"), etag);
        assert_eq!(res.header("cache-control"), "no-cache");
    }
    assert_eq!(
        h.get_with(&uri, "if-none-match", "\"other\"").await.status,
        StatusCode::OK
    );

    // A new version has a new tag, so the old one no longer matches.
    h.put_manifest(b"v2", 2, &[]).await;
    let fresh = h.get_with(&uri, "if-none-match", &etag).await;
    assert_eq!(fresh.status, StatusCode::OK);
    assert_eq!(fresh.body, b"v2");
}

#[tokio::test]
async fn writes_are_rate_limited_per_client_and_reads_more_loosely() {
    let h = host_with(|c| {
        c.trust_proxy = true;
        c.write_burst = 3;
        c.write_per_minute = 60;
        c.read_burst = 5;
        c.read_per_minute = 60;
    })
    .await;
    let write = |ip: &'static str, n: usize| {
        let data = format!("blob {n}").into_bytes();
        let sha = sha256_hex(&data);
        let req = Request::put(format!("/pub/{ID}/{sha}"))
            .header("authorization", format!("Bearer {TOKEN}"))
            .header("x-forwarded-for", ip)
            .body(Body::from(data))
            .unwrap();
        h.send(req)
    };
    for n in 0..3 {
        assert_eq!(write("203.0.113.5", n).await.status, StatusCode::CREATED);
    }
    let limited = write("203.0.113.5", 3).await;
    assert_eq!(limited.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(limited.header("retry-after"), "1");
    // Another client is not affected; the proxy-appended (last) address counts.
    assert_eq!(write("203.0.113.6", 4).await.status, StatusCode::CREATED);
    assert_eq!(
        write("198.51.100.1, 203.0.113.6", 5).await.status,
        StatusCode::CREATED
    );
    // Tokens come back with time: one a second.
    h.clock.advance(1);
    assert_eq!(write("203.0.113.5", 6).await.status, StatusCode::CREATED);
    assert_eq!(
        write("203.0.113.5", 7).await.status,
        StatusCode::TOO_MANY_REQUESTS
    );

    // Reads have their own, looser bucket (the writes above did not touch it).
    let read = || h.get_with("/p/anything", "x-forwarded-for", "203.0.113.5");
    for _ in 0..5 {
        assert_eq!(read().await.status, StatusCode::OK);
    }
    assert_eq!(read().await.status, StatusCode::TOO_MANY_REQUESTS);
    // Health checks are never limited.
    assert_eq!(h.get("/health").await.status, StatusCode::OK);
    h.clock.advance(120);
    h.app.sweep().await;
    assert_eq!(read().await.status, StatusCode::OK);
}

#[tokio::test]
async fn forwarded_addresses_are_ignored_unless_the_proxy_is_trusted() {
    let h = host_with(|c| {
        c.read_burst = 2;
        c.read_per_minute = 1;
    })
    .await;
    // Spoofed headers do not buy a fresh bucket.
    for (i, expected) in [
        StatusCode::OK,
        StatusCode::OK,
        StatusCode::TOO_MANY_REQUESTS,
    ]
    .into_iter()
    .enumerate()
    {
        let res = h
            .get_with("/p/x", "x-forwarded-for", &format!("10.0.0.{i}"))
            .await;
        assert_eq!(res.status, expected);
    }
}

#[tokio::test]
async fn the_viewer_page_and_its_headers() {
    let h = host().await;
    for id in [ID, "no-such-publication"] {
        let res = h.get(&format!("/p/{id}")).await;
        assert_eq!(res.status, StatusCode::OK, "{id}");
        assert_eq!(res.body, b"<!doctype html><title>viewer</title>");
        assert_eq!(res.header("content-type"), "text/html; charset=utf-8");
        assert_eq!(res.header("content-security-policy"), CSP);
        assert_eq!(res.header("referrer-policy"), "no-referrer");
        assert_eq!(res.header("x-content-type-options"), "nosniff");
        assert_eq!(res.header("cache-control"), "no-store");
    }
    let js = h.get("/p/assets/app.js").await;
    assert_eq!(js.status, StatusCode::OK);
    assert_eq!(js.body, b"console.log(1)");
    assert_eq!(js.header("content-type"), "text/javascript; charset=utf-8");
    assert_eq!(js.header("content-security-policy"), CSP);
    assert_eq!(js.header("x-content-type-options"), "nosniff");
    assert_eq!(
        h.get("/p/assets/missing.js").await.status,
        StatusCode::NOT_FOUND
    );

    // No way out of the viewer directory, encoded or not.
    for uri in [
        "/p/assets/..%2f..%2fsecret.txt",
        "/p/assets/%2e%2e/%2e%2e/secret.txt",
        "/p/assets/../../secret.txt",
        "/p/%2e%2e/secret.txt",
    ] {
        let res = h.get(uri).await;
        assert!(res.status.is_client_error(), "{uri} -> {}", res.status);
        assert_ne!(res.body, b"not for the web", "{uri}");
    }

    // API answers carry the hardening headers too.
    let api = h.get("/pub/0123456789abcdef").await;
    assert_eq!(api.header("x-content-type-options"), "nosniff");
    assert_eq!(api.header("referrer-policy"), "no-referrer");
    assert_eq!(h.get("/health").await.body, b"ok");

    // Without a build the page says so rather than pretending.
    std::fs::remove_file(h.tmp.path().join("viewer/index.html")).unwrap();
    assert_eq!(
        h.get(&format!("/p/{ID}")).await.status,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

#[tokio::test]
async fn ids_and_hashes_have_a_strict_shape() {
    let h = host().await;
    let sha = sha256_hex(b"x");
    for uri in [
        "/pub/0123".to_string(),
        "/pub/0123456789ABCDEF".to_string(),
        "/pub/0123456789abcdeg".to_string(),
        format!("/pub/{ID}/{}", sha.to_uppercase()),
        format!("/pub/{ID}/{}", &sha[..63]),
    ] {
        assert_eq!(h.get(&uri).await.status, StatusCode::BAD_REQUEST, "{uri}");
    }
    let bad_put = h
        .send(
            Request::put("/pub/xyz")
                .header("authorization", "Bearer t")
                .body(Body::from("m"))
                .unwrap(),
        )
        .await;
    assert_eq!(bad_put.status, StatusCode::BAD_REQUEST);
    let bad_ref = h
        .send(
            Request::put(format!("/pub/{ID}"))
                .header("authorization", format!("Bearer {TOKEN}"))
                .header("x-pub-version", "1")
                .header("x-pub-expires", (T0 + DAY).to_string())
                .header("x-pub-blobs", "not-a-sha")
                .body(Body::from("m"))
                .unwrap(),
        )
        .await;
    assert_eq!(bad_ref.status, StatusCode::BAD_REQUEST);
    assert_eq!(
        h.get(&format!("/pub/{OTHER_ID}")).await.status,
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn a_closed_host_wants_its_upload_token_on_every_first_write() {
    let h = host_with(|c| c.upload_token = Some("host-secret".into())).await;
    let clip = b"clip".to_vec();
    let sha = sha256_hex(&clip);
    let blob_req = |upload: Option<&str>, token: &str| {
        let mut r = Request::put(format!("/pub/{ID}/{sha}"))
            .header("authorization", format!("Bearer {token}"));
        if let Some(u) = upload {
            r = r.header("x-upload-token", u);
        }
        h.send(r.body(Body::from(clip.clone())).unwrap())
    };
    let manifest_req = |upload: Option<&str>, id: &str, token: &str| {
        let mut r = Request::put(format!("/pub/{id}"))
            .header("authorization", format!("Bearer {token}"))
            .header("x-pub-version", "1")
            .header("x-pub-expires", (T0 + DAY).to_string());
        if let Some(u) = upload {
            r = r.header("x-upload-token", u);
        }
        h.send(r.body(Body::from("m")).unwrap())
    };

    let res = blob_req(None, TOKEN).await;
    assert_eq!(
        (res.status, res.json()["error"].as_str()),
        (StatusCode::FORBIDDEN, Some("upload_token_required"))
    );
    assert_eq!(
        blob_req(Some("wrong"), TOKEN).await.status,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        manifest_req(None, OTHER_ID, "t2").await.status,
        StatusCode::FORBIDDEN
    );
    assert!(!h.pub_dir(ID).exists() && !h.pub_dir(OTHER_ID).exists());

    assert_eq!(
        blob_req(Some("host-secret"), TOKEN).await.status,
        StatusCode::CREATED
    );
    // Once the publication exists its own token is enough; the host token is not needed again.
    assert_eq!(
        h.put_manifest(b"m", 1, &[&clip]).await.status,
        StatusCode::OK
    );
    // ...and does not replace it.
    assert_eq!(
        blob_req(Some("host-secret"), "someone-else").await.status,
        StatusCode::FORBIDDEN
    );
    // Manifest-first publications (no clips) go through the same gate.
    assert_eq!(
        manifest_req(Some("host-secret"), OTHER_ID, "t2")
            .await
            .status,
        StatusCode::OK
    );
    // Reading stays open.
    assert_eq!(h.get(&format!("/pub/{ID}")).await.status, StatusCode::OK);
}

#[tokio::test]
async fn a_restart_keeps_publications_and_the_byte_count() {
    let h = host().await;
    h.put_blob(b"clip").await;
    h.put_manifest(b"manifest", 1, &[b"clip"]).await;
    // A half-written file from a crash.
    std::fs::write(h.pub_dir(ID).join("blobs/.tmp-1-1"), b"partial").unwrap();

    let mut cfg = h.app.cfg.as_ref().clone();
    cfg.write_per_minute = 0;
    let again = App::new(cfg, h.clock.clone()).await.unwrap();
    assert_eq!(again.store.used_bytes().await, 12);
    assert!(!h.pub_dir(ID).join("blobs/.tmp-1-1").exists());
    let res = router(again)
        .oneshot(
            Request::get(format!("/pub/{ID}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
}

#[tokio::test]
async fn uploading_an_existing_blob_again_changes_nothing() {
    let h = host().await;
    assert_eq!(h.put_blob(b"clip").await.status, StatusCode::CREATED);
    assert_eq!(h.put_blob(b"clip").await.status, StatusCode::NO_CONTENT);
    assert_eq!(h.app.store.used_bytes().await, 4);
}

#[tokio::test]
async fn guest_page_csp_lists_relays() {
    let tmp = tempfile::tempdir().unwrap();
    let guest = tmp.path().join("guest");
    std::fs::create_dir(&guest).unwrap();
    std::fs::write(
        guest.join("index.html"),
        "<!doctype html><title>guest</title>",
    )
    .unwrap();
    let dir = guest.clone();
    let h = host_with(move |c| {
        c.guest_dir = dir;
        c.relay_origins = vec!["relay.example.org:7842".into()];
    })
    .await;
    for uri in [format!("/n/{ID}"), "/n/".to_string()] {
        let r = h.get(&uri).await;
        assert_eq!(r.status, StatusCode::OK, "{uri}");
        let csp = r.header("content-security-policy");
        assert!(
            csp.contains("https://*.relay.n0.iroh.link wss://*.relay.n0.iroh.link"),
            "{csp}"
        );
        assert!(
            csp.contains("https://relay.example.org:7842 wss://relay.example.org:7842"),
            "{csp}"
        );
        assert!(csp.contains("connect-src 'self' "), "{csp}");
    }
}
