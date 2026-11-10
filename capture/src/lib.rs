//! `mnemosyne-capture`: records one audio source to a growing WAV file, keeping the contract
//! the backend has with `pw-record` (see backend/mnemosyne/audio/capture.py): the same
//! 44-byte placeholder header, 16-bit PCM appended as it arrives, a file that is valid
//! after a clean stop and repairable (services/recovery.py `repair_wav`) after a kill.

//!
//! Real sources exist on macOS only (`macos/`: Core Audio devices, cpal for the microphone, a
//! global process tap for the system's audio); Linux records with `pw-record` and only the
//! fake sources build here.

pub mod devices;
pub mod doctor;
pub mod fake;
pub mod log;
#[cfg(target_os = "macos")]
pub mod macos;
pub mod record;
pub mod resample;
pub mod source;
pub mod wav;

/// The fixed rate of the output file. A source's own rate may change under a recording.
pub const DEFAULT_RATE: u32 = 48_000;

/// Why a real `--target` could not be opened.
#[derive(Debug)]
pub enum OpenError {
    /// This system has no real sources (exit 2).
    Unsupported(String),
    /// The device or the system tap could not be opened here and now (exit 1).
    Failed(anyhow::Error),
}

impl std::fmt::Display for OpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpenError::Unsupported(msg) => f.write_str(msg),
            OpenError::Failed(e) => write!(f, "{e:#}"),
        }
    }
}

/// Open a real source: a device UID or "system".
pub fn open_target(target: &str) -> Result<Box<dyn source::Source>, OpenError> {
    #[cfg(target_os = "macos")]
    {
        macos::open(target).map_err(OpenError::Failed)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(OpenError::Unsupported(format!(
            "recording {target:?} is not supported on this system yet"
        )))
    }
}
