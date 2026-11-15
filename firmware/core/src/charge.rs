//! The BMCU's charge counter, kept in LMU 1 (protocol doc §7.6).
//!
//! The BMCU has no non-volatile memory for its coulomb counter. It writes it to registers
//! `0x7E0`–`0x7E3` of slave 1 every few seconds and reads it back once at start-up to restore
//! its SoC. The board keeps the 4 bytes in RAM (`lmu::handle`) and logs them to a ring of flash
//! pages, so the counter survives a power cycle.
//!
//! Flash log: [`PAGES`] pages of [`SLOTS`] 8-byte records (the STM32G0 write unit). A record is
//! the 4 register bytes, a 16-bit sequence number and a CRC-16. Records are appended in order;
//! when a page is full the next record goes to the start of the next page, then the full page
//! is erased. All pages but the current one are therefore blank, and a power loss at any point
//! leaves at least the previous record readable. At start-up the newest valid record wins.

use crate::daly;

/// Registers `0x7E0`–`0x7E3`, as the BMCU writes them: remaining charge (int16 LE), then the
/// coulomb-counter remainder below one unit (int16 LE).
pub type Charge = [u8; 4];

/// Value before the BMCU has ever written one.
pub const NONE: Charge = [0; 4];

pub const PAGE_SIZE: usize = 2048;
pub const PAGES: usize = 8;
pub const RECORD_LEN: usize = 8;
pub const SLOTS: usize = PAGE_SIZE / RECORD_LEN;
/// Size of the flash region holding the log.
pub const REGION_SIZE: usize = PAGES * PAGE_SIZE;

pub type Record = [u8; RECORD_LEN];
const BLANK: Record = [0xFF; RECORD_LEN];

fn encode(c: &Charge, seq: u16) -> Record {
    let mut r = [0u8; RECORD_LEN];
    r[..4].copy_from_slice(c);
    r[4..6].copy_from_slice(&seq.to_le_bytes());
    let crc = daly::crc16(&r[..6]);
    r[6..].copy_from_slice(&crc.to_le_bytes());
    r
}

fn decode(r: &Record) -> Option<(Charge, u16)> {
    if *r == BLANK || daly::crc16(&r[..6]) != u16::from_le_bytes([r[6], r[7]]) {
        return None;
    }
    Some(([r[0], r[1], r[2], r[3]], u16::from_le_bytes([r[4], r[5]])))
}

/// `a` was written after `b`. At most `PAGES * SLOTS` records exist, far less than half the
/// sequence range, so the wrapping difference orders them.
fn newer(a: u16, b: u16) -> bool {
    (a.wrapping_sub(b) as i16) > 0
}

/// Where the next record goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Log {
    page: usize,
    next: usize,
    seq: u16,
}

/// Result of reading the log at start-up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Boot {
    /// Newest valid record, `None` if the log is blank or unreadable.
    pub charge: Option<Charge>,
    pub log: Log,
    /// Pages to erase before the first append (left over from a power loss, or corrupt).
    pub erase: [bool; PAGES],
}

/// Read the log. `read(page, slot)` returns one record.
pub fn scan(mut read: impl FnMut(usize, usize) -> Record) -> Boot {
    let mut best: Option<(usize, u16, Charge)> = None;
    let mut last_used = [None; PAGES];
    for (page, last) in last_used.iter_mut().enumerate() {
        for slot in 0..SLOTS {
            let r = read(page, slot);
            if r == BLANK {
                continue;
            }
            *last = Some(slot);
            if let Some((c, seq)) = decode(&r)
                && best.is_none_or(|(_, s, _)| newer(seq, s))
            {
                best = Some((page, seq, c));
            }
        }
    }
    let (page, seq, charge) = match best {
        Some((page, seq, c)) => (page, seq, Some(c)),
        None => (0, 0, None),
    };
    let mut erase = last_used.map(|l| l.is_some());
    let next = if charge.is_some() {
        erase[page] = false;
        // After the last used slot: a torn write after the newest record is skipped.
        last_used[page].map_or(0, |s| s + 1)
    } else {
        0
    };
    Boot { charge, log: Log { page, next, seq }, erase }
}

/// One append: write `record` at (`page`, `slot`), then erase page `erase` if set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Append {
    pub page: usize,
    pub slot: usize,
    pub record: Record,
    pub erase: Option<usize>,
}

impl Append {
    /// Offset of the record from the start of the log region.
    pub fn offset(&self) -> usize {
        self.page * PAGE_SIZE + self.slot * RECORD_LEN
    }
}

impl Log {
    pub fn append(&mut self, c: &Charge) -> Append {
        self.seq = self.seq.wrapping_add(1);
        let record = encode(c, self.seq);
        if self.next < SLOTS {
            let a = Append { page: self.page, slot: self.next, record, erase: None };
            self.next += 1;
            a
        } else {
            let old = self.page;
            self.page = (old + 1) % PAGES;
            self.next = 1;
            Append { page: self.page, slot: 0, record, erase: Some(old) }
        }
    }
}

/// Whether a value written by the BMCU should be logged. The first value is always saved (`saved`
/// is `None` while the log is blank). After that the remaining charge (first word) is saved
/// whenever it changes; a change of the remainder alone only once the last save is old
/// (`saved_long_ago`), since losing it costs less than one unit of charge.
pub fn needs_save(saved: Option<&Charge>, new: &Charge, saved_long_ago: bool) -> bool {
    match saved {
        None => true,
        Some(saved) => new[..2] != saved[..2] || (new != saved && saved_long_ago),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// NOR flash: writes may only go to erased (0xFF) bytes.
    struct Flash(Vec<u8>);

    impl Flash {
        fn new() -> Self {
            Self(vec![0xFF; REGION_SIZE])
        }
        fn read(&self, page: usize, slot: usize) -> Record {
            let o = page * PAGE_SIZE + slot * RECORD_LEN;
            self.0[o..o + RECORD_LEN].try_into().unwrap()
        }
        fn erase(&mut self, page: usize) {
            self.0[page * PAGE_SIZE..(page + 1) * PAGE_SIZE].fill(0xFF);
        }
        fn write(&mut self, a: &Append) {
            let o = a.offset();
            assert!(self.0[o..o + RECORD_LEN].iter().all(|&b| b == 0xFF), "write to a non-erased slot");
            self.0[o..o + RECORD_LEN].copy_from_slice(&a.record);
        }
        fn boot(&mut self) -> (Option<Charge>, Log) {
            let b = scan(|p, s| self.read(p, s));
            for (p, &e) in b.erase.iter().enumerate() {
                if e {
                    self.erase(p);
                }
            }
            (b.charge, b.log)
        }
        fn append(&mut self, log: &mut Log, c: &Charge) {
            let a = log.append(c);
            self.write(&a);
            if let Some(p) = a.erase {
                self.erase(p);
            }
        }
    }

    fn value(i: u32) -> Charge {
        i.to_le_bytes()
    }

    #[test]
    fn blank_flash() {
        let mut f = Flash::new();
        assert_eq!(f.boot().0, None);
    }

    #[test]
    fn newest_record_survives_reboot_across_many_page_wraps() {
        let mut f = Flash::new();
        let (_, mut log) = f.boot();
        // Several times around the ring, rebooting now and then, past the u16 sequence wrap.
        for i in 0..70_000u32 {
            f.append(&mut log, &value(i));
            if i % 997 == 0 {
                let (c, l) = f.boot();
                assert_eq!(c, Some(value(i)));
                log = l;
            }
        }
        assert_eq!(f.boot().0, Some(value(69_999)));
        // Only the current page holds records.
        let used = (0..PAGES).filter(|&p| (0..SLOTS).any(|s| f.read(p, s) != BLANK)).count();
        assert_eq!(used, 1);
    }

    #[test]
    fn power_loss_before_erase_of_full_page() {
        let mut f = Flash::new();
        let (_, mut log) = f.boot();
        for i in 0..SLOTS as u32 {
            f.append(&mut log, &value(i));
        }
        // The page switch: the record is written, the power goes before the erase.
        let a = log.append(&value(1000));
        f.write(&a);
        assert_eq!(a.erase, Some(0));
        let (c, mut log) = f.boot();
        assert_eq!(c, Some(value(1000)));
        assert!((0..SLOTS).all(|s| f.read(0, s) == BLANK), "left-over page erased at boot");
        f.append(&mut log, &value(1001));
        assert_eq!(f.boot().0, Some(value(1001)));
    }

    #[test]
    fn torn_write_is_skipped() {
        let mut f = Flash::new();
        let (_, mut log) = f.boot();
        f.append(&mut log, &value(1));
        f.append(&mut log, &value(2));
        // Third record half-written: bytes 4..8 still erased.
        let a = log.append(&value(3));
        let o = a.offset();
        f.0[o..o + 4].copy_from_slice(&a.record[..4]);
        let (c, mut log) = f.boot();
        assert_eq!(c, Some(value(2)));
        // The next append goes after the torn slot, not on top of it.
        f.append(&mut log, &value(4));
        assert_eq!(f.boot().0, Some(value(4)));
    }

    #[test]
    fn corrupt_only_page_is_erased() {
        let mut f = Flash::new();
        f.0[3 * PAGE_SIZE + 40] = 0x12;
        let b = scan(|p, s| f.read(p, s));
        assert_eq!(b.charge, None);
        assert_eq!(b.erase.iter().filter(|&&e| e).count(), 1);
        assert!(b.erase[3]);
    }

    #[test]
    fn save_policy() {
        let saved = [0x10, 0x00, 0xF6, 0xFF];
        assert!(!needs_save(Some(&saved), &saved, true));
        assert!(needs_save(Some(&saved), &[0x11, 0x00, 0xF6, 0xFF], false));
        assert!(!needs_save(Some(&saved), &[0x10, 0x00, 0xF0, 0xFF], false));
        assert!(needs_save(Some(&saved), &[0x10, 0x00, 0xF0, 0xFF], true));
        // Blank log: the first value is saved, even if it equals `NONE`.
        assert!(needs_save(None, &NONE, false));
    }
}
