//! `mnemosyne-capture devices`: the backend's `AudioDevice` shape (audio/capture.py), as JSON.

use serde::Serialize;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Device {
    pub id: u32,
    /// A stable name the UI can save and the recorder takes as `--target`.
    pub name: String,
    pub description: String,
    /// "Audio/Source" or "Audio/Sink".
    pub media_class: String,
    pub is_monitor: bool,
    pub is_echo_cancelled: bool,
    /// The system's default input or output.
    pub default: bool,
}

/// The devices this system can record from: Core Audio's on macOS (plus the `system` entry),
/// none elsewhere (Linux lists PipeWire's itself).
pub fn list() -> anyhow::Result<Vec<Device>> {
    #[cfg(target_os = "macos")]
    {
        crate::macos::devices()
    }
    #[cfg(not(target_os = "macos"))]
    {
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_device_has_the_backends_fields() {
        let d = Device {
            id: 1,
            name: "system".into(),
            description: "System audio (all apps)".into(),
            media_class: "Audio/Sink".into(),
            is_monitor: false,
            is_echo_cancelled: false,
            default: false,
        };
        let v = serde_json::to_value(d).unwrap();
        let mut keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "default",
                "description",
                "id",
                "is_echo_cancelled",
                "is_monitor",
                "media_class",
                "name"
            ]
        );
    }
}
