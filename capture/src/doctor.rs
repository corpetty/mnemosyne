//! `mnemosyne-capture doctor`: what this binary can do on this machine, as JSON for the
//! diagnostics. The permission and device fields come with the real sources.

use serde_json::{json, Value};

pub fn report() -> Value {
    json!({
        "version": env!("CARGO_PKG_VERSION"),
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        // What `record` can take as a source here.
        "sources": ["fake"],
        // Whether a real microphone or system audio can be recorded on this system.
        "real_capture": false,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_report_names_the_binary_and_the_system() {
        let r = super::report();
        assert_eq!(r["version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(r["os"], std::env::consts::OS);
        assert!(r["sources"].as_array().unwrap().iter().any(|s| s == "fake"));
    }
}
