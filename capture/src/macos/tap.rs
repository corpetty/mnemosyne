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

static LAST_STAGE: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

/// The last Core Audio step `open` reached (for the tests: where a call blocks).
pub fn last_stage() -> String {
    LAST_STAGE.lock().map(|s| s.clone()).unwrap_or_default()
}

/// `MNEMOSYNE_CAPTURE_TRACE=1`: one stderr line per Core Audio step, with the time since the
/// first one. For finding which call waits on a permission prompt (the tester's Mac, CI).
fn trace(stage: &str) {
    use std::sync::OnceLock;
    static START: OnceLock<Option<Instant>> = OnceLock::new();
    if let Ok(mut last) = LAST_STAGE.lock() {
        *last = stage.to_string();
    }
    let start =
        START.get_or_init(|| std::env::var_os("MNEMOSYNE_CAPTURE_TRACE").map(|_| Instant::now()));
    if let Some(start) = start {
        say(&format!(
            "mnemosyne-capture trace +{:.3}s {stage}",
            start.elapsed().as_secs_f64()
        ));
    }
}

/// A line on stderr, written to the handle directly so the test harness's capture (which
/// only wraps the print macros) never hides it.
pub(crate) fn say(line: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr(), "{line}");
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
        let description = composition(Some(tap_uuid), main_uid);
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

fn composition(tap_uuid: Option<&str>, main_uid: Option<&str>) -> CFRetained<CFDictionary> {
    let pid = std::process::id();
    let d = Dict::new();
    d.set_str(
        kAudioAggregateDeviceNameKey,
        &format!("Mnemosyne system audio {pid}"),
    );
    d.set_str(
        kAudioAggregateDeviceUIDKey,
        &format!(
            "com.corpetty.mnemosyne.capture.{pid}.{}",
            tap_uuid.unwrap_or("notap")
        ),
    );
    d.set_bool(kAudioAggregateDeviceIsPrivateKey, true);
    d.set_bool(kAudioAggregateDeviceIsStackedKey, false);

    if let Some(tap_uuid) = tap_uuid {
        d.set_bool(kAudioAggregateDeviceTapAutoStartKey, true);
        let tap = Dict::new();
        tap.set_str(kAudioSubTapUIDKey, tap_uuid);
        tap.set_bool(kAudioSubTapDriftCompensationKey, true);
        let taps = cf_array(&[tap.as_ptr()]);
        d.set(
            kAudioAggregateDeviceTapListKey,
            (&*taps as *const CFArray).cast(),
        );
    }

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
    use std::sync::mpsc::RecvTimeoutError;

    macro_rules! say_fmt {
        ($($arg:tt)*) => { say(&format!($($arg)*)) };
    }

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
        let _ = composition(Some("ABC-123"), Some("BuiltInSpeakerDevice"));
        let _ = composition(Some("ABC-123"), None);
        let _ = composition(None, Some("BuiltInSpeakerDevice"));
    }

    /// Run `f` on a thread of its own; None when it has not returned after `secs`.
    fn within<T: Send + 'static>(secs: u64, f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            let _ = tx.send(f());
        });
        match rx.recv_timeout(Duration::from_secs(secs)) {
            Ok(v) => Some(v),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => panic!("the probe thread panicked"),
        }
    }

    /// Create and destroy an IOProc on `device` (no start): does the call return?
    fn probe_io_proc(device: AudioObjectID) -> Result<()> {
        let mut proc_id: AudioDeviceIOProcID = None;
        // SAFETY: a null client; `io_proc` returns at once for it.
        let status = unsafe {
            AudioDeviceCreateIOProcID(
                device,
                Some(io_proc),
                std::ptr::null_mut(),
                NonNull::from(&mut proc_id),
            )
        };
        check(status, "AudioDeviceCreateIOProcID")?;
        // SAFETY: just created.
        unsafe { AudioDeviceDestroyIOProcID(device, proc_id) };
        Ok(())
    }

    fn probe_aggregate(tap: bool, sub_device: bool) -> Result<()> {
        let tap_uuid = if tap { Some(create_tap()?.1) } else { None };
        let main_uid = if sub_device {
            default_output_uid()
        } else {
            None
        };
        let description = composition(tap_uuid.as_deref(), main_uid.as_deref());
        let mut id: AudioObjectID = 0;
        // SAFETY: a complete composition and an out-pointer.
        let status =
            unsafe { AudioHardwareCreateAggregateDevice(&description, NonNull::from(&mut id)) };
        check(status, "AudioHardwareCreateAggregateDevice")?;
        let r = probe_io_proc(id);
        // SAFETY: ours; the tap (if any) leaks on purpose in this probe.
        unsafe { AudioHardwareDestroyAggregateDevice(id) };
        r
    }

    /// Tell the HAL to use a thread of its own for notifications instead of a run loop
    /// (kAudioHardwarePropertyRunLoop = NULL; the constant is in the deprecated set).
    fn detach_hal_run_loop() -> Result<()> {
        use objc2_core_audio::AudioObjectSetPropertyData;
        const RUN_LOOP: u32 = 0x726e6c70; // 'rnlp'
        let addr = global(RUN_LOOP);
        let null: *const c_void = std::ptr::null();
        // SAFETY: the property takes a CFRunLoopRef; NULL is documented.
        let status = unsafe {
            AudioObjectSetPropertyData(
                SYSTEM_OBJECT,
                NonNull::from(&addr),
                0,
                std::ptr::null(),
                std::mem::size_of::<*const c_void>() as u32,
                NonNull::from(&null).cast(),
            )
        };
        check(status, "kAudioHardwarePropertyRunLoop = NULL")
    }

    type Opened = Result<(String, usize), String>;

    fn try_open(label: &str) -> Option<Opened> {
        let started = Instant::now();
        let outcome = within(30, || {
            SystemTap::open()
                .and_then(|mut tap| {
                    let mut got = 0;
                    for _ in 0..20 {
                        if let Event::Audio(c) = tap.next(Duration::from_millis(100))? {
                            assert!(c.channels >= 1);
                            assert!(c.rate > 0);
                            got += 1;
                        }
                    }
                    Ok((tap.format().to_string(), got))
                })
                .map_err(|e| format!("{e:#}"))
        });
        match &outcome {
            Some(Ok((format, buffers))) => say_fmt!(
                "{label}: tap ok in {:.2}s: {format}, {buffers} buffers in 2 s",
                started.elapsed().as_secs_f64()
            ),
            Some(Err(e)) => say_fmt!("{label}: tap refused: {e}"),
            None => say_fmt!("{label}: no answer after 30 s; last step: {}", last_stage()),
        }
        outcome
    }

    type Probe = Box<dyn FnOnce() -> Result<()> + Send>;

    /// A hosted runner has no "System Audio Recording" grant. Creating the tap and the
    /// aggregate device must work (they need none); creating the IOProc is where TCC asks
    /// (and, with nobody to answer, waits). The test proves the first two and accepts the
    /// wait at the third; an answer must be Ok or a clear error, never a crash.
    #[test]
    fn opening_the_system_tap_works_or_waits_for_the_permission() {
        std::env::set_var("MNEMOSYNE_CAPTURE_TRACE", "1");
        say_fmt!("doctor: {}", crate::doctor::report());
        say_fmt!(
            "devices: {}",
            serde_json::to_string(&crate::macos::devices().unwrap()).unwrap()
        );
        // Where does an IOProc wait? Diagnostics only, printed for the CI log.
        let probes: [(&str, Probe); 4] = [
            (
                "IOProc on the default output device",
                Box::new(|| {
                    let id = coreaudio::default_device(false)
                        .ok_or_else(|| anyhow::anyhow!("no default output"))?;
                    probe_io_proc(id)
                }),
            ),
            (
                "IOProc on an aggregate of the default output, no tap",
                Box::new(|| probe_aggregate(false, true)),
            ),
            (
                "IOProc on an aggregate of the tap alone",
                Box::new(|| probe_aggregate(true, false)),
            ),
            (
                "IOProc on an aggregate of the tap and the default output",
                Box::new(|| probe_aggregate(true, true)),
            ),
        ];
        for (label, probe) in probes {
            let started = Instant::now();
            match within(10, probe) {
                Some(Ok(())) => {
                    say_fmt!("{label}: ok in {:.2}s", started.elapsed().as_secs_f64())
                }
                Some(Err(e)) => say_fmt!("{label}: error: {e:#}"),
                None => say_fmt!("{label}: no answer after 10 s"),
            }
        }

        let mut outcome = try_open("open");
        if outcome.is_none() {
            say_fmt!("detaching the HAL run loop: {:?}", detach_hal_run_loop());
            outcome = try_open("open after detaching the run loop");
        }
        match outcome {
            Some(Ok(_)) => {}
            Some(Err(e)) => assert!(e.contains("system audio"), "{e}"),
            None => {
                let stage = last_stage();
                assert!(
                    stage.starts_with("rate ") || stage.starts_with("AudioDeviceStart"),
                    "blocked before the IOProc step, at: {stage}"
                );
            }
        }
    }
}
