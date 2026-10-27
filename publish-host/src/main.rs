use std::net::SocketAddr;

use clap::Parser;
use mnemosyne_publish_host::app::{router, spawn_sweeper, App};
use mnemosyne_publish_host::clock::Clock;
use mnemosyne_publish_host::config::Config;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> std::io::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let cfg = Config::parse();
    let listen = cfg.listen;
    let app = App::new(cfg, Clock::system()).await?;
    app.sweep().await;
    spawn_sweeper(app.clone());

    if let Some(addr) = app.cfg.metrics_addr() {
        let metrics = tokio::net::TcpListener::bind(addr).await?;
        tracing::info!("metrics on http://{addr}/metrics");
        tokio::spawn(mnemosyne_publish_host::metrics::serve(metrics, app.clone()));
    }
    let listener = tokio::net::TcpListener::bind(listen).await?;
    tracing::info!("publish host listening on {listen}");
    axum::serve(
        listener,
        router(app).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async {
        let _ = tokio::signal::ctrl_c().await;
    })
    .await
}
