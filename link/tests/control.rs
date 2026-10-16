//! The control channel of a running home: pairing with peer info, dialing peers from home's own
//! endpoint, hanging up, bad lines. Two homes on this machine (no relays, no mDNS), each with a
//! stand-in backend.

use std::{
    io::{BufRead, BufReader, Write},
    net::SocketAddr,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    time::Duration,
};

use iroh::Endpoint;
use mnemosyne_link::{bind_with, serve_home, Control, EndpointTicket, Home, Lan, Relays};
use serde_json::{json, Value};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

type Redeemed = Arc<Mutex<Vec<Value>>>;

/// Answers POST /api/pairing/redeem like the backend (code "good" pairs the endpoint id),
/// remembering each redeem body, and every other request with `greeting`.
async fn fake_backend(
    devices_file: PathBuf,
    greeting: &'static str,
    redeemed: Redeemed,
) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let (mut tcp, _) = listener.accept().await.unwrap();
            let (devices_file, redeemed) = (devices_file.clone(), redeemed.clone());
            tokio::spawn(async move {
                let mut buf = vec![0u8; 8192];
                let mut n = 0;
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
                    let body: Value =
                        serde_json::from_str(text.split("\r\n\r\n").nth(1).unwrap()).unwrap();
                    redeemed.lock().unwrap().push(body.clone());
                    if body["code"] == "good" {
                        let file = json!({"devices": [{"endpoint_id": body["endpoint_id"]}]});
                        std::fs::write(&devices_file, file.to_string()).unwrap();
                        ("200 OK", r#"{"token":"device-token","sync":{"id":"b1b2b3b4b5b6","name":"Desk B"}}"#.to_string())
                    } else {
                        ("403 Forbidden", r#"{"detail":"invalid code"}"#.to_string())
                    }
                } else {
                    ("200 OK", greeting.to_string())
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

/// One machine: a home endpoint serving its own fake backend.
struct Machine {
    endpoint: Endpoint,
    devices_file: PathBuf,
    redeemed: Redeemed,
    _dir: tempfile::TempDir,
}

impl Machine {
    async fn start(greeting: &'static str) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let devices_file = dir.path().join("paired_devices.json");
        let redeemed = Redeemed::default();
        let backend = fake_backend(devices_file.clone(), greeting, redeemed.clone()).await;
        let endpoint = bind_with(iroh::SecretKey::generate(), &Relays::Off, Lan::Off, |b| b)
            .await
            .unwrap();
        tokio::spawn(serve_home(
            endpoint.clone(),
            Home {
                backend,
                devices_file: devices_file.clone(),
            },
        ));
        Machine {
            endpoint,
            devices_file,
            redeemed,
            _dir: dir,
        }
    }

    fn ticket(&self) -> String {
        EndpointTicket::new(self.endpoint.addr()).to_string()
    }

    /// Let `other` in, as pairing would.
    fn trust(&self, other: &str) {
        trust(&self.devices_file, other);
    }
}

fn trust(devices_file: &Path, endpoint_id: &str) {
    let file = json!({"devices": [{"endpoint_id": endpoint_id}]});
    std::fs::write(devices_file, file.to_string()).unwrap();
}

async fn get(local: SocketAddr) -> String {
    let mut tcp = TcpStream::connect(local).await.unwrap();
    tcp.write_all(b"GET /api/sync/hello HTTP/1.1\r\nHost: x\r\n\r\n")
        .await
        .unwrap();
    let mut out = String::new();
    let _ = tokio::time::timeout(Duration::from_secs(5), tcp.read_to_string(&mut out)).await;
    out
}

async fn next(lines: &mut tokio::sync::mpsc::UnboundedReceiver<Value>) -> Value {
    tokio::time::timeout(Duration::from_secs(20), lines.recv())
        .await
        .expect("no line from the child")
        .expect("the child's stdout closed")
}

fn listen_of(reply: &Value) -> SocketAddr {
    assert_eq!(reply["ok"], true, "{reply}");
    reply["listen"].as_str().unwrap().parse().unwrap()
}

#[tokio::test]
async fn pair_with_peer_info() {
    let a = Machine::start("from a").await;
    let b = Machine::start("from b").await;
    let control = Control::new(a.endpoint.clone());

    let peer = json!({"id": "a1b2c3d4e5f6", "name": "Desk A", "ticket": a.ticket(), "token": "token-for-a"});
    let line = json!({
        "id": "p1", "op": "pair", "invite": format!("{}#good", b.ticket()),
        "name": "Desk A", "peer": peer,
    });
    let reply = control.handle(&line.to_string()).await;
    assert_eq!(reply["id"], "p1", "{reply}");
    assert_eq!(reply["op"], "pair");
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["token"], "device-token");
    // Home's backend told who it is for sync; the sidecar passes it on.
    assert_eq!(reply["sync"]["id"], "b1b2b3b4b5b6", "{reply}");
    assert_eq!(reply["sync"]["name"], "Desk B");
    assert_eq!(reply["ticket"], b.ticket());
    assert_eq!(reply["peer"], b.endpoint.id().to_string());

    // B's backend got the peer info unchanged, for A's own endpoint id.
    let body = b.redeemed.lock().unwrap().last().cloned().unwrap();
    assert_eq!(body["peer"], peer);
    assert_eq!(body["name"], "Desk A");
    assert_eq!(body["endpoint_id"], a.endpoint.id().to_string());

    // Without peer info the body has no `peer` (the old request), and a refusal is ok:false.
    let line =
        json!({"id": "p2", "op": "pair", "invite": format!("{}#bad", b.ticket()), "name": "A"});
    let reply = control.handle(&line.to_string()).await;
    assert_eq!(reply["ok"], false, "{reply}");
    assert!(reply["error"].as_str().unwrap().contains("invalid code"));
    let body = b.redeemed.lock().unwrap().last().cloned().unwrap();
    assert!(body.get("peer").is_none(), "{body}");

    a.endpoint.close().await;
    b.endpoint.close().await;
}

#[tokio::test]
async fn dial_both_ways_then_hang_up() {
    let a = Machine::start("from a").await;
    let b = Machine::start("from b").await;
    a.trust(&b.endpoint.id().to_string());
    b.trust(&a.endpoint.id().to_string());
    let control_a = Control::new(a.endpoint.clone());
    let control_b = Control::new(b.endpoint.clone());

    let dial = |id: &str, ticket: String| json!({"id": id, "op": "dial", "ticket": ticket});
    let reply = control_a.handle(&dial("d1", b.ticket()).to_string()).await;
    assert_eq!(reply["peer"], b.endpoint.id().to_string());
    let a_to_b = listen_of(&reply);
    assert!(a_to_b.ip().is_loopback());
    let b_to_a = listen_of(&control_b.handle(&dial("d2", a.ticket()).to_string()).await);

    // Each side reaches the other's backend, while still serving its own peer.
    assert!(get(a_to_b).await.ends_with("from b"));
    assert!(get(b_to_a).await.ends_with("from a"));
    assert!(get(a_to_b).await.ends_with("from b"));

    // Dialing again returns the listener that is already there.
    let again = control_a.handle(&dial("d3", b.ticket()).to_string()).await;
    assert_eq!(listen_of(&again), a_to_b);

    // Hang up: the listener is closed at once; the other direction is untouched.
    let hangup = json!({"id": "h1", "op": "hangup", "peer": b.endpoint.id().to_string()});
    let reply = control_a.handle(&hangup.to_string()).await;
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["closed"], true);
    assert!(TcpStream::connect(a_to_b).await.is_err());
    assert!(get(b_to_a).await.ends_with("from a"));
    let reply = control_a.handle(&hangup.to_string()).await;
    assert_eq!(reply["closed"], false, "{reply}");

    // Dialing after a hangup opens a new listener.
    let a_to_b = listen_of(&control_a.handle(&dial("d4", b.ticket()).to_string()).await);
    assert!(get(a_to_b).await.ends_with("from b"));

    a.endpoint.close().await;
    b.endpoint.close().await;
}

#[tokio::test]
async fn bad_lines_are_answered_and_change_nothing() {
    let a = Machine::start("from a").await;
    let b = Machine::start("from b").await;
    b.trust(&a.endpoint.id().to_string());
    let control = Control::new(a.endpoint.clone());

    let reply = control.handle("not json").await;
    assert_eq!(reply["ok"], false, "{reply}");
    assert_eq!(reply["id"], Value::Null);
    for (line, id) in [
        (json!({"id": "1", "op": "nope"}), "1"),
        (json!({"id": "2", "op": "dial"}), "2"),
        (json!({"id": "3", "op": "dial", "ticket": "garbage"}), "3"),
        (json!({"id": "4", "op": "hangup", "peer": "garbage"}), "4"),
        (
            json!({"id": "5", "op": "pair", "invite": "no-hash", "name": "A"}),
            "5",
        ),
        (json!({"id": "6", "op": "dial", "ticket": a.ticket()}), "6"),
    ] {
        let reply = control.handle(&line.to_string()).await;
        assert_eq!(reply["id"], id, "{reply}");
        assert_eq!(reply["ok"], false, "{reply}");
        assert!(!reply["error"].as_str().unwrap().is_empty());
    }

    let dial = json!({"id": "7", "op": "dial", "ticket": b.ticket()});
    let listen = listen_of(&control.handle(&dial.to_string()).await);
    assert!(get(listen).await.ends_with("from b"));

    a.endpoint.close().await;
    b.endpoint.close().await;
}

/// The binary itself: the ready line first, replies on stdout, and closing stdin keeps both
/// home and what was dialed running.
#[tokio::test]
async fn home_binary_takes_commands_on_stdin() {
    let b = Machine::start("from b").await;
    let dir = tempfile::tempdir().unwrap();
    let devices_file = dir.path().join("paired_devices.json");
    let backend = fake_backend(devices_file.clone(), "from child", Redeemed::default()).await;

    let mut child = Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"))
        .args([
            "home",
            "--no-relay",
            "--backend",
            &backend.to_string(),
            "--data-dir",
        ])
        .arg(dir.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let (tx, mut lines) = tokio::sync::mpsc::unbounded_channel::<Value>();
    let stdout = child.stdout.take().unwrap();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if tx.send(serde_json::from_str(&line).unwrap()).is_err() {
                break;
            }
        }
    });

    let ready = next(&mut lines).await;
    let child_id = ready["endpoint_id"].as_str().unwrap().to_string();
    assert!(ready["ticket"].is_string(), "{ready}");
    assert!(ready.get("id").is_none());
    b.trust(&child_id);

    let mut stdin = child.stdin.take().unwrap();
    writeln!(stdin, "{{ broken").unwrap();
    let reply = next(&mut lines).await;
    assert_eq!(reply["ok"], false, "{reply}");
    writeln!(
        stdin,
        "{}",
        json!({"id": "d", "op": "dial", "ticket": b.ticket()})
    )
    .unwrap();
    let reply = next(&mut lines).await;
    assert_eq!(reply["id"], "d");
    let listen = listen_of(&reply);
    assert!(get(listen).await.ends_with("from b"));

    // stdin closed: still serving, both ways (b reaches the child's backend too).
    drop(stdin);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        child.try_wait().unwrap().is_none(),
        "home exited on stdin EOF"
    );
    assert!(get(listen).await.ends_with("from b"));
    trust(&devices_file, &b.endpoint.id().to_string());
    let back = Control::new(b.endpoint.clone());
    let ticket = ready["ticket"].as_str().unwrap().to_string();
    let reply = back
        .handle(&json!({"id": "x", "op": "dial", "ticket": ticket}).to_string())
        .await;
    assert!(get(listen_of(&reply)).await.ends_with("from child"));

    child.kill().unwrap();
    child.wait().unwrap();
    b.endpoint.close().await;
}
