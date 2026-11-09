//! The binary as the backend runs it: spawn, signal, read the WAV it leaves.

use std::path::Path;
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_mnemosyne-capture");

fn capture(args: &[&str]) -> Command {
    let mut c = Command::new(BIN);
    c.args(args).stdin(Stdio::null());
    c
}

fn wait_for_size(path: &Path, bytes: u64) {
    let t = Instant::now();
    while std::fs::metadata(path).map(|m| m.len()).unwrap_or(0) < bytes {
        assert!(
            t.elapsed() < Duration::from_secs(10),
            "{} never grew",
            path.display()
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn wait_exit(mut child: Child) -> Output {
    let t = Instant::now();
    loop {
        if child.try_wait().unwrap().is_some() {
            return child.wait_with_output().unwrap();
        }
        assert!(t.elapsed() < Duration::from_secs(15), "did not exit");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(unix)]
fn signal(child: &Child, sig: i32) {
    assert_eq!(unsafe { libc::kill(child.id() as i32, sig) }, 0);
}

struct Header {
    riff: u32,
    data: u32,
    file_len: usize,
}

fn header_of(path: &Path) -> Header {
    let bytes = std::fs::read(path).unwrap();
    assert!(bytes.len() >= 44);
    assert_eq!(&bytes[0..4], b"RIFF");
    assert_eq!(&bytes[8..16], b"WAVEfmt ");
    assert_eq!(&bytes[36..40], b"data");
    Header {
        riff: u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
        data: u32::from_le_bytes(bytes[40..44].try_into().unwrap()),
        file_len: bytes.len(),
    }
}

fn assert_final(path: &Path) -> u32 {
    let h = header_of(path);
    assert_eq!(
        h.data as usize,
        h.file_len - 44,
        "data size is the rest of the file"
    );
    assert_eq!(
        h.riff as usize,
        h.file_len - 8,
        "RIFF size is the file less 8"
    );
    let r = hound::WavReader::open(path).unwrap();
    assert_eq!(r.spec().sample_rate, 48000);
    assert_eq!(r.spec().channels, 1);
    assert_eq!(r.spec().bits_per_sample, 16);
    assert_eq!(r.len(), h.data / 2);
    h.data / 2
}

#[test]
fn the_header_is_the_placeholder_from_the_first_moment() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let mut child = capture(&["record", "--fake", "silence", out.to_str().unwrap()])
        .spawn()
        .unwrap();
    wait_for_size(&out, 44);
    let head = std::fs::read(&out).unwrap();
    // The backend's wav_header(48000): sizes are unknown (0xFFFFFFFF) until they are fixed.
    assert_eq!(&head[0..4], b"RIFF");
    assert_eq!(&head[4..8], &[0xFF; 4]);
    assert_eq!(&head[24..28], &48000u32.to_le_bytes());
    assert_eq!(&head[40..44], &[0xFF; 4]);
    child.kill().unwrap();
    child.wait().unwrap();
}

#[cfg(unix)]
#[test]
fn sigterm_fixes_the_sizes_and_exits_zero() {
    for sig in [libc::SIGTERM, libc::SIGINT] {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("a.wav");
        let log = dir.path().join("capture.log");
        let child = capture(&[
            "record",
            "--fake",
            "tone",
            "--log",
            log.to_str().unwrap(),
            out.to_str().unwrap(),
        ])
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
        wait_for_size(&out, 44 + 9600 * 2);
        signal(&child, sig);
        let output = wait_exit(child);
        assert_eq!(
            output.status.code(),
            Some(0),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let frames = assert_final(&out);
        assert!(frames >= 9600, "{frames}");
        let text = std::fs::read_to_string(&log).unwrap();
        assert!(text.contains(" started "), "{text}");
        assert!(text.contains(" stopped reason=stopped "), "{text}");
    }
}

#[cfg(unix)]
#[test]
fn a_hard_kill_leaves_a_file_whose_sizes_were_rewritten() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let mut child = capture(&[
        "record",
        "--fake",
        "tone",
        "--size-interval-ms",
        "300",
        out.to_str().unwrap(),
    ])
    .spawn()
    .unwrap();
    std::thread::sleep(Duration::from_millis(1500));
    child.kill().unwrap(); // SIGKILL: no chance to clean up
    child.wait().unwrap();
    let h = header_of(&out);
    assert_ne!(h.data, 0xFFFF_FFFF, "sizes were rewritten while recording");
    assert!(
        h.data as usize >= 48000 / 2 * 2,
        "at least half a second: {}",
        h.data
    );
    assert!(
        h.data as usize <= h.file_len - 44,
        "never ahead of the file"
    );
    assert_eq!(h.riff, h.data + 36);
    // Any WAV reader opens it and finds the audio the sizes cover.
    let r = hound::WavReader::open(&out).unwrap();
    assert_eq!(r.len(), h.data / 2);
    let samples: Vec<i16> = r.into_samples::<i16>().map(|s| s.unwrap()).collect();
    assert!(samples.iter().any(|s| *s != 0));
}

#[test]
fn a_file_is_resampled_downmixed_and_the_end_is_a_clean_stop() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("in.wav");
    let out = dir.path().join("out.wav");
    let spec = hound::WavSpec {
        channels: 2,
        sample_rate: 16000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut w = hound::WavWriter::create(&input, spec).unwrap();
    for i in 0..16000 {
        let v = ((i as f32 * 0.1).sin() * 8000.0) as i16;
        w.write_sample(v).unwrap();
        w.write_sample(v).unwrap();
    }
    w.finalize().unwrap();
    let spec_arg = format!("file:{}", input.display());
    let output = capture(&[
        "record",
        "--fake",
        &spec_arg,
        "--fake-fast",
        out.to_str().unwrap(),
    ])
    .output()
    .unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(assert_final(&out), 48000);
}

#[test]
fn a_rate_switch_keeps_one_valid_file() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let log = dir.path().join("capture.log");
    let output = capture(&[
        "record",
        "--fake",
        "tone:600",
        "--fake-fast",
        "--fake-duration",
        "3s",
        "--fake-switch-rate-after",
        "1s:16000",
        "--log",
        log.to_str().unwrap(),
        out.to_str().unwrap(),
    ])
    .output()
    .unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(assert_final(&out), 3 * 48000);
    let text = std::fs::read_to_string(&log).unwrap();
    assert!(
        text.contains("rate_change source=fake from=48000 to=16000"),
        "{text}"
    );
    assert!(text.contains("reason=source_ended"), "{text}");
}

#[test]
fn a_source_that_disappears_is_a_failure_with_a_valid_file_and_a_log_line() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let log = dir.path().join("capture.log");
    let output = capture(&[
        "record",
        "--fake",
        "tone",
        "--fake-fast",
        "--fake-vanish-after",
        "500ms",
        "--log",
        log.to_str().unwrap(),
        out.to_str().unwrap(),
    ])
    .output()
    .unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("disappeared"));
    assert_eq!(assert_final(&out), 24000);
    let text = std::fs::read_to_string(&log).unwrap();
    assert!(text.contains(" error "), "{text}");
    assert!(text.contains(" stopped reason=error "), "{text}");
}

#[test]
fn stdin_eof_stops_it_when_asked_to() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let mut child = capture(&[
        "record",
        "--fake",
        "tone",
        "--stdin-stop",
        out.to_str().unwrap(),
    ])
    .stdin(Stdio::piped())
    .spawn()
    .unwrap();
    wait_for_size(&out, 44 + 4800 * 2);
    drop(child.stdin.take());
    let output = wait_exit(child);
    assert_eq!(output.status.code(), Some(0));
    assert!(assert_final(&out) >= 4800);
}

#[test]
fn zero_buffers_show_in_the_log_lines() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    let log = dir.path().join("capture.log");
    let output = capture(&[
        "record",
        "--fake",
        "silence",
        "--fake-duration",
        "600ms",
        "--stats-interval-ms",
        "200",
        "--log",
        log.to_str().unwrap(),
        out.to_str().unwrap(),
    ])
    .output()
    .unwrap();
    assert_eq!(output.status.code(), Some(0));
    let text = std::fs::read_to_string(&log).unwrap();
    assert!(text.contains(" stats source=fake "), "{text}");
    assert!(text.contains(" digital_silence source=fake "), "{text}");
    assert!(
        text.contains("fake_buffers=60 fake_zero_buffers=60"),
        "{text}"
    );
}

#[test]
fn a_real_target_is_not_supported_yet_and_usage_errors_exit_two() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("a.wav");
    for args in [
        vec!["record", "--target", "system", out.to_str().unwrap()],
        vec![
            "record",
            "--fake",
            "tone",
            "--channels",
            "2",
            out.to_str().unwrap(),
        ],
        vec!["record", "--fake", "noise", out.to_str().unwrap()],
        vec!["record", out.to_str().unwrap()],
        vec!["record"],
    ] {
        let output = capture(&args).output().unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
    }
    let output = capture(&["record", "--target", "system", out.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&output.stderr).contains("not supported on this system yet"));
    assert!(
        !out.exists(),
        "nothing is written for a source that cannot start"
    );
}

#[test]
fn devices_and_doctor_print_json() {
    let output = capture(&["devices"]).output().unwrap();
    assert_eq!(output.status.code(), Some(0));
    let v: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(v, serde_json::json!([]));
    let output = capture(&["doctor"]).output().unwrap();
    assert_eq!(output.status.code(), Some(0));
    let v: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(v["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(v["os"], std::env::consts::OS);
}
