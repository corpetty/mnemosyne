//! The growing WAV file: the same placeholder header the backend writes for pw-record
//! (`backend/mnemosyne/audio/capture.py` `wav_header`), then 16-bit PCM appended.
//!
//! Readers in the backend go by the file size, not the header (`WavTail`, `repair_wav`), so
//! the header starts with sizes of 0xFFFFFFFF. Every few seconds the sizes are rewritten to
//! what is really in the file, so a recorder killed outright (SIGKILL, power loss) leaves a
//! file any WAV reader can open, and on a clean stop they are final.

use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};
use std::path::Path;

pub const HEADER_LEN: usize = 44;
const RIFF_SIZE_AT: u64 = 4;
const DATA_SIZE_AT: u64 = 40;
const PLACEHOLDER: u32 = 0xFFFF_FFFF;

/// A 16-bit PCM header with unknown (0xFFFFFFFF) sizes, byte for byte `wav_header()`.
pub fn header(sample_rate: u32, channels: u16) -> [u8; HEADER_LEN] {
    let mut h = [0u8; HEADER_LEN];
    let block_align = channels * 2;
    h[0..4].copy_from_slice(b"RIFF");
    h[4..8].copy_from_slice(&PLACEHOLDER.to_le_bytes());
    h[8..16].copy_from_slice(b"WAVEfmt ");
    h[16..20].copy_from_slice(&16u32.to_le_bytes());
    h[20..22].copy_from_slice(&1u16.to_le_bytes()); // PCM
    h[22..24].copy_from_slice(&channels.to_le_bytes());
    h[24..28].copy_from_slice(&sample_rate.to_le_bytes());
    h[28..32].copy_from_slice(&(sample_rate * u32::from(block_align)).to_le_bytes());
    h[32..34].copy_from_slice(&block_align.to_le_bytes());
    h[34..36].copy_from_slice(&16u16.to_le_bytes());
    h[36..40].copy_from_slice(b"data");
    h[40..44].copy_from_slice(&PLACEHOLDER.to_le_bytes());
    h
}

/// f32 in [-1, 1] to s16 with rounding and clipping.
pub fn to_i16(x: f32) -> i16 {
    (x * 32768.0).round().clamp(-32768.0, 32767.0) as i16
}

pub struct WavWriter {
    file: File,
    // A second handle for the size fields: its position never disturbs the appends.
    patcher: File,
    buf: Vec<u8>,
    frame_bytes: u64,
    /// Bytes of audio in the file (not counting the ones still in `buf`).
    flushed: u64,
}

impl WavWriter {
    /// Create (truncate) the file and write the header at once, as pw-record does.
    pub fn create(path: &Path, sample_rate: u32, channels: u16) -> io::Result<Self> {
        let mut file = File::create(path)?;
        file.write_all(&header(sample_rate, channels))?;
        let patcher = std::fs::OpenOptions::new().write(true).open(path)?;
        Ok(Self {
            file,
            patcher,
            buf: Vec::new(),
            frame_bytes: u64::from(channels) * 2,
            flushed: 0,
        })
    }

    /// Queue samples (interleaved if `channels` > 1) for the next `flush`.
    pub fn push(&mut self, samples: &[i16]) {
        self.buf.reserve(samples.len() * 2);
        for s in samples {
            self.buf.extend_from_slice(&s.to_le_bytes());
        }
    }

    /// Write what is queued to the file.
    pub fn flush(&mut self) -> io::Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        self.file.write_all(&self.buf)?;
        self.flushed += self.buf.len() as u64;
        self.buf.clear();
        Ok(())
    }

    pub fn frames_flushed(&self) -> u64 {
        self.flushed / self.frame_bytes
    }

    pub fn frames_queued(&self) -> u64 {
        self.buf.len() as u64 / self.frame_bytes
    }

    /// Rewrite the RIFF and data sizes to the audio flushed so far. Never ahead of the file.
    pub fn patch_sizes(&mut self) -> io::Result<()> {
        let data = u32::try_from(self.flushed).unwrap_or(PLACEHOLDER);
        let riff = data.saturating_add(HEADER_LEN as u32 - 8);
        self.patcher.seek(SeekFrom::Start(RIFF_SIZE_AT))?;
        self.patcher.write_all(&riff.to_le_bytes())?;
        self.patcher.seek(SeekFrom::Start(DATA_SIZE_AT))?;
        self.patcher.write_all(&data.to_le_bytes())?;
        Ok(())
    }

    /// Flush, fix the sizes for good and sync. Returns the frames in the file.
    pub fn finish(mut self) -> io::Result<u64> {
        self.flush()?;
        self.patch_sizes()?;
        self.file.sync_all()?;
        Ok(self.frames_flushed())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_is_the_backends_wav_header() {
        // Python: wav_header(48000, 1) in backend/mnemosyne/audio/capture.py.
        let expected: [u8; 44] = [
            0x52, 0x49, 0x46, 0x46, 0xFF, 0xFF, 0xFF, 0xFF, 0x57, 0x41, 0x56, 0x45, 0x66, 0x6D,
            0x74, 0x20, 0x10, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x80, 0xBB, 0x00, 0x00,
            0x00, 0x77, 0x01, 0x00, 0x02, 0x00, 0x10, 0x00, 0x64, 0x61, 0x74, 0x61, 0xFF, 0xFF,
            0xFF, 0xFF,
        ];
        assert_eq!(header(48000, 1), expected);
    }

    #[test]
    fn conversion_clips_and_rounds() {
        assert_eq!(to_i16(0.0), 0);
        assert_eq!(to_i16(1.0), 32767);
        assert_eq!(to_i16(-1.0), -32768);
        assert_eq!(to_i16(2.0), 32767);
        assert_eq!(to_i16(0.5), 16384);
    }

    #[test]
    fn sizes_follow_what_is_flushed_not_what_is_queued() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.wav");
        let mut w = WavWriter::create(&path, 48000, 1).unwrap();
        w.push(&[1, 2, 3, 4]);
        w.patch_sizes().unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(bytes.len(), 44);
        assert_eq!(&bytes[40..44], &0u32.to_le_bytes());
        w.flush().unwrap();
        w.patch_sizes().unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(bytes.len(), 52);
        assert_eq!(&bytes[40..44], &8u32.to_le_bytes());
        assert_eq!(&bytes[4..8], &44u32.to_le_bytes());
        assert_eq!(w.finish().unwrap(), 4);
    }
}
