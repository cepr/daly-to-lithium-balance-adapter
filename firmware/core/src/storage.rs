//! Flash record holding the LMU settings (README "LMU settings").
//!
//! One record in a dedicated flash page: magic, format version, the settings of the 10 LMUs,
//! and a CRC-16. A blank (erased) or corrupt page decodes to `None`, and the firmware then
//! uses the defaults.

use crate::lmu::{SETTINGS_LEN, Settings};
use crate::{N_LMU, daly};

const MAGIC: [u8; 4] = *b"LMUS";
const VERSION: u8 = 1;
const HEADER_LEN: usize = 8;
const DATA_LEN: usize = N_LMU * SETTINGS_LEN;
/// Header, data, CRC, padded to the 8-byte flash write unit of the STM32G0.
pub const RECORD_LEN: usize = (HEADER_LEN + DATA_LEN + 2).next_multiple_of(8);

pub type Record = [u8; RECORD_LEN];

pub fn encode(settings: &[Settings; N_LMU]) -> Record {
    let mut r = [0xFF; RECORD_LEN];
    r[..4].copy_from_slice(&MAGIC);
    r[4] = VERSION;
    r[5..HEADER_LEN].fill(0);
    for (chunk, s) in r[HEADER_LEN..HEADER_LEN + DATA_LEN].as_chunks_mut::<SETTINGS_LEN>().0.iter_mut().zip(settings) {
        *chunk = *s;
    }
    let crc = daly::crc16(&r[..HEADER_LEN + DATA_LEN]);
    r[HEADER_LEN + DATA_LEN..HEADER_LEN + DATA_LEN + 2].copy_from_slice(&crc.to_le_bytes());
    r
}

pub fn decode(r: &Record) -> Option<[Settings; N_LMU]> {
    if r[..4] != MAGIC || r[4] != VERSION {
        return None;
    }
    let crc = u16::from_le_bytes([r[HEADER_LEN + DATA_LEN], r[HEADER_LEN + DATA_LEN + 1]]);
    if daly::crc16(&r[..HEADER_LEN + DATA_LEN]) != crc {
        return None;
    }
    let (chunks, _) = r[HEADER_LEN..HEADER_LEN + DATA_LEN].as_chunks::<SETTINGS_LEN>();
    Some(core::array::from_fn(|i| chunks[i]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lmu::DEFAULT_SETTINGS;

    #[test]
    fn round_trip() {
        let mut s = [DEFAULT_SETTINGS; N_LMU];
        s[0][7] = 0;
        s[9][15] = 1;
        let r = encode(&s);
        assert_eq!(RECORD_LEN % 8, 0);
        assert_eq!(decode(&r), Some(s));
    }

    #[test]
    fn blank_or_corrupt_page() {
        assert_eq!(decode(&[0xFF; RECORD_LEN]), None);
        let mut r = encode(&[DEFAULT_SETTINGS; N_LMU]);
        r[20] ^= 1;
        assert_eq!(decode(&r), None);
    }
}
