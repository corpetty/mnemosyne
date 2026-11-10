//! Thin wrappers over the Core Audio HAL property API (`AudioObjectGetPropertyData` and
//! friends): device lists, UIDs, names, defaults, rates, channel counts, and property
//! listeners. Every call here needs no permission; TCC only guards the audio itself.

use anyhow::{anyhow, bail, Result};
use objc2_core_audio::{
    kAudioDevicePropertyDeviceUID, kAudioDevicePropertyNominalSampleRate,
    kAudioDevicePropertyStreamConfiguration, kAudioHardwarePropertyDefaultInputDevice,
    kAudioHardwarePropertyDefaultOutputDevice, kAudioHardwarePropertyDevices,
    kAudioHardwarePropertyTranslateUIDToDevice, kAudioObjectPropertyElementMain,
    kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyScopeInput,
    kAudioObjectPropertyScopeOutput, kAudioObjectSystemObject, kAudioObjectUnknown,
    AudioObjectAddPropertyListener, AudioObjectGetPropertyData, AudioObjectGetPropertyDataSize,
    AudioObjectID, AudioObjectPropertyAddress, AudioObjectPropertyScope,
    AudioObjectPropertySelector, AudioObjectRemovePropertyListener,
};
use objc2_core_audio_types::AudioBufferList;
use objc2_core_foundation::{CFRetained, CFString};
use std::ffi::c_void;
use std::mem::{size_of, MaybeUninit};
use std::ptr::{null, NonNull};

/// Core Audio's status type (the bindings keep theirs private).
pub type OSStatus = i32;

pub const SYSTEM_OBJECT: AudioObjectID = kAudioObjectSystemObject as AudioObjectID;

pub fn address(
    selector: AudioObjectPropertySelector,
    scope: AudioObjectPropertyScope,
) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress {
        mSelector: selector,
        mScope: scope,
        mElement: kAudioObjectPropertyElementMain,
    }
}

pub fn global(selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    address(selector, kAudioObjectPropertyScopeGlobal)
}

/// Core Audio statuses are four-character codes ('!obj', 'who?') or plain numbers.
pub fn status_text(status: OSStatus) -> String {
    let bytes = (status as u32).to_be_bytes();
    if bytes.iter().all(|b| b.is_ascii_graphic() || *b == b' ') {
        format!("'{}' ({status})", String::from_utf8_lossy(&bytes))
    } else {
        status.to_string()
    }
}

pub fn check(status: OSStatus, what: &str) -> Result<()> {
    if status == 0 {
        Ok(())
    } else {
        Err(anyhow!("{what}: Core Audio error {}", status_text(status)))
    }
}

/// Read a fixed-size property into `out`, with an optional qualifier.
///
/// # Safety
/// `out` must point to `size` writable bytes; `qualifier` to `qualifier_size` readable ones.
unsafe fn get_raw(
    object: AudioObjectID,
    addr: &AudioObjectPropertyAddress,
    qualifier: *const c_void,
    qualifier_size: u32,
    out: *mut c_void,
    size: &mut u32,
) -> OSStatus {
    AudioObjectGetPropertyData(
        object,
        NonNull::from(addr),
        qualifier_size,
        qualifier,
        NonNull::from(size),
        NonNull::new(out).expect("out pointer"),
    )
}

/// A plain-data property (an id, a float, a struct).
pub fn get_pod<T: Copy>(object: AudioObjectID, addr: &AudioObjectPropertyAddress) -> Result<T> {
    get_pod_with(object, addr, null(), 0)
}

fn get_pod_with<T: Copy>(
    object: AudioObjectID,
    addr: &AudioObjectPropertyAddress,
    qualifier: *const c_void,
    qualifier_size: u32,
) -> Result<T> {
    let mut value = MaybeUninit::<T>::uninit();
    let mut size = size_of::<T>() as u32;
    // SAFETY: `value` has room for `size` bytes; the HAL writes at most that many.
    let status = unsafe {
        get_raw(
            object,
            addr,
            qualifier,
            qualifier_size,
            value.as_mut_ptr().cast(),
            &mut size,
        )
    };
    check(
        status,
        &format!("property {}", selector_text(addr.mSelector)),
    )?;
    if size as usize != size_of::<T>() {
        bail!(
            "property {} is {size} bytes, expected {}",
            selector_text(addr.mSelector),
            size_of::<T>()
        );
    }
    // SAFETY: the HAL filled all of it (size checked).
    Ok(unsafe { value.assume_init() })
}

/// A CFString property (returned under the create rule: we own one reference).
pub fn get_string(object: AudioObjectID, addr: &AudioObjectPropertyAddress) -> Result<String> {
    let ptr: *const CFString = get_pod(object, addr)?;
    let Some(ptr) = NonNull::new(ptr as *mut CFString) else {
        bail!("property {} is null", selector_text(addr.mSelector));
    };
    // SAFETY: a +1 reference the HAL handed over.
    let s = unsafe { CFRetained::from_raw(ptr) };
    Ok(s.to_string())
}

/// An array of AudioObjectIDs (devices, streams).
pub fn get_ids(
    object: AudioObjectID,
    addr: &AudioObjectPropertyAddress,
) -> Result<Vec<AudioObjectID>> {
    let mut size: u32 = 0;
    // SAFETY: plain out-parameter call.
    let status = unsafe {
        AudioObjectGetPropertyDataSize(
            object,
            NonNull::from(addr),
            0,
            null(),
            NonNull::from(&mut size),
        )
    };
    check(
        status,
        &format!("size of {}", selector_text(addr.mSelector)),
    )?;
    let count = size as usize / size_of::<AudioObjectID>();
    let mut ids = vec![kAudioObjectUnknown; count.max(1)];
    let mut size = (ids.len() * size_of::<AudioObjectID>()) as u32;
    // SAFETY: `ids` holds `size` bytes.
    let status = unsafe { get_raw(object, addr, null(), 0, ids.as_mut_ptr().cast(), &mut size) };
    check(
        status,
        &format!("property {}", selector_text(addr.mSelector)),
    )?;
    ids.truncate(size as usize / size_of::<AudioObjectID>());
    Ok(ids)
}

fn selector_text(selector: AudioObjectPropertySelector) -> String {
    let bytes = selector.to_be_bytes();
    if bytes.iter().all(|b| b.is_ascii_graphic() || *b == b' ') {
        format!("'{}'", String::from_utf8_lossy(&bytes))
    } else {
        selector.to_string()
    }
}

/// Every audio device the HAL knows (private aggregate devices are not among them).
pub fn all_devices() -> Result<Vec<AudioObjectID>> {
    get_ids(SYSTEM_OBJECT, &global(kAudioHardwarePropertyDevices))
}

/// The default input (`true`) or output device, if there is one.
pub fn default_device(input: bool) -> Option<AudioObjectID> {
    let selector = if input {
        kAudioHardwarePropertyDefaultInputDevice
    } else {
        kAudioHardwarePropertyDefaultOutputDevice
    };
    match get_pod::<AudioObjectID>(SYSTEM_OBJECT, &global(selector)) {
        Ok(id) if id != kAudioObjectUnknown => Some(id),
        _ => None,
    }
}

pub fn uid(device: AudioObjectID) -> Result<String> {
    get_string(device, &global(kAudioDevicePropertyDeviceUID))
}

pub fn name(device: AudioObjectID) -> Result<String> {
    get_string(device, &global(kAudioObjectPropertyName))
}

pub fn nominal_rate(device: AudioObjectID) -> Result<f64> {
    get_pod(device, &global(kAudioDevicePropertyNominalSampleRate))
}

/// The device with this UID, if it is present right now.
pub fn device_by_uid(uid: &str) -> Result<Option<AudioObjectID>> {
    let cf = CFString::from_str(uid);
    let ptr: *const CFString = &*cf;
    let id: AudioObjectID = get_pod_with(
        SYSTEM_OBJECT,
        &global(kAudioHardwarePropertyTranslateUIDToDevice),
        (&ptr as *const *const CFString).cast(),
        size_of::<*const CFString>() as u32,
    )?;
    Ok((id != kAudioObjectUnknown).then_some(id))
}

/// Channels the device has on the input (capture) or output side; 0 when it has none or
/// the property cannot be read.
pub fn channels(device: AudioObjectID, input: bool) -> u32 {
    let scope = if input {
        kAudioObjectPropertyScopeInput
    } else {
        kAudioObjectPropertyScopeOutput
    };
    let addr = address(kAudioDevicePropertyStreamConfiguration, scope);
    let mut size: u32 = 0;
    // SAFETY: plain out-parameter call.
    let status = unsafe {
        AudioObjectGetPropertyDataSize(
            device,
            NonNull::from(&addr),
            0,
            null(),
            NonNull::from(&mut size),
        )
    };
    if status != 0 || (size as usize) < size_of::<AudioBufferList>() {
        return 0;
    }
    // u64 cells keep the AudioBufferList (which holds pointers) aligned.
    let mut buf = vec![0u64; (size as usize).div_ceil(size_of::<u64>())];
    let mut got = size;
    // SAFETY: `buf` holds `size` bytes, aligned for the struct.
    let status = unsafe { get_raw(device, &addr, null(), 0, buf.as_mut_ptr().cast(), &mut got) };
    if status != 0 {
        return 0;
    }
    // SAFETY: the HAL wrote an AudioBufferList with mNumberBuffers AudioBuffers in a row.
    unsafe {
        let list = &*(buf.as_ptr() as *const AudioBufferList);
        let n = list.mNumberBuffers as usize;
        let buffers = std::slice::from_raw_parts(list.mBuffers.as_ptr(), n);
        buffers.iter().map(|b| b.mNumberChannels).sum()
    }
}

pub type ListenerProc = unsafe extern "C-unwind" fn(
    AudioObjectID,
    u32,
    NonNull<AudioObjectPropertyAddress>,
    *mut c_void,
) -> OSStatus;

/// A property listener, removed when dropped. Core Audio wants a listener added and removed
/// from the same thread: open and drop sources on one thread (the recorder does).
pub struct Listener {
    object: AudioObjectID,
    addr: AudioObjectPropertyAddress,
    proc_: ListenerProc,
    data: *mut c_void,
}

// SAFETY: `data` is only handed back to the HAL; the owner keeps what it points to alive
// (an Arc) for as long as the listener exists.
unsafe impl Send for Listener {}

impl Listener {
    /// # Safety
    /// `data` must stay valid until the listener is dropped.
    pub unsafe fn add(
        object: AudioObjectID,
        addr: AudioObjectPropertyAddress,
        proc_: ListenerProc,
        data: *mut c_void,
    ) -> Result<Self> {
        let status =
            AudioObjectAddPropertyListener(object, NonNull::from(&addr), Some(proc_), data);
        check(
            status,
            &format!("listening to {}", selector_text(addr.mSelector)),
        )?;
        Ok(Self {
            object,
            addr,
            proc_,
            data,
        })
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        // SAFETY: the same arguments the listener was added with.
        unsafe {
            AudioObjectRemovePropertyListener(
                self.object,
                NonNull::from(&self.addr),
                Some(self.proc_),
                self.data,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statuses_read_as_four_character_codes_when_they_are_ones() {
        assert_eq!(status_text(0x21646576), "'!dev' (560227702)");
        assert_eq!(status_text(-50), "-50");
    }

    #[test]
    fn an_unknown_uid_translates_to_nothing() {
        assert!(device_by_uid("mnemosyne-no-such-device").unwrap().is_none());
    }

    #[test]
    fn the_device_list_can_be_read() {
        // A hosted runner may have no devices at all; the call itself must work.
        let devices = all_devices().unwrap();
        for d in devices {
            let _ = uid(d);
            let _ = name(d);
            let _ = channels(d, true);
            let _ = channels(d, false);
        }
    }
}
