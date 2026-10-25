//! The guest-notes room contract: HTTP for the owner (create, snapshot, end, delete), a real
//! listener and WebSocket clients for the live side, and the `/n/` guest page.

use std::net::SocketAddr;
use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderMap, Request, StatusCode};
use futures_util::{SinkExt, StreamExt};
use mnemosyne_publish_host::app::{router, App, Shared};
use mnemosyne_publish_host::clock::Clock;
use mnemosyne_publish_host::config::Config;
use serde_json::{json, Value};
use tempfile::TempDir;
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::{self, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};
use tower::ServiceExt;

const T0: u64 = 1_800_000_000;
/// T0 + one day, as the Python client sends it.
const T0_PLUS_DAY_ISO: &str = "2027-01-16T08:00:00+00:00";
const DAY: u64 = 86_400;
const ROOM: &str = "0123456789abcdef";
const TOKEN: &str = "publisher-secret-token";
const ROOM_TOKEN: &str = "room-token-from-the-link";
const VIEWER_CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src \
'self'; media-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const GUEST_CSP: &str = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src \
'self'; connect-src 'self' https://*.relay.n0.iroh.link wss://*.relay.n0.iroh.link; media-src blob:; img-src data: blob:; base-uri 'none'; form-action \
'none'; frame-ancestors 'none'";
const WAIT: Duration = Duration::from_secs(10);

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;

struct Host {
    app: Shared,
    addr: SocketAddr,
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
    fn code(&self) -> String {
        self.json()["error"].as_str().unwrap_or("").to_string()
    }
}

/// What a client gets, pings and pongs left out.
#[derive(Debug, PartialEq)]
enum Got {
    Text(String),
    Bin(Vec<u8>),
    Closed(u16, String),
}

impl Got {
    fn json(&self) -> Value {
        match self {
            Got::Text(t) => serde_json::from_str(t).unwrap_or_else(|_| panic!("not JSON: {t}")),
            other => panic!("expected text, got {other:?}"),
        }
    }
}

async fn host() -> Host {
    host_with(|_| {}).await
}

async fn host_with(tweak: impl FnOnce(&mut Config)) -> Host {
    let tmp = tempfile::tempdir().unwrap();
    for dir in ["viewer", "viewer/assets", "guest", "guest/assets"] {
        std::fs::create_dir(tmp.path().join(dir)).unwrap();
    }
    std::fs::write(
        tmp.path().join("viewer/index.html"),
        "<title>viewer</title>",
    )
    .unwrap();
    std::fs::write(tmp.path().join("guest/index.html"), "<title>guest</title>").unwrap();
    std::fs::write(tmp.path().join("guest/assets/app.js"), "console.log(1)").unwrap();
    std::fs::write(
        tmp.path().join("guest/assets/notes_bg.wasm"),
        b"\0asm\x01\0\0\0",
    )
    .unwrap();
    std::fs::write(tmp.path().join("guest/.hidden"), "no").unwrap();
    std::fs::write(tmp.path().join("secret.txt"), "not for the web").unwrap();
    let mut cfg = Config::defaults();
    cfg.data_dir = tmp.path().join("data");
    cfg.viewer_dir = tmp.path().join("viewer");
    cfg.guest_dir = tmp.path().join("guest");
    cfg.write_per_minute = 0;
    cfg.read_per_minute = 0;
    tweak(&mut cfg);
    let clock = Clock::manual(T0);
    let app = App::new(cfg, clock.clone()).await.unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let service = router(app.clone()).into_make_service_with_connect_info::<SocketAddr>();
    tokio::spawn(async move { axum::serve(listener, service).await.unwrap() });
    Host {
        app,
        addr,
        clock,
        tmp,
    }
}

fn frame(kind: u8, seq: u64, bytes: &[u8]) -> Vec<u8> {
    let mut out = vec![kind];
    out.extend_from_slice(&seq.to_be_bytes());
    out.extend_from_slice(bytes);
    out
}

fn tagged(kind: u8, bytes: &[u8]) -> Vec<u8> {
    let mut out = vec![kind];
    out.extend_from_slice(bytes);
    out
}

impl Host {
    async fn send(&self, req: Request<Body>) -> Reply {
        let res = router(self.app.clone()).oneshot(req).await.unwrap();
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

    /// `PUT /live/<room>` as the publisher, with these extra headers.
    async fn create_with(&self, room: &str, headers: &[(&str, &str)]) -> Reply {
        let mut req = Request::put(format!("/live/{room}"));
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        self.send(req.body(Body::empty()).unwrap()).await
    }

    async fn create(&self, room: &str) -> Reply {
        let bearer = format!("Bearer {TOKEN}");
        let r = self
            .create_with(
                room,
                &[
                    ("authorization", &bearer),
                    ("x-room-token", ROOM_TOKEN),
                    ("x-room-expires", T0_PLUS_DAY_ISO),
                ],
            )
            .await;
        assert!(r.status.is_success(), "{} {:?}", r.status, r.json());
        r
    }

    async fn owner(&self, method: &str, uri: &str, headers: &[(&str, &str)], body: &[u8]) -> Reply {
        let mut req = Request::builder()
            .method(method)
            .uri(uri)
            .header("authorization", format!("Bearer {TOKEN}"));
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        self.send(req.body(Body::from(body.to_vec())).unwrap())
            .await
    }

    async fn snapshot(&self, room: &str, since: u64, bytes: &[u8]) -> Reply {
        let since = since.to_string();
        self.owner(
            "PUT",
            &format!("/live/{room}/snapshot"),
            &[("x-room-since", &since)],
            bytes,
        )
        .await
    }

    /// A WebSocket to the room, or the HTTP status that refused it.
    async fn try_open(&self, room: &str) -> Result<Ws, StatusCode> {
        match tokio_tungstenite::connect_async(format!("ws://{}/live/{room}", self.addr)).await {
            Ok((ws, _)) => Ok(ws),
            Err(tungstenite::Error::Http(res)) => {
                Err(StatusCode::from_u16(res.status().as_u16()).unwrap())
            }
            Err(e) => panic!("connect failed: {e}"),
        }
    }

    async fn open(&self, room: &str) -> Ws {
        self.try_open(room).await.unwrap()
    }

    /// Opens and joins; returns the socket and its welcome.
    async fn join(&self, room: &str, token: &str, since: u64) -> (Ws, Value) {
        let mut ws = self.open(room).await;
        send_join(&mut ws, token, since).await;
        let welcome = next(&mut ws).await.json();
        assert_eq!(welcome["type"], "welcome", "{welcome}");
        (ws, welcome)
    }

    async fn connections(&self, room: &str) -> usize {
        self.app
            .rooms
            .open_room(room)
            .await
            .unwrap()
            .lock()
            .await
            .connections()
    }

    /// Waits for the room's connection count to reach `n` (disconnects land a moment later).
    async fn await_connections(&self, room: &str, n: usize) {
        tokio::time::timeout(WAIT, async {
            while self.connections(room).await != n {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("connections never reached {n}"));
    }
}

async fn send_join(ws: &mut Ws, token: &str, since: u64) {
    let msg = json!({ "type": "join", "token": token, "since": since }).to_string();
    ws.send(Message::text(msg)).await.unwrap();
}

async fn send_bin(ws: &mut Ws, bytes: Vec<u8>) {
    ws.send(Message::binary(bytes)).await.unwrap();
}

async fn next(ws: &mut Ws) -> Got {
    tokio::time::timeout(WAIT, async {
        loop {
            match ws.next().await {
                Some(Ok(Message::Text(t))) => return Got::Text(t.as_str().to_string()),
                Some(Ok(Message::Binary(b))) => return Got::Bin(b.to_vec()),
                Some(Ok(Message::Close(Some(f)))) => {
                    return Got::Closed(u16::from(f.code), f.reason.as_str().to_string())
                }
                Some(Ok(Message::Close(None))) | None | Some(Err(_)) => {
                    return Got::Closed(0, String::new())
                }
                Some(Ok(_)) => continue,
            }
        }
    })
    .await
    .expect("nothing arrived")
}

/// The next message that is not a `count`.
async fn next_but_counts(ws: &mut Ws) -> Got {
    loop {
        let got = next(ws).await;
        if let Got::Text(t) = &got {
            if serde_json::from_str::<Value>(t).is_ok_and(|v| v["type"] == "count") {
                continue;
            }
        }
        return got;
    }
}

/// Proves nothing else is queued: a `ping` is answered by the very next message.
async fn nothing_more(ws: &mut Ws) {
    ws.send(Message::text("ping")).await.unwrap();
    assert_eq!(next_but_counts(ws).await, Got::Text("pong".into()));
}

/// Sends a frame and waits for its ack; returns the seq.
async fn append(ws: &mut Ws, bytes: &[u8]) -> u64 {
    send_bin(ws, tagged(0x01, bytes)).await;
    let ack = next_but_counts(ws).await.json();
    assert_eq!(ack["type"], "ack", "{ack}");
    ack["seq"].as_u64().unwrap()
}

#[tokio::test]
async fn create_needs_the_tokens() {
    let h = host().await;
    let bearer = format!("Bearer {TOKEN}");
    let r = h
        .create_with(
            ROOM,
            &[
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", T0_PLUS_DAY_ISO),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::UNAUTHORIZED, "token_required")
    );
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-expires", T0_PLUS_DAY_ISO),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "room_token_required")
    );
    let r = h
        .create_with(
            ROOM,
            &[("authorization", &bearer), ("x-room-token", ROOM_TOKEN)],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "bad_expires")
    );
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", "soon"),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "bad_expires")
    );
    let r = h
        .create_with(
            "not-a-room",
            &[
                ("authorization", &bearer),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", T0_PLUS_DAY_ISO),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "bad_id")
    );

    // ISO expiry; 201 when new.
    let r = h.create(ROOM).await;
    assert_eq!(r.status, StatusCode::CREATED);
    assert_eq!(
        r.json(),
        json!({ "created": T0, "expires": T0 + DAY, "ended_at": null, "last_seq": 0, "snapshot_seq": null })
    );
    // Again, in unix seconds and past room_max_days: 200 and clamped.
    let far = (T0 + 30 * DAY).to_string();
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", &far),
            ],
        )
        .await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.json()["expires"], T0 + 7 * DAY);
    // Another room token for the same room, or another publisher: refused.
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-token", "other"),
                ("x-room-expires", &far),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::CONFLICT, "room_exists")
    );
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", "Bearer someone-else"),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", &far),
            ],
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::FORBIDDEN, "wrong_token")
    );
    // Only the hash of the room token is stored.
    let meta =
        std::fs::read_to_string(h.tmp.path().join("data/rooms").join(ROOM).join("meta.json"))
            .unwrap();
    assert!(!meta.contains(ROOM_TOKEN) && !meta.contains(TOKEN));
}

#[tokio::test]
async fn a_closed_host_needs_the_upload_token_to_create() {
    let h = host_with(|c| c.upload_token = Some("let-me-in".into())).await;
    let bearer = format!("Bearer {TOKEN}");
    let base = [
        ("authorization", bearer.as_str()),
        ("x-room-token", ROOM_TOKEN),
        ("x-room-expires", T0_PLUS_DAY_ISO),
    ];
    let r = h.create_with(ROOM, &base).await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::FORBIDDEN, "upload_token_required")
    );
    let mut wrong = base.to_vec();
    wrong.push(("x-upload-token", "guess"));
    assert_eq!(
        h.create_with(ROOM, &wrong).await.status,
        StatusCode::FORBIDDEN
    );
    let mut right = base.to_vec();
    right.push(("x-upload-token", "let-me-in"));
    assert_eq!(
        h.create_with(ROOM, &right).await.status,
        StatusCode::CREATED
    );
    // The room's owner then needs no upload token.
    assert_eq!(h.create_with(ROOM, &base).await.status, StatusCode::OK);
    assert_eq!(
        h.snapshot(ROOM, 0, b"S").await.status,
        StatusCode::NO_CONTENT
    );
}

#[tokio::test]
async fn join_needs_the_room_token() {
    let h = host().await;
    h.create(ROOM).await;

    let mut ws = h.open(ROOM).await;
    send_join(&mut ws, "guess", 0).await;
    assert_eq!(
        next(&mut ws).await,
        Got::Closed(4401, "wrong_room_token".into())
    );

    // The first message must be the join.
    let mut ws = h.open(ROOM).await;
    send_bin(&mut ws, tagged(0x01, b"early")).await;
    assert_eq!(
        next(&mut ws).await,
        Got::Closed(4401, "join_required".into())
    );
    let mut ws = h.open(ROOM).await;
    ws.send(Message::text("ping")).await.unwrap();
    assert_eq!(
        next(&mut ws).await,
        Got::Closed(4401, "join_required".into())
    );
    let mut ws = h.open(ROOM).await;
    ws.send(Message::text(
        json!({ "type": "hello", "token": ROOM_TOKEN }).to_string(),
    ))
    .await
    .unwrap();
    assert_eq!(
        next(&mut ws).await,
        Got::Closed(4401, "join_required".into())
    );
    assert!(h.app.rooms.replay(ROOM, 0).await.unwrap().1.is_empty());

    h.await_connections(ROOM, 0).await;
    let (mut ws, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(
        welcome,
        json!({
            "type": "welcome", "seq": 0, "snapshot_seq": null, "ended": false,
            "ended_at": null, "expires": T0 + DAY, "present": 1,
        })
    );
    nothing_more(&mut ws).await;

    // An unknown room is upgraded and closed at once (a browser cannot read an HTTP status);
    // a malformed id is refused before the upgrade.
    let mut unknown = h.open("fedcba9876543210").await;
    assert_eq!(next(&mut unknown).await, Got::Closed(4410, "gone".into()));
    assert_eq!(
        h.try_open("nope").await.err(),
        Some(StatusCode::BAD_REQUEST)
    );
}

#[tokio::test]
async fn frames_are_acked_and_relayed_to_the_others() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let (mut b, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(welcome["present"], 2);
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "count", "present": 2 })
    );

    assert_eq!(append(&mut a, b"from a").await, 1);
    assert_eq!(
        next_but_counts(&mut b).await,
        Got::Bin(frame(0x01, 1, b"from a"))
    );
    assert_eq!(append(&mut b, b"from b").await, 2);
    // A never sees its own frame: the next thing it gets is B's.
    assert_eq!(
        next_but_counts(&mut a).await,
        Got::Bin(frame(0x01, 2, b"from b"))
    );

    // Presence is relayed to the others and never logged.
    send_bin(&mut a, tagged(0x02, b"a is typing")).await;
    assert_eq!(
        next_but_counts(&mut b).await,
        Got::Bin(tagged(0x02, b"a is typing"))
    );
    nothing_more(&mut a).await;
    let (_, frames) = h.app.rooms.replay(ROOM, 0).await.unwrap();
    assert_eq!(
        frames,
        vec![(1, b"from a".to_vec()), (2, b"from b".to_vec())]
    );

    // Empty and unknown frames are answered, the connection stays.
    send_bin(&mut a, vec![0x01]).await;
    assert_eq!(
        next_but_counts(&mut a).await.json(),
        json!({ "type": "error", "code": "bad_frame" })
    );
    send_bin(&mut a, tagged(0x07, b"?")).await;
    assert_eq!(
        next_but_counts(&mut a).await.json(),
        json!({ "type": "error", "code": "bad_frame" })
    );
    nothing_more(&mut a).await;
    nothing_more(&mut b).await;

    // Counts follow joins and leaves.
    let (c, _) = h.join(ROOM, ROOM_TOKEN, 2).await;
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "count", "present": 3 })
    );
    drop(c);
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "count", "present": 2 })
    );
    h.await_connections(ROOM, 2).await;
}

#[tokio::test]
async fn a_late_client_replays_the_log_since_what_it_has() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    for (i, f) in [b"one".as_slice(), b"two", b"three"].iter().enumerate() {
        assert_eq!(append(&mut a, f).await, i as u64 + 1);
    }
    let (mut late, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(
        (welcome["seq"].as_u64(), welcome["snapshot_seq"].is_null()),
        (Some(3), true)
    );
    for (seq, f) in [(1, b"one".as_slice()), (2, b"two"), (3, b"three")] {
        assert_eq!(next(&mut late).await, Got::Bin(frame(0x01, seq, f)));
    }
    nothing_more(&mut late).await;

    let (mut rejoin, welcome) = h.join(ROOM, ROOM_TOKEN, 2).await;
    assert_eq!(welcome["seq"], 3);
    assert_eq!(next(&mut rejoin).await, Got::Bin(frame(0x01, 3, b"three")));
    nothing_more(&mut rejoin).await;
    // Then live frames follow on.
    assert_eq!(append(&mut a, b"four").await, 4);
    assert_eq!(
        next_but_counts(&mut rejoin).await,
        Got::Bin(frame(0x01, 4, b"four"))
    );
}

#[tokio::test]
async fn a_snapshot_replaces_the_frames_it_covers() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    for f in [b"a", b"b", b"c"] {
        append(&mut a, f).await;
    }

    let r = h
        .owner("PUT", &format!("/live/{ROOM}/snapshot"), &[], b"S")
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "bad_since")
    );
    let r = h
        .send(
            Request::put(format!("/live/{ROOM}/snapshot"))
                .header("authorization", "Bearer someone-else")
                .header("x-room-since", "2")
                .body(Body::from("S"))
                .unwrap(),
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::FORBIDDEN, "wrong_token")
    );
    let r = h.snapshot(ROOM, 9, b"S").await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::CONFLICT, "since_ahead")
    );
    let r = h.snapshot(ROOM, 2, b"SNAP@2").await;
    assert_eq!(r.status, StatusCode::NO_CONTENT);
    let r = h.snapshot(ROOM, 1, b"older").await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::CONFLICT, "snapshot_stale")
    );

    let (mut late, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(
        (welcome["seq"].as_u64(), welcome["snapshot_seq"].as_u64()),
        (Some(3), Some(2))
    );
    assert_eq!(next(&mut late).await, Got::Bin(frame(0x03, 2, b"SNAP@2")));
    assert_eq!(next(&mut late).await, Got::Bin(frame(0x01, 3, b"c")));
    nothing_more(&mut late).await;
    // A client behind the snapshot gets it too; one at or past it only the frames after.
    let (mut behind, _) = h.join(ROOM, ROOM_TOKEN, 1).await;
    assert_eq!(next(&mut behind).await, Got::Bin(frame(0x03, 2, b"SNAP@2")));
    assert_eq!(next(&mut behind).await, Got::Bin(frame(0x01, 3, b"c")));
    let (mut current, _) = h.join(ROOM, ROOM_TOKEN, 2).await;
    assert_eq!(next(&mut current).await, Got::Bin(frame(0x01, 3, b"c")));
    nothing_more(&mut current).await;
}

#[tokio::test]
async fn snapshots_over_the_cap_are_refused() {
    let h = host_with(|c| c.room_snapshot_max_bytes = 8).await;
    h.create(ROOM).await;
    let r = h.snapshot(ROOM, 0, &[1; 9]).await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::PAYLOAD_TOO_LARGE, "snapshot_too_large")
    );
    assert_eq!(
        h.snapshot(ROOM, 0, &[1; 8]).await.status,
        StatusCode::NO_CONTENT
    );
}

#[tokio::test]
async fn ending_the_room_makes_it_read_only() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let (mut b, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    append(&mut a, b"before the end").await;
    assert_eq!(
        next_but_counts(&mut b).await,
        Got::Bin(frame(0x01, 1, b"before the end"))
    );

    let r = h
        .send(
            Request::post(format!("/live/{ROOM}/end"))
                .header("authorization", "Bearer someone-else")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    h.clock.advance(60);
    // Without X-Room-Expires the expiry stays.
    let r = h
        .owner("POST", &format!("/live/{ROOM}/end"), &[], b"")
        .await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(
        r.json(),
        json!({ "created": T0, "expires": T0 + DAY, "ended_at": T0 + 60, "last_seq": 1, "snapshot_seq": null })
    );
    let ended = json!({ "type": "ended", "ended_at": T0 + 60, "expires": T0 + DAY });
    assert_eq!(next_but_counts(&mut a).await.json(), ended);
    assert_eq!(next_but_counts(&mut b).await.json(), ended);

    // Frames are refused with 4403; presence still flows.
    send_bin(&mut b, tagged(0x02, b"still here")).await;
    assert_eq!(
        next_but_counts(&mut a).await,
        Got::Bin(tagged(0x02, b"still here"))
    );
    send_bin(&mut a, tagged(0x01, b"after the end")).await;
    assert_eq!(
        next_but_counts(&mut a).await,
        Got::Closed(4403, "ended".into())
    );

    // Moving the expiry (ISO or seconds) keeps the first end.
    let r = h
        .owner(
            "POST",
            &format!("/live/{ROOM}/end"),
            &[("x-room-expires", "2027-01-17T08:00:00Z")],
            b"",
        )
        .await;
    assert_eq!(
        (r.json()["ended_at"].as_u64(), r.json()["expires"].as_u64()),
        (Some(T0 + 60), Some(T0 + 2 * DAY))
    );
    let r = h
        .owner(
            "POST",
            &format!("/live/{ROOM}/end"),
            &[("x-room-expires", "yesterday")],
            b"",
        )
        .await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::BAD_REQUEST, "bad_expires")
    );

    // Still joinable and readable; the owner may still post the final snapshot.
    assert_eq!(
        h.snapshot(ROOM, 1, b"final").await.status,
        StatusCode::NO_CONTENT
    );
    let (mut late, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(
        (welcome["ended"].as_bool(), welcome["ended_at"].as_u64()),
        (Some(true), Some(T0 + 60))
    );
    assert_eq!(next(&mut late).await, Got::Bin(frame(0x03, 1, b"final")));
    nothing_more(&mut late).await;
}

#[tokio::test]
async fn deleting_the_room_closes_its_sockets() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    append(&mut a, b"x").await;
    // Upgraded but not yet joined when the room goes.
    let mut pending = h.open(ROOM).await;

    let r = h
        .send(
            Request::delete(format!("/live/{ROOM}"))
                .header("authorization", "Bearer someone-else")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    assert_eq!(
        h.owner("DELETE", &format!("/live/{ROOM}"), &[], b"")
            .await
            .status,
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        next_but_counts(&mut a).await,
        Got::Closed(4410, "deleted".into())
    );
    send_join(&mut pending, ROOM_TOKEN, 0).await;
    assert_eq!(next(&mut pending).await, Got::Closed(4410, "gone".into()));

    let mut late = h.open(ROOM).await;
    assert_eq!(next(&mut late).await, Got::Closed(4410, "gone".into()));
    let r = h.owner("DELETE", &format!("/live/{ROOM}"), &[], b"").await;
    assert_eq!((r.status, r.code().as_str()), (StatusCode::GONE, "gone"));
    // The tombstone keeps the id from being created again.
    let bearer = format!("Bearer {TOKEN}");
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", T0_PLUS_DAY_ISO),
            ],
        )
        .await;
    assert_eq!((r.status, r.code().as_str()), (StatusCode::GONE, "gone"));
}

#[tokio::test]
async fn an_expired_room_refuses_the_socket() {
    let h = host().await;
    h.create(ROOM).await;
    let bearer = format!("Bearer {TOKEN}");
    let r = h
        .create_with(
            ROOM,
            &[
                ("authorization", &bearer),
                ("x-room-token", ROOM_TOKEN),
                ("x-room-expires", "1800000001"),
            ],
        )
        .await;
    assert_eq!(r.json()["expires"], T0 + 1);
    // Expired, not yet swept: closed as gone.
    h.clock.advance(1);
    let mut late = h.open(ROOM).await;
    assert_eq!(next(&mut late).await, Got::Closed(4410, "gone".into()));
}

#[tokio::test]
async fn frames_over_the_caps_close_with_4413() {
    let h = host_with(|c| {
        c.room_frame_max_bytes = 64;
        c.room_log_max_bytes = 2 * (12 + 64);
    })
    .await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    send_bin(&mut a, tagged(0x01, &[7; 65])).await;
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "error", "code": "frame_too_large" })
    );
    assert_eq!(
        next(&mut a).await,
        Got::Closed(4413, "frame_too_large".into())
    );

    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    send_bin(&mut a, tagged(0x02, &[7; 65])).await;
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "error", "code": "frame_too_large" })
    );
    assert_eq!(
        next(&mut a).await,
        Got::Closed(4413, "frame_too_large".into())
    );

    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(append(&mut a, &[7; 64]).await, 1);
    assert_eq!(append(&mut a, &[7; 64]).await, 2);
    send_bin(&mut a, tagged(0x01, &[7; 1])).await;
    assert_eq!(
        next(&mut a).await.json(),
        json!({ "type": "error", "code": "log_full" })
    );
    assert_eq!(next(&mut a).await, Got::Closed(4413, "log_full".into()));
    // A snapshot makes room again.
    assert_eq!(
        h.snapshot(ROOM, 2, b"S").await.status,
        StatusCode::NO_CONTENT
    );
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 2).await;
    assert_eq!(append(&mut a, &[7; 64]).await, 3);
}

#[tokio::test]
async fn connections_are_capped() {
    let h = host_with(|c| c.room_max_connections = 2).await;
    h.create(ROOM).await;
    let a = h.open(ROOM).await;
    let (_b, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    // Over the cap: upgraded, then closed at once with 4429.
    let mut over = h.open(ROOM).await;
    assert_eq!(next(&mut over).await, Got::Closed(4429, "room_full".into()));
    drop(over);
    // A socket that goes away (joined or not) is counted out.
    drop(a);
    h.await_connections(ROOM, 1).await;
    let (_c, welcome) = h.join(ROOM, ROOM_TOKEN, 0).await;
    assert_eq!(welcome["present"], 2);
}

/// Floods the room from the host's side (no connection, so no per-connection cap) while `slow`
/// reads nothing: its socket fills, then the room's channel overflows. `kind` 0x01 appends
/// frames, 0x02 relays presence.
async fn flood_while_stalled(h: &Host, kind: u8, n: usize) {
    let blob = vec![1u8; 64 * 1024];
    for _ in 0..n {
        if kind == 0x01 {
            h.app.rooms.append(ROOM, 0, &blob).await.unwrap();
        } else {
            h.app.rooms.presence(ROOM, 0, &blob).await.unwrap();
        }
    }
}

#[tokio::test]
async fn a_client_too_slow_for_the_relay_is_told_to_rejoin() {
    let h = host_with(|c| c.room_log_max_bytes = 1 << 30).await;
    h.create(ROOM).await;
    let (mut slow, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    // Enough frames to fill the socket and then the channel while `slow` reads nothing.
    flood_while_stalled(&h, 0x01, 600).await;
    let got = loop {
        match next(&mut slow).await {
            Got::Bin(_) | Got::Text(_) => continue,
            closed => break closed,
        }
    };
    assert_eq!(got, Got::Closed(4409, "lagged".into()));
}

#[tokio::test]
async fn a_client_behind_on_presence_is_not_closed() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut slow, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let (mut fast, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let sent = 600;
    flood_while_stalled(&h, 0x02, sent).await;
    // A frame after the flood still reaches `slow`, which was never closed.
    // (`fast` read nothing either; it skips its share of the presence to find its ack.)
    send_bin(&mut fast, tagged(0x01, b"later")).await;
    loop {
        match next(&mut fast).await {
            Got::Text(t) if t.contains("\"ack\"") => break,
            Got::Closed(code, why) => panic!("closed {code} {why}"),
            _ => continue,
        }
    }
    let mut presence = 0;
    loop {
        match next(&mut slow).await {
            Got::Bin(b) if b[0] == 0x02 => presence += 1,
            Got::Bin(b) => {
                assert_eq!(b, frame(0x01, 1, b"later"));
                break;
            }
            Got::Text(_) => continue,
            closed => panic!("a listener behind on presence was closed: {closed:?}"),
        }
    }
    // It missed some of the presence (that is what lagging is), and it is still served.
    assert!(
        presence < sent,
        "{presence} of {sent} arrived: it never lagged"
    );
    slow.send(Message::text("ping")).await.unwrap();
    loop {
        match next(&mut slow).await {
            Got::Text(t) if t == "pong" => break,
            Got::Closed(code, why) => panic!("closed {code} {why}"),
            _ => continue,
        }
    }
}

#[tokio::test]
async fn presence_from_one_connection_is_capped() {
    let h = host().await;
    h.create(ROOM).await;
    let (mut a, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let (mut b, _) = h.join(ROOM, ROOM_TOKEN, 0).await;
    let started = std::time::Instant::now();
    for i in 0..300u32 {
        send_bin(&mut a, tagged(0x02, &i.to_be_bytes())).await;
    }
    // B reads until the relay has been quiet for a while.
    let mut received = 0usize;
    loop {
        match tokio::time::timeout(Duration::from_millis(500), b.next()).await {
            Err(_) => break,
            Ok(Some(Ok(Message::Binary(m)))) => {
                assert_eq!(m[0], 0x02);
                received += 1;
            }
            Ok(Some(Ok(Message::Close(f)))) => panic!("closed: {f:?}"),
            Ok(None | Some(Err(_))) => panic!("socket lost"),
            Ok(Some(Ok(_))) => continue,
        }
    }
    let secs = started.elapsed().as_secs_f64();
    assert!(received >= 20, "the burst was cut: {received}");
    let most = 20.0 + 10.0 * secs + 1.0;
    assert!(
        (received as f64) <= most,
        "{received} presence messages in {secs:.2} s, at most {most:.1} allowed"
    );
    assert!(received < 300);
    // Dropped silently: no error for A, and its connection (and B's) still works; frames are
    // not touched by the cap.
    nothing_more(&mut a).await;
    assert_eq!(append(&mut a, b"still fine").await, 1);
    assert_eq!(
        next_but_counts(&mut b).await,
        Got::Bin(frame(0x01, 1, b"still fine"))
    );
}

#[tokio::test]
async fn the_vector_room_token_opens_its_room() {
    let vectors: Value =
        serde_json::from_str(include_str!("../../backend/tests/vectors/guest_v1.json")).unwrap();
    let room = vectors["room"].as_str().unwrap();
    let token = vectors["room_token"].as_str().unwrap();
    let h = host().await;
    let bearer = format!("Bearer {TOKEN}");
    let r = h
        .create_with(
            room,
            &[
                ("authorization", &bearer),
                ("x-room-token", token),
                ("x-room-expires", T0_PLUS_DAY_ISO),
            ],
        )
        .await;
    assert_eq!(r.status, StatusCode::CREATED);
    let (mut ws, welcome) = h.join(room, token, 0).await;
    assert_eq!(welcome["seq"], 0);
    nothing_more(&mut ws).await;
    let mut ws = h.open(room).await;
    send_join(&mut ws, &token[..token.len() - 1], 0).await;
    assert_eq!(
        next(&mut ws).await,
        Got::Closed(4401, "wrong_room_token".into())
    );
}

#[tokio::test]
async fn the_guest_page_is_served_with_its_own_policy() {
    let h = host().await;
    // Any well-formed room id, existing or not, gets the page.
    for uri in [format!("/n/{ROOM}"), "/n/fedcba9876543210".into()] {
        let r = h.get(&uri).await;
        assert_eq!(r.status, StatusCode::OK, "{uri}");
        assert_eq!(r.body, b"<title>guest</title>");
        assert_eq!(r.header("content-type"), "text/html; charset=utf-8");
        assert_eq!(r.header("content-security-policy"), GUEST_CSP);
        assert_eq!(r.header("cache-control"), "no-store");
        assert_eq!(r.header("x-content-type-options"), "nosniff");
        assert_eq!(r.header("referrer-policy"), "no-referrer");
    }
    assert_eq!(h.get("/n/not-a-room").await.status, StatusCode::NOT_FOUND);

    let r = h.get("/n/assets/notes_bg.wasm").await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(r.header("content-type"), "application/wasm");
    assert_eq!(r.header("content-security-policy"), GUEST_CSP);
    assert_eq!(r.header("cache-control"), "no-cache");
    let r = h.get("/n/assets/app.js").await;
    assert_eq!(r.header("content-type"), "text/javascript; charset=utf-8");
    for uri in [
        "/n/assets/missing.js",
        "/n/assets/../../secret.txt",
        "/n/.hidden/x",
        "/n/assets/%2e%2e/%2e%2e/secret.txt",
    ] {
        assert_eq!(h.get(uri).await.status, StatusCode::NOT_FOUND, "{uri}");
    }

    // The viewer keeps its stricter policy.
    let r = h.get(&format!("/p/{ROOM}")).await;
    assert_eq!(r.body, b"<title>viewer</title>");
    assert_eq!(r.header("content-security-policy"), VIEWER_CSP);

    std::fs::remove_dir_all(h.tmp.path().join("guest")).unwrap();
    let r = h.get(&format!("/n/{ROOM}")).await;
    assert_eq!(
        (r.status, r.code().as_str()),
        (StatusCode::SERVICE_UNAVAILABLE, "guest_missing")
    );
}
