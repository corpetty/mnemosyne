//! `mnemosyne-capture`: records one audio source to a growing WAV file, keeping the contract
//! the backend has with `pw-record` (see backend/mnemosyne/audio/capture.py): the same
//! 44-byte placeholder header, 16-bit PCM appended as it arrives, a file that is valid
//! after a clean stop and repairable (services/recovery.py `repair_wav`) after a kill.

pub mod devices;
pub mod doctor;
pub mod fake;
pub mod log;
pub mod record;
pub mod resample;
pub mod source;
pub mod wav;

/// The fixed rate of the output file. A source's own rate may change under a recording.
pub const DEFAULT_RATE: u32 = 48_000;
