//! The 10 emulated LMUs (protocol doc §5–§7).
//!
//! Measurement registers are computed from a [`Snapshot`] of the latest balancer and
//! temperature data each time they are read. The only stored state is the [`Settings`] that the
//! diagnostic software reads and writes on its "Local cell settings" tab (§7.5). The PC's
//! requests reach the bus through the BMCU with `SRC = FE`, and replies go back to the sender.

use crate::sbms::{self, FrameBuf};
use crate::{CELLS_PER_LMU, N_LMU};

pub const MASTER: u8 = 0x00;
pub const UNADDRESSED: u8 = 0xF0;

pub const REG_CELLS: u16 = 0x7D0;
pub const REG_INFO: u16 = 0x7E0;
pub const REG_TEMPS: u16 = 0x7E5;
const REG_SPACE: u16 = 0x1000;

// Configuration registers of the diagnostic software (§7.5).
pub const REG_BOOT_VERSION: u16 = 0x000;
/// 0x004–0x009: uint16 BE, uint8, uint8, ASCII character, uint8 → "PPPPP MM.mm.C.BBB".
pub const REG_FW_VERSION: u16 = 0x004;
/// 0x046–0x055: Cell 1–8 enables, Max °C, Δ °C, Max mV (BE), Δ mV, unused, Batt 1/2 enables.
pub const REG_SETTINGS: u16 = 0x046;
/// 0x0E0–0x0EE: 15 ASCII characters, stored in reverse, shown as "XXXXX-XXXXXXXXXX".
pub const REG_SERIAL: u16 = 0x0E0;
pub const SETTINGS_LEN: usize = 16;
const SERIAL_LEN: u16 = 15;

const BOOT_VERSION: u8 = 1;
/// Shown as "00000 00.01.A.000".
const FW_VERSION: [u8; 6] = [0x00, 0x00, 0, 1, b'A', 0];
/// Serial number "IFB00-00000000nn", nn = LMU address (only the last two digits vary).
const SERIAL_PREFIX: &[u8; 13] = b"IFB0000000000";

/// Registers 0x046–0x055 of one LMU (writable from the diagnostic software).
pub type Settings = [u8; SETTINGS_LEN];

/// All 8 cells enabled, Max 55 °C, Δ 10 °C, Max 4145 mV, Δ 25 mV, Batt 1 on, Batt 2 off.
pub const DEFAULT_SETTINGS: Settings = {
    let mv = 4145u16;
    [1, 1, 1, 1, 1, 1, 1, 1, 55, 10, (mv >> 8) as u8, mv as u8, 25, 0, 1, 0]
};

/// Character `i` (0-based, as displayed) of the serial number of LMU `index`.
fn serial_char(index: usize, i: usize) -> u8 {
    let n = index as u8 + 1;
    match i {
        13 => b'0' + n / 10,
        14 => b'0' + n % 10,
        _ => SERIAL_PREFIX[i],
    }
}

/// Everything the BMCU can read from the 10 LMUs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Snapshot {
    /// Cell register words (mV or a §7.1 code), per LMU.
    pub cells: [[u16; CELLS_PER_LMU]; N_LMU],
    /// Temperature registers T1…T4 (0x7E5–0x7E8, °C or a §7.2 code), per LMU.
    pub temps: [[u8; 4]; N_LMU],
}

/// Byte at register `reg` of LMU `index` (0-based).
pub fn register(snap: &Snapshot, settings: &Settings, index: usize, reg: u16) -> u8 {
    match reg {
        REG_BOOT_VERSION => BOOT_VERSION,
        0x004..=0x009 => FW_VERSION[(reg - REG_FW_VERSION) as usize],
        0x046..=0x055 => settings[(reg - REG_SETTINGS) as usize],
        0x0E0..=0x0EE => serial_char(index, (SERIAL_LEN - 1 - (reg - REG_SERIAL)) as usize),
        REG_CELLS..=0x7DF => {
            let off = (reg - REG_CELLS) as usize;
            let v = snap.cells[index][off / 2];
            if off.is_multiple_of(2) { (v >> 8) as u8 } else { v as u8 }
        }
        // 0x7E0–0x7E3: two int16 of unknown meaning, read once from slave 1. Report 0.
        0x7E5..=0x7E8 => snap.temps[index][(reg - REG_TEMPS) as usize],
        _ => 0,
    }
}

/// What the board did with a frame (for logging).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum Event {
    /// Not for us (another address, a BMCU broadcast, a reply from another node).
    Ignored,
    /// For us, but with a bad CRC: stay silent so the BMCU retries.
    BadCrc,
    /// Malformed request for one of our addresses.
    Malformed,
    /// `i` enumeration: took address `addr`.
    Assigned { addr: u8, last: bool },
    /// `q` read served.
    Read { addr: u8, reg: u16 },
    /// `w` write acknowledged. Bytes in the settings range are stored, the rest ignored.
    Write { addr: u8, reg: u16, len: u16 },
    /// Request outside the register space: NAK.
    OutOfRange { addr: u8 },
}

fn lmu_index(addr: u8) -> Option<usize> {
    (1..=N_LMU as u8).contains(&addr).then(|| addr as usize - 1)
}

/// Handle one frame from the bus. Returns the event and, when the board must answer,
/// the reply frame (without the CR the BMCU puts in front of its own frames).
/// `settings` holds the stored settings of the 10 LMUs, updated by `w` requests.
pub fn handle(frame: &sbms::Frame, snap: &Snapshot, settings: &mut [Settings; N_LMU]) -> (Event, Option<FrameBuf>) {
    let p = frame.payload;

    // Start-up enumeration. The real LMUs pass the token down a daisy chain; here every
    // address 1…10 is answered, so the BMCU finds exactly 10 modules.
    if frame.cmd == b'i' && frame.dest == UNADDRESSED {
        if !frame.crc_ok {
            return (Event::BadCrc, None);
        }
        let Some(v) = p.get(..2).and_then(sbms::hex_value) else {
            return (Event::Malformed, None);
        };
        let addr = (v & 0x7F) as u8;
        if lmu_index(addr).is_none() {
            return (Event::Ignored, None);
        }
        let mut f = FrameBuf::new(b'!', MASTER, addr);
        f.push(b'i');
        return (Event::Assigned { addr, last: v & 0x80 != 0 }, Some(f));
    }

    // Requests to one of our addresses are answered, whether from the BMCU (SRC 00) or
    // forwarded from the diagnostic PC (SRC FE). The board answers addresses 1…10 even
    // before enumeration, so it can restart (watchdog) without the BMCU noticing.
    let Some(index) = lmu_index(frame.dest) else {
        return (Event::Ignored, None);
    };
    if !matches!(frame.cmd, b'q' | b'w') {
        return (Event::Ignored, None);
    }
    if !frame.crc_ok {
        return (Event::BadCrc, None);
    }
    let addr = frame.dest;
    let (Some(reg), Some(n)) = (p.get(..3).and_then(sbms::hex_value), p.get(3..4).and_then(sbms::hex_value))
    else {
        return (Event::Malformed, None);
    };
    let n = n + 1;
    let to = frame.src;

    if reg + n > REG_SPACE {
        let mut f = FrameBuf::new(b'#', to, addr);
        f.push(frame.cmd);
        f.push(b'D');
        return (Event::OutOfRange { addr }, Some(f));
    }

    if frame.cmd == b'w' {
        let data = &p[4..];
        if data.len() != 2 * n as usize {
            return (Event::Malformed, None);
        }
        let mut bytes = [0u8; 16];
        for (i, b) in bytes[..n as usize].iter_mut().enumerate() {
            let Some(v) = sbms::hex_value(&data[2 * i..2 * i + 2]) else {
                return (Event::Malformed, None);
            };
            *b = v as u8;
        }
        // Store what falls in the settings range. Other writes (the bootloader entry at
        // 0x002: there is no bootloader to enter) are acknowledged and ignored.
        for (r, &b) in (reg..reg + n).zip(&bytes) {
            if let Some(i) = r.checked_sub(REG_SETTINGS).filter(|&i| (i as usize) < SETTINGS_LEN) {
                settings[index][i as usize] = b;
            }
        }
        let mut f = FrameBuf::new(b'!', to, addr);
        f.push(b'w');
        return (Event::Write { addr, reg, len: n }, Some(f));
    }

    let mut f = FrameBuf::new(b'r', to, addr);
    f.push_str(&p[..4]);
    for r in reg..reg + n {
        f.push_hex(register(snap, &settings[index], index, r));
    }
    (Event::Read { addr, reg }, Some(f))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap() -> Snapshot {
        let mut s = Snapshot { cells: [[3300; 8]; 10], temps: [[30, 0x7A, 25, 0x7A]; 10] };
        s.cells[0] = [3300, 3302, 3297, 3304, 3298, 3299, 3301, 3303];
        s.temps[0] = [31, 0x7A, 23, 0x7A];
        s
    }

    fn run(req: &[u8]) -> (Event, Option<Vec<u8>>) {
        run_with(req, &mut [DEFAULT_SETTINGS; N_LMU])
    }

    fn run_with(req: &[u8], settings: &mut [Settings; N_LMU]) -> (Event, Option<Vec<u8>>) {
        let f = sbms::parse(req).unwrap();
        let (e, r) = handle(&f, &snap(), settings);
        (e, r.map(|mut r| r.finish().to_vec()))
    }

    /// Request from the diagnostic PC, forwarded by the BMCU (SRC FE).
    fn pc(cmd: u8, addr: u8, payload: &[u8]) -> Vec<u8> {
        let mut f = FrameBuf::new(cmd, addr, 0xFE);
        f.push_str(payload);
        f.finish().to_vec()
    }

    /// Payload of a reply, without the CRC and CR.
    fn body(r: &[u8]) -> &[u8] {
        assert!(sbms::parse(r).unwrap().crc_ok);
        &r[..r.len() - 3]
    }

    #[test]
    fn enumeration() {
        let (e, r) = run(b"iF00001\xC3\xC6\r");
        assert_eq!(e, Event::Assigned { addr: 1, last: false });
        assert_eq!(r.unwrap(), b"!0001i\x98\xDC\r");
        let (e, _) = run(b"iF00084\x42\xA8\r");
        assert_eq!(e, Event::Assigned { addr: 4, last: true });
    }

    #[test]
    fn enumeration_beyond_ten_is_silent() {
        let mut f = FrameBuf::new(b'i', UNADDRESSED, MASTER);
        f.push_hex(11);
        assert_eq!(run(f.finish()), (Event::Ignored, None));
    }

    #[test]
    fn read_cells() {
        let (e, r) = run(b"q01007D0F\xDB\x1C\r");
        assert_eq!(e, Event::Read { addr: 1, reg: 0x7D0 });
        assert_eq!(r.unwrap(), b"r00017D0F0CE40CE60CE10CE80CE20CE30CE50CE7\x1C\x38\r");
    }

    #[test]
    fn read_temps() {
        let (_, r) = run(b"q01007E54\x42\x54\r");
        let r = r.unwrap();
        assert_eq!(&r[..r.len() - 3], b"r00017E541F7A177A00");
        assert!(sbms::parse(&r).unwrap().crc_ok);
    }

    #[test]
    fn bad_crc_is_silent() {
        assert_eq!(run(b"q01007D0F\xDB\x1D\r"), (Event::BadCrc, None));
    }

    #[test]
    fn other_addresses_ignored() {
        let mut f = FrameBuf::new(b'q', 0x21, MASTER);
        f.push_str(b"7D07");
        assert_eq!(run(f.finish()).0, Event::Ignored);
    }

    #[test]
    fn bootloader_write_acked() {
        let mut f = FrameBuf::new(b'w', 0x03, MASTER);
        f.push_str(b"0020AC");
        let (e, r) = run(f.finish());
        assert_eq!(e, Event::Write { addr: 3, reg: 0x002, len: 1 });
        assert_eq!(&r.unwrap()[..6], b"!0003w");
    }

    #[test]
    fn pc_reads_config() {
        // Frames as captured from the diagnostic software (lower-case register address).
        let (_, r) = run(b"q01FE0e0E},\r");
        assert_eq!(body(&r.unwrap()), b"rFE010e0E313030303030303030303030424649"); // "IFB00-0000000001" reversed
        let (_, r) = run(&pc(b'q', 0x0A, b"0e0E"));
        let r = r.unwrap();
        let serial: Vec<u8> = body(&r)[9..].chunks(2).map(|c| sbms::hex_value(c).unwrap() as u8).rev().collect();
        assert_eq!(serial, b"IFB000000000010");
        let (_, r) = run(&pc(b'q', 1, b"0000"));
        assert_eq!(body(&r.unwrap()), b"rFE01000001");
        let (_, r) = run(&pc(b'q', 1, b"0045"));
        assert_eq!(body(&r.unwrap()), b"rFE010045000000014100");
        let (_, r) = run(&pc(b'q', 1, b"046C"));
        assert_eq!(body(&r.unwrap()), b"rFE01046C0101010101010101370A103119");
        let (_, r) = run(&pc(b'q', 1, b"0541"));
        assert_eq!(body(&r.unwrap()), b"rFE0105410100");
    }

    #[test]
    fn pc_writes_settings() {
        let mut settings = [DEFAULT_SETTINGS; N_LMU];
        // Captured Write frame: Cell 8 off, 60 °C, 10 °C, 4200 mV, 50 mV.
        let (e, r) = run_with(b"w01FE046c01010101010101003C0A106832lR\r", &mut settings);
        assert_eq!(e, Event::Write { addr: 1, reg: 0x046, len: 13 });
        assert_eq!(r.unwrap(), b"!FE01w0\x18\r");
        let (_, r) = run_with(&pc(b'w', 1, b"05410001"), &mut settings);
        assert_eq!(body(&r.unwrap()), b"!FE01w");
        assert_eq!(settings[0], [1, 1, 1, 1, 1, 1, 1, 0, 60, 10, 0x10, 0x68, 50, 0, 0, 1]);
        assert_eq!(settings[1], DEFAULT_SETTINGS);
        let (_, r) = run_with(&pc(b'q', 1, b"046C"), &mut settings);
        assert_eq!(body(&r.unwrap()), b"rFE01046C01010101010101003C0A106832");
    }

    #[test]
    fn malformed_write_is_silent() {
        assert_eq!(run(&pc(b'w', 1, b"0541010")).0, Event::Malformed);
    }
}
