//! `mnemosyne-link relay-check`: does a relay carry traffic between two fresh endpoints?
//!
//! Two ephemeral endpoints use only the given relay (no address lookup, no LAN discovery). One
//! accepts on an echo ALPN; the other dials it by endpoint id and relay URL, with no direct
//! addresses, sends a payload and reads it back. Both must register with the relay as their
//! home and the dial must be introduced through it, so a pass proves the relay works. Once
//! connected, iroh may switch to a direct path (it learns addresses through the relay); that
//! is fine, the relay was still needed to get there.

use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use iroh::{
    endpoint::{presets, Builder},
    Endpoint, EndpointAddr, RelayMode, RelayUrl, SecretKey,
};

/// The environment variable `relay-check` reads: a path to a PEM file whose certificates are
/// trusted as extra roots (a relay behind a private CA, e.g. the deploy smoke test's).
pub const CA_ENV: &str = "MNEMOSYNE_RELAY_CA";

/// The DER certificates in a PEM text (every `CERTIFICATE` block). Errors when there is none.
pub fn pem_certificates(pem: &str) -> Result<Vec<Vec<u8>>> {
    use base64::Engine;
    let mut out = Vec::new();
    let mut body: Option<String> = None;
    for line in pem.lines().map(str::trim) {
        if line == "-----BEGIN CERTIFICATE-----" {
            body = Some(String::new());
        } else if line == "-----END CERTIFICATE-----" {
            let b64 = body.take().context("END CERTIFICATE without BEGIN")?;
            out.push(
                base64::engine::general_purpose::STANDARD
                    .decode(b64)
                    .context("a certificate is not valid base64")?,
            );
        } else if let Some(b) = body.as_mut() {
            b.push_str(line);
        }
    }
    if out.is_empty() {
        bail!("no certificate in the PEM file");
    }
    Ok(out)
}

/// The extra roots named by `MNEMOSYNE_RELAY_CA`, if it is set and not empty.
pub fn extra_roots_from_env() -> Result<Option<Vec<Vec<u8>>>> {
    let Some(path) = std::env::var_os(CA_ENV).filter(|p| !p.is_empty()) else {
        return Ok(None);
    };
    let pem = std::fs::read_to_string(&path)
        .with_context(|| format!("reading {CA_ENV} {}", std::path::Path::new(&path).display()))?;
    Ok(Some(pem_certificates(&pem).with_context(|| {
        format!("{CA_ENV} {}", std::path::Path::new(&path).display())
    })?))
}

/// A builder adjustment that trusts `roots` (DER) besides the embedded web roots.
pub fn trust_extra_roots(roots: Vec<Vec<u8>>) -> impl Fn(Builder) -> Builder {
    move |b| {
        b.ca_tls_config(
            iroh::tls::CaTlsConfig::default()
                .with_extra_roots(roots.iter().cloned().map(Into::into)),
        )
    }
}

pub const ALPN: &[u8] = b"mnemosyne/relay-check/1";
const PAYLOAD: &[u8] = b"mnemosyne relay check";

async fn bind(relay: &RelayUrl, adjust: &impl Fn(Builder) -> Builder) -> Result<Endpoint> {
    let builder = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Custom(std::iter::once(relay.clone()).collect()))
        .secret_key(SecretKey::generate())
        .alpns(vec![ALPN.to_vec()]);
    Ok(adjust(builder).bind().await?)
}

async fn run(relay: &RelayUrl, adjust: &impl Fn(Builder) -> Builder) -> Result<()> {
    let server = bind(relay, adjust).await?;
    let client = bind(relay, adjust).await?;
    let result = async {
        let accept = {
            let server = server.clone();
            tokio::spawn(async move {
                let incoming = server.accept().await.ok_or_else(|| anyhow!("closed"))?;
                let conn = incoming.await?;
                let (mut send, mut recv) = conn.accept_bi().await?;
                let data = recv.read_to_end(1024).await?;
                send.write_all(&data).await?;
                send.finish()?;
                // Let the client read the echo before the connection goes away.
                conn.closed().await;
                Ok::<_, anyhow::Error>(())
            })
        };
        let addr = EndpointAddr::new(server.id()).with_relay_url(relay.clone());
        let conn = client
            .connect(addr, ALPN)
            .await
            .context("connecting through the relay")?;
        let (mut send, mut recv) = conn.open_bi().await?;
        send.write_all(PAYLOAD).await?;
        send.finish()?;
        let echo = recv.read_to_end(1024).await?;
        if echo != PAYLOAD {
            bail!("the echo differs from what was sent");
        }
        conn.close(0u32.into(), b"done");
        accept.abort();
        Ok(())
    }
    .await;
    client.close().await;
    server.close().await;
    result
}

/// Run the check; `adjust` is a last change to both builders (tests trust a local relay's
/// certificate). Returns the milliseconds the round trip took, or why it failed.
pub async fn check(
    relay: &str,
    timeout: Duration,
    adjust: impl Fn(Builder) -> Builder,
) -> Result<u64> {
    let url: RelayUrl = relay
        .trim()
        .parse()
        .with_context(|| format!("invalid relay URL {relay}"))?;
    let started = Instant::now();
    tokio::time::timeout(timeout, run(&url, &adjust))
        .await
        .map_err(|_| anyhow!("timed out after {}s", timeout.as_secs()))??;
    Ok(started.elapsed().as_millis() as u64)
}
