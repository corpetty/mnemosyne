//! `mnemosyne-capture`: records one audio source to a growing WAV file, keeping the contract
//! the backend has with `pw-record` (see backend/mnemosyne/audio/capture.py): the same
//! 44-byte placeholder header, 16-bit PCM appended as it arrives, a file that is valid
//! after a clean stop and repairable (services/recovery.py `repair_wav`) after a kill.

//!
//! Real sources exist on macOS only (`macos/`: Core Audio devices, cpal for the microphone, a
//! global process tap for the system's audio); Linux records with `pw-record` and only the
//! fake sources build here.

pub mod apps;
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

/// The `--target` that means the system's audio (every app's output).
pub const SYSTEM_TARGET: &str = "system";

/// Open a real source: a device UID or "system". `voice_processing` records a microphone
/// through Apple's voice processing (echo cancellation; macOS only, never "system").
pub fn open_target(
    target: &str,
    voice_processing: bool,
) -> Result<Box<dyn source::Source>, OpenError> {
    if voice_processing {
        check_voice_processing(target).map_err(OpenError::Unsupported)?;
    }
    #[cfg(target_os = "macos")]
    {
        macos::open(target, voice_processing).map_err(OpenError::Failed)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(OpenError::Unsupported(format!(
            "recording {target:?} is not supported on this system yet"
        )))
    }
}

/// Whether `--voice-processing` can apply to `target` on this system: a microphone, on macOS.
pub fn check_voice_processing(target: &str) -> Result<(), String> {
    if target == SYSTEM_TARGET {
        return Err(
            "--voice-processing applies to a microphone (--target <uid>), not to the system audio"
                .to_string(),
        );
    }
    if !cfg!(target_os = "macos") {
        return Err("--voice-processing (echo cancellation) needs macOS".to_string());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn voice_processing_is_for_a_microphone_on_macos() {
        let e = check_voice_processing(SYSTEM_TARGET).unwrap_err();
        assert!(e.contains("not to the system audio"), "{e}");
        let mic = check_voice_processing("BuiltInMicrophoneDevice");
        if cfg!(target_os = "macos") {
            assert!(mic.is_ok());
        } else {
            assert!(mic.unwrap_err().contains("needs macOS"));
        }
    }

    #[test]
    fn opening_with_voice_processing_says_why_it_cannot() {
        match open_target(SYSTEM_TARGET, true) {
            Err(OpenError::Unsupported(msg)) => assert!(msg.contains("microphone"), "{msg}"),
            other => panic!("expected unsupported, got {:?}", other.err()),
        }
        #[cfg(not(target_os = "macos"))]
        match open_target("BuiltInMicrophoneDevice", true) {
            Err(OpenError::Unsupported(msg)) => assert!(msg.contains("needs macOS"), "{msg}"),
            other => panic!("expected unsupported, got {:?}", other.err()),
        }
    }
}
