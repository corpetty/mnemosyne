use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand};
use mnemosyne_capture::fake::{self, Fake};
use mnemosyne_capture::log::Log;
use mnemosyne_capture::record::{self, Pipeline, Settings};
use mnemosyne_capture::source::Source;
use mnemosyne_capture::{devices, doctor, DEFAULT_RATE};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Exit codes: 0 done, 1 the source failed or disappeared, 2 usage or not supported here.
const EXIT_UNSUPPORTED: u8 = 2;

#[derive(Parser)]
#[command(
    name = "mnemosyne-capture",
    version,
    about = "Audio capture for Mnemosyne"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Record a source to a growing WAV file (SIGTERM or SIGINT ends it cleanly).
    Record(Box<RecordArgs>),
    /// List the devices that can be recorded, as JSON.
    Devices,
    /// Say what this binary can do on this machine, as JSON.
    Doctor,
}

#[derive(Args)]
struct RecordArgs {
    /// A device UID or "system" (all apps' output).
    #[arg(long)]
    target: Option<String>,
    /// Output sample rate; whatever the source runs at is resampled to it.
    #[arg(long, default_value_t = DEFAULT_RATE)]
    rate: u32,
    /// Output channels. Only 1 (mono) for now.
    #[arg(long, default_value_t = 1)]
    channels: u16,
    /// Append one line per event to this file.
    #[arg(long)]
    log: Option<PathBuf>,
    /// Also stop (cleanly) when standard input reaches end of file.
    #[arg(long)]
    stdin_stop: bool,
    /// A source that needs no hardware: tone[:hz], silence or file:<path.wav>.
    #[arg(long, value_name = "SPEC")]
    fake: Option<String>,
    /// With --fake: the source switches to this rate after a time, like AirPods opening
    /// the microphone, as `2s:16000`.
    #[arg(long, value_name = "AFTER:RATE")]
    fake_switch_rate_after: Option<String>,
    /// With --fake: the source fails (as an unplugged device) after this much audio.
    #[arg(long, value_name = "DURATION")]
    fake_vanish_after: Option<String>,
    /// With --fake: the source ends by itself after this much audio.
    #[arg(long, value_name = "DURATION")]
    fake_duration: Option<String>,
    /// With --fake: the rate of a tone or silence (default 48000).
    #[arg(long, value_name = "HZ")]
    fake_rate: Option<u32>,
    /// With --fake: the channel count of a tone or silence (default 1).
    #[arg(long, value_name = "N")]
    fake_channels: Option<u16>,
    /// With --fake: hand audio over as fast as possible instead of in real time.
    #[arg(long)]
    fake_fast: bool,
    /// How often the sizes in the WAV header are rewritten, in milliseconds (default 5000).
    #[arg(long, hide = true, value_name = "MS")]
    size_interval_ms: Option<u64>,
    /// How often the counters are logged, in milliseconds (default 60000).
    #[arg(long, hide = true, value_name = "MS")]
    stats_interval_ms: Option<u64>,
    /// Where the audio goes.
    out: PathBuf,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Record(args) => return record(*args),
        Command::Devices => devices::list().and_then(|d| print_json(&d)),
        Command::Doctor => print_json(&doctor::report()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("mnemosyne-capture: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn print_json<T: serde::Serialize>(value: &T) -> Result<()> {
    println!("{}", serde_json::to_string(value)?);
    Ok(())
}

fn fake_source(spec: &str, args: &RecordArgs) -> Result<Fake> {
    let kind = fake::parse_kind(spec)?;
    let mut opts = fake::Options {
        fast: args.fake_fast,
        ..fake::Options::default()
    };
    if let Some(rate) = args.fake_rate {
        opts.rate = rate;
    }
    if let Some(ch) = args.fake_channels {
        opts.channels = ch;
    }
    if let Some(s) = &args.fake_switch_rate_after {
        opts.switch = Some(fake::parse_switch(s)?);
    }
    if let Some(s) = &args.fake_vanish_after {
        opts.vanish_after = Some(fake::parse_duration(s)?);
    }
    if let Some(s) = &args.fake_duration {
        opts.duration = Some(fake::parse_duration(s)?);
    }
    if opts.rate == 0 || opts.channels == 0 {
        bail!("the fake rate and channels must be positive");
    }
    Fake::new(kind, opts)
}

fn record(args: RecordArgs) -> ExitCode {
    let unsupported = |msg: &str| {
        eprintln!("mnemosyne-capture: {msg}");
        ExitCode::from(EXIT_UNSUPPORTED)
    };
    if args.channels != 1 {
        return unsupported("only --channels 1 is supported");
    }
    if args.rate == 0 {
        return unsupported("--rate must be positive");
    }
    let mut source: Box<dyn Source> = match (&args.fake, &args.target) {
        (Some(spec), _) => match fake_source(spec, &args) {
            Ok(f) => Box::new(f),
            Err(e) => return unsupported(&format!("{e:#}")),
        },
        (None, Some(target)) => {
            return unsupported(&format!(
                "recording {target:?} is not supported on this system yet"
            ))
        }
        (None, None) => return unsupported("give --target <device|system> (or --fake <spec>)"),
    };

    let log = match &args.log {
        Some(path) => match Log::open(path) {
            Ok(l) => l,
            Err(e) => {
                // A recording matters more than its log: say so and carry on.
                eprintln!(
                    "mnemosyne-capture: cannot open the log {}: {e}",
                    path.display()
                );
                Log::none()
            }
        },
        None => Log::none(),
    };

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        if let Err(e) = ctrlc::set_handler(move || stop.store(true, Ordering::SeqCst)) {
            eprintln!("mnemosyne-capture: cannot handle signals: {e}");
            return ExitCode::from(1);
        }
    }
    if args.stdin_stop {
        let stop = Arc::clone(&stop);
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink());
            stop.store(true, Ordering::SeqCst);
        });
    }

    let mut settings = Settings::new(args.rate);
    if let Some(ms) = args.size_interval_ms {
        settings.size_every = Duration::from_millis(ms);
    }
    if let Some(ms) = args.stats_interval_ms {
        settings.stats_every = Duration::from_millis(ms);
    }

    let outcome = Pipeline::create(&args.out, settings, log)
        .and_then(|pipeline| record::run(source.as_mut(), pipeline, &stop))
        .with_context(|| format!("recording to {}", args.out.display()));
    match outcome {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("mnemosyne-capture: {e:#}");
            ExitCode::from(1)
        }
    }
}
