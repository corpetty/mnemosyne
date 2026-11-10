//! The recording loop: a source's buffers, downmixed and resampled to the output rate,
//! appended to the WAV, with the file kept valid as it grows.

use crate::log::Log;
use crate::resample::{downmix, Resampler};
use crate::source::{Chunk, Event, Source};
use crate::wav::{to_i16, WavWriter};
use anyhow::{Context, Result};
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct Settings {
    pub out_rate: u32,
    /// How often what has arrived is written to the file (the live transcript and the level
    /// meters read the file as it grows).
    pub flush_every: Duration,
    /// How often the RIFF and data sizes are rewritten, so a killed recorder leaves a valid file.
    pub size_every: Duration,
    /// How often the counters are logged.
    pub stats_every: Duration,
}

impl Settings {
    pub fn new(out_rate: u32) -> Self {
        Self {
            out_rate,
            flush_every: Duration::from_millis(100),
            size_every: Duration::from_secs(5),
            stats_every: Duration::from_secs(60),
        }
    }
}

/// Per source: buffers received and how many were exactly digital zero (a muted device, or a
/// permission the system denied by delivering silence).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Counters {
    pub buffers: u64,
    pub zero_buffers: u64,
    pub interval_buffers: u64,
    pub interval_zero: u64,
}

#[derive(Debug)]
pub struct Summary {
    /// Frames of audio in the file, at the output rate.
    pub frames: u64,
    pub counters: BTreeMap<&'static str, Counters>,
}

pub struct Pipeline {
    writer: WavWriter,
    resampler: Resampler,
    settings: Settings,
    counters: BTreeMap<&'static str, Counters>,
    log: Log,
    in_rate: u32,
    last_flush: Instant,
    last_size: Instant,
    last_stats: Instant,
}

impl Pipeline {
    pub fn create(path: &Path, settings: Settings, log: Log) -> Result<Self> {
        let writer = WavWriter::create(path, settings.out_rate, 1)
            .with_context(|| format!("cannot write {}", path.display()))?;
        let now = Instant::now();
        Ok(Self {
            writer,
            resampler: Resampler::new(settings.out_rate),
            settings,
            counters: BTreeMap::new(),
            log,
            in_rate: 0,
            last_flush: now,
            last_size: now,
            last_stats: now,
        })
    }

    pub fn log(&mut self) -> &mut Log {
        &mut self.log
    }

    fn put(&mut self, mono: &[f32]) {
        let pcm: Vec<i16> = mono.iter().map(|s| to_i16(*s)).collect();
        self.writer.push(&pcm);
    }

    /// Take one buffer from a source.
    pub fn push(&mut self, chunk: &Chunk) -> Result<()> {
        let c = self.counters.entry(chunk.source).or_default();
        c.buffers += 1;
        c.interval_buffers += 1;
        if chunk.is_digital_zero() {
            c.zero_buffers += 1;
            c.interval_zero += 1;
        }
        if chunk.rate != self.in_rate {
            if self.in_rate == 0 {
                self.log.event(
                    "input",
                    &format!(
                        "source={} rate={} channels={}",
                        chunk.source, chunk.rate, chunk.channels
                    ),
                );
            } else {
                self.log.event(
                    "rate_change",
                    &format!(
                        "source={} from={} to={}",
                        chunk.source, self.in_rate, chunk.rate
                    ),
                );
            }
            self.in_rate = chunk.rate;
        }
        let mono = downmix(&chunk.data, usize::from(chunk.channels));
        let out = self.resampler.process(&mono, chunk.rate)?;
        self.put(&out);
        Ok(())
    }

    /// Do what is due by the clock: write the buffer, rewrite the sizes, log the counters.
    pub fn tick(&mut self, now: Instant) -> Result<()> {
        let io = |e| anyhow::Error::new(e).context("cannot write the recording");
        if now.duration_since(self.last_flush) >= self.settings.flush_every {
            self.writer.flush().map_err(io)?;
            self.last_flush = now;
        }
        if now.duration_since(self.last_size) >= self.settings.size_every {
            self.writer.flush().map_err(io)?;
            self.writer.patch_sizes().map_err(io)?;
            self.last_size = now;
        }
        if now.duration_since(self.last_stats) >= self.settings.stats_every {
            let secs = now.duration_since(self.last_stats).as_secs();
            self.log_stats(secs);
            self.last_stats = now;
        }
        Ok(())
    }

    fn log_stats(&mut self, secs: u64) {
        let frames = self.writer.frames_flushed() + self.writer.frames_queued();
        let mut lines = Vec::new();
        for (name, c) in &mut self.counters {
            lines.push((
                format!(
                    "source={name} buffers={} zero_buffers={} interval_buffers={} \
                     interval_zero={} frames={frames}",
                    c.buffers, c.zero_buffers, c.interval_buffers, c.interval_zero
                ),
                (c.interval_buffers > 0 && c.interval_zero == c.interval_buffers)
                    .then(|| format!("source={name} secs={secs}")),
            ));
            c.interval_buffers = 0;
            c.interval_zero = 0;
        }
        for (stats, silent) in lines {
            self.log.event("stats", &stats);
            if let Some(silent) = silent {
                self.log.event("digital_silence", &silent);
            }
        }
    }

    /// Drain the resampler, write everything, fix the sizes for good and log why it ended.
    pub fn finish(mut self, reason: &str) -> Result<Summary> {
        let out_rate = self.settings.out_rate;
        let tail = std::mem::replace(&mut self.resampler, Resampler::new(out_rate)).finish()?;
        self.put(&tail);
        let frames = self
            .writer
            .finish()
            .context("cannot finish the recording")?;
        self.log.event(
            "stopped",
            &format!(
                "reason={reason} frames={frames} seconds={:.1} {}",
                frames as f64 / f64::from(out_rate),
                counters_text(&self.counters)
            ),
        );
        Ok(Summary {
            frames,
            counters: self.counters,
        })
    }
}

fn counters_text(counters: &BTreeMap<&'static str, Counters>) -> String {
    counters
        .iter()
        .map(|(n, c)| {
            format!(
                "{n}_buffers={} {n}_zero_buffers={}",
                c.buffers, c.zero_buffers
            )
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Record until `stop` is set, the source ends, or it fails. The file is finished in every
/// case; a failing source is returned as the error.
pub fn run(source: &mut dyn Source, mut pipeline: Pipeline, stop: &AtomicBool) -> Result<Summary> {
    let out_rate = pipeline.settings.out_rate;
    pipeline.log().event(
        "started",
        &format!("source={:?} out_rate={out_rate}", source.name()),
    );
    let mut failure = None;
    let mut reason = "stopped";
    while !stop.load(Ordering::Relaxed) {
        match source.next(Duration::from_millis(50)) {
            Ok(Event::Audio(chunk)) => {
                if let Err(e) = pipeline.push(&chunk) {
                    failure = Some(e);
                    break;
                }
            }
            Ok(Event::Idle) => {}
            Ok(Event::Note(text)) => pipeline.log().event("note", &text),
            Ok(Event::End) => {
                reason = "source_ended";
                break;
            }
            Err(e) => {
                failure = Some(e);
                break;
            }
        }
        if let Err(e) = pipeline.tick(Instant::now()) {
            failure = Some(e);
            break;
        }
    }
    if let Some(e) = &failure {
        pipeline.log().event("error", &format!("{e:#}"));
    }
    // Finish even after a failure: what was recorded is kept, with valid sizes.
    let finished = pipeline.finish(if failure.is_some() { "error" } else { reason });
    match (failure, finished) {
        (Some(e), _) => Err(e),
        (None, finished) => finished,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk(source: &'static str, rate: u32, value: f32, frames: usize) -> Chunk {
        Chunk {
            source,
            rate,
            channels: 1,
            data: vec![value; frames],
        }
    }

    fn pipeline(dir: &Path) -> (Pipeline, std::path::PathBuf, std::path::PathBuf) {
        let wav = dir.join("a.wav");
        let log = dir.join("a.log");
        let p = Pipeline::create(&wav, Settings::new(48000), Log::open(&log).unwrap()).unwrap();
        (p, wav, log)
    }

    #[test]
    fn zero_buffers_are_counted_per_source() {
        let dir = tempfile::tempdir().unwrap();
        let (mut p, _, _) = pipeline(dir.path());
        p.push(&chunk("mic", 48000, 0.1, 480)).unwrap();
        p.push(&chunk("mic", 48000, 0.0, 480)).unwrap();
        p.push(&chunk("system", 48000, 0.0, 480)).unwrap();
        p.push(&chunk("system", 48000, -0.0, 480)).unwrap();
        p.push(&chunk("system", 48000, 1e-9, 480)).unwrap();
        let s = p.finish("stopped").unwrap();
        assert_eq!(s.counters["mic"].buffers, 2);
        assert_eq!(s.counters["mic"].zero_buffers, 1);
        assert_eq!(s.counters["system"].buffers, 3);
        assert_eq!(s.counters["system"].zero_buffers, 2);
        assert_eq!(s.frames, 2400);
    }

    #[test]
    fn a_rate_change_is_logged_and_the_file_stays_one_valid_wav() {
        let dir = tempfile::tempdir().unwrap();
        let (mut p, wav, log) = pipeline(dir.path());
        p.push(&chunk("mic", 48000, 0.2, 4800)).unwrap();
        p.push(&chunk("mic", 16000, 0.2, 1600)).unwrap();
        p.push(&chunk("mic", 24000, 0.2, 2400)).unwrap();
        let s = p.finish("stopped").unwrap();
        assert_eq!(s.frames, 4800 + 4800 + 4800);
        let r = hound::WavReader::open(&wav).unwrap();
        assert_eq!(r.spec().sample_rate, 48000);
        assert_eq!(r.spec().channels, 1);
        assert_eq!(r.len(), 14400);
        let text = std::fs::read_to_string(log).unwrap();
        assert!(text.contains("input source=mic rate=48000"), "{text}");
        assert!(
            text.contains("rate_change source=mic from=48000 to=16000"),
            "{text}"
        );
        assert!(
            text.contains("rate_change source=mic from=16000 to=24000"),
            "{text}"
        );
        assert!(
            text.contains("stopped reason=stopped frames=14400"),
            "{text}"
        );
    }

    #[test]
    fn a_stats_line_reports_digital_silence_once_an_interval_is_all_zero() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("a.wav");
        let log = dir.path().join("a.log");
        let mut settings = Settings::new(48000);
        settings.stats_every = Duration::from_secs(60);
        let mut p = Pipeline::create(&wav, settings, Log::open(&log).unwrap()).unwrap();
        for _ in 0..10 {
            p.push(&chunk("system", 48000, 0.0, 480)).unwrap();
        }
        let later = Instant::now() + Duration::from_secs(61);
        p.tick(later).unwrap();
        p.push(&chunk("system", 48000, 0.5, 480)).unwrap();
        p.tick(later + Duration::from_secs(61)).unwrap();
        p.finish("stopped").unwrap();
        let text = std::fs::read_to_string(log).unwrap();
        let stats: Vec<&str> = text.lines().filter(|l| l.contains(" stats ")).collect();
        assert_eq!(stats.len(), 2, "{text}");
        assert!(stats[0].contains("zero_buffers=10 interval_buffers=10 interval_zero=10"));
        assert!(stats[1].contains("zero_buffers=10 interval_buffers=1 interval_zero=0"));
        assert_eq!(
            text.matches("digital_silence source=system secs=").count(),
            1,
            "{text}"
        );
    }

    #[test]
    fn sizes_are_rewritten_on_the_interval_and_never_pass_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let (mut p, wav, _) = pipeline(dir.path());
        p.push(&chunk("mic", 48000, 0.1, 4800)).unwrap();
        let t = Instant::now();
        p.tick(t + Duration::from_secs(6)).unwrap();
        let bytes = std::fs::read(&wav).unwrap();
        let data = u32::from_le_bytes(bytes[40..44].try_into().unwrap()) as usize;
        let riff = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
        assert_eq!(data, 9600);
        assert_eq!(riff, 36 + data);
        assert_eq!(bytes.len(), 44 + data);
    }
}
