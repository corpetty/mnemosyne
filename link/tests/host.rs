//! The embedded loopback host: it answers, and only the backend's upload token opens rooms.

use mnemosyne_link::host::{request, start_loopback_host, HostOptions};

const UPLOAD: &str = "upload-secret-for-the-test";
const ROOM: &str = "0123456789abcdef";

async fn start(dir: &tempfile::TempDir) -> std::net::SocketAddr {
    let (addr, _app) = start_loopback_host(HostOptions {
        data_dir: dir.path().to_path_buf(),
        guest_dir: dir.path().join("guest"),
        viewer_dir: dir.path().join("viewer"),
        upload_token: UPLOAD.into(),
        relay_origins: vec![],
    })
    .await
    .unwrap();
    addr
}

#[tokio::test]
async fn health_answers() {
    let dir = tempfile::tempdir().unwrap();
    let addr = start(&dir).await;
    assert!(addr.ip().is_loopback());
    let (status, body) = request(addr, "GET", "/health", &[], b"").await.unwrap();
    assert_eq!((status, body.as_slice()), (200, b"ok".as_slice()));
}

#[tokio::test]
async fn rooms_need_the_upload_token() {
    let dir = tempfile::tempdir().unwrap();
    let addr = start(&dir).await;
    let path = format!("/live/{ROOM}");
    let expires = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 3600)
        .to_string();
    let base = [
        ("Authorization", "Bearer owner-token"),
        ("X-Room-Token", "guest-room-token"),
        ("X-Room-Expires", expires.as_str()),
    ];

    let (status, body) = request(addr, "PUT", &path, &base, b"").await.unwrap();
    assert_eq!(status, 403, "{}", String::from_utf8_lossy(&body));
    assert!(String::from_utf8_lossy(&body).contains("upload_token_required"));

    let mut with = base.to_vec();
    with.push(("X-Upload-Token", UPLOAD));
    let (status, body) = request(addr, "PUT", &path, &with, b"").await.unwrap();
    assert_eq!(status, 201, "{}", String::from_utf8_lossy(&body));

    // A wrong upload token is refused for a new room.
    let mut wrong = base.to_vec();
    wrong.push(("X-Upload-Token", "nope"));
    let (status, _) = request(
        addr,
        "PUT",
        &format!("/live/{}", "fedcba9876543210"),
        &wrong,
        b"",
    )
    .await
    .unwrap();
    assert_eq!(status, 403);
}

#[tokio::test]
async fn loopback_clients_are_not_throttled_together() {
    let dir = tempfile::tempdir().unwrap();
    let addr = start(&dir).await;
    // The default read burst is 120: go well past it from one address.
    for _ in 0..200 {
        let (status, _) = request(addr, "GET", "/n/", &[], b"").await.unwrap();
        assert_ne!(status, 429);
    }
}
