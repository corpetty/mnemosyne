//! The microphone through Apple's voice processing (`record --target <uid> --voice-processing`):
//! a VoiceProcessingIO AudioUnit (`kAudioUnitSubType_VoiceProcessingIO`) instead of cpal. It
//! takes out of the microphone what the Mac itself plays (the other people on a call on laptop
//! speakers) and turns noise down. What comes out is the same `Chunk`s mic.rs hands over, so
//! the recorder, the resampler and the WAV writer do not change.
//!
//! The setup follows WebRTC's voice_processing_audio_unit.mm and Chromium's VPIO input on the
//! Mac, and Apple's WWDC23 "What's new in voice processing" (ducking):
//! - VPIO is a full-duplex unit: input (element 1) and output (element 0) are both enabled.
//!   Nothing is played: the output's render callback hands it silence (and says so with
//!   `OutputIsSilence`), as Chromium does. The echo reference is what the output device plays.
//! - `kAudioOutputUnitProperty_CurrentDevice` is the requested microphone on element 1 and the
//!   default output device on element 0. When the default output changes (AirPods), the unit
//!   is built again around the new one.
//! - Ducking: by default VPIO turns down everything else the Mac plays.
//!   `kAUVoiceIOProperty_OtherAudioDuckingConfiguration` (macOS 14+) is set to no advanced
//!   ducking at the minimum level; where it cannot be set a `note` line says so.
//! - AGC (`kAUVoiceIOProperty_VoiceProcessingEnableAGC`, on by default) is turned off, so levels
//!   stay comparable to the plain microphone.
//! - Our format, Float32 mono at the unit's rate, is set on the input element's output scope
//!   (and on the output element's input scope, for the silence). The unit converts from the
//!   device's own format; when the device switches rate (a Bluetooth profile change) and the
//!   unit stops delivering, it is built again at the new rate and the file sees a `rate_change`.
//! - A process can run one voice processor (`kAudioUnitErr_MultipleVoiceProcessors`): a
//!   rebuild disposes of the old unit first.
//!
//! In the IO callback: one buffer allocated per callback and a channel send, as mic.rs does
//! (cpal's callback hands over `data.to_vec()`); no locks.

use super::coreaudio::{self, check, global, Listener, OSStatus, SYSTEM_OBJECT};
use crate::source::{Chunk, Event, Source};
use anyhow::{anyhow, bail, Result};
use objc2_audio_toolbox::{
    kAUVoiceIOProperty_OtherAudioDuckingConfiguration, kAUVoiceIOProperty_VoiceProcessingEnableAGC,
    kAudioOutputUnitProperty_CurrentDevice, kAudioOutputUnitProperty_EnableIO,
    kAudioOutputUnitProperty_SetInputCallback, kAudioUnitManufacturer_Apple,
    kAudioUnitProperty_SetRenderCallback, kAudioUnitProperty_ShouldAllocateBuffer,
    kAudioUnitProperty_StreamFormat, kAudioUnitScope_Global, kAudioUnitScope_Input,
    kAudioUnitScope_Output, kAudioUnitSubType_VoiceProcessingIO, kAudioUnitType_Output,
    AURenderCallbackStruct, AUVoiceIOOtherAudioDuckingConfiguration,
    AUVoiceIOOtherAudioDuckingLevel, AudioComponentDescription, AudioComponentFindNext,
    AudioComponentInstanceDispose, AudioComponentInstanceNew, AudioOutputUnitStart,
    AudioOutputUnitStop, AudioUnit, AudioUnitElement, AudioUnitGetProperty, AudioUnitInitialize,
    AudioUnitPropertyID, AudioUnitRender, AudioUnitRenderActionFlags, AudioUnitScope,
    AudioUnitSetProperty, AudioUnitUninitialize,
};
use objc2_core_audio::{
    kAudioHardwarePropertyDefaultOutputDevice, AudioObjectID, AudioObjectPropertyAddress,
};
use objc2_core_audio_types::{
    kAudioFormatFlagsNativeFloatPacked, kAudioFormatLinearPCM, AudioBuffer, AudioBufferList,
    AudioStreamBasicDescription, AudioTimeStamp,
};
use std::collections::VecDeque;
use std::ffi::c_void;
use std::mem::{size_of, MaybeUninit};
use std::ptr::{null_mut, NonNull};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// The unit's elements: 1 is the microphone side, 0 the speaker side.
const INPUT: AudioUnitElement = 1;
const OUTPUT: AudioUnitElement = 0;

/// A running unit delivers a buffer every few milliseconds, silence included. Nothing for this
/// long: it stopped (a Bluetooth device switching profile under it), so it is built again.
const STALL: Duration = Duration::from_secs(3);
/// Rebuilds in a row that brought no audio, after which it only waits (as tap.rs).
const MAX_STALL_REBUILDS: u32 = 3;
/// How long the device may be away while the unit is built again (as mic.rs).
const REOPEN_ATTEMPTS: u32 = 25;
const REOPEN_PAUSE: Duration = Duration::from_millis(200);

fn description() -> AudioComponentDescription {
    AudioComponentDescription {
        componentType: kAudioUnitType_Output,
        componentSubType: kAudioUnitSubType_VoiceProcessingIO,
        componentManufacturer: kAudioUnitManufacturer_Apple,
        componentFlags: 0,
        componentFlagsMask: 0,
    }
}

/// Whether this Mac has the voice-processing unit (for `doctor`). Opens nothing.
pub fn available() -> bool {
    let desc = description();
    // SAFETY: a null "start after" component and a valid description.
    let component = unsafe { AudioComponentFindNext(null_mut(), NonNull::from(&desc)) };
    !component.is_null()
}

/// The rate to run at: the device side of the unit, else the device's nominal rate, else ours.
fn pick_rate(unit_side: f64, device_nominal: Option<f64>) -> u32 {
    for rate in [Some(unit_side), device_nominal].into_iter().flatten() {
        if rate.is_finite() && (1.0..=768_000.0).contains(&rate) {
            return rate.round() as u32;
        }
    }
    crate::DEFAULT_RATE
}

/// Float32, packed, one channel.
fn mono_f32(rate: u32) -> AudioStreamBasicDescription {
    AudioStreamBasicDescription {
        mSampleRate: f64::from(rate),
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagsNativeFloatPacked,
        mBytesPerPacket: 4,
        mFramesPerPacket: 1,
        mBytesPerFrame: 4,
        mChannelsPerFrame: 1,
        mBitsPerChannel: 32,
        mReserved: 0,
    }
}

fn set<T>(
    unit: AudioUnit,
    property: AudioUnitPropertyID,
    scope: AudioUnitScope,
    element: AudioUnitElement,
    value: &T,
) -> OSStatus {
    // SAFETY: `value` is `size_of::<T>()` readable bytes of the type the property takes.
    unsafe {
        AudioUnitSetProperty(
            unit,
            property,
            scope,
            element,
            (value as *const T).cast(),
            size_of::<T>() as u32,
        )
    }
}

fn get<T: Copy>(
    unit: AudioUnit,
    property: AudioUnitPropertyID,
    scope: AudioUnitScope,
    element: AudioUnitElement,
) -> Result<T, OSStatus> {
    let mut value = MaybeUninit::<T>::uninit();
    let mut size = size_of::<T>() as u32;
    // SAFETY: `value` has room for `size` bytes; the unit writes at most that many.
    let status = unsafe {
        AudioUnitGetProperty(
            unit,
            property,
            scope,
            element,
            NonNull::new_unchecked(value.as_mut_ptr().cast()),
            NonNull::from(&mut size),
        )
    };
    if status != 0 {
        return Err(status);
    }
    if size as usize != size_of::<T>() {
        return Err(-50); // paramErr: not the size asked for
    }
    // SAFETY: filled (status and size checked).
    Ok(unsafe { value.assume_init() })
}

/// What the IO callbacks reach: the unit (to render its input into our buffer), the rate it
/// was set up at and the channel to the recorder.
struct Callback {
    unit: AudioUnit,
    rate: u32,
    tx: Sender<Chunk>,
}

/// One VoiceProcessingIO instance, stopped and disposed of when dropped.
struct Unit {
    unit: AudioUnit,
    initialized: bool,
    started: bool,
    rate: u32,
    /// The output device it takes its echo reference from (None: none was set).
    output_uid: Option<String>,
    /// Boxed for a stable address; dropped after the unit is disposed of (Drop runs first).
    callback: Box<Callback>,
}

// SAFETY: the unit is only configured, started, stopped and disposed of from the thread that
// owns the source (the recorder's); Core Audio calls the callbacks on its own IO thread with
// the boxed `Callback`, which lives until the unit is gone.
unsafe impl Send for Unit {}

impl Unit {
    /// Build, configure and start the unit on `device`. Things that only make it work less
    /// well (ducking, AGC, the reference device) go to `notes`; anything else is an error.
    fn open(device: AudioObjectID, tx: Sender<Chunk>, notes: &mut Vec<String>) -> Result<Self> {
        let desc = description();
        // SAFETY: as in `available`.
        let component = unsafe { AudioComponentFindNext(null_mut(), NonNull::from(&desc)) };
        if component.is_null() {
            bail!("this Mac has no voice-processing audio unit");
        }
        let mut instance: AudioUnit = null_mut();
        // SAFETY: a component found above and an out-pointer.
        let status = unsafe { AudioComponentInstanceNew(component, NonNull::from(&mut instance)) };
        check(status, "AudioComponentInstanceNew (voice processing)")?;
        if instance.is_null() {
            bail!("AudioComponentInstanceNew returned no unit");
        }
        // From here on, Drop cleans up whatever fails.
        let mut unit = Self {
            unit: instance,
            initialized: false,
            started: false,
            rate: 0,
            output_uid: None,
            callback: Box::new(Callback {
                unit: instance,
                rate: 0,
                tx,
            }),
        };
        let u = unit.unit;
        let on: u32 = 1;
        check(
            set(
                u,
                kAudioOutputUnitProperty_EnableIO,
                kAudioUnitScope_Input,
                INPUT,
                &on,
            ),
            "enabling the voice-processing input",
        )?;
        check(
            set(
                u,
                kAudioOutputUnitProperty_EnableIO,
                kAudioUnitScope_Output,
                OUTPUT,
                &on,
            ),
            "enabling the voice-processing output",
        )?;
        check(
            set(
                u,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                INPUT,
                &device,
            ),
            "choosing the microphone",
        )?;
        // The echo reference: the default output device (what the other people sound like).
        if let Some(out) = coreaudio::default_device(false) {
            let status = set(
                u,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                OUTPUT,
                &out,
            );
            if status == 0 {
                unit.output_uid = coreaudio::uid(out).ok();
            } else {
                notes.push(format!(
                    "source=mic voice_processing_output=unset status=\"{}\"",
                    coreaudio::status_text(status)
                ));
            }
        }
        let device_side = get::<AudioStreamBasicDescription>(
            u,
            kAudioUnitProperty_StreamFormat,
            kAudioUnitScope_Input,
            INPUT,
        )
        .map(|f| f.mSampleRate)
        .unwrap_or(0.0);
        let rate = pick_rate(device_side, coreaudio::nominal_rate(device).ok());
        let format = mono_f32(rate);
        check(
            set(
                u,
                kAudioUnitProperty_StreamFormat,
                kAudioUnitScope_Output,
                INPUT,
                &format,
            ),
            &format!("setting the microphone format (Float32 mono {rate} Hz)"),
        )?;
        check(
            set(
                u,
                kAudioUnitProperty_StreamFormat,
                kAudioUnitScope_Input,
                OUTPUT,
                &format,
            ),
            &format!("setting the output format (Float32 mono {rate} Hz)"),
        )?;
        // We render the input into a buffer of our own (as WebRTC does); a failure only
        // costs the unit a buffer it does not need.
        let off: u32 = 0;
        let _ = set(
            u,
            kAudioUnitProperty_ShouldAllocateBuffer,
            kAudioUnitScope_Output,
            INPUT,
            &off,
        );
        unit.rate = rate;
        unit.callback.rate = rate;
        let refcon = (&*unit.callback as *const Callback)
            .cast_mut()
            .cast::<c_void>();
        let input = AURenderCallbackStruct {
            inputProc: Some(input_proc),
            inputProcRefCon: refcon,
        };
        check(
            set(
                u,
                kAudioOutputUnitProperty_SetInputCallback,
                kAudioUnitScope_Global,
                INPUT,
                &input,
            ),
            "setting the microphone callback",
        )?;
        let silence = AURenderCallbackStruct {
            inputProc: Some(render_silence),
            inputProcRefCon: refcon,
        };
        check(
            set(
                u,
                kAudioUnitProperty_SetRenderCallback,
                kAudioUnitScope_Input,
                OUTPUT,
                &silence,
            ),
            "setting the output callback",
        )?;
        // SAFETY: a configured unit.
        check(
            unsafe { AudioUnitInitialize(u) },
            "AudioUnitInitialize (voice processing)",
        )?;
        unit.initialized = true;
        // AGC off and ducking at its minimum, after initializing (WebRTC reads and sets AGC
        // there too) and before starting.
        let agc = match set(
            u,
            kAUVoiceIOProperty_VoiceProcessingEnableAGC,
            kAudioUnitScope_Global,
            INPUT,
            &off,
        ) {
            0 => match get::<u32>(
                u,
                kAUVoiceIOProperty_VoiceProcessingEnableAGC,
                kAudioUnitScope_Global,
                INPUT,
            ) {
                Ok(0) => "off".to_string(),
                Ok(_) => "on".to_string(),
                Err(_) => "off".to_string(),
            },
            status => format!("unchanged:{}", coreaudio::status_text(status)),
        };
        let ducking = AUVoiceIOOtherAudioDuckingConfiguration {
            mEnableAdvancedDucking: 0,
            mDuckingLevel: AUVoiceIOOtherAudioDuckingLevel::Min,
        };
        let ducking = match set(
            u,
            kAUVoiceIOProperty_OtherAudioDuckingConfiguration,
            kAudioUnitScope_Global,
            OUTPUT,
            &ducking,
        ) {
            0 => "min".to_string(),
            // Before macOS 14 the property does not exist: VPIO's fixed ducking applies.
            status => format!("unavailable:{}", coreaudio::status_text(status)),
        };
        // SAFETY: an initialized unit. The microphone permission is asked here.
        check(
            unsafe { AudioOutputUnitStart(u) },
            "AudioOutputUnitStart (voice processing)",
        )?;
        unit.started = true;
        notes.push(format!(
            "source=mic voice_processing=true rate={rate} output={:?} agc={agc} ducking={ducking}",
            unit.output_uid.as_deref().unwrap_or("default"),
        ));
        Ok(unit)
    }
}

impl Drop for Unit {
    fn drop(&mut self) {
        // SAFETY: each call undoes one that succeeded in `open`, in reverse order; once
        // disposed of, the unit calls neither callback again. Statuses are ignored while
        // shutting down.
        unsafe {
            if self.started {
                AudioOutputUnitStop(self.unit);
            }
            if self.initialized {
                AudioUnitUninitialize(self.unit);
            }
            AudioComponentInstanceDispose(self.unit);
        }
    }
}

/// The unit has microphone audio: render it (processed) into a buffer of ours and hand it over.
unsafe extern "C-unwind" fn input_proc(
    refcon: NonNull<c_void>,
    flags: NonNull<AudioUnitRenderActionFlags>,
    time: NonNull<AudioTimeStamp>,
    bus: u32,
    frames: u32,
    _data: *mut AudioBufferList,
) -> OSStatus {
    let callback = refcon.cast::<Callback>().as_ref();
    if frames == 0 {
        return 0;
    }
    let mut data = vec![0.0f32; frames as usize];
    let mut list = AudioBufferList {
        mNumberBuffers: 1,
        mBuffers: [AudioBuffer {
            mNumberChannels: 1,
            mDataByteSize: frames * 4,
            mData: data.as_mut_ptr().cast(),
        }],
    };
    let status = AudioUnitRender(
        callback.unit,
        flags.as_ptr(),
        time,
        bus,
        frames,
        NonNull::from(&mut list),
    );
    if status != 0 {
        return status;
    }
    let got = (list.mBuffers[0].mDataByteSize / 4) as usize;
    data.truncate(got.min(frames as usize));
    if data.is_empty() {
        return 0;
    }
    let _ = callback.tx.send(Chunk {
        source: "mic",
        rate: callback.rate,
        channels: 1,
        data,
    });
    0
}

/// The output side plays nothing: silence, flagged as such.
unsafe extern "C-unwind" fn render_silence(
    _refcon: NonNull<c_void>,
    mut flags: NonNull<AudioUnitRenderActionFlags>,
    _time: NonNull<AudioTimeStamp>,
    _bus: u32,
    _frames: u32,
    data: *mut AudioBufferList,
) -> OSStatus {
    if let Some(list) = data.as_mut() {
        let n = list.mNumberBuffers as usize;
        let buffers = std::slice::from_raw_parts_mut(list.mBuffers.as_mut_ptr(), n);
        for b in buffers {
            if !b.mData.is_null() {
                std::ptr::write_bytes(b.mData.cast::<u8>(), 0, b.mDataByteSize as usize);
            }
        }
    }
    *flags.as_mut() |= AudioUnitRenderActionFlags::UnitRenderAction_OutputIsSilence;
    0
}

/// What the default-output listener sets.
struct Flags {
    default_output_changed: AtomicBool,
}

/// The microphone through voice processing. Field order matters: the unit goes first, then
/// the listener, then what they point at.
pub struct VoiceMicrophone {
    unit: Option<Unit>,
    _default_listener: Option<Listener>,
    flags: Arc<Flags>,
    uid: String,
    name: String,
    tx: Sender<Chunk>,
    rx: Receiver<Chunk>,
    notes: VecDeque<String>,
    last_audio: Instant,
    stall_rebuilds: u32,
    reopen_attempts: u32,
}

impl VoiceMicrophone {
    pub fn open(uid: &str) -> Result<Self> {
        let Some(device) = coreaudio::device_by_uid(uid)? else {
            bail!("no audio device with the UID {uid:?}");
        };
        if coreaudio::channels(device, true) == 0 {
            bail!("the device {uid:?} has no input (it is not a microphone)");
        }
        let name = coreaudio::name(device).unwrap_or_else(|_| uid.to_string());
        let (tx, rx) = channel();
        let mut notes = Vec::new();
        let unit = Unit::open(device, tx.clone(), &mut notes).map_err(|e| {
            anyhow!("cannot open the microphone {name:?} with voice processing: {e:#}")
        })?;
        let flags = Arc::new(Flags {
            default_output_changed: AtomicBool::new(false),
        });
        // SAFETY: `flags` outlives the listener (both are fields, the listener dropped first).
        let listener = unsafe {
            Listener::add(
                SYSTEM_OBJECT,
                global(kAudioHardwarePropertyDefaultOutputDevice),
                on_default_output_changed,
                Arc::as_ptr(&flags) as *mut c_void,
            )
        };
        let listener = match listener {
            Ok(l) => Some(l),
            Err(e) => {
                notes.push(format!(
                    "source=mic default_output_listener=failed error=\"{e:#}\""
                ));
                None
            }
        };
        Ok(Self {
            unit: Some(unit),
            _default_listener: listener,
            flags,
            uid: uid.to_string(),
            name: format!("{name} ({uid}, voice processing)"),
            tx,
            rx,
            notes: notes.into(),
            last_audio: Instant::now(),
            stall_rebuilds: 0,
            reopen_attempts: 0,
        })
    }

    /// Build the unit again (the old one is disposed of first: one voice processor per
    /// process). A device that is away is tried again on the next calls, up to
    /// REOPEN_ATTEMPTS times, then the recording fails.
    fn rebuild(&mut self, why: &str) -> Result<Event> {
        self.unit = None;
        let opened = match coreaudio::device_by_uid(&self.uid) {
            Ok(Some(device)) => {
                let mut notes = Vec::new();
                Unit::open(device, self.tx.clone(), &mut notes).map(|u| (u, notes))
            }
            Ok(None) => Err(anyhow!("the device {:?} is gone", self.uid)),
            Err(e) => Err(e),
        };
        match opened {
            Ok((unit, notes)) => {
                let attempts = self.reopen_attempts + 1;
                self.unit = Some(unit);
                self.reopen_attempts = 0;
                self.last_audio = Instant::now();
                self.notes.extend(notes);
                Ok(Event::Note(format!(
                    "source=mic reopened=true reason={why} attempts={attempts}"
                )))
            }
            Err(e) => {
                self.reopen_attempts += 1;
                if self.reopen_attempts >= REOPEN_ATTEMPTS {
                    bail!(
                        "microphone: cannot open the device with voice processing again after {} tries: {e:#}",
                        self.reopen_attempts
                    );
                }
                std::thread::sleep(REOPEN_PAUSE);
                Ok(Event::Idle)
            }
        }
    }
}

impl Source for VoiceMicrophone {
    fn name(&self) -> String {
        self.name.clone()
    }

    fn next(&mut self, timeout: Duration) -> Result<Event> {
        if let Some(note) = self.notes.pop_front() {
            return Ok(Event::Note(note));
        }
        if self.unit.is_none() {
            return self.rebuild("device_away");
        }
        if self
            .flags
            .default_output_changed
            .swap(false, Ordering::SeqCst)
        {
            let now = coreaudio::default_device(false).and_then(|id| coreaudio::uid(id).ok());
            let current = self.unit.as_ref().and_then(|u| u.output_uid.clone());
            if now != current {
                return self.rebuild("default_output_changed");
            }
        }
        match self.rx.recv_timeout(timeout) {
            Ok(chunk) => {
                self.last_audio = Instant::now();
                self.stall_rebuilds = 0;
                Ok(Event::Audio(chunk))
            }
            Err(RecvTimeoutError::Timeout) => {
                // As tap.rs: a few rebuilds, then wait. Not an error: the microphone
                // permission prompt may be what holds the audio back.
                if self.last_audio.elapsed() < STALL || self.stall_rebuilds >= MAX_STALL_REBUILDS {
                    return Ok(Event::Idle);
                }
                self.stall_rebuilds += 1;
                self.rebuild("stalled")
            }
            // Impossible: we hold a sender for as long as we live.
            Err(RecvTimeoutError::Disconnected) => Ok(Event::Idle),
        }
    }
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
    let flags = &*(client as *const Flags);
    flags.default_output_changed.store(true, Ordering::SeqCst);
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_rate_comes_from_the_unit_then_the_device_then_the_default() {
        assert_eq!(pick_rate(24_000.0, Some(48_000.0)), 24_000);
        assert_eq!(pick_rate(0.0, Some(16_000.0)), 16_000);
        assert_eq!(pick_rate(f64::NAN, None), crate::DEFAULT_RATE);
        assert_eq!(pick_rate(0.0, Some(0.0)), crate::DEFAULT_RATE);
        assert_eq!(pick_rate(44_100.4, None), 44_100);
    }

    #[test]
    fn the_format_is_float32_mono_packed() {
        let f = mono_f32(48_000);
        assert_eq!(f.mSampleRate, 48_000.0);
        assert_eq!(f.mChannelsPerFrame, 1);
        assert_eq!(f.mBytesPerFrame, 4);
        assert_eq!(f.mBitsPerChannel, 32);
        assert_eq!(f.mFormatID, kAudioFormatLinearPCM);
        assert_eq!(f.mFormatFlags, kAudioFormatFlagsNativeFloatPacked);
    }

    #[test]
    fn the_ducking_configuration_has_the_c_layout() {
        // Boolean (u8) then a u32 level: 8 bytes with padding, as the C struct.
        assert_eq!(size_of::<AUVoiceIOOtherAudioDuckingConfiguration>(), 8);
    }

    #[test]
    fn an_unknown_uid_is_a_clear_error() {
        let e = VoiceMicrophone::open("mnemosyne-no-such-device")
            .err()
            .expect("an error");
        assert!(
            e.to_string().contains("no audio device with the UID"),
            "{e}"
        );
    }

    #[test]
    fn availability_can_be_asked_without_opening_anything() {
        // Every supported macOS has the unit; the call itself must not fail or block.
        let _ = available();
    }
}
