//! The guest wire on an iroh bi stream: each phase 9 WebSocket message becomes one frame
//! `u32 BE len || tag || payload`, where `len` counts the tag and the payload (never 0) and the
//! tag is [`TAG_TEXT`] or [`TAG_BINARY`]. The owner's guest endpoint (link/) reads and writes
//! the same frames; keep the two in step.

use std::fmt;

/// A WebSocket text message (UTF-8 JSON in the phase 9 protocol).
pub const TAG_TEXT: u8 = 0;
/// A WebSocket binary message (a sealed envelope).
pub const TAG_BINARY: u8 = 1;
/// The largest payload: the publish host's room frame cap (2 MiB) plus the 64 KiB of slack its
/// WebSocket allows (publish-host/src/app.rs `live_socket`).
pub const MAX_PAYLOAD: usize = 2 * 1024 * 1024 + 64 * 1024;
/// The length prefix.
pub const HEADER_LEN: usize = 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameError {
    /// A tag other than [`TAG_TEXT`] or [`TAG_BINARY`].
    BadTag(u8),
    /// A payload over [`MAX_PAYLOAD`] (the payload length, without the tag).
    TooLarge(usize),
    /// A length field of 0: there is not even a tag.
    Empty,
}

impl fmt::Display for FrameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FrameError::BadTag(tag) => write!(f, "unknown frame tag {tag}"),
            FrameError::TooLarge(n) => {
                write!(
                    f,
                    "frame payload of {n} bytes is over the {MAX_PAYLOAD} byte cap"
                )
            }
            FrameError::Empty => write!(f, "empty frame (no tag)"),
        }
    }
}

impl std::error::Error for FrameError {}

fn check_tag(tag: u8) -> Result<(), FrameError> {
    match tag {
        TAG_TEXT | TAG_BINARY => Ok(()),
        other => Err(FrameError::BadTag(other)),
    }
}

/// One frame for `payload`.
pub fn encode(tag: u8, payload: &[u8]) -> Result<Vec<u8>, FrameError> {
    check_tag(tag)?;
    if payload.len() > MAX_PAYLOAD {
        return Err(FrameError::TooLarge(payload.len()));
    }
    // MAX_PAYLOAD + 1 fits in a u32.
    let len = (payload.len() + 1) as u32;
    let mut out = Vec::with_capacity(HEADER_LEN + 1 + payload.len());
    out.extend_from_slice(&len.to_be_bytes());
    out.push(tag);
    out.extend_from_slice(payload);
    Ok(out)
}

/// The number of bytes after the header (tag + payload), checked against the cap, so a reader
/// knows how much to read before allocating it.
pub fn body_len(header: [u8; HEADER_LEN]) -> Result<usize, FrameError> {
    let len = u32::from_be_bytes(header) as usize;
    if len == 0 {
        return Err(FrameError::Empty);
    }
    if len - 1 > MAX_PAYLOAD {
        return Err(FrameError::TooLarge(len - 1));
    }
    Ok(len)
}

/// The tag and payload of a body of [`body_len`] bytes.
pub fn split_body(body: &[u8]) -> Result<(u8, &[u8]), FrameError> {
    let (&tag, payload) = body.split_first().ok_or(FrameError::Empty)?;
    check_tag(tag)?;
    if payload.len() > MAX_PAYLOAD {
        return Err(FrameError::TooLarge(payload.len()));
    }
    Ok((tag, payload))
}

/// A decoded frame: the tag, the payload and how many bytes of the buffer it used.
pub type Decoded<'a> = (u8, &'a [u8], usize);

/// The frame at the front of `buf`, or `Ok(None)` while it is incomplete.
pub fn decode(buf: &[u8]) -> Result<Option<Decoded<'_>>, FrameError> {
    let Some(header) = buf.get(..HEADER_LEN) else {
        return Ok(None);
    };
    let len = body_len(header.try_into().expect("four bytes"))?;
    let Some(body) = buf.get(HEADER_LEN..HEADER_LEN + len) else {
        return Ok(None);
    };
    let (tag, payload) = split_body(body)?;
    Ok(Some((tag, payload, HEADER_LEN + len)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_length_tag_payload() {
        assert_eq!(
            encode(TAG_TEXT, b"hi").unwrap(),
            vec![0, 0, 0, 3, TAG_TEXT, b'h', b'i']
        );
        assert_eq!(
            encode(TAG_BINARY, b"").unwrap(),
            vec![0, 0, 0, 1, TAG_BINARY]
        );
    }

    #[test]
    fn round_trips_and_reports_consumed() {
        let mut buf = encode(TAG_BINARY, &[9, 8, 7]).unwrap();
        buf.extend(encode(TAG_TEXT, b"{\"type\":\"ack\"}").unwrap());
        let (tag, payload, used) = decode(&buf).unwrap().unwrap();
        assert_eq!((tag, payload, used), (TAG_BINARY, &[9u8, 8, 7][..], 8));
        let (tag, payload, used2) = decode(&buf[used..]).unwrap().unwrap();
        assert_eq!((tag, payload), (TAG_TEXT, &b"{\"type\":\"ack\"}"[..]));
        assert_eq!(used + used2, buf.len());
    }

    #[test]
    fn incomplete_frames_wait() {
        let frame = encode(TAG_TEXT, b"hello").unwrap();
        for cut in 0..frame.len() {
            assert_eq!(decode(&frame[..cut]).unwrap(), None, "cut at {cut}");
        }
    }

    #[test]
    fn rejects_unknown_tags() {
        assert_eq!(encode(2, b"x"), Err(FrameError::BadTag(2)));
        assert_eq!(decode(&[0, 0, 0, 2, 7, 0]), Err(FrameError::BadTag(7)));
    }

    #[test]
    fn rejects_empty_frames() {
        assert_eq!(body_len([0, 0, 0, 0]), Err(FrameError::Empty));
        assert_eq!(split_body(&[]), Err(FrameError::Empty));
    }

    #[test]
    fn caps_the_payload() {
        let max = vec![0u8; MAX_PAYLOAD];
        let frame = encode(TAG_BINARY, &max).unwrap();
        assert_eq!(frame.len(), HEADER_LEN + 1 + MAX_PAYLOAD);
        assert_eq!(decode(&frame).unwrap().unwrap().1.len(), MAX_PAYLOAD);
        assert_eq!(
            encode(TAG_BINARY, &vec![0u8; MAX_PAYLOAD + 1]),
            Err(FrameError::TooLarge(MAX_PAYLOAD + 1))
        );
        // The header alone is refused, before anything is read or allocated.
        let over = ((MAX_PAYLOAD + 2) as u32).to_be_bytes();
        assert_eq!(body_len(over), Err(FrameError::TooLarge(MAX_PAYLOAD + 1)));
        assert_eq!(
            body_len(u32::MAX.to_be_bytes()).unwrap_err(),
            FrameError::TooLarge(u32::MAX as usize - 1)
        );
    }
}
