//! s-BMS slave-bus framing (protocol doc §2–§3).
//!
//! Frame: `CMD | DEST (2 hex) | SRC (2 hex) | PAYLOAD (ASCII hex) | CRC_H CRC_L (raw) | CR`

pub const CR: u8 = 0x0D;
/// Longest frame accepted, command letter to CR included.
pub const MAX_FRAME: usize = 120;
/// Command letters that may start a frame on the bus.
const FRAME_START: &[u8] = b"wqri!#apd:";

const fn crc_table() -> [u16; 256] {
    let mut t = [0u16; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = (i as u16) << 8;
        let mut k = 0;
        while k < 8 {
            c = if c & 0x8000 != 0 { (c << 1) ^ 0xAC9A } else { c << 1 };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

static TABLE: [u16; 256] = crc_table();

const fn fix(b: u8) -> u8 {
    match b {
        0x00 | 0x0D | 0x1B => b + 1,
        _ => b,
    }
}

/// CRC-16 (poly 0xAC9A, init 0xFFFF) with the NUL/CR/ESC byte fix-up, high byte first.
pub fn crc(data: &[u8]) -> [u8; 2] {
    let mut c: u16 = 0xFFFF;
    for &b in data {
        c = (c << 8) ^ TABLE[((c >> 8) as u8 ^ b) as usize];
    }
    [fix((c >> 8) as u8), fix(c as u8)]
}

/// Streaming receiver: hunts for a command letter and collects bytes up to CR.
///
/// The CRC fix-up guarantees a CRC byte is never 0x0D, so CR always ends a frame.
pub struct Receiver {
    buf: [u8; MAX_FRAME],
    len: usize,
}

impl Default for Receiver {
    fn default() -> Self {
        Self::new()
    }
}

impl Receiver {
    pub const fn new() -> Self {
        Self { buf: [0; MAX_FRAME], len: 0 }
    }

    /// Feed one byte. Returns the complete frame (command letter to CR) when one ends.
    pub fn push(&mut self, b: u8) -> Option<&[u8]> {
        if self.len == 0 {
            if FRAME_START.contains(&b) {
                self.buf[0] = b;
                self.len = 1;
            }
            return None;
        }
        if self.len == MAX_FRAME {
            // Too long: drop it and hunt for the next frame start.
            self.len = 0;
            return None;
        }
        self.buf[self.len] = b;
        self.len += 1;
        if b == CR {
            let n = self.len;
            self.len = 0;
            return Some(&self.buf[..n]);
        }
        None
    }
}

/// A frame split into its fields. `payload` borrows from the received bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Frame<'a> {
    pub cmd: u8,
    pub dest: u8,
    pub src: u8,
    pub payload: &'a [u8],
    pub crc_ok: bool,
}

/// Value of one upper- or lower-case hex digit.
pub fn hex_digit(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'A'..=b'F' => Some(c - b'A' + 10),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    }
}

/// Parse hex digits into a number (at most 4 digits).
pub fn hex_value(s: &[u8]) -> Option<u16> {
    if s.is_empty() || s.len() > 4 {
        return None;
    }
    s.iter().try_fold(0u16, |acc, &c| Some(acc << 4 | hex_digit(c)? as u16))
}

/// Split `raw` (command letter up to and including CR) into its fields.
pub fn parse(raw: &[u8]) -> Option<Frame<'_>> {
    if raw.len() < 1 + 4 + 2 + 1 || raw[raw.len() - 1] != CR {
        return None;
    }
    let body = &raw[..raw.len() - 3];
    let crc_rx = &raw[raw.len() - 3..raw.len() - 1];
    Some(Frame {
        cmd: body[0],
        dest: hex_value(&body[1..3])? as u8,
        src: hex_value(&body[3..5])? as u8,
        payload: &body[5..],
        crc_ok: crc(body) == crc_rx,
    })
}

/// Longest frame this board sends: `r` + addresses + `AAAN` + 16 data bytes + CRC + CR.
pub const MAX_TX: usize = 1 + 4 + 4 + 32 + 3;

/// Builder for an outgoing frame.
pub struct FrameBuf {
    buf: [u8; MAX_TX],
    len: usize,
}

impl FrameBuf {
    pub fn new(cmd: u8, dest: u8, src: u8) -> Self {
        let mut f = Self { buf: [0; MAX_TX], len: 0 };
        f.push(cmd);
        f.push_hex(dest);
        f.push_hex(src);
        f
    }

    pub fn push(&mut self, b: u8) {
        self.buf[self.len] = b;
        self.len += 1;
    }

    pub fn push_str(&mut self, s: &[u8]) {
        for &b in s {
            self.push(b);
        }
    }

    /// Append a byte as two upper-case hex digits.
    pub fn push_hex(&mut self, b: u8) {
        const HEX: &[u8; 16] = b"0123456789ABCDEF";
        self.push(HEX[(b >> 4) as usize]);
        self.push(HEX[(b & 0xF) as usize]);
    }

    /// Append the CRC and the CR, and return the finished frame.
    pub fn finish(&mut self) -> &[u8] {
        let c = crc(&self.buf[..self.len]);
        self.push(c[0]);
        self.push(c[1]);
        self.push(CR);
        &self.buf[..self.len]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// CRCs from the examples in the protocol document (§6.1, §7.3).
    #[test]
    fn crc_reference_frames() {
        assert_eq!(crc(b"iF00001"), [0xC3, 0xC6]);
        assert_eq!(crc(b"!0001i"), [0x98, 0xDC]);
        assert_eq!(crc(b"iF00084"), [0x42, 0xA8]);
        assert_eq!(crc(b"q01007D0F"), [0xDB, 0x1C]);
        assert_eq!(crc(b"r00017D0F0CE40CE60CE10CE80CE20CE30CE50CE7"), [0x1C, 0x38]);
        assert_eq!(crc(b"q01007E54"), [0x42, 0x54]);
        assert_eq!(crc(b"r00017E541716181A7A"), [0xDE, 0x5A]);
    }

    #[test]
    fn crc_never_contains_cr() {
        for a in 0..=255u8 {
            for b in 0..=255u8 {
                let c = crc(&[b'q', a, b]);
                assert!(!c.contains(&CR) && !c.contains(&0) && !c.contains(&0x1B));
            }
        }
    }

    #[test]
    fn receiver_skips_junk_and_lone_cr() {
        let mut rx = Receiver::new();
        let mut frames = 0;
        let stream = [b"\r\x55\x00".as_slice(), b"q01007D0F\xDB\x1C\r"].concat();
        for &b in &stream {
            if let Some(raw) = rx.push(b) {
                let f = parse(raw).unwrap();
                assert_eq!((f.cmd, f.dest, f.src, f.payload, f.crc_ok), (b'q', 1, 0, b"7D0F".as_slice(), true));
                frames += 1;
            }
        }
        assert_eq!(frames, 1);
    }

    #[test]
    fn bad_crc_is_reported() {
        let f = parse(b"q01007D0F\xDB\x1D\r").unwrap();
        assert!(!f.crc_ok);
    }

    #[test]
    fn build_ack() {
        let mut f = FrameBuf::new(b'!', 0x00, 0x01);
        f.push(b'i');
        assert_eq!(f.finish(), b"!0001i\x98\xDC\r");
    }
}
