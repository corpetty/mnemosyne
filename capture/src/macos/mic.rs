//! The microphone: a cpal input stream on the device with the given UID, owned by a thread of
//! its own (cpal streams stay on the thread that made them). cpal invalidates its stream when
//! the device's sample rate changes (AirPods opening the microphone switch to the hands-free
//! profile): the stream is then opened again at the new rate and the recorder sees a
//! `rate_change`. A device that disappears ends the recording with an error (exit 1).

use super::coreaudio;
use crate::source::{Chunk, Event, Source};
use anyhow::{anyhow, bail, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::ErrorKind;
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::time::Duration;

/// How long the device may be away while its stream is opened again (a Bluetooth profile
/// switch takes a second or two).
const REOPEN_ATTEMPTS: u32 = 25;
const REOPEN_PAUSE: Duration = Duration::from_millis(200);

enum Msg {
    Audio(Chunk),
    Note(String),
    Failed(String),
}

pub struct Microphone {
    name: String,
    rx: Receiver<Msg>,
    /// Dropping it tells the driver thread to stop.
    _stop: Sender<()>,
}

impl Microphone {
    pub fn open(uid: &str) -> Result<Self> {
        let Some(device) = coreaudio::device_by_uid(uid)? else {
            bail!("no audio device with the UID {uid:?}");
        };
        if coreaudio::channels(device, true) == 0 {
            bail!("the device {uid:?} has no input (it is not a microphone)");
        }
        let name = coreaudio::name(device).unwrap_or_else(|_| uid.to_string());
        let (tx, rx) = channel();
        let (stop_tx, stop_rx) = channel();
        let (ready_tx, ready_rx) = channel();
        let uid_owned = uid.to_string();
        std::thread::Builder::new()
            .name("microphone".into())
            .spawn(move || drive(uid_owned, tx, stop_rx, ready_tx))
            .map_err(|e| anyhow!("cannot start the microphone thread: {e}"))?;
        match ready_rx.recv() {
            Ok(Ok(())) => {}
            Ok(Err(e)) => bail!("cannot open the microphone {name:?}: {e}"),
            Err(_) => bail!("cannot open the microphone {name:?}: the stream thread ended"),
        }
        Ok(Self {
            name: format!("{name} ({uid})"),
            rx,
            _stop: stop_tx,
        })
    }
}

impl Source for Microphone {
    fn name(&self) -> String {
        self.name.clone()
    }

    fn next(&mut self, timeout: Duration) -> Result<Event> {
        match self.rx.recv_timeout(timeout) {
            Ok(Msg::Audio(chunk)) => Ok(Event::Audio(chunk)),
            Ok(Msg::Note(text)) => Ok(Event::Note(text)),
            Ok(Msg::Failed(why)) => Err(anyhow!("microphone: {why}")),
            Err(RecvTimeoutError::Timeout) => Ok(Event::Idle),
            Err(RecvTimeoutError::Disconnected) => Err(anyhow!("microphone: the stream ended")),
        }
    }
}

/// Owns the cpal stream. Ends when `stop` is dropped or the device fails for good.
fn drive(uid: String, tx: Sender<Msg>, stop: Receiver<()>, ready: Sender<Result<(), String>>) {
    let host = cpal::default_host();
    let mut ready = Some(ready);
    let mut attempts = 0u32;
    loop {
        let (stream, errors) = match open_stream(&host, &uid, &tx) {
            Ok(opened) => opened,
            Err(e) => {
                attempts += 1;
                if let Some(ready) = ready.take() {
                    let _ = ready.send(Err(e));
                    return;
                }
                if attempts >= REOPEN_ATTEMPTS {
                    let _ = tx.send(Msg::Failed(format!(
                        "cannot open the device again after {attempts} tries: {e}"
                    )));
                    return;
                }
                std::thread::sleep(REOPEN_PAUSE);
                continue;
            }
        };
        if let Some(ready) = ready.take() {
            let _ = ready.send(Ok(()));
        } else {
            let _ = tx.send(Msg::Note(format!(
                "source=mic reopened=true attempts={}",
                attempts + 1
            )));
        }
        attempts = 0;
        // Wait for a stop or a stream error. The stream lives in this scope.
        let outcome = loop {
            match stop.recv_timeout(Duration::from_millis(100)) {
                Ok(()) | Err(RecvTimeoutError::Disconnected) => break None,
                Err(RecvTimeoutError::Timeout) => {}
            }
            match errors.try_recv() {
                // An overrun loses a little audio; the stream goes on.
                Ok(e) if e.kind() == ErrorKind::Xrun => {
                    let _ = tx.send(Msg::Note("source=mic xrun=true".to_string()));
                }
                Ok(e) => break Some(e),
                Err(TryRecvError::Empty) => {}
                Err(TryRecvError::Disconnected) => {
                    break Some(cpal::Error::with_message(
                        ErrorKind::StreamInvalidated,
                        "the stream went away",
                    ))
                }
            }
        };
        drop(stream);
        let Some(e) = outcome else { return };
        // cpal ends the stream when the device's rate changes (AirPods switching profile) or
        // the device goes away, and the device may be back in a moment: open it again, for up
        // to REOPEN_ATTEMPTS tries. A device that is really gone fails there (exit 1).
        let _ = tx.send(Msg::Note(format!(
            "source=mic stream_ended=\"{e}\" kind={:?}",
            e.kind()
        )));
        std::thread::sleep(REOPEN_PAUSE);
    }
}

type Opened = (cpal::Stream, Receiver<cpal::Error>);

fn open_stream(host: &cpal::Host, uid: &str, tx: &Sender<Msg>) -> Result<Opened, String> {
    let device = host
        .input_devices()
        .map_err(|e| format!("cannot list input devices: {e}"))?
        .find(|d| d.id().map(|id| id.id() == uid).unwrap_or(false))
        .ok_or_else(|| format!("the device {uid:?} is gone"))?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("no input configuration: {e}"))?;
    let config = supported.config();
    let rate = config.sample_rate;
    let channels = config.channels;
    if rate == 0 || channels == 0 {
        return Err(format!(
            "odd input configuration: {rate} Hz, {channels} channels"
        ));
    }
    let (err_tx, err_rx) = channel();
    let data_tx = tx.clone();
    let stream = device
        .build_input_stream::<f32, _, _>(
            config,
            move |data, _info| {
                let _ = data_tx.send(Msg::Audio(Chunk {
                    source: "mic",
                    rate,
                    channels,
                    data: data.to_vec(),
                }));
            },
            move |e| {
                let _ = err_tx.send(e);
            },
            None,
        )
        .map_err(|e| format!("cannot open the input stream: {e}"))?;
    stream
        .play()
        .map_err(|e| format!("cannot start the input stream: {e}"))?;
    Ok((stream, err_rx))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unknown_uid_is_a_clear_error() {
        let e = Microphone::open("mnemosyne-no-such-device")
            .err()
            .expect("an error");
        assert!(
            e.to_string().contains("no audio device with the UID"),
            "{e}"
        );
    }
}
