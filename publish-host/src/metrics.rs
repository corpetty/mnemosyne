//! Usage counters, counts only: nothing here is per room, per publication or per client.
//!
//! `Metrics` is plain atomics bumped by the handlers; `render` writes them in the Prometheus
//! text exposition format (version 0.0.4), by hand. They are served by [`serve`] on its own
//! listener (`--metrics-listen`, meant for 127.0.0.1), never by the public router: `router()`
//! has no `/metrics` route, and the metrics router has nothing else.
//!
//! Counters start at zero when the process starts (Prometheus counters reset on restart);
//! `mnemosyne_publish_stored_bytes` is a gauge read from the store's quota counter, so it
//! survives restarts.

use std::fmt::Write;
use std::sync::atomic::{AtomicU64, Ordering};

use axum::http::{header, HeaderValue};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use tokio::net::TcpListener;

use crate::app::Shared;

#[derive(Default)]
pub struct Metrics {
    /// Guest rooms created (not counting an owner moving a room's expiry).
    pub rooms_created: AtomicU64,
    /// WebSocket joins that were accepted (right token, room open to the guest).
    pub guest_connections: AtomicU64,
    /// Frames accepted into a room's log and relayed, once each (not once per listener).
    pub frames_relayed: AtomicU64,
    /// Publications whose first manifest was stored.
    pub publications_created: AtomicU64,
    /// Manifest reads (`GET /pub/<id>` answered 200 or 304).
    pub publications_opened: AtomicU64,
    /// Writes refused by the daily write budget (HTTP 429 `write_budget` and room frames).
    pub budget_refusals: AtomicU64,
}

impl Metrics {
    pub fn inc(counter: &AtomicU64) {
        counter.fetch_add(1, Ordering::Relaxed);
    }

    fn get(counter: &AtomicU64) -> u64 {
        counter.load(Ordering::Relaxed)
    }

    /// The exposition text. `stored_bytes` is the store's quota counter.
    pub fn render(&self, stored_bytes: u64) -> String {
        let mut out = String::new();
        let mut family = |name: &str, kind: &str, help: &str, value: u64| {
            let _ = writeln!(out, "# HELP {name} {help}");
            let _ = writeln!(out, "# TYPE {name} {kind}");
            let _ = writeln!(out, "{name} {value}");
        };
        family(
            "mnemosyne_publish_rooms_created_total",
            "counter",
            "Guest rooms created.",
            Self::get(&self.rooms_created),
        );
        family(
            "mnemosyne_publish_guest_connections_total",
            "counter",
            "Guest WebSocket connections that joined a room.",
            Self::get(&self.guest_connections),
        );
        family(
            "mnemosyne_publish_frames_relayed_total",
            "counter",
            "Room frames accepted and relayed.",
            Self::get(&self.frames_relayed),
        );
        family(
            "mnemosyne_publish_publications_created_total",
            "counter",
            "Publications whose first manifest was stored.",
            Self::get(&self.publications_created),
        );
        family(
            "mnemosyne_publish_publications_opened_total",
            "counter",
            "Publication manifest reads.",
            Self::get(&self.publications_opened),
        );
        family(
            "mnemosyne_publish_budget_refusals_total",
            "counter",
            "Writes refused by the daily write budget.",
            Self::get(&self.budget_refusals),
        );
        family(
            "mnemosyne_publish_stored_bytes",
            "gauge",
            "Bytes of publications stored now (the quota counter).",
            stored_bytes,
        );
        out
    }
}

async fn scrape(axum::extract::State(app): axum::extract::State<Shared>) -> Response {
    let body = app.metrics.render(app.store.used_bytes().await);
    let mut res = body.into_response();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"),
    );
    res
}

/// The only router on the metrics listener: `GET /metrics`, nothing else.
pub fn metrics_router(app: Shared) -> Router {
    Router::new().route("/metrics", get(scrape)).with_state(app)
}

/// Serves [`metrics_router`] on `listener` until the process ends.
pub async fn serve(listener: TcpListener, app: Shared) -> std::io::Result<()> {
    axum::serve(listener, metrics_router(app)).await
}
