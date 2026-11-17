//! Core Audio's process objects (macOS 14+): every process that is a client of the audio
//! server, with its pid, bundle id and whether its input is running. Reading them needs no
//! permission. `apps` (crate::apps) keeps the ones with input running.

use super::coreaudio::{self, SYSTEM_OBJECT};
use crate::apps::{name_from_path, RawProcess};
use anyhow::Result;
use objc2_core_audio::{
    kAudioHardwarePropertyProcessObjectList, kAudioProcessPropertyBundleID,
    kAudioProcessPropertyIsRunningInput, kAudioProcessPropertyPID, AudioObjectID,
};

/// Every process object, read as far as it can be. A process that exits between the list and
/// its properties (its pid can no longer be read) is left out.
pub fn all() -> Result<Vec<RawProcess>> {
    let objects = coreaudio::get_ids(
        SYSTEM_OBJECT,
        &coreaudio::global(kAudioHardwarePropertyProcessObjectList),
    )?;
    Ok(objects.into_iter().filter_map(read).collect())
}

fn read(object: AudioObjectID) -> Option<RawProcess> {
    let pid: libc::pid_t =
        coreaudio::get_pod(object, &coreaudio::global(kAudioProcessPropertyPID)).ok()?;
    let running_input = coreaudio::get_pod::<u32>(
        object,
        &coreaudio::global(kAudioProcessPropertyIsRunningInput),
    )
    .map(|v| v != 0)
    .unwrap_or(false);
    if !running_input {
        // Not needed: skip the bundle id and the name.
        return Some(RawProcess {
            pid,
            bundle_id: String::new(),
            name: String::new(),
            running_input,
        });
    }
    // A process without a bundle (a command-line tool) has an empty or null bundle id.
    let bundle_id =
        coreaudio::get_string(object, &coreaudio::global(kAudioProcessPropertyBundleID))
            .unwrap_or_default();
    Some(RawProcess {
        pid,
        bundle_id,
        name: executable_name(pid),
        running_input,
    })
}

/// The executable's file name: from its full path (`proc_pidpath`), else the kernel's short
/// name (`proc_name`, at most 32 bytes), else "".
fn executable_name(pid: libc::pid_t) -> String {
    let mut path = vec![0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
    // SAFETY: `path` has room for the size we pass; the call writes at most that many bytes
    // and returns how many it wrote (0 or less on failure).
    let n = unsafe { libc::proc_pidpath(pid, path.as_mut_ptr().cast(), path.len() as u32) };
    if n > 0 {
        let n = (n as usize).min(path.len());
        let name = name_from_path(&String::from_utf8_lossy(&path[..n]));
        if !name.is_empty() {
            return name;
        }
    }
    let mut short = [0u8; 64];
    // SAFETY: as above, with a 64-byte buffer.
    let n = unsafe { libc::proc_name(pid, short.as_mut_ptr().cast(), short.len() as u32) };
    if n > 0 {
        let n = (n as usize).min(short.len());
        let end = short[..n].iter().position(|b| *b == 0).unwrap_or(n);
        return String::from_utf8_lossy(&short[..end]).into_owned();
    }
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_process_list_can_be_read() {
        // A hosted runner may have no process capturing; the call itself must work.
        all().unwrap();
        // The JSON `apps` prints never includes this process.
        let own = std::process::id() as i32;
        assert!(crate::apps::list().unwrap().iter().all(|p| p.pid != own));
    }

    #[test]
    fn this_process_has_a_name() {
        let name = executable_name(std::process::id() as libc::pid_t);
        assert!(!name.is_empty());
        assert!(!name.contains('/'), "{name}");
    }
}
