//! `relay-check` against an in-process relay, and against one that is not there.

use std::time::Duration;

use iroh::endpoint::Builder;
use mnemosyne_link::relay_check::check;

fn trust(b: Builder) -> Builder {
    b.ca_tls_config(iroh_relay::tls::CaTlsConfig::insecure_skip_verify())
}

#[tokio::test]
async fn passes_through_a_working_relay() {
    let (_map, relay, _server) = iroh::test_utils::run_relay_server().await.unwrap();
    let ms = check(relay.as_str(), Duration::from_secs(20), trust)
        .await
        .unwrap();
    assert!(ms < 20_000);
}

#[tokio::test]
async fn fails_without_a_relay() {
    let err = check("https://127.0.0.1:9", Duration::from_secs(3), trust)
        .await
        .unwrap_err();
    assert!(!format!("{err:#}").is_empty());
    assert!(check("not a url", Duration::from_secs(1), trust)
        .await
        .is_err());
}

/// A relay with a certificate from a private CA: refused by default, accepted once the CA is
/// given as an extra root (`MNEMOSYNE_RELAY_CA` in the binary).
mod private_ca {
    use std::time::Duration;

    use base64::Engine;
    use iroh_relay::server::{
        testing::self_signed_tls_certs_and_config, CertConfig, RelayConfig, Server, ServerConfig,
        TlsConfig,
    };
    use mnemosyne_link::relay_check::{
        check, extra_roots_from_env, pem_certificates, trust_extra_roots, CA_ENV,
    };

    async fn relay_with_private_cert() -> (String, String, Server) {
        let (certs, server_config) = self_signed_tls_certs_and_config();
        let b64 = base64::engine::general_purpose::STANDARD.encode(certs[0].as_ref());
        let pem = format!("-----BEGIN CERTIFICATE-----\n{b64}\n-----END CERTIFICATE-----\n");
        let mut relay = RelayConfig::new((std::net::Ipv4Addr::LOCALHOST, 0));
        relay.tls = Some(TlsConfig::new(
            (std::net::Ipv4Addr::LOCALHOST, 0),
            CertConfig::Manual { server_config },
        ));
        let mut config = ServerConfig::default();
        config.relay = Some(relay);
        let server = Server::spawn(config).await.unwrap();
        let url = format!("https://{}", server.https_addr().unwrap());
        (url, pem, server)
    }

    #[test]
    fn pem_parsing() {
        assert!(pem_certificates("nothing here").is_err());
        assert!(
            pem_certificates("-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n")
                .is_err()
        );
        let one = "-----BEGIN CERTIFICATE-----\nAQID\nBAU=\n-----END CERTIFICATE-----\n";
        let two = format!("junk\n{one}more junk\n{one}");
        assert_eq!(pem_certificates(one).unwrap(), vec![vec![1, 2, 3, 4, 5]]);
        assert_eq!(pem_certificates(&two).unwrap().len(), 2);
    }

    #[tokio::test]
    async fn an_extra_root_makes_a_private_relay_pass() {
        let (url, pem, _server) = relay_with_private_cert().await;
        // Default roots only: the certificate is not trusted.
        assert!(check(&url, Duration::from_secs(5), |b| b).await.is_err());
        let roots = pem_certificates(&pem).unwrap();
        let ms = check(&url, Duration::from_secs(20), trust_extra_roots(roots))
            .await
            .unwrap();
        assert!(ms < 20_000);
    }

    async fn relay_check_binary(
        url: &str,
        secs: u32,
        ca: Option<std::path::PathBuf>,
    ) -> std::process::Output {
        let url = url.to_string();
        tokio::task::spawn_blocking(move || {
            let mut cmd = std::process::Command::new(env!("CARGO_BIN_EXE_mnemosyne-link"));
            cmd.args(["relay-check", "--relay", &url, "--timeout-secs"])
                .arg(secs.to_string())
                .env_remove(CA_ENV);
            if let Some(ca) = ca {
                cmd.env(CA_ENV, ca);
            }
            cmd.output().unwrap()
        })
        .await
        .unwrap()
    }

    #[tokio::test]
    async fn the_binary_reads_the_ca_from_the_environment() {
        let (url, pem, _server) = relay_with_private_cert().await;
        let dir = tempfile::tempdir().unwrap();
        let ca = dir.path().join("ca.pem");
        std::fs::write(&ca, pem).unwrap();

        let out = relay_check_binary(&url, 20, Some(ca.clone())).await;
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stdout)
        );
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(v["ok"], true, "{v}");

        // Unset: today's behaviour, the private certificate is refused.
        assert!(!relay_check_binary(&url, 5, None).await.status.success());

        // A file that is not a PEM certificate fails with a message, not a silent fallback.
        std::fs::write(&ca, "nope").unwrap();
        let out = relay_check_binary(&url, 5, Some(ca)).await;
        assert!(!out.status.success());
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert!(v["error"].as_str().unwrap().contains(CA_ENV), "{v}");
    }

    #[test]
    fn env_unset_means_no_extra_roots() {
        // Not run with the variable set by this test binary.
        if std::env::var_os(CA_ENV).is_none() {
            assert!(extra_roots_from_env().unwrap().is_none());
        }
    }
}
