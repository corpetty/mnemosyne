//! Sources that need no hardware, on every OS: for tests, CI, and trying the pipeline.
//!
//! `tone[:hz]`, `silence`, `file:<path.wav>`. They are paced in real time (like a device)
//! unless `fast` is set. The time options count the source's own media time.

use crate::source::{Chunk, Event, Source};
use anyhow::{anyhow, bail, Context, Result};
use std::path::Path;
use std::time::{Duration, Instant};

/// Media time is a float sum of 10 ms buffers; this absorbs its rounding.
const EPS: f64 = 1e-6;

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    Tone(f32),
    Silence,
    File(String),
}

pub fn parse_kind(spec: &str) -> Result<Kind> {
    let (kind, arg) = match spec.split_once(':') {
        Some((k, a)) => (k, Some(a)),
        None => (spec, None),
    };
    match (kind, arg) {
        ("tone", None) => Ok(Kind::Tone(440.0)),
        ("tone", Some(hz)) => {
            let hz: f32 = hz
                .parse()
                .with_context(|| format!("not a frequency: {hz}"))?;
            if hz.is_nan() || hz <= 0.0 {
                bail!("the frequency must be positive");
            }
            Ok(Kind::Tone(hz))
        }
        ("silence", None) => Ok(Kind::Silence),
        ("file", Some(path)) if !path.is_empty() => Ok(Kind::File(path.to_string())),
        _ => bail!("unknown fake source {spec:?}; use tone[:hz], silence or file:<path.wav>"),
    }
}

/// "500ms", "2s", "1.5s", or a bare number of seconds.
pub fn parse_duration(s: &str) -> Result<Duration> {
    let s = s.trim();
    let (num, scale) = if let Some(n) = s.strip_suffix("ms") {
        (n, 0.001)
    } else if let Some(n) = s.strip_suffix('s') {
        (n, 1.0)
    } else {
        (s, 1.0)
    };
    let v: f64 = num
        .parse()
        .with_context(|| format!("not a duration: {s}"))?;
    if v.is_nan() || v < 0.0 || !v.is_finite() {
        bail!("not a duration: {s}");
    }
    Ok(Duration::from_secs_f64(v * scale))
}

/// "2s:16000": after 2 s of media time the source runs at 16000 Hz.
pub fn parse_switch(s: &str) -> Result<(Duration, u32)> {
    let (after, rate) = s
        .split_once(':')
        .ok_or_else(|| anyhow!("expected <duration>:<rate>, like 2s:16000"))?;
    let rate: u32 = rate
        .parse()
        .with_context(|| format!("not a rate: {rate}"))?;
    if rate == 0 {
        bail!("the rate must be positive");
    }
    Ok((parse_duration(after)?, rate))
}

#[derive(Debug, Clone)]
pub struct Options {
    pub rate: u32,
    pub channels: u16,
    /// Do not wait for real time: hand over buffers as fast as they are asked for.
    pub fast: bool,
    pub switch: Option<(Duration, u32)>,
    /// Fail like a device that was unplugged, after this much media time.
    pub vanish_after: Option<Duration>,
    /// End by itself after this much media time (tone and silence; a file ends at its end).
    pub duration: Option<Duration>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            rate: crate::DEFAULT_RATE,
            channels: 1,
            fast: false,
            switch: None,
            vanish_after: None,
            duration: None,
        }
    }
}

pub struct Fake {
    kind: Kind,
    opts: Options,
    rate: u32,
    channels: u16,
    // Media time already handed out, in seconds, and the wall clock it started on.
    media: f64,
    start: Instant,
    phase: f32,
    switched: bool,
    file: Option<(Vec<f32>, usize)>,
}

impl Fake {
    pub fn new(kind: Kind, mut opts: Options) -> Result<Self> {
        let mut file = None;
        if let Kind::File(path) = &kind {
            if opts.switch.is_some() {
                bail!("--fake-switch-rate-after works with tone and silence, not a file");
            }
            let (data, rate, channels) = read_wav(Path::new(path))?;
            opts.rate = rate;
            opts.channels = channels;
            file = Some((data, 0));
        }
        Ok(Self {
            kind,
            rate: opts.rate,
            channels: opts.channels.max(1),
            opts,
            media: 0.0,
            start: Instant::now(),
            phase: 0.0,
            switched: false,
            file,
        })
    }
}

/// A WAV as interleaved f32, its rate and channel count.
fn read_wav(path: &Path) -> Result<(Vec<f32>, u32, u16)> {
    let mut reader =
        hound::WavReader::open(path).with_context(|| format!("cannot read {}", path.display()))?;
    let spec = reader.spec();
    let data: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.samples::<f32>().collect::<Result<_, _>>()?,
        hound::SampleFormat::Int => {
            let scale = 1.0 / (1u64 << (spec.bits_per_sample - 1)) as f32;
            reader
                .samples::<i32>()
                .map(|s| s.map(|v| v as f32 * scale))
                .collect::<Result<_, _>>()?
        }
    };
    Ok((data, spec.sample_rate, spec.channels))
}

impl Source for Fake {
    fn name(&self) -> String {
        match &self.kind {
            Kind::Tone(hz) => format!("fake tone {hz} Hz"),
            Kind::Silence => "fake silence".into(),
            Kind::File(p) => format!("fake file {p}"),
        }
    }

    fn next(&mut self, timeout: Duration) -> Result<Event> {
        if let Some(v) = self.opts.vanish_after {
            if self.media + EPS >= v.as_secs_f64() {
                bail!("the fake source disappeared after {:.1} s", self.media);
            }
        }
        if let Some(d) = self.opts.duration {
            if self.media + EPS >= d.as_secs_f64() {
                return Ok(Event::End);
            }
        }
        if let Some((after, rate)) = self.opts.switch {
            if !self.switched && self.media + EPS >= after.as_secs_f64() {
                self.switched = true;
                self.rate = rate;
            }
        }
        if !self.opts.fast {
            let due = self.start + Duration::from_secs_f64(self.media);
            let now = Instant::now();
            if due > now {
                let wait = due - now;
                std::thread::sleep(wait.min(timeout));
                if Instant::now() < due {
                    return Ok(Event::Idle);
                }
            }
        }
        // 10 ms a buffer, up to the switch point or the end so those fall on a boundary.
        let mut frames = (self.rate / 100).max(1) as usize;
        if let Some((data, pos)) = &self.file {
            let left = (data.len() - *pos) / usize::from(self.channels);
            if left == 0 {
                return Ok(Event::End);
            }
            frames = frames.min(left);
        }
        let channels = usize::from(self.channels);
        let mut out = vec![0.0f32; frames * channels];
        match (&self.kind, &mut self.file) {
            (Kind::Tone(hz), _) => {
                let step = 2.0 * std::f32::consts::PI * hz / self.rate as f32;
                for frame in out.chunks_mut(channels) {
                    let v = 0.25 * self.phase.sin();
                    frame.fill(v);
                    self.phase = (self.phase + step) % (2.0 * std::f32::consts::PI);
                }
            }
            (Kind::Silence, _) => {}
            (Kind::File(_), Some((data, pos))) => {
                out.copy_from_slice(&data[*pos..*pos + frames * channels]);
                *pos += frames * channels;
            }
            (Kind::File(_), None) => unreachable!("a file source has its data"),
        }
        self.media += frames as f64 / self.rate as f64;
        Ok(Event::Audio(Chunk {
            source: "fake",
            rate: self.rate,
            channels: self.channels,
            data: out,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn drain(mut f: Fake) -> (usize, Vec<u32>, Result<()>) {
        let mut frames = 0;
        let mut rates = Vec::new();
        loop {
            match f.next(Duration::from_millis(1)) {
                Ok(Event::Audio(c)) => {
                    frames += c.frames();
                    if rates.last() != Some(&c.rate) {
                        rates.push(c.rate);
                    }
                }
                Ok(Event::Idle) | Ok(Event::Note(_)) => {}
                Ok(Event::End) => return (frames, rates, Ok(())),
                Err(e) => return (frames, rates, Err(e)),
            }
        }
    }

    fn fast(duration_ms: u64) -> Options {
        Options {
            fast: true,
            duration: Some(Duration::from_millis(duration_ms)),
            ..Options::default()
        }
    }

    #[test]
    fn specs_and_durations_parse() {
        assert_eq!(parse_kind("tone").unwrap(), Kind::Tone(440.0));
        assert_eq!(parse_kind("tone:880").unwrap(), Kind::Tone(880.0));
        assert_eq!(parse_kind("silence").unwrap(), Kind::Silence);
        assert_eq!(
            parse_kind("file:/a:b.wav").unwrap(),
            Kind::File("/a:b.wav".into())
        );
        assert!(parse_kind("tone:-1").is_err());
        assert!(parse_kind("noise").is_err());
        assert_eq!(parse_duration("500ms").unwrap(), Duration::from_millis(500));
        assert_eq!(parse_duration("1.5s").unwrap(), Duration::from_millis(1500));
        assert_eq!(
            parse_switch("2s:16000").unwrap(),
            (Duration::from_secs(2), 16000)
        );
        assert!(parse_switch("2s").is_err());
    }

    #[test]
    fn a_tone_runs_for_its_duration_and_a_rate_switch_shows() {
        let mut opts = fast(3000);
        opts.switch = Some((Duration::from_secs(1), 16000));
        let (frames, rates, end) = drain(Fake::new(Kind::Tone(440.0), opts).unwrap());
        end.unwrap();
        assert_eq!(rates, vec![48000, 16000]);
        assert_eq!(frames, 48000 + 2 * 16000);
    }

    #[test]
    fn silence_is_exact_zero_and_a_tone_is_not() {
        let mut s = Fake::new(Kind::Silence, fast(100)).unwrap();
        let Event::Audio(c) = s.next(Duration::from_millis(1)).unwrap() else {
            panic!("no audio");
        };
        assert!(c.is_digital_zero());
        let mut t = Fake::new(Kind::Tone(440.0), fast(100)).unwrap();
        t.next(Duration::from_millis(1)).unwrap();
        let Event::Audio(c) = t.next(Duration::from_millis(1)).unwrap() else {
            panic!("no audio");
        };
        assert!(!c.is_digital_zero());
    }

    #[test]
    fn vanishing_is_an_error() {
        let mut opts = fast(5000);
        opts.vanish_after = Some(Duration::from_millis(200));
        let (frames, _, end) = drain(Fake::new(Kind::Tone(440.0), opts).unwrap());
        assert_eq!(frames, 9600);
        assert!(end.unwrap_err().to_string().contains("disappeared"));
    }

    #[test]
    fn real_time_pacing_waits() {
        let mut f = Fake::new(Kind::Silence, Options::default()).unwrap();
        let t = Instant::now();
        let mut got = 0;
        while got < 5 {
            if let Event::Audio(_) = f.next(Duration::from_millis(20)).unwrap() {
                got += 1;
            }
        }
        // Five 10 ms buffers: the first is due at once, the fifth 40 ms later.
        assert!(
            t.elapsed() >= Duration::from_millis(35),
            "{:?}",
            t.elapsed()
        );
    }

    #[test]
    fn a_file_plays_to_its_end_in_its_own_format() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("in.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 16000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(&path, spec).unwrap();
        for _ in 0..1234 {
            w.write_sample(1000i16).unwrap();
            w.write_sample(-1000i16).unwrap();
        }
        w.finalize().unwrap();
        let f = Fake::new(
            Kind::File(path.display().to_string()),
            Options {
                fast: true,
                ..Options::default()
            },
        )
        .unwrap();
        let (frames, rates, end) = drain(f);
        end.unwrap();
        assert_eq!((frames, rates), (1234, vec![16000]));
    }
}
