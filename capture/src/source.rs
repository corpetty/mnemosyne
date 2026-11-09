//! Where audio comes from. Real sources (a microphone, the system's output) push buffers from
//! the audio system's thread into a channel; `next` is the pull side the recorder loop uses.

use anyhow::Result;
use std::time::Duration;

/// Interleaved f32 frames at the rate the source runs at right now.
#[derive(Debug, Clone)]
pub struct Chunk {
    /// Which source this came from ("mic", "system", "fake"), for the per-source counters.
    pub source: &'static str,
    pub rate: u32,
    pub channels: u16,
    pub data: Vec<f32>,
}

impl Chunk {
    pub fn frames(&self) -> usize {
        self.data.len() / usize::from(self.channels.max(1))
    }

    /// Exactly digital zero in every sample: a muted source, or a denied permission.
    pub fn is_digital_zero(&self) -> bool {
        self.data.iter().all(|s| *s == 0.0)
    }
}

pub enum Event {
    Audio(Chunk),
    /// Nothing arrived within the timeout.
    Idle,
    /// The source ended by itself (a fake file played to its end).
    End,
}

pub trait Source: Send {
    fn name(&self) -> String;
    /// Wait up to `timeout` for the next buffer. An error means the source failed or
    /// disappeared (a device unplugged): the recording ends, non-zero.
    fn next(&mut self, timeout: Duration) -> Result<Event>;
}
