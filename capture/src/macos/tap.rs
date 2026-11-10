//! System audio: a global Core Audio process tap (every process, whichever output device it
//! plays to) read through a private aggregate device whose IOProc hands the tap's buffers to
//! the recorder. References: cpal's `host/coreaudio/macos/loopback.rs`, insidegui/AudioCap,
//! makeusabrew/audiotee.
//!
//! The permission prompt ("System Audio Recording Only") appears when the aggregate device
//! STARTS, not when the tap is made; a denied or unanswered tap delivers exact zeros, which the
//! recorder counts (`digital_silence` in the log). Private aggregate devices and taps are
//! owned by this process's HAL client and are destroyed on drop (also after SIGTERM); when the
//! process is killed outright, coreaudiod drops the client and with it what the client made.

use super::coreaudio::{self, check, global, Listener, OSStatus, SYSTEM_OBJECT};
use crate::source::{Chunk, Event, Source};
use anyhow::{Context, Result};
use objc2::rc::Retained;
use objc2::AnyThread;
use objc2_core_audio::{
    kAudioAggregateDeviceIsPrivateKey, kAudioAggregateDeviceIsStackedKey,
    kAudioAggregateDeviceMainSubDeviceKey, kAudioAggregateDeviceNameKey,
    kAudioAggregateDeviceSubDeviceListKey, kAudioAggregateDeviceTapAutoStartKey,
    kAudioAggregateDeviceTapListKey, kAudioAggregateDeviceUIDKey,
    kAudioDevicePropertyNominalSampleRate, kAudioHardwarePropertyDefaultOutputDevice,
    kAudioSubDeviceUIDKey, kAudioSubTapDriftCompensationKey, kAudioSubTapUIDKey,
    kAudioTapPropertyFormat, AudioDeviceCreateIOProcID, AudioDeviceDestroyIOProcID,
    AudioDeviceIOProcID, AudioDeviceStart, AudioDeviceStop, AudioHardwareCreateAggregateDevice,
    AudioHardwareCreateProcessTap, AudioHardwareDestroyAggregateDevice,
    AudioHardwareDestroyProcessTap, AudioObjectID, AudioObjectPropertyAddress, CATapDescription,
    CATapMuteBehavior,
};
use objc2_core_audio_types::{AudioBufferList, AudioStreamBasicDescription, AudioTimeStamp};
use objc2_core_foundation::{
    kCFAllocatorDefault, kCFTypeArrayCallBacks, kCFTypeDictionaryKeyCallBacks,
    kCFTypeDictionaryValueCallBacks, CFArray, CFBoolean, CFDictionary, CFMutableDictionary,
    CFRetained, CFString,
};
use objc2_foundation::{NSArray, NSNumber, NSString};
use std::ffi::{c_void, CStr};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const NAME: &str = "System audio (all apps)";

/// No buffer for this long from a started aggregate device: something is wrong with it (its
/// output device went away without a default change); it is built again.
const STALL: Duration = Duration::from_secs(10);
const MAX_STALL_REBUILDS: u32 = 3;

/// `MNEMOSYNE_CAPTURE_TRACE=1`: one stderr line per Core Audio step, with the time since the
/// first one. For finding which call waits on a permission prompt (the tester's Mac, CI).
fn trace(stage: &str) {
    use std::sync::OnceLock;
    static START: OnceLock<Option<Instant>> = OnceLock::new();
    let start =
        START.get_or_init(|| std::env::var_os("MNEMOSYNE_CAPTURE_TRACE").map(|_| Instant::now()));
    if let Some(start) = start {
        eprintln!(
            "mnemosyne-capture trace +{:.3}s {stage}",
            start.elapsed().as_secs_f64()
        );
    }
}

/// What the IOProc and the listeners reach: the channel to the recorder and the current rate.
struct Shared {
    tx: Sender<Chunk>,
    rate: AtomicU32,
    default_output_changed: AtomicBool,
}

/// Field order matters: fields drop in declaration order, and the aggregate device and the
/// listener point at `shared`, so they come before it.
pub struct SystemTap {
    aggregate: Option<Aggregate>,
    _default_listener: Listener,
    tap: AudioObjectID,
    /// The UID of the output device the aggregate was built around (None: none existed).
    main_uid: Option<String>,
    rx: Receiver<Chunk>,
    shared: Arc<Shared>,
    last_audio: Instant,
    stall_rebuilds: u32,
    format: String,
}

struct Aggregate {
    id: AudioObjectID,
    proc_id: AudioDeviceIOProcID,
    started: bool,
    _rate_listener: Option<Listener>,
}

impl SystemTap {
    pub fn open() -> Result<Self> {
        let (tx, rx) = channel();
        let shared = Arc::new(Shared {
            tx,
            rate: AtomicU32::new(0),
            default_output_changed: AtomicBool::new(false),
        });
        let (tap, tap_uuid, format) = create_tap().context("cannot tap the system audio")?;
        let main_uid = default_output_uid();
        trace(&format!("default output: {main_uid:?}"));
        let aggregate = match Aggregate::create(&tap_uuid, main_uid.as_deref(), &shared) {
            Ok(a) => a,
            Err(e) => {
                // SAFETY: the tap was created above and nothing else refers to it.
                unsafe { AudioHardwareDestroyProcessTap(tap) };
                return Err(e.context("cannot record the system audio"));
            }
        };
        trace("adding the default-output listener");
        // SAFETY: `shared` outlives the listener (both are fields, the listener dropped first).
        let default_listener = unsafe {
            Listener::add(
                SYSTEM_OBJECT,
                global(kAudioHardwarePropertyDefaultOutputDevice),
                on_default_output_changed,
                Arc::as_ptr(&shared) as *mut c_void,
            )
        };
        let default_listener = match default_listener {
            Ok(l) => l,
            Err(e) => {
                drop(aggregate);
                // SAFETY: as above.
                unsafe { AudioHardwareDestroyProcessTap(tap) };
                return Err(e);
            }
        };
        trace("open");
        Ok(Self {
            aggregate: Some(aggregate),
            _default_listener: default_listener,
            tap,
            main_uid,
            rx,
            shared,
            last_audio: Instant::now(),
            stall_rebuilds: 0,
            format,
        })
    }

    /// What the tap delivers (channels and rate), for the log.
    pub fn format(&self) -> &str {
        &self.format
    }

    fn rebuild(&mut self, why: &str) -> Result<String> {
        let (tap, tap_uuid, format) = create_tap().context("cannot tap the system audio again")?;
        let main_uid = default_output_uid();
        let aggregate = Aggregate::create(&tap_uuid, main_uid.as_deref(), &self.shared);
        let aggregate = match aggregate {
            Ok(a) => a,
            Err(e) => {
                // SAFETY: the new tap, not yet owned by anything.
                unsafe { AudioHardwareDestroyProcessTap(tap) };
                return Err(e.context("cannot record the system audio again"));
            }
        };
        // Tear the old one down only once the new one is up: no gap when it works.
        self.aggregate = None;
        // SAFETY: our old tap; its aggregate device is gone.
        unsafe { AudioHardwareDestroyProcessTap(self.tap) };
        self.tap = tap;
        self.aggregate = Some(aggregate);
        self.format = format;
        self.last_audio = Instant::now();
        let note = format!(
            "source=system reason={why} output={} format=\"{}\"",
            quoted(main_uid.as_deref().unwrap_or("none")),
            self.format
        );
        self.main_uid = main_uid;
        Ok(note)
    }
}

fn quoted(s: &str) -> String {
    format!("{s:?}")
}

impl Source for SystemTap {
    fn name(&self) -> String {
        NAME.to_string()
    }

    fn next(&mut self, timeout: Duration) -> Result<Event> {
        if self
            .shared
            .default_output_changed
            .swap(false, Ordering::SeqCst)
        {
            let now = default_output_uid();
            if now != self.main_uid {
                return Ok(Event::Note(self.rebuild("default_output_changed")?));
            }
        }
        match self.rx.recv_timeout(timeout) {
            Ok(chunk) => {
                self.last_audio = Instant::now();
                self.stall_rebuilds = 0;
                Ok(Event::Audio(chunk))
            }
            Err(RecvTimeoutError::Timeout) => {
                if self.last_audio.elapsed() >= STALL && self.stall_rebuilds < MAX_STALL_REBUILDS {
                    self.stall_rebuilds += 1;
                    let note = self.rebuild("stalled")?;
                    return Ok(Event::Note(note));
                }
                Ok(Event::Idle)
            }
            // Impossible: `shared` holds a sender for as long as we live.
            Err(RecvTimeoutError::Disconnected) => Ok(Event::Idle),
        }
    }
}

impl Drop for SystemTap {
    fn drop(&mut self) {
        // The aggregate device first (it uses the tap), then the tap.
        self.aggregate = None;
        // SAFETY: our tap, no longer used by any device.
        unsafe { AudioHardwareDestroyProcessTap(self.tap) };
    }
}

/// A stereo global tap of every process (none excluded: this process never plays audio),
/// private, leaving what is played audible.
fn create_tap() -> Result<(AudioObjectID, String, String)> {
    let excluded: Retained<NSArray<NSNumber>> = NSArray::new();
    // SAFETY: documented initializer and setters on a fresh CATapDescription.
    let desc = unsafe {
        let desc = CATapDescription::initStereoGlobalTapButExcludeProcesses(
            CATapDescription::alloc(),
            &excluded,
        );
        desc.setName(&NSString::from_str(&format!(
            "Mnemosyne system audio {}",
            std::process::id()
        )));
        desc.setPrivate(true);
        desc.setMuteBehavior(CATapMuteBehavior::Unmuted);
        desc
    };
    let mut tap: AudioObjectID = 0;
    trace("AudioHardwareCreateProcessTap");
    // SAFETY: a valid description and an out-pointer.
    let status = unsafe { AudioHardwareCreateProcessTap(Some(&desc), &mut tap) };
    trace(&format!(
        "AudioHardwareCreateProcessTap -> {}",
        coreaudio::status_text(status)
    ));
    check(status, "AudioHardwareCreateProcessTap")?;
    // SAFETY: the description's UUID is set by its initializer.
    let uuid = unsafe { desc.UUID().UUIDString() }.to_string();
    let format = match coreaudio::get_pod::<AudioStreamBasicDescription>(
        tap,
        &global(kAudioTapPropertyFormat),
    ) {
        Ok(f) => format!("{} ch {} Hz", f.mChannelsPerFrame, f.mSampleRate),
        Err(_) => "unknown".to_string(),
    };
    Ok((tap, uuid, format))
}

fn default_output_uid() -> Option<String> {
    coreaudio::default_device(false).and_then(|id| coreaudio::uid(id).ok())
}

impl Aggregate {
    /// A private aggregate device: the tap (auto-started, drift-compensated), with the default
    /// output device as its main sub-device when there is one (the clock to follow), as
    /// AudioCap builds it. Without an output device (a bare CI runner) it is the tap alone,
    /// as cpal builds it.
    fn create(tap_uuid: &str, main_uid: Option<&str>, shared: &Arc<Shared>) -> Result<Self> {
        let description = composition(tap_uuid, main_uid);
        let mut id: AudioObjectID = 0;
        trace("AudioHardwareCreateAggregateDevice");
        // SAFETY: a complete composition dictionary and an out-pointer.
        let status =
            unsafe { AudioHardwareCreateAggregateDevice(&description, NonNull::from(&mut id)) };
        trace(&format!(
            "AudioHardwareCreateAggregateDevice -> {} id={id}",
            coreaudio::status_text(status)
        ));
        check(status, "AudioHardwareCreateAggregateDevice")?;
        let mut agg = Self {
            id,
            proc_id: None,
            started: false,
            _rate_listener: None,
        };
        let rate = coreaudio::nominal_rate(id).unwrap_or(0.0);
        let rate = if rate > 0.0 {
            rate as u32
        } else {
            crate::DEFAULT_RATE
        };
        shared.rate.store(rate, Ordering::SeqCst);
        let data = Arc::as_ptr(shared) as *mut c_void;
        // SAFETY: `shared` outlives the aggregate (the recorder owns both; the aggregate is
        // dropped first).
        agg._rate_listener = unsafe {
            Listener::add(
                id,
                global(kAudioDevicePropertyNominalSampleRate),
                on_rate_changed,
                data,
            )
        }
        .ok();
        trace(&format!(
            "rate {} Hz; AudioDeviceCreateIOProcID",
            shared.rate.load(Ordering::SeqCst)
        ));
        let mut proc_id: AudioDeviceIOProcID = None;
        // SAFETY: a valid device and an IOProc whose client data lives as long as it does.
        let status = unsafe {
            AudioDeviceCreateIOProcID(id, Some(io_proc), data, NonNull::from(&mut proc_id))
        };
        check(status, "AudioDeviceCreateIOProcID")?;
        agg.proc_id = proc_id;
        trace("AudioDeviceStart");
        // SAFETY: the IOProc was created on this device.
        let status = unsafe { AudioDeviceStart(id, proc_id) };
        trace(&format!(
            "AudioDeviceStart -> {}",
            coreaudio::status_text(status)
        ));
        check(status, "AudioDeviceStart")?;
        agg.started = true;
        Ok(agg)
    }
}

impl Drop for Aggregate {
    fn drop(&mut self) {
        // SAFETY: each call undoes one that succeeded in `create`, in reverse order. Statuses
        // are ignored: nothing can be done about them while shutting down.
        unsafe {
            if self.started {
                AudioDeviceStop(self.id, self.proc_id);
            }
            if self.proc_id.is_some() {
                AudioDeviceDestroyIOProcID(self.id, self.proc_id);
            }
            self._rate_listener = None;
            AudioHardwareDestroyAggregateDevice(self.id);
        }
    }
}

/// Called on the HAL's IO thread with the tap's buffers: Float32, interleaved in one buffer
/// or one buffer per channel. Allocation here is fine for us (pw-record parity, not a DAW).
unsafe extern "C-unwind" fn io_proc(
    _device: AudioObjectID,
    _now: NonNull<AudioTimeStamp>,
    input: NonNull<AudioBufferList>,
    _input_time: NonNull<AudioTimeStamp>,
    _output: NonNull<AudioBufferList>,
    _output_time: NonNull<AudioTimeStamp>,
    client: *mut c_void,
) -> OSStatus {
    if client.is_null() {
        return 0;
    }
    let shared = &*(client as *const Shared);
    let list = input.as_ref();
    let n = list.mNumberBuffers as usize;
    if n == 0 {
        return 0;
    }
    let buffers = std::slice::from_raw_parts(list.mBuffers.as_ptr(), n);
    let planes: Vec<(u16, &[f32])> = buffers
        .iter()
        .filter(|b| !b.mData.is_null() && b.mDataByteSize >= 4)
        .map(|b| {
            let samples =
                std::slice::from_raw_parts(b.mData as *const f32, b.mDataByteSize as usize / 4);
            (b.mNumberChannels.clamp(1, u16::MAX as u32) as u16, samples)
        })
        .collect();
    let Some((channels, data)) = interleave(&planes) else {
        return 0;
    };
    let _ = shared.tx.send(Chunk {
        source: "system",
        rate: shared.rate.load(Ordering::Relaxed),
        channels,
        data,
    });
    0
}

/// One buffer: already interleaved. Several: one per channel group, interleaved here.
fn interleave(planes: &[(u16, &[f32])]) -> Option<(u16, Vec<f32>)> {
    match planes {
        [] => None,
        [(channels, samples)] => Some((*channels, samples.to_vec())),
        many => {
            let frames = many
                .iter()
                .map(|(ch, s)| s.len() / usize::from(*ch))
                .min()?;
            let total: usize = many.iter().map(|(ch, _)| usize::from(*ch)).sum();
            if frames == 0 || total == 0 || total > usize::from(u16::MAX) {
                return None;
            }
            let mut out = vec![0.0f32; frames * total];
            let mut offset = 0;
            for (ch, samples) in many {
                let ch = usize::from(*ch);
                for f in 0..frames {
                    for c in 0..ch {
                        out[f * total + offset + c] = samples[f * ch + c];
                    }
                }
                offset += ch;
            }
            Some((total as u16, out))
        }
    }
}

unsafe extern "C-unwind" fn on_rate_changed(
    device: AudioObjectID,
    _count: u32,
    _addresses: NonNull<AudioObjectPropertyAddress>,
    client: *mut c_void,
) -> OSStatus {
    if client.is_null() {
        return 0;
    }
    let shared = &*(client as *const Shared);
    if let Ok(rate) = coreaudio::nominal_rate(device) {
        if rate > 0.0 {
            shared.rate.store(rate as u32, Ordering::SeqCst);
        }
    }
    0
}

unsafe extern "C-unwind" fn on_default_output_changed(
    _object: AudioObjectID,
    _count: u32,
    _addresses: NonNull<AudioObjectPropertyAddress>,
    client: *mut c_void,
) -> OSStatus {
    if client.is_null() {
        return 0;
    }
    let shared = &*(client as *const Shared);
    shared.default_output_changed.store(true, Ordering::SeqCst);
    0
}

// --- the composition dictionary -------------------------------------------------------------

struct Dict(CFRetained<CFMutableDictionary>);

impl Dict {
    fn new() -> Self {
        // SAFETY: the default allocator and the standard CFType callbacks.
        let d = unsafe {
            CFMutableDictionary::new(
                kCFAllocatorDefault,
                0,
                &kCFTypeDictionaryKeyCallBacks,
                &kCFTypeDictionaryValueCallBacks,
            )
        }
        .expect("CFDictionaryCreateMutable");
        Self(d)
    }

    fn set(&self, key: &CStr, value: *const c_void) {
        let key = CFString::from_str(key.to_str().expect("ASCII key"));
        // SAFETY: a CFString key and a CFType value; the dictionary retains both.
        unsafe {
            CFMutableDictionary::set_value(Some(&self.0), (&*key as *const CFString).cast(), value)
        };
    }

    fn set_str(&self, key: &CStr, value: &str) {
        let v = CFString::from_str(value);
        self.set(key, (&*v as *const CFString).cast());
    }

    fn set_bool(&self, key: &CStr, value: bool) {
        self.set(key, (CFBoolean::new(value) as *const CFBoolean).cast());
    }

    fn as_ptr(&self) -> *const c_void {
        (&*self.0 as *const CFMutableDictionary).cast()
    }
}

fn cf_array(items: &[*const c_void]) -> CFRetained<CFArray> {
    let mut items: Vec<*const c_void> = items.to_vec();
    // SAFETY: `items` are CFType pointers; the array retains them.
    unsafe {
        CFArray::new(
            kCFAllocatorDefault,
            items.as_mut_ptr(),
            items.len() as isize,
            &kCFTypeArrayCallBacks,
        )
    }
    .expect("CFArrayCreate")
}

fn composition(tap_uuid: &str, main_uid: Option<&str>) -> CFRetained<CFDictionary> {
    let pid = std::process::id();
    let d = Dict::new();
    d.set_str(
        kAudioAggregateDeviceNameKey,
        &format!("Mnemosyne system audio {pid}"),
    );
    d.set_str(
        kAudioAggregateDeviceUIDKey,
        &format!("com.corpetty.mnemosyne.capture.{pid}.{tap_uuid}"),
    );
    d.set_bool(kAudioAggregateDeviceIsPrivateKey, true);
    d.set_bool(kAudioAggregateDeviceIsStackedKey, false);
    d.set_bool(kAudioAggregateDeviceTapAutoStartKey, true);

    let tap = Dict::new();
    tap.set_str(kAudioSubTapUIDKey, tap_uuid);
    tap.set_bool(kAudioSubTapDriftCompensationKey, true);
    let taps = cf_array(&[tap.as_ptr()]);
    d.set(
        kAudioAggregateDeviceTapListKey,
        (&*taps as *const CFArray).cast(),
    );

    if let Some(uid) = main_uid {
        let sub = Dict::new();
        sub.set_str(kAudioSubDeviceUIDKey, uid);
        let subs = cf_array(&[sub.as_ptr()]);
        d.set(
            kAudioAggregateDeviceSubDeviceListKey,
            (&*subs as *const CFArray).cast(),
        );
        d.set_str(kAudioAggregateDeviceMainSubDeviceKey, uid);
    }
    // SAFETY: a CFMutableDictionary is a CFDictionary.
    unsafe { CFRetained::cast_unchecked::<CFDictionary>(d.0) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn planar_buffers_are_interleaved() {
        let l = [1.0, 2.0, 3.0];
        let r = [10.0, 20.0, 30.0];
        let (ch, data) = interleave(&[(1, &l), (1, &r)]).unwrap();
        assert_eq!(ch, 2);
        assert_eq!(data, [1.0, 10.0, 2.0, 20.0, 3.0, 30.0]);
        let (ch, data) = interleave(&[(2, &[1.0, 2.0, 3.0, 4.0])]).unwrap();
        assert_eq!(ch, 2);
        assert_eq!(data, [1.0, 2.0, 3.0, 4.0]);
        assert!(interleave(&[]).is_none());
    }

    #[test]
    fn the_composition_builds() {
        let _ = composition("ABC-123", Some("BuiltInSpeakerDevice"));
        let _ = composition("ABC-123", None);
    }

    /// A hosted runner has no TCC grant and maybe no output device. Whatever happens, it must
    /// be an Ok tap or a clear error, never a crash or a hang.
    #[test]
    fn opening_the_system_tap_works_or_fails_clearly() {
        std::env::set_var("MNEMOSYNE_CAPTURE_TRACE", "1");
        eprintln!("doctor: {}", crate::doctor::report());
        eprintln!(
            "devices: {}",
            serde_json::to_string(&crate::macos::devices().unwrap()).unwrap()
        );
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            let outcome = SystemTap::open().and_then(|mut tap| {
                let mut got = 0;
                for _ in 0..20 {
                    if let Event::Audio(c) = tap.next(Duration::from_millis(100))? {
                        assert!(c.channels >= 1);
                        assert!(c.rate > 0);
                        got += 1;
                    }
                }
                Ok((tap.format().to_string(), got))
            });
            let _ = tx.send(outcome.map_err(|e| format!("{e:#}")));
        });
        match rx.recv_timeout(Duration::from_secs(30)) {
            Ok(Ok((format, buffers))) => eprintln!("tap ok: {format}, {buffers} buffers in 2 s"),
            Ok(Err(e)) => {
                eprintln!("tap refused: {e}");
                assert!(e.contains("system audio"), "{e}");
            }
            Err(_) => panic!(
                "opening the system tap hung for 30 s (a permission prompt? see the trace lines)"
            ),
        }
    }
}
