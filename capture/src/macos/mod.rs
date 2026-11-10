//! macOS: Core Audio devices, the microphone through cpal, the system's audio through a
//! global process tap. Everything here needs macOS 14.2+ (we require 15.4).

pub mod coreaudio;
pub mod mic;
pub mod tap;

use crate::devices::Device;
use crate::source::Source;
use anyhow::Result;
use objc2::msg_send;
use objc2::runtime::AnyClass;
use objc2_foundation::NSString;
use serde_json::{json, Map, Value};

/// The `--target` that means the system's audio (every app's output).
pub const SYSTEM_TARGET: &str = "system";

/// The entry `devices` adds for the system's audio. Its id is kAudioObjectSystemObject (1),
/// which no real device has.
pub fn system_entry() -> Device {
    Device {
        id: coreaudio::SYSTEM_OBJECT,
        name: SYSTEM_TARGET.to_string(),
        description: "System audio (all apps)".to_string(),
        media_class: "Audio/Sink".to_string(),
        is_monitor: false,
        is_echo_cancelled: false,
        default: false,
    }
}

/// Core Audio's devices in the backend's shape: the `system` entry first, then every device
/// with input channels as an `Audio/Source` (`name` = the UID the UI saves, `description` =
/// the device's name). Output devices are not listed: one output cannot be recorded on its
/// own (the global tap records them all), and the backend keys devices by id, so a headset
/// listed as both would record its output in place of its microphone.
pub fn devices() -> Result<Vec<Device>> {
    let default_in = coreaudio::default_device(true);
    let mut out = vec![system_entry()];
    for id in coreaudio::all_devices()? {
        let Ok(uid) = coreaudio::uid(id) else {
            continue;
        };
        if uid.is_empty() {
            continue;
        }
        let description = coreaudio::name(id).unwrap_or_else(|_| uid.clone());
        if coreaudio::channels(id, true) > 0 {
            out.push(Device {
                id,
                name: uid,
                description,
                media_class: "Audio/Source".to_string(),
                is_monitor: false,
                is_echo_cancelled: false,
                default: default_in == Some(id),
            });
        }
    }
    Ok(out)
}

/// Open `--target`: "system" or a device UID.
pub fn open(target: &str) -> Result<Box<dyn Source>> {
    if target == SYSTEM_TARGET {
        Ok(Box::new(tap::SystemTap::open()?))
    } else {
        Ok(Box::new(mic::Microphone::open(target)?))
    }
}

/// The macOS part of `doctor`.
pub fn doctor_fields(report: &mut Map<String, Value>) {
    report.insert("sources".into(), json!(["fake", "device", "system"]));
    report.insert("real_capture".into(), json!(true));
    report.insert("taps_supported".into(), json!(true));
    report.insert("macos_version".into(), json!(macos_version()));
    report.insert("mic_authorization".into(), json!(mic_authorization()));
    report.insert(
        "default_input".into(),
        device_json(coreaudio::default_device(true)),
    );
    report.insert(
        "default_output".into(),
        device_json(coreaudio::default_device(false)),
    );
}

fn device_json(id: Option<u32>) -> Value {
    let Some(id) = id else { return Value::Null };
    json!({
        "id": id,
        "uid": coreaudio::uid(id).ok(),
        "name": coreaudio::name(id).ok(),
        "rate": coreaudio::nominal_rate(id).ok(),
    })
}

/// `kern.osproductversion`, "15.4" and the like.
pub fn macos_version() -> Option<String> {
    let mut buf = [0u8; 64];
    let mut len = buf.len();
    // SAFETY: a documented sysctl name, a buffer of `len` bytes.
    let rc = unsafe {
        libc::sysctlbyname(
            c"kern.osproductversion".as_ptr(),
            buf.as_mut_ptr().cast(),
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    if rc != 0 || len == 0 {
        return None;
    }
    let end = buf[..len].iter().position(|b| *b == 0).unwrap_or(len);
    Some(String::from_utf8_lossy(&buf[..end]).into_owned())
}

// AVCaptureDevice lives in AVFoundation; linking it makes the class available at runtime.
#[link(name = "AVFoundation", kind = "framework")]
extern "C" {}

/// The microphone permission as TCC has it for this process chain:
/// not_determined, restricted, denied, authorized, or unknown when the class is missing.
pub fn mic_authorization() -> &'static str {
    let Some(class) = AnyClass::get(c"AVCaptureDevice") else {
        return "unknown";
    };
    // AVMediaTypeAudio is the string "soun".
    let media = NSString::from_str("soun");
    // SAFETY: +[AVCaptureDevice authorizationStatusForMediaType:] takes an NSString and
    // returns an NSInteger (AVAuthorizationStatus).
    let status: isize = unsafe { msg_send![class, authorizationStatusForMediaType: &*media] };
    match status {
        0 => "not_determined",
        1 => "restricted",
        2 => "denied",
        3 => "authorized",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_device_list_starts_with_the_system_entry_and_has_the_shape() {
        let list = devices().unwrap();
        assert_eq!(list[0], system_entry());
        assert_eq!(list[0].id, 1);
        for d in &list[1..] {
            assert_ne!(d.id, 1);
            assert!(!d.name.is_empty());
            assert!(!d.description.is_empty());
            assert_eq!(d.media_class, "Audio/Source", "{d:?}");
        }
        // One entry per id: the backend keys devices by it.
        let mut ids: Vec<u32> = list.iter().map(|d| d.id).collect();
        ids.sort_unstable();
        ids.dedup();
        assert_eq!(ids.len(), list.len());
        assert!(list.iter().filter(|d| d.default).count() <= 1);
        // It is valid JSON for the backend.
        let text = serde_json::to_string(&list).unwrap();
        assert!(text.contains("\"name\":\"system\""));
    }

    #[test]
    fn the_doctor_fields_have_the_shape() {
        let mut m = Map::new();
        doctor_fields(&mut m);
        assert_eq!(m["taps_supported"], json!(true));
        assert_eq!(m["real_capture"], json!(true));
        assert_eq!(m["sources"], json!(["fake", "device", "system"]));
        let version = m["macos_version"].as_str().expect("a version string");
        assert!(
            version.starts_with(|c: char| c.is_ascii_digit()),
            "{version}"
        );
        let auth = m["mic_authorization"].as_str().unwrap();
        assert!(
            [
                "not_determined",
                "restricted",
                "denied",
                "authorized",
                "unknown"
            ]
            .contains(&auth),
            "{auth}"
        );
        for key in ["default_input", "default_output"] {
            let v = &m[key];
            assert!(v.is_null() || v["id"].is_number(), "{key}: {v}");
        }
    }

    #[test]
    fn a_bogus_target_is_a_clear_error() {
        let e = open("mnemosyne-no-such-device").err().expect("an error");
        assert!(e.to_string().contains("no audio device"), "{e}");
    }
}
