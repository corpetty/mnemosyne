//! `mnemosyne --keychain get|set|delete <account>`: the backend's encryption key kept in the
//! macOS keychain by the signed app instead of by the venv's Python (backend storage/crypto.py
//! `ShellKeyStore`). An item the app creates trusts the app's signature, which stays the same
//! across updates; one Python creates trusts that Python binary, and a Python upgrade makes
//! macOS ask "python3.13 wants to use your confidential information…".
//!
//! Handled in main.rs before Tauri and single-instance start (single-instance would hand the
//! arguments to a running app). The secret only ever travels on stdin (set) and stdout (get),
//! never in argv and never in a log. The value is stored as the backend sends it (base64 text),
//! like Python keyring stored it.
//!
//! Exit codes: 0 done; 1 keychain error; 2 usage, or not macOS; 3 no such item (get);
//! 4 the user denied the keychain prompt.

/// The backend's `KEYRING_SERVICE` (storage/crypto.py): one namespace for both stores.
pub const SERVICE: &str = "Mnemosyne";

/// The longest value accepted on stdin (a 32-byte key is 44 characters of base64).
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
const MAX_SECRET: usize = 4096;

#[derive(Debug, PartialEq, Eq)]
pub enum Op {
    Get,
    Set,
    Delete,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Request {
    pub op: Op,
    pub account: String,
}

/// `None` when the arguments are not the keychain mode (the app starts normally); else the
/// request or a usage message. Only a first argument of exactly `--keychain` counts.
pub fn parse<S: AsRef<str>>(args: &[S]) -> Option<Result<Request, String>> {
    if args.get(1).map(AsRef::as_ref) != Some("--keychain") {
        return None;
    }
    let rest: Vec<&str> = args[2..].iter().map(AsRef::as_ref).collect();
    let usage = || "usage: mnemosyne --keychain get|set|delete <account>".to_string();
    let [op, account] = rest.as_slice() else {
        return Some(Err(usage()));
    };
    let op = match *op {
        "get" => Op::Get,
        "set" => Op::Set,
        "delete" => Op::Delete,
        _ => return Some(Err(usage())),
    };
    if account.trim().is_empty() {
        return Some(Err(usage()));
    }
    Some(Ok(Request {
        op,
        account: account.to_string(),
    }))
}

/// The keychain mode, when the arguments ask for it: `Some(exit code)`.
pub fn run_cli(args: &[String]) -> Option<i32> {
    let request = match parse(args)? {
        Ok(request) => request,
        Err(usage) => {
            eprintln!("{usage}");
            return Some(2);
        }
    };
    Some(execute(&request))
}

#[cfg(not(target_os = "macos"))]
fn execute(_request: &Request) -> i32 {
    eprintln!("mnemosyne --keychain is only for macOS");
    2
}

/// The trimmed secret from stdin: non-empty printable ASCII (base64), at most MAX_SECRET.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn clean_secret(raw: &str) -> Result<&str, &'static str> {
    let secret = raw.trim();
    if secret.is_empty() {
        return Err("no secret on stdin");
    }
    if secret.len() > MAX_SECRET || !secret.bytes().all(|b| b.is_ascii_graphic()) {
        return Err("the secret on stdin is not base64 text");
    }
    Ok(secret)
}

#[cfg(target_os = "macos")]
fn execute(request: &Request) -> i32 {
    use security_framework::passwords::{
        delete_generic_password, generic_password, set_generic_password, PasswordOptions,
    };
    use std::io::{Read, Write};

    // security-framework-sys base.rs: errSecItemNotFound, errSecAuthFailed; errSecUserCanceled
    // (-128) is the Cancel button of the keychain prompt ("Deny" gives errSecAuthFailed).
    const NOT_FOUND: i32 = -25300;
    const AUTH_FAILED: i32 = -25293;
    const USER_CANCELED: i32 = -128;

    // Only the status and Apple's message for it: never the secret.
    let fail = |what: &str, e: security_framework::base::Error| -> i32 {
        match e.code() {
            AUTH_FAILED | USER_CANCELED => {
                eprintln!("keychain {what}: access denied");
                4
            }
            code => {
                eprintln!(
                    "keychain {what}: {} ({code})",
                    e.message().unwrap_or_else(|| "error".into())
                );
                1
            }
        }
    };

    match request.op {
        Op::Get => match generic_password(PasswordOptions::new_generic_password(
            SERVICE,
            &request.account,
        )) {
            Ok(secret) => {
                let mut out = std::io::stdout().lock();
                let written = out
                    .write_all(&secret)
                    .and_then(|_| out.write_all(b"\n"))
                    .and_then(|_| out.flush());
                i32::from(written.is_err())
            }
            Err(e) if e.code() == NOT_FOUND => 3,
            Err(e) => fail("get", e),
        },
        Op::Set => {
            let mut raw = String::new();
            if std::io::stdin()
                .take(MAX_SECRET as u64 + 1)
                .read_to_string(&mut raw)
                .is_err()
            {
                eprintln!("keychain set: could not read stdin");
                return 1;
            }
            let secret = match clean_secret(&raw) {
                Ok(secret) => secret,
                Err(why) => {
                    eprintln!("keychain set: {why}");
                    return 2;
                }
            };
            // Adds the item (trusting this app), or updates its value when it exists.
            match set_generic_password(SERVICE, &request.account, secret.as_bytes()) {
                Ok(()) => 0,
                Err(e) => fail("set", e),
            }
        }
        Op::Delete => match delete_generic_password(SERVICE, &request.account) {
            Ok(()) => 0,
            Err(e) if e.code() == NOT_FOUND => 0,
            Err(e) => fail("delete", e),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(op: Op, account: &str) -> Option<Result<Request, String>> {
        Some(Ok(Request {
            op,
            account: account.into(),
        }))
    }

    #[test]
    fn parses_the_keychain_mode() {
        let account = "data key for /Users/x/Library/Application Support/mnemosyne (app)";
        assert_eq!(
            parse(&["mnemosyne", "--keychain", "get", account]),
            req(Op::Get, account)
        );
        assert_eq!(parse(&["m", "--keychain", "set", "a"]), req(Op::Set, "a"));
        assert_eq!(
            parse(&["m", "--keychain", "delete", "a"]),
            req(Op::Delete, "a")
        );
    }

    #[test]
    fn other_arguments_start_the_app() {
        assert_eq!(parse(&["mnemosyne"]), None);
        assert_eq!(parse::<&str>(&[]), None);
        assert_eq!(parse(&["mnemosyne", "--toggle"]), None);
        // Only as the first argument: a remote action never turns into the keychain mode.
        assert_eq!(
            parse(&["mnemosyne", "--toggle", "--keychain", "get", "a"]),
            None
        );
    }

    #[test]
    fn rejects_bad_usage() {
        for args in [
            vec!["m", "--keychain"],
            vec!["m", "--keychain", "get"],
            vec!["m", "--keychain", "list", "a"],
            vec!["m", "--keychain", "get", "  "],
            vec!["m", "--keychain", "set", "a", "SECRET"], // never a secret in argv
        ] {
            assert!(matches!(parse(&args), Some(Err(_))), "{args:?}");
        }
    }

    #[test]
    fn secrets_from_stdin_are_base64_text() {
        assert_eq!(clean_secret("QUJD\n"), Ok("QUJD"));
        assert!(clean_secret(" \n").is_err());
        assert!(clean_secret("a b").is_err());
        assert!(clean_secret(&"A".repeat(MAX_SECRET + 1)).is_err());
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn not_on_this_os() {
        let args: Vec<String> = ["m", "--keychain", "get", "a"].map(String::from).to_vec();
        assert_eq!(run_cli(&args), Some(2));
        assert_eq!(run_cli(&["m".to_string()]), None);
    }
}
