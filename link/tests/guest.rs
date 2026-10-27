//! Guest rooms through a local relay: native clients stand in for the guest page (guest-wasm),
//! the embedded loopback host for the room, and the test itself for the backend's bridge.

use std::{net::SocketAddr, sync::Arc, time::Duration};

use iroh::{
    endpoint::{presets, Builder, Connection, ConnectionError, RecvStream, SendStream},
    Endpoint, EndpointAddr, RelayMode, RelayUrl,
};
use mnemosyne_link::guest::{
    frame, pick_relay_with, Adjust, GuestOptions, GuestRooms, RoomInfo, GUEST_ALPN,
};
use mnemosyne_link::host::{request, start_loopback_host, HostOptions};
use mnemosyne_link::{bind_with, Control, Lan, Relays, ALPN};
use serde_json::{json, Value};
use tokio::time::timeout;

const UPLOAD: &str = "upload-secret-for-the-test";
const OWNER: &str = "Bearer owner-token";
const ROOM: &str = "0123456789abcdef";
const TOKEN: &str = "guest-room-token";
const WAIT: Duration = Duration::from_secs(20);

fn trust(b: Builder) -> Builder {
    b.ca_tls_config(iroh_relay::tls::CaTlsConfig::insecure_skip_verify())
}

fn adjust() -> Adjust {
    Arc::new(trust)
}

async fn loopback_host(dir: &tempfile::TempDir) -> SocketAddr {
    let guest_dir = dir.path().join("guest");
    std::fs::create_dir_all(&guest_dir).unwrap();
    std::fs::write(
        guest_dir.join("index.html"),
        "<!doctype html><title>guest</title>",
    )
    .unwrap();
    let (addr, _app) = start_loopback_host(HostOptions {
        data_dir: dir.path().to_path_buf(),
        guest_dir,
        viewer_dir: dir.path().join("viewer"),
        upload_token: UPLOAD.into(),
        relay_origins: vec![],
    })
    .await
    .unwrap();
    addr
}

/// Creates the room as the bridge does: upload token, owner token, room token, expiry.
async fn create_room(host: SocketAddr, room: &str) {
    let expires = (std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 3600)
        .to_string();
    let headers = [
        ("Authorization", OWNER),
        ("X-Room-Token", TOKEN),
        ("X-Room-Expires", expires.as_str()),
        ("X-Upload-Token", UPLOAD),
    ];
    let (status, body) = request(host, "PUT", &format!("/live/{room}"), &headers, b"")
        .await
        .unwrap();
    assert_eq!(status, 201, "{}", String::from_utf8_lossy(&body));
}

async fn owner(host: SocketAddr, method: &str, path: &str) -> u16 {
    request(host, method, path, &[("Authorization", OWNER)], b"")
        .await
        .unwrap()
        .0
}

/// One guest, as the page would be: an endpoint on the same relay, one bi stream.
struct Guest {
    endpoint: Endpoint,
    conn: Connection,
    send: SendStream,
    recv: RecvStream,
}

async fn client(relay: &RelayUrl) -> Endpoint {
    trust(
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Custom(std::iter::once(relay.clone()).collect())),
    )
    .bind()
    .await
    .unwrap()
}

impl Guest {
    async fn dial(info: &RoomInfo) -> Guest {
        let endpoint = client(&info.relay).await;
        let addr = EndpointAddr::new(info.endpoint_id).with_relay_url(info.relay.clone());
        let conn = timeout(WAIT, endpoint.connect(addr, GUEST_ALPN))
            .await
            .expect("the room endpoint was not reached")
            .unwrap();
        let (send, recv) = conn.open_bi().await.unwrap();
        Guest {
            endpoint,
            conn,
            send,
            recv,
        }
    }

    async fn join(info: &RoomInfo, token: &str) -> Guest {
        let mut guest = Guest::dial(info).await;
        guest
            .text(json!({"type": "join", "token": token, "since": 0}))
            .await;
        guest
    }

    async fn text(&mut self, value: Value) {
        let bytes = frame::encode(frame::TAG_TEXT, value.to_string().as_bytes()).unwrap();
        self.send.write_all(&bytes).await.unwrap();
    }

    async fn binary(&mut self, payload: &[u8]) {
        let bytes = frame::encode(frame::TAG_BINARY, payload).unwrap();
        self.send.write_all(&bytes).await.unwrap();
    }

    /// The next frame, or None once the proxy finished the stream (or the connection ended).
    async fn next(&mut self) -> Option<(u8, Vec<u8>)> {
        let mut header = [0u8; frame::HEADER_LEN];
        timeout(WAIT, self.recv.read_exact(&mut header))
            .await
            .expect("no frame")
            .ok()?;
        let len = frame::body_len(header).unwrap();
        let mut body = vec![0u8; len];
        self.recv.read_exact(&mut body).await.ok()?;
        let (tag, payload) = frame::split_body(&body).unwrap();
        Some((tag, payload.to_vec()))
    }

    /// The next text message of `kind`, skipping presence counts and the like.
    async fn expect(&mut self, kind: &str) -> Value {
        loop {
            let (tag, payload) = self.next().await.expect("stream ended");
            if tag != frame::TAG_TEXT {
                continue;
            }
            let value: Value = serde_json::from_slice(&payload).unwrap();
            if value["type"] == kind {
                return value;
            }
        }
    }

    /// The next binary message.
    async fn expect_binary(&mut self) -> Vec<u8> {
        loop {
            let (tag, payload) = self.next().await.expect("stream ended");
            if tag == frame::TAG_BINARY {
                return payload;
            }
        }
    }

    /// Reads to the end of the stream, then how the connection was closed.
    async fn close_code(&mut self) -> (u64, String) {
        while self.next().await.is_some() {}
        match timeout(WAIT, self.conn.closed()).await.expect("not closed") {
            ConnectionError::ApplicationClosed(close) => (
                close.error_code.into_inner(),
                String::from_utf8_lossy(&close.reason).into_owned(),
            ),
            other => panic!("closed without an application close: {other}"),
        }
    }
}

#[tokio::test]
async fn guests_reach_the_room_through_the_relay() {
    let (_map, relay, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let host = loopback_host(&dir).await;
    create_room(host, ROOM).await;

    let rooms = GuestRooms::with_adjust(
        GuestOptions {
            data_dir: dir.path().to_path_buf(),
            host,
            relays: Relays::Custom(vec![relay.clone()]),
        },
        adjust(),
    );
    let info = rooms.start(ROOM, None).await.unwrap();
    assert_eq!(info.relay, relay);
    assert!(dir
        .path()
        .join("guest_rooms/keys")
        .join(format!("{ROOM}.key"))
        .exists());
    // Starting it again returns the room as it is.
    assert_eq!(rooms.start(ROOM, None).await.unwrap(), info);
    assert_eq!(rooms.list().await, vec![info.clone()]);

    // Join: the welcome comes back over the stream.
    let mut a = Guest::join(&info, TOKEN).await;
    let welcome = a.expect("welcome").await;
    assert_eq!(welcome["seq"], 0);
    assert_eq!(welcome["ended"], false);

    let mut b = Guest::join(&info, TOKEN).await;
    b.expect("welcome").await;

    // A frame is acknowledged to its sender and relayed to the other guest.
    a.binary(&[0x01, b'e', b'n', b'v']).await;
    assert_eq!(a.expect("ack").await["seq"], 1);
    let relayed = b.expect_binary().await;
    assert_eq!(relayed[0], 0x01);
    assert_eq!(&relayed[1..9], &1u64.to_be_bytes());
    assert_eq!(&relayed[9..], b"env");

    // Text ping is the room protocol's own: answered by the host.
    let ping = frame::encode(frame::TAG_TEXT, b"ping").unwrap();
    b.send.write_all(&ping).await.unwrap();
    loop {
        let (tag, payload) = b.next().await.unwrap();
        if tag == frame::TAG_TEXT && payload == b"pong" {
            break;
        }
    }

    // A wrong room token: the host's 4401 is the connection's application close.
    let mut wrong = Guest::join(&info, "not-the-token").await;
    assert_eq!(wrong.close_code().await, (4401, "wrong_room_token".into()));

    // A guest that finishes its stream: the WebSocket closes with 1000, and so does the guest.
    let mut done = Guest::join(&info, TOKEN).await;
    done.expect("welcome").await;
    done.send.finish().unwrap();
    assert_eq!(done.close_code().await.0, 1000);

    // A frame over the cap: an error message, then 4413.
    let mut big = Guest::join(&info, TOKEN).await;
    big.expect("welcome").await;
    let over = ((frame::MAX_PAYLOAD + 2) as u32).to_be_bytes();
    big.send.write_all(&over).await.unwrap();
    assert_eq!(big.expect("error").await["code"], "frame_too_large");
    assert_eq!(big.close_code().await, (4413, "frame_too_large".into()));

    // Ended: guests are told, and a frame is refused with 4403.
    assert_eq!(owner(host, "POST", &format!("/live/{ROOM}/end")).await, 200);
    a.expect("ended").await;
    a.binary(&[0x01, b'x']).await;
    assert_eq!(a.close_code().await, (4403, "ended".into()));

    // Deleted: a connected guest gets 4410.
    b.expect("ended").await;
    assert_eq!(owner(host, "DELETE", &format!("/live/{ROOM}")).await, 204);
    assert_eq!(b.close_code().await.0, 4410);

    for g in [a, b, wrong, done, big] {
        g.endpoint.close().await;
    }
    rooms.close_all().await;
}

#[tokio::test]
async fn the_endpoint_id_survives_a_restart_until_stopped() {
    let (_map, relay, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let host = loopback_host(&dir).await;
    let opts = GuestOptions {
        data_dir: dir.path().to_path_buf(),
        host,
        relays: Relays::Custom(vec![relay.clone()]),
    };

    // No relay configured (`--no-relay`): refused, even when the caller names one (a link made
    // earlier must not be served through a relay we were told not to use).
    let off = GuestRooms::with_adjust(
        GuestOptions {
            relays: Relays::Off,
            ..opts.clone()
        },
        adjust(),
    );
    assert!(off.start(ROOM, None).await.is_err());
    let err = off.start(ROOM, Some(relay.clone())).await.unwrap_err();
    assert!(err.to_string().contains("need a relay"), "{err:#}");
    assert!(off.list().await.is_empty());

    let first = GuestRooms::with_adjust(opts.clone(), adjust());
    assert!(first
        .start("not-a-room", Some(relay.clone()))
        .await
        .is_err());

    let info = first.start(ROOM, Some(relay.clone())).await.unwrap();
    assert_eq!(info.relay, relay);
    first.close_all().await;
    drop(first);

    // A new process (same data dir): the same endpoint id, still reachable.
    let second = GuestRooms::with_adjust(opts.clone(), adjust());
    let again = second.start(ROOM, Some(relay.clone())).await.unwrap();
    assert_eq!(again.endpoint_id, info.endpoint_id);
    create_room(host, ROOM).await;
    let mut guest = Guest::join(&again, TOKEN).await;
    guest.expect("welcome").await;
    guest.endpoint.close().await;

    // Stopped: the key is gone, so the next start is a new endpoint.
    assert!(second.stop(ROOM).await);
    assert!(!second.stop(ROOM).await);
    assert!(second.list().await.is_empty());
    assert!(!dir
        .path()
        .join("guest_rooms/keys")
        .join(format!("{ROOM}.key"))
        .exists());
    let fresh = second.start(ROOM, Some(relay.clone())).await.unwrap();
    assert_ne!(fresh.endpoint_id, info.endpoint_id);
    second.close_all().await;
}

#[tokio::test]
async fn the_room_endpoint_does_not_speak_the_link_protocol() {
    let (_map, relay, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let host = loopback_host(&dir).await;
    let rooms = GuestRooms::with_adjust(
        GuestOptions {
            data_dir: dir.path().to_path_buf(),
            host,
            relays: Relays::Custom(vec![relay.clone()]),
        },
        adjust(),
    );
    let info = rooms.start(ROOM, None).await.unwrap();
    let endpoint = client(&relay).await;
    let addr = EndpointAddr::new(info.endpoint_id).with_relay_url(relay);
    let result = timeout(WAIT, endpoint.connect(addr, ALPN))
        .await
        .expect("no answer");
    assert!(result.is_err(), "the link ALPN was accepted");
    endpoint.close().await;
    rooms.close_all().await;
}

#[tokio::test]
async fn several_relays_pick_one_that_answers() {
    let (_m1, one, _s1) = iroh::test_utils::run_relay_server().await.unwrap();
    let (_m2, two, _s2) = iroh::test_utils::run_relay_server().await.unwrap();
    let relays = Relays::Custom(vec![one.clone(), two.clone()]);
    let picked = pick_relay_with(&relays, trust).await.unwrap();
    assert!(picked == one || picked == two, "{picked}");
    // Remembered for the process.
    assert_eq!(pick_relay_with(&relays, trust).await.unwrap(), picked);
    // A single relay is taken as it is, without asking it.
    let only = Relays::Custom(vec![one.clone()]);
    assert_eq!(pick_relay_with(&only, |b| b).await.unwrap(), one);
    assert!(pick_relay_with(&Relays::Off, |b| b).await.is_err());
}

#[tokio::test]
async fn control_lines() {
    let (_map, relay, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let host = loopback_host(&dir).await;
    let rooms = GuestRooms::with_adjust(
        GuestOptions {
            data_dir: dir.path().to_path_buf(),
            host,
            relays: Relays::Custom(vec![relay.clone()]),
        },
        adjust(),
    );

    let reply = rooms
        .handle(&json!({"id": 1, "op": "room_start", "room": ROOM}).to_string())
        .await;
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["id"], 1);
    assert_eq!(reply["op"], "room_start");
    assert_eq!(reply["relay"], relay.to_string());
    let endpoint_id = reply["endpoint_id"].as_str().unwrap().to_string();

    // A stored relay comes back as given.
    let reply = rooms
        .handle(
            &json!({"id": 2, "op": "room_start", "room": ROOM, "relay": relay.to_string()})
                .to_string(),
        )
        .await;
    assert_eq!(reply["endpoint_id"], endpoint_id);

    let reply = rooms
        .handle(&json!({"id": 3, "op": "room_list"}).to_string())
        .await;
    assert_eq!(
        reply["rooms"],
        json!([{"room": ROOM, "endpoint_id": endpoint_id, "relay": relay.to_string()}])
    );

    let reply = rooms
        .handle(&json!({"id": 4, "op": "room_start", "room": "../etc"}).to_string())
        .await;
    assert_eq!(reply["ok"], false);
    assert!(reply["error"].as_str().unwrap().contains("invalid room id"));

    let reply = rooms
        .handle(&json!({"id": 5, "op": "room_start", "room": ROOM, "relay": "nope"}).to_string())
        .await;
    assert_eq!(reply["ok"], false);

    // Home's ops are not served here.
    let reply = rooms
        .handle(&json!({"id": 6, "op": "hangup", "peer": endpoint_id}).to_string())
        .await;
    assert_eq!(reply["ok"], false);
    assert!(reply["error"].as_str().unwrap().contains("not supported"));

    let reply = rooms
        .handle(&json!({"id": 7, "op": "room_stop", "room": ROOM}).to_string())
        .await;
    assert_eq!(
        (reply["ok"].clone(), reply["closed"].clone()),
        (json!(true), json!(true))
    );
    let reply = rooms
        .handle(&json!({"id": 8, "op": "room_stop", "room": ROOM}).to_string())
        .await;
    assert_eq!(reply["closed"], false);

    // And home does not serve the room ops.
    let home = bind_with(iroh::SecretKey::generate(), &Relays::Off, Lan::Off, |b| b)
        .await
        .unwrap();
    let control = Control::new(home.clone());
    for op in ["room_list", "room_stop", "room_start"] {
        let reply = control
            .handle(&json!({"id": 9, "op": op, "room": ROOM}).to_string())
            .await;
        assert_eq!(reply["ok"], false, "{reply}");
        assert!(reply["error"].as_str().unwrap().contains("not supported"));
    }
    home.close().await;
    rooms.close_all().await;
}

#[tokio::test]
async fn rooms_binary_needs_a_private_token_file() {
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::process::{Command, Stdio};

    let dir = tempfile::tempdir().unwrap();
    let token_file = dir.path().join("upload.token");
    let run = |extra: &[&str]| {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"));
        cmd.arg("rooms")
            .arg("--data-dir")
            .arg(dir.path())
            .arg("--guest-dir")
            .arg(dir.path().join("guest"))
            .arg("--viewer-dir")
            .arg(dir.path().join("viewer"))
            .arg("--upload-token-file")
            .arg(&token_file)
            .args(extra)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        cmd
    };

    // Missing, then world-readable: refused before anything starts.
    assert!(!run(&[]).status().unwrap().success());
    std::fs::write(&token_file, format!("{UPLOAD}\n")).unwrap();
    std::fs::set_permissions(&token_file, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(!run(&[]).status().unwrap().success());

    std::fs::set_permissions(&token_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut child = run(&["--relay", "https://relay.example.org"])
        .spawn()
        .unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    let ready: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(ready["relay_kind"], "custom", "{ready}");
    let listen: SocketAddr = ready["listen"].as_str().unwrap().parse().unwrap();
    assert!(listen.ip().is_loopback());
    let (status, _) = request(listen, "GET", "/health", &[], b"").await.unwrap();
    assert_eq!(status, 200);
    // The token from the file opens rooms (trimmed of its newline).
    create_room(listen, ROOM).await;

    let mut stdin = child.stdin.take().unwrap();
    writeln!(stdin, "{}", json!({"id": "l", "op": "room_list"})).unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    let reply: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(
        reply,
        json!({"id": "l", "op": "room_list", "ok": true, "rooms": []})
    );
    writeln!(stdin, "{}", json!({"id": "d", "op": "dial", "ticket": "x"})).unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    let reply: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(reply["ok"], false, "{reply}");

    child.kill().ok();
    child.wait().ok();
}

/// `rooms --no-relay`: the loopback host runs (a tunnel can front it), the ready line says
/// "none", and no room is served over iroh, with or without a relay named by the caller.
#[tokio::test]
async fn rooms_binary_without_a_relay_keeps_the_host_but_serves_no_room() {
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::process::{Command, Stdio};

    let dir = tempfile::tempdir().unwrap();
    let token_file = dir.path().join("upload.token");
    std::fs::write(&token_file, format!("{UPLOAD}\n")).unwrap();
    std::fs::set_permissions(&token_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"))
        .arg("rooms")
        .arg("--data-dir")
        .arg(dir.path())
        .arg("--guest-dir")
        .arg(dir.path().join("guest"))
        .arg("--viewer-dir")
        .arg(dir.path().join("viewer"))
        .arg("--upload-token-file")
        .arg(&token_file)
        .arg("--no-relay")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    stdout.read_line(&mut line).unwrap();
    let ready: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(ready["relay_kind"], "none", "{ready}");
    let listen: SocketAddr = ready["listen"].as_str().unwrap().parse().unwrap();
    assert!(listen.ip().is_loopback());
    let (status, _) = request(listen, "GET", "/health", &[], b"").await.unwrap();
    assert_eq!(status, 200);
    // A tunnel room lives on the host without any iroh endpoint.
    create_room(listen, ROOM).await;

    let mut stdin = child.stdin.take().unwrap();
    for (id, extra) in [
        ("a", json!({})),
        ("b", json!({"relay": "https://relay.example.org"})),
    ] {
        let mut req = json!({"id": id, "op": "room_start", "room": ROOM});
        req.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        writeln!(stdin, "{req}").unwrap();
        line.clear();
        stdout.read_line(&mut line).unwrap();
        let reply: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(reply["ok"], false, "{reply}");
        assert!(
            reply["error"].as_str().unwrap().contains("need a relay"),
            "{reply}"
        );
    }
    writeln!(stdin, "{}", json!({"id": "l", "op": "room_list"})).unwrap();
    line.clear();
    stdout.read_line(&mut line).unwrap();
    let reply: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(reply["rooms"], json!([]), "{reply}");

    child.kill().ok();
    child.wait().ok();
}
