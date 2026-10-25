//! The guest page's iroh client (phase 10): one QUIC connection over one relay to the owner's
//! per-room guest endpoint, and one bi stream on it carrying the phase 9 room wire as frames
//! (see [`frame`]). The page wraps it as a WebSocket-like socket (guest/src), so the room
//! client does not care which transport it runs on.
//!
//! In a browser iroh is relay-only: the endpoint dials `wss://<relay>/relay` and QUIC runs over
//! it. The endpoint is built from `presets::Minimal` (a crypto provider and nothing else: no
//! DNS or pkarr address lookup) with exactly the relay the link names.
//!
//! Everything compiles natively too (`cargo check` / `cargo test` here), but only runs in a
//! browser: the futures are driven by `wasm_bindgen_futures` and errors are JS values.

pub mod frame;

use std::cell::RefCell;
use std::fmt::Display;
use std::rc::Rc;
use std::str::FromStr;

use futures::channel::{mpsc, oneshot};
use futures::StreamExt;
use iroh::endpoint::{
    presets, Connection, ConnectionError, ReadError, ReadExactError, RecvStream, SendStream, VarInt,
};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, RelayUrl};
use js_sys::{Object, Promise, Reflect, Uint8Array};
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::{future_to_promise, spawn_local};

/// The close code `closed()` reports when the connection ended without an application close
/// (timeout, reset, a transport error), as a WebSocket reports 1006.
pub const CLOSE_ABNORMAL: u32 = 1006;
/// The close code `close()` sends when the page frees the connection without closing it.
pub const CLOSE_GOING_AWAY: u32 = 1001;
/// The close code `closed()` reports when this side ended the connection without `close()`.
pub const CLOSE_NORMAL: u32 = 1000;

type Write = (Vec<u8>, oneshot::Sender<Result<(), String>>);

enum RecvState {
    Idle(RecvStream),
    Busy,
    Ended,
    Failed(String),
}

/// A connection to a room's guest endpoint. Created by [`connect`].
#[wasm_bindgen]
pub struct GuestConn {
    endpoint: Endpoint,
    conn: Connection,
    writes: mpsc::UnboundedSender<Write>,
    recv: Rc<RefCell<RecvState>>,
    local_close: Rc<RefCell<Option<(u32, String)>>>,
}

fn js_err(msg: impl Display) -> JsValue {
    JsError::new(&msg.to_string()).into()
}

/// Connects to `endpoint_id_hex` (the room endpoint's id, hex or base32) through the one relay
/// `relay_url` (`https://host[:port]`, or `http://` for a loopback dev relay) with ALPN `alpn`
/// (`mnemosyne/guest/1`), and opens the bi stream. The stream reaches the owner's side only with
/// the first frame sent, so send the join right away.
#[wasm_bindgen]
pub async fn connect(
    endpoint_id_hex: String,
    relay_url: String,
    alpn: String,
) -> Result<GuestConn, JsError> {
    let id = EndpointId::from_str(endpoint_id_hex.trim())
        .map_err(|e| JsError::new(&format!("bad endpoint id: {e}")))?;
    let relay = RelayUrl::from_str(relay_url.trim())
        .map_err(|e| JsError::new(&format!("bad relay url: {e}")))?;
    let endpoint = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::custom([relay.clone()]))
        .bind()
        .await
        .map_err(|e| JsError::new(&format!("endpoint: {e}")))?;
    let addr = EndpointAddr::new(id).with_relay_url(relay);
    let conn = match endpoint.connect(addr, alpn.as_bytes()).await {
        Ok(conn) => conn,
        Err(e) => {
            endpoint.close().await;
            return Err(JsError::new(&format!("connect: {e}")));
        }
    };
    let (send, recv) = match conn.open_bi().await {
        Ok(pair) => pair,
        Err(e) => {
            conn.close(VarInt::from_u32(CLOSE_GOING_AWAY), b"");
            endpoint.close().await;
            return Err(JsError::new(&format!("open stream: {e}")));
        }
    };

    let (writes, queue) = mpsc::unbounded();
    spawn_local(write_loop(send, queue));
    // Whoever closes the connection, the endpoint (and its relay socket) goes with it.
    let (watch_conn, watch_endpoint) = (conn.clone(), endpoint.clone());
    spawn_local(async move {
        watch_conn.closed().await;
        watch_endpoint.close().await;
    });

    Ok(GuestConn {
        endpoint,
        conn,
        writes,
        recv: Rc::new(RefCell::new(RecvState::Idle(recv))),
        local_close: Rc::new(RefCell::new(None)),
    })
}

/// Writes frames in the order `send` queued them; the first failure ends the stream and every
/// later send fails.
async fn write_loop(mut send: SendStream, mut queue: mpsc::UnboundedReceiver<Write>) {
    while let Some((frame, done)) = queue.next().await {
        let result = send.write_all(&frame).await.map_err(|e| e.to_string());
        let failed = result.is_err();
        let _ = done.send(result);
        if failed {
            return;
        }
    }
    // The page dropped the connection: end the stream cleanly.
    let _ = send.finish();
}

/// Reads one frame. `Ok(None)` when the peer finished the stream at a frame boundary or the
/// connection is gone (`closed()` says why).
async fn read_frame(stream: &mut RecvStream) -> Result<Option<(u8, Vec<u8>)>, String> {
    let mut header = [0u8; frame::HEADER_LEN];
    match stream.read_exact(&mut header).await {
        Ok(()) => {}
        Err(ReadExactError::FinishedEarly(0)) => return Ok(None),
        Err(ReadExactError::ReadError(ReadError::ConnectionLost(_))) => return Ok(None),
        Err(ReadExactError::FinishedEarly(_)) => return Err("truncated frame header".into()),
        Err(e) => return Err(e.to_string()),
    }
    let len = frame::body_len(header).map_err(|e| e.to_string())?;
    let mut body = vec![0u8; len];
    match stream.read_exact(&mut body).await {
        Ok(()) => {}
        Err(ReadExactError::ReadError(ReadError::ConnectionLost(_))) => return Ok(None),
        Err(ReadExactError::FinishedEarly(_)) => return Err("truncated frame".into()),
        Err(e) => return Err(e.to_string()),
    }
    let (tag, _) = frame::split_body(&body).map_err(|e| e.to_string())?;
    body.remove(0);
    Ok(Some((tag, body)))
}

/// The `{code, reason}` that `closed()` reports for how the connection ended. `local` is what
/// `close()` sent, if it was called.
pub fn close_info(err: &ConnectionError, local: Option<(u32, String)>) -> (u32, String) {
    match err {
        ConnectionError::ApplicationClosed(close) => (
            u32::try_from(close.error_code.into_inner()).unwrap_or(u32::MAX),
            String::from_utf8_lossy(&close.reason).into_owned(),
        ),
        ConnectionError::LocallyClosed => local.unwrap_or((CLOSE_NORMAL, String::new())),
        other => (CLOSE_ABNORMAL, other.to_string()),
    }
}

fn set(obj: &Object, key: &str, value: &JsValue) {
    // Setting a property on a plain new Object cannot fail.
    let _ = Reflect::set(obj, &JsValue::from_str(key), value);
}

#[wasm_bindgen]
impl GuestConn {
    /// Queues one frame (`tag` 0 = text, 1 = binary) and resolves once it is written to the
    /// stream (not when the peer has read it). Frames go out in call order. Rejects for a bad
    /// tag, a payload over 8 MiB + 64 KiB (`frame::MAX_PAYLOAD`), or a closed connection.
    pub fn send(&self, tag: u8, bytes: &[u8]) -> Promise {
        let frame = match frame::encode(tag, bytes) {
            Ok(frame) => frame,
            Err(e) => return Promise::reject(&js_err(e)),
        };
        let (done, written) = oneshot::channel();
        if self.writes.unbounded_send((frame, done)).is_err() {
            return Promise::reject(&js_err("connection closed"));
        }
        future_to_promise(async move {
            match written.await {
                Ok(Ok(())) => Ok(JsValue::UNDEFINED),
                Ok(Err(e)) => Err(js_err(e)),
                Err(_) => Err(js_err("connection closed")),
            }
        })
    }

    /// The next frame as `{tag: number, data: Uint8Array}`, or `undefined` once the stream or
    /// the connection has ended (then `closed()` gives the code). One call at a time: a second
    /// while one is pending rejects. Rejects on a malformed frame, and every later call too.
    pub fn recv(&self) -> Promise {
        let state = std::mem::replace(&mut *self.recv.borrow_mut(), RecvState::Busy);
        let mut stream = match state {
            RecvState::Idle(stream) => stream,
            RecvState::Busy => return Promise::reject(&js_err("recv() is already pending")),
            RecvState::Ended => {
                *self.recv.borrow_mut() = RecvState::Ended;
                return Promise::resolve(&JsValue::UNDEFINED);
            }
            RecvState::Failed(e) => {
                let rejected = Promise::reject(&js_err(&e));
                *self.recv.borrow_mut() = RecvState::Failed(e);
                return rejected;
            }
        };
        let slot = self.recv.clone();
        future_to_promise(async move {
            let result = read_frame(&mut stream).await;
            match result {
                Ok(Some((tag, data))) => {
                    *slot.borrow_mut() = RecvState::Idle(stream);
                    let obj = Object::new();
                    set(&obj, "tag", &JsValue::from(tag));
                    set(&obj, "data", &Uint8Array::from(data.as_slice()).into());
                    Ok(obj.into())
                }
                Ok(None) => {
                    *slot.borrow_mut() = RecvState::Ended;
                    Ok(JsValue::UNDEFINED)
                }
                Err(e) => {
                    let err = js_err(&e);
                    *slot.borrow_mut() = RecvState::Failed(e);
                    Err(err)
                }
            }
        })
    }

    /// Closes the connection with an application close (`code`, `reason`), what the peer sees
    /// as the WebSocket close code (e.g. 1000). Resolves once the endpoint has shut down (up to
    /// a few seconds on a bad network); safe to call more than once.
    pub fn close(&self, code: u32, reason: String) -> Promise {
        {
            let mut local = self.local_close.borrow_mut();
            if local.is_none() {
                *local = Some((code, reason.clone()));
            }
        }
        self.conn.close(VarInt::from_u32(code), reason.as_bytes());
        let endpoint = self.endpoint.clone();
        future_to_promise(async move {
            endpoint.close().await;
            Ok(JsValue::UNDEFINED)
        })
    }

    /// Resolves to `{code: number, reason: string}` when the connection closes: the peer's
    /// application close (e.g. 4410 "gone"), what `close()` sent, or 1006 with the error text
    /// when it ended otherwise (timeout, reset, transport error). Can be awaited many times.
    pub fn closed(&self) -> Promise {
        let conn = self.conn.clone();
        let local = self.local_close.clone();
        future_to_promise(async move {
            let err = conn.closed().await;
            let (code, reason) = close_info(&err, local.borrow().clone());
            let obj = Object::new();
            set(&obj, "code", &JsValue::from(code));
            set(&obj, "reason", &JsValue::from_str(&reason));
            Ok(obj.into())
        })
    }
}

impl Drop for GuestConn {
    /// `free()` without `close()`: tell the peer rather than let it time out.
    fn drop(&mut self) {
        if self.local_close.borrow().is_none() {
            self.conn
                .close(VarInt::from_u32(CLOSE_GOING_AWAY), b"going away");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use iroh::endpoint::ApplicationClose;

    #[test]
    fn close_info_reads_the_peers_app_close() {
        let err = ConnectionError::ApplicationClosed(ApplicationClose {
            error_code: VarInt::from_u32(4410),
            reason: "gone".into(),
        });
        assert_eq!(close_info(&err, None), (4410, "gone".to_string()));
    }

    #[test]
    fn close_info_reports_our_own_close() {
        let err = ConnectionError::LocallyClosed;
        assert_eq!(
            close_info(&err, Some((1000, "bye".into()))),
            (1000, "bye".to_string())
        );
        assert_eq!(close_info(&err, None), (CLOSE_NORMAL, String::new()));
    }

    #[test]
    fn close_info_maps_other_endings_to_abnormal() {
        let (code, reason) = close_info(&ConnectionError::TimedOut, None);
        assert_eq!(code, CLOSE_ABNORMAL);
        assert_eq!(reason, "timed out");
        assert_eq!(close_info(&ConnectionError::Reset, None).0, CLOSE_ABNORMAL);
    }
}
