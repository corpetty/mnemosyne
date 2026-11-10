//! The capture log (`--log <file>`): one line per event, appended, flushed at once so the
//! last line is there after a kill. `<UTC time> <event> key=value ...`. The backend keeps it
//! at `<data_dir>/logs/capture.log` and the doctor reads the counters from it.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct Log {
    file: Option<File>,
}

impl Log {
    pub fn none() -> Self {
        Self { file: None }
    }

    pub fn open(path: &Path) -> std::io::Result<Self> {
        if let Some(dir) = path.parent() {
            if !dir.as_os_str().is_empty() {
                std::fs::create_dir_all(dir)?;
            }
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Self { file: Some(file) })
    }

    /// One event line. Never fails: a full disk must not stop a recording.
    pub fn event(&mut self, event: &str, fields: &str) {
        let Some(file) = &mut self.file else { return };
        let line = if fields.is_empty() {
            format!("{} {event}\n", utc_now())
        } else {
            format!("{} {event} {fields}\n", utc_now())
        };
        let _ = file.write_all(line.as_bytes());
    }
}

pub fn utc_now() -> String {
    let d = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    utc(d.as_secs() as i64)
}

/// ISO 8601 UTC from Unix seconds (days since 1970 to civil date, after H. Hinnant).
pub fn utc(secs: i64) -> String {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_are_right() {
        assert_eq!(utc(0), "1970-01-01T00:00:00Z");
        assert_eq!(utc(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(utc(1_794_225_296), "2026-11-09T11:54:56Z");
        assert_eq!(utc(-1), "1969-12-31T23:59:59Z");
    }

    #[test]
    fn lines_are_appended() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sub").join("capture.log");
        Log::open(&path).unwrap().event("a", "x=1");
        Log::open(&path).unwrap().event("b", "");
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].ends_with("Z a x=1"));
        assert!(lines[1].ends_with("Z b"));
    }
}
