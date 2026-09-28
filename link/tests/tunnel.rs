//! Home and a device on this machine (no relays), with a stand-in backend: pairing, tunnelling
//! and removal.

use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use mnemosyne_link::{bind, serve_home, serve_local, Home, Relays, Remote};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

/// Answers POST /api/pairing/redeem like the backend (code "good" pairs the endpoint id by
/// writing it to the devices file) and every other request with "hi".
async fn fake_backend(devices_file: PathBuf) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let (mut tcp, _) = listener.accept().await.unwrap();
            let devices_file = devices_file.clone();
            tokio::spawn(async move {
                let mut buf = vec![0u8; 8192];
                let mut n = 0;
                // Read until the head and (for POST) the JSON body have arrived.
                loop {
                    let got = tcp.read(&mut buf[n..]).await.unwrap();
                    n += got;
                    let text = String::from_utf8_lossy(&buf[..n]).to_string();
                    if got == 0
                        || (text.contains("\r\n\r\n")
                            && (!text.starts_with("POST") || text.trim_end().ends_with('}')))
                    {
                        break;
                    }
                }
                let text = String::from_utf8_lossy(&buf[..n]).to_string();
                let (status, body) = if text.starts_with("POST /api/pairing/redeem") {
                    let json: serde_json::Value =
                        serde_json::from_str(text.split("\r\n\r\n").nth(1).unwrap()).unwrap();
                    if json["code"] == "good" {
                        let file =
                            serde_json::json!({"devices": [{"endpoint_id": json["endpoint_id"]}]});
                        std::fs::write(&devices_file, file.to_string()).unwrap();
                        ("200 OK", r#"{"token":"device-token"}"#.to_string())
                    } else {
                        (
                            "403 Forbidden",
                            r#"{"detail":"This pairing code is invalid or has expired"}"#
                                .to_string(),
                        )
                    }
                } else {
                    ("200 OK", "hi".to_string())
                };
                let reply = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                tcp.write_all(reply.as_bytes()).await.unwrap();
                tcp.shutdown().await.ok();
            });
        }
    });
    addr
}

async fn get(local: SocketAddr) -> String {
    let mut tcp = TcpStream::connect(local).await.unwrap();
    tcp.write_all(b"GET /api/sessions HTTP/1.1\r\nHost: x\r\n\r\n")
        .await
        .unwrap();
    let mut out = String::new();
    let _ = tokio::time::timeout(Duration::from_secs(5), tcp.read_to_string(&mut out)).await;
    out
}

#[tokio::test]
async fn pair_then_tunnel_then_remove() {
    let dir = tempfile::tempdir().unwrap();
    let devices_file = dir.path().join("paired_devices.json");
    let backend = fake_backend(devices_file.clone()).await;

    let home_ep = bind(iroh::SecretKey::generate(), &Relays::Off)
        .await
        .unwrap();
    let home_addr = home_ep.addr();
    tokio::spawn(serve_home(
        home_ep.clone(),
        Home {
            backend,
            devices_file: devices_file.clone(),
        },
    ));

    let device_ep = bind(iroh::SecretKey::generate(), &Relays::Off)
        .await
        .unwrap();
    let remote = Arc::new(Remote::new(device_ep.clone(), home_addr));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let local = listener.local_addr().unwrap();
    tokio::spawn(serve_local(remote.clone(), listener));

    // Not paired yet: the tunnel is refused.
    assert!(!get(local).await.contains("hi"));

    // A wrong code is refused with the backend's reason; the right one returns the token.
    let err = remote.pair("bad", "Laptop").await.unwrap_err().to_string();
    assert!(err.contains("expired"), "{err}");
    assert_eq!(remote.pair("good", "Laptop").await.unwrap(), "device-token");

    // Paired: requests reach the backend.
    assert!(get(local).await.ends_with("hi"));
    assert!(get(local).await.ends_with("hi"));

    // Removed: new requests are refused at once.
    std::fs::write(&devices_file, r#"{"devices": []}"#).unwrap();
    assert!(!get(local).await.contains("hi"));

    device_ep.close().await;
    home_ep.close().await;
}

#[tokio::test]
async fn through_our_own_relay() {
    // A local iroh-relay stands in for one we host; both ends only know its address, so every
    // byte goes through it (no direct addresses in the ticket, no n0 services).
    let (_map, relay_url, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let relays = mnemosyne_link::Relays::Custom(vec![relay_url.clone()]);
    let trust_test_relay = |b: iroh::endpoint::Builder| {
        b.ca_tls_config(iroh_relay::tls::CaTlsConfig::insecure_skip_verify())
    };

    let dir = tempfile::tempdir().unwrap();
    let devices_file = dir.path().join("paired_devices.json");
    let backend = fake_backend(devices_file.clone()).await;

    let home_ep = mnemosyne_link::bind_with(iroh::SecretKey::generate(), &relays, trust_test_relay)
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(10), home_ep.online())
        .await
        .unwrap();
    let home_addr = iroh::EndpointAddr::new(home_ep.id()).with_relay_url(relay_url);
    tokio::spawn(serve_home(
        home_ep.clone(),
        Home {
            backend,
            devices_file,
        },
    ));

    let device_ep =
        mnemosyne_link::bind_with(iroh::SecretKey::generate(), &relays, trust_test_relay)
            .await
            .unwrap();
    let remote = Arc::new(Remote::new(device_ep.clone(), home_addr));
    assert_eq!(remote.pair("good", "Laptop").await.unwrap(), "device-token");
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let local = listener.local_addr().unwrap();
    tokio::spawn(serve_local(remote, listener));
    assert!(get(local).await.ends_with("hi"));

    device_ep.close().await;
    home_ep.close().await;
}
