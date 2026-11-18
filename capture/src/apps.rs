//! `mnemosyne-capture apps`: the processes capturing audio input right now, as JSON, for
//! auto-record (backend/mnemosyne/audio/streams.py). Linux reads `pw-dump` instead.
//!
//! On macOS these are Core Audio's process objects (`macos/processes.rs`) whose input is
//! running. Browsers capture from helper processes (`com.google.Chrome.helper`,
//! `com.apple.WebKit.GPU`); the backend maps those to an app by bundle id.

use serde::Serialize;

/// One process with audio input running.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CaptureProcess {
    pub pid: i32,
    /// "" when the process has no bundle (a command-line tool).
    pub bundle_id: String,
    /// The executable's file name ("zoom.us", "Google Chrome Helper"); "" when unknown.
    pub name: String,
}

/// What the platform part reads for each process object.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawProcess {
    pub pid: i32,
    pub bundle_id: String,
    pub name: String,
    pub running_input: bool,
}

/// Whether this system can list them (macOS); elsewhere `apps` exits 2.
pub const SUPPORTED: bool = cfg!(target_os = "macos");

/// The processes with input running, without `own_pid` (this sidecar), one entry per pid,
/// in pid order so the output is stable.
pub fn select(raw: Vec<RawProcess>, own_pid: i32) -> Vec<CaptureProcess> {
    let mut out: Vec<CaptureProcess> = raw
        .into_iter()
        .filter(|p| p.running_input && p.pid > 0 && p.pid != own_pid)
        .map(|p| CaptureProcess {
            pid: p.pid,
            bundle_id: p.bundle_id,
            name: p.name,
        })
        .collect();
    out.sort_by_key(|p| p.pid);
    out.dedup_by_key(|p| p.pid);
    out
}

/// The file name of an executable's path ("/Applications/zoom.us.app/Contents/MacOS/zoom.us"
/// gives "zoom.us").
pub fn name_from_path(path: &str) -> String {
    path.trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or("")
        .to_string()
}

/// The processes capturing audio input right now (macOS only).
pub fn list() -> anyhow::Result<Vec<CaptureProcess>> {
    #[cfg(target_os = "macos")]
    {
        let own = std::process::id() as i32;
        Ok(select(crate::macos::processes::all()?, own))
    }
    #[cfg(not(target_os = "macos"))]
    {
        anyhow::bail!("listing the apps that capture audio is not supported on this system")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(pid: i32, bundle: &str, name: &str, running: bool) -> RawProcess {
        RawProcess {
            pid,
            bundle_id: bundle.into(),
            name: name.into(),
            running_input: running,
        }
    }

    #[test]
    fn only_running_input_and_never_ourselves() {
        let got = select(
            vec![
                raw(
                    700,
                    "com.google.Chrome.helper",
                    "Google Chrome Helper",
                    true,
                ),
                raw(500, "us.zoom.xos", "zoom.us", true),
                raw(42, "", "mnemosyne-capture", true), // this sidecar
                raw(600, "com.spotify.client", "Spotify", false), // output only
                raw(500, "us.zoom.xos", "zoom.us", true), // listed twice
                raw(0, "", "", true),                   // no pid: nothing to name
            ],
            42,
        );
        assert_eq!(
            got,
            vec![
                CaptureProcess {
                    pid: 500,
                    bundle_id: "us.zoom.xos".into(),
                    name: "zoom.us".into()
                },
                CaptureProcess {
                    pid: 700,
                    bundle_id: "com.google.Chrome.helper".into(),
                    name: "Google Chrome Helper".into()
                },
            ]
        );
    }

    #[test]
    fn the_json_has_the_backends_fields() {
        let v = serde_json::to_value(select(vec![raw(7, "", "rec", true)], 1)).unwrap();
        assert_eq!(
            v,
            serde_json::json!([{"pid": 7, "bundle_id": "", "name": "rec"}])
        );
    }

    #[test]
    fn names_are_the_executables_file_name() {
        assert_eq!(
            name_from_path("/Applications/zoom.us.app/Contents/MacOS/zoom.us"),
            "zoom.us"
        );
        assert_eq!(name_from_path("/usr/bin/rec/"), "rec");
        assert_eq!(name_from_path("rec"), "rec");
        assert_eq!(name_from_path(""), "");
    }

    #[test]
    fn listing_is_macos_only() {
        assert_eq!(SUPPORTED, cfg!(target_os = "macos"));
        if !SUPPORTED {
            let e = list().unwrap_err();
            assert!(e.to_string().contains("not supported"), "{e}");
        }
    }
}
