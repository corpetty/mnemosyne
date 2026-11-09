//! Any source rate to the fixed output rate, as mono.
//!
//! A recording is one WAV at one rate. The rate of a source can change under it (AirPods
//! drop to 16 or 24 kHz when a call opens the microphone), so each run of input at one rate
//! has its own resampler; on a change the old one is drained (its tail is real audio) and a
//! new one started. At the output rate the samples pass through untouched.

use anyhow::{anyhow, Result};
use rubato::audioadapter_buffers::direct::InterleavedSlice;
use rubato::{Fft, FixedSync, Indexing, Resampler as _};

/// Input frames asked of the FFT resampler per call, about 20 ms at 48 kHz.
const CHUNK: usize = 1024;

/// Average the channels of interleaved frames.
pub fn downmix(data: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return data.to_vec();
    }
    let k = 1.0 / channels as f32;
    data.chunks_exact(channels)
        .map(|frame| frame.iter().sum::<f32>() * k)
        .collect()
}

/// One run of input at one rate through rubato's FFT resampler (mono).
struct Stage {
    fft: Fft<f32>,
    in_rate: u32,
    pending: Vec<f32>,
    out: Vec<f32>,
    /// Frames of start-up delay still to drop from the output.
    skip: usize,
    fed: u64,
    emitted: u64,
}

impl Stage {
    fn new(in_rate: u32, out_rate: u32) -> Result<Self> {
        let fft = Fft::<f32>::new(
            in_rate as usize,
            out_rate as usize,
            CHUNK,
            1,
            FixedSync::Input,
        )
        .map_err(|e| anyhow!("cannot resample {in_rate} Hz to {out_rate} Hz: {e}"))?;
        let skip = fft.output_delay();
        let out = vec![0.0; fft.output_frames_max()];
        Ok(Self {
            fft,
            in_rate,
            pending: Vec::new(),
            out,
            skip,
            fed: 0,
            emitted: 0,
        })
    }

    /// Run whole chunks of the pending input (`partial` pads the last one with silence).
    fn run(&mut self, result: &mut Vec<f32>, partial: Option<usize>) -> Result<usize> {
        let need = self.fft.input_frames_next();
        let n_in = partial.unwrap_or(need);
        let mut idx = Indexing::new();
        if let Some(p) = partial {
            idx = idx.partial_len(p);
        }
        // The input buffer must hold a whole chunk even when only `partial` frames are real.
        let mut padded;
        let input: &[f32] = if self.pending.len() >= need {
            &self.pending
        } else {
            padded = self.pending.clone();
            padded.resize(need, 0.0);
            &padded
        };
        let adapter_in = InterleavedSlice::new(input, 1, input.len())
            .map_err(|e| anyhow!("resampler input: {e}"))?;
        let cap = self.out.len();
        let mut adapter_out = InterleavedSlice::new_mut(&mut self.out[..], 1, cap)
            .map_err(|e| anyhow!("resampler output: {e}"))?;
        let (used, produced) = self
            .fft
            .process_into_buffer(&adapter_in, &mut adapter_out, Some(&idx))
            .map_err(|e| anyhow!("resampling failed: {e}"))?;
        let used = used.min(n_in);
        self.pending.drain(..used.min(self.pending.len()));
        let skip = self.skip.min(produced);
        self.skip -= skip;
        result.extend_from_slice(&self.out[skip..produced]);
        self.emitted += (produced - skip) as u64;
        Ok(used)
    }

    fn push(&mut self, mono: &[f32], result: &mut Vec<f32>) -> Result<()> {
        self.fed += mono.len() as u64;
        self.pending.extend_from_slice(mono);
        while self.pending.len() >= self.fft.input_frames_next() {
            self.run(result, None)?;
        }
        Ok(())
    }

    /// Drain the tail: the output is exactly the rounded `fed * ratio` frames, no more.
    fn finish(mut self, out_rate: u32, result: &mut Vec<f32>) -> Result<()> {
        let want = (self.fed * u64::from(out_rate) + u64::from(self.in_rate) / 2)
            / u64::from(self.in_rate);
        let mut guard = 0;
        while self.emitted < want && guard < 64 {
            let left = self.pending.len().min(self.fft.input_frames_next());
            self.run(result, Some(left))?;
            guard += 1;
        }
        if self.emitted > want {
            let extra = (self.emitted - want) as usize;
            result.truncate(result.len() - extra);
        }
        Ok(())
    }
}

pub struct Resampler {
    out_rate: u32,
    stage: Option<Stage>,
    in_rate: u32,
}

impl Resampler {
    pub fn new(out_rate: u32) -> Self {
        Self {
            out_rate,
            stage: None,
            in_rate: 0,
        }
    }

    /// Feed mono samples at `in_rate`; returns the samples at the output rate that are ready.
    pub fn process(&mut self, mono: &[f32], in_rate: u32) -> Result<Vec<f32>> {
        let mut result = Vec::new();
        if in_rate != self.in_rate {
            self.drain_into(&mut result)?;
            self.in_rate = in_rate;
            if in_rate != self.out_rate {
                self.stage = Some(Stage::new(in_rate, self.out_rate)?);
            }
        }
        match &mut self.stage {
            Some(stage) => stage.push(mono, &mut result)?,
            None => result.extend_from_slice(mono),
        }
        Ok(result)
    }

    fn drain_into(&mut self, result: &mut Vec<f32>) -> Result<()> {
        if let Some(stage) = self.stage.take() {
            stage.finish(self.out_rate, result)?;
        }
        Ok(())
    }

    /// The tail of the last run, at the end of the recording.
    pub fn finish(mut self) -> Result<Vec<f32>> {
        let mut result = Vec::new();
        self.drain_into(&mut result)?;
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(hz: f32, rate: u32, frames: usize) -> Vec<f32> {
        (0..frames)
            .map(|i| 0.5 * (2.0 * std::f32::consts::PI * hz * i as f32 / rate as f32).sin())
            .collect()
    }

    fn run(input: &[f32], rate: u32, piece: usize) -> Vec<f32> {
        let mut r = Resampler::new(48000);
        let mut out = Vec::new();
        for c in input.chunks(piece) {
            out.extend(r.process(c, rate).unwrap());
        }
        out.extend(r.finish().unwrap());
        out
    }

    #[test]
    fn the_output_rate_passes_through_untouched() {
        let input = tone(440.0, 48000, 4800);
        let out = run(&input, 48000, 480);
        assert_eq!(out, input);
    }

    #[test]
    fn sixteen_k_to_forty_eight_k_is_three_times_as_long() {
        for piece in [1, 160, 333, 1024, 16000] {
            let out = run(&tone(440.0, 16000, 16000), 16000, piece);
            assert_eq!(out.len(), 48000, "pieces of {piece}");
        }
    }

    #[test]
    fn odd_rates_keep_the_duration() {
        let out = run(&tone(440.0, 44100, 44100), 44100, 441);
        assert_eq!(out.len(), 48000);
        let out = run(&tone(440.0, 24000, 12345), 24000, 480);
        assert_eq!(out.len(), 24690);
        let out = run(&tone(440.0, 96000, 96000), 96000, 960);
        assert_eq!(out.len(), 48000);
    }

    #[test]
    fn a_resampled_tone_keeps_its_pitch_and_level() {
        let out = run(&tone(1000.0, 16000, 16000), 16000, 320);
        // Middle second, away from the edges: amplitude 0.5 and 1000 crossings upward.
        let mid = &out[4800..out.len() - 4800];
        let peak = mid.iter().fold(0.0f32, |m, x| m.max(x.abs()));
        assert!((peak - 0.5).abs() < 0.02, "peak {peak}");
        let ups = mid.windows(2).filter(|w| w[0] < 0.0 && w[1] >= 0.0).count();
        let secs = mid.len() as f32 / 48000.0;
        assert!((ups as f32 / secs - 1000.0).abs() < 15.0, "{ups} in {secs}");
    }

    #[test]
    fn a_rate_change_keeps_one_continuous_stream() {
        let mut r = Resampler::new(48000);
        let mut out = r.process(&tone(500.0, 48000, 48000), 48000).unwrap();
        out.extend(r.process(&tone(500.0, 16000, 32000), 16000).unwrap());
        out.extend(r.process(&tone(500.0, 24000, 24000), 24000).unwrap());
        out.extend(r.process(&tone(500.0, 48000, 4800), 48000).unwrap());
        out.extend(r.finish().unwrap());
        assert_eq!(out.len(), 48000 + 96000 + 48000 + 4800);
        assert!(out.iter().all(|x| x.abs() <= 0.6));
    }

    #[test]
    fn a_few_frames_are_not_lost_or_invented() {
        let out = run(&tone(440.0, 16000, 7), 16000, 7);
        assert_eq!(out.len(), 21);
        assert_eq!(run(&[], 16000, 1).len(), 0);
    }

    #[test]
    fn downmix_averages_channels() {
        assert_eq!(downmix(&[1.0, 0.0, 0.5, 0.5], 2), vec![0.5, 0.5]);
        assert_eq!(downmix(&[0.25, 0.5], 1), vec![0.25, 0.5]);
        let three = downmix(&[0.3, 0.3, 0.3, 0.6, 0.0, 0.0], 3);
        assert!((three[0] - 0.3).abs() < 1e-6 && (three[1] - 0.2).abs() < 1e-6);
    }
}
