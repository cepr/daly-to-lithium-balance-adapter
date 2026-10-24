//! DALY balancer D2 "Modbus-like" protocol (DALY doc §4).
//!
//! Not yet validated on a real balancer: check the frames against a capture first
//! (DALY doc §8).

use crate::MAX_CHANNELS;

pub const ADDR: u8 = 0xD2;
pub const FN_READ: u8 = 0x03;
pub const FN_WRITE: u8 = 0x06;

/// Real-time block: 80 registers from 0x0000 (DALY doc §4.5), the longer of the two
/// documented reads (it also contains 0x0043, which a BMS uses for its board temperature).
pub const REALTIME_REGS: u16 = 80;
/// Reply length for the real-time read: header 3 + 160 data + CRC 2.
pub const REALTIME_REPLY_LEN: usize = 3 + 2 * REALTIME_REGS as usize + 2;

const REG_CELL_COUNT: usize = 0x31;
const REG_BALANCER_STATE: usize = 0x34;
const REG_CELL_DELTA: usize = 0x38;
/// Balancer PCB temperature, raw − 40 → °C: "Temperature 1", the balancer's only sensor
/// (0x0032 = 1). Checked on a real balancer: 0x0043 (board temperature on a BMS) reads 0.
const REG_BOARD_TEMP: usize = 0x20;
const TEMP_OFFSET: i16 = 40;

/// Sleep wait time register; 65535 = never sleep (DALY doc §4.6).
pub const REG_SLEEP_TIME: u16 = 0x008A;

/// CRC-16/Modbus (init 0xFFFF, reflected poly 0xA001).
pub const fn crc16(data: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    let mut i = 0;
    while i < data.len() {
        crc ^= data[i] as u16;
        let mut k = 0;
        while k < 8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xA001 } else { crc >> 1 };
            k += 1;
        }
        i += 1;
    }
    crc
}

const fn request(func: u8, reg: u16, value: u16) -> [u8; 8] {
    let mut f = [ADDR, func, (reg >> 8) as u8, reg as u8, (value >> 8) as u8, value as u8, 0, 0];
    let mut body = [0u8; 6];
    let mut i = 0;
    while i < 6 {
        body[i] = f[i];
        i += 1;
    }
    let c = crc16(&body);
    f[6] = c as u8;
    f[7] = (c >> 8) as u8;
    f
}

/// Read `count` registers from `reg`.
pub const fn read_request(reg: u16, count: u16) -> [u8; 8] {
    request(FN_READ, reg, count)
}

/// Write one register. The balancer echoes the request.
pub const fn write_request(reg: u16, value: u16) -> [u8; 8] {
    request(FN_WRITE, reg, value)
}

/// `D2 03 00 00 00 50 56 55`
pub const REALTIME_REQUEST: [u8; 8] = read_request(0x0000, REALTIME_REGS);

/// Fields of the real-time block used by the interface board.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct Realtime {
    /// Cell voltages 1…24 in mV.
    pub cells: [u16; MAX_CHANNELS],
    pub cell_count: u16,
    pub balancer_state: u16,
    pub cell_delta_mv: u16,
    /// Balancer PCB temperature in °C.
    pub board_temp_c: i16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum Error {
    Length,
    Header,
    Crc,
}

/// Parse the reply to [`REALTIME_REQUEST`].
pub fn parse_realtime(reply: &[u8]) -> Result<Realtime, Error> {
    if reply.len() != REALTIME_REPLY_LEN {
        return Err(Error::Length);
    }
    if reply[..3] != [ADDR, FN_READ, 2 * REALTIME_REGS as u8] {
        return Err(Error::Header);
    }
    let n = reply.len() - 2;
    if crc16(&reply[..n]) != u16::from_le_bytes([reply[n], reply[n + 1]]) {
        return Err(Error::Crc);
    }
    let reg = |i: usize| u16::from_be_bytes([reply[3 + 2 * i], reply[4 + 2 * i]]);
    Ok(Realtime {
        cells: core::array::from_fn(reg),
        cell_count: reg(REG_CELL_COUNT),
        balancer_state: reg(REG_BALANCER_STATE),
        cell_delta_mv: reg(REG_CELL_DELTA),
        board_temp_c: reg(REG_BOARD_TEMP) as i16 - TEMP_OFFSET,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Ready-made frames from DALY doc §4.8.
    #[test]
    fn reference_frames() {
        assert_eq!(read_request(0x0000, 0x3E), [0xD2, 0x03, 0x00, 0x00, 0x00, 0x3E, 0xD7, 0xB9]);
        assert_eq!(REALTIME_REQUEST, [0xD2, 0x03, 0x00, 0x00, 0x00, 0x50, 0x56, 0x55]);
        assert_eq!(write_request(REG_SLEEP_TIME, 0xFFFF), [0xD2, 0x06, 0x00, 0x8A, 0xFF, 0xFF, 0xBA, 0x33]);
        assert_eq!(write_request(0x00CF, 1), [0xD2, 0x06, 0x00, 0xCF, 0x00, 0x01, 0x6B, 0x96]);
    }

    fn fake_reply() -> Vec<u8> {
        let mut regs = [0u16; REALTIME_REGS as usize];
        for (i, r) in regs.iter_mut().take(24).enumerate() {
            *r = 3600 + i as u16;
        }
        regs[REG_CELL_COUNT] = 24;
        regs[REG_BALANCER_STATE] = 2;
        regs[REG_CELL_DELTA] = 23;
        regs[REG_BOARD_TEMP] = 40 + 31;
        regs[0x43] = 0; // as on a real balancer
        let mut f = vec![ADDR, FN_READ, 2 * REALTIME_REGS as u8];
        for r in regs {
            f.extend_from_slice(&r.to_be_bytes());
        }
        let c = crc16(&f);
        f.extend_from_slice(&c.to_le_bytes());
        f
    }

    #[test]
    fn parse_reply() {
        let r = parse_realtime(&fake_reply()).unwrap();
        assert_eq!(r.cells[0], 3600);
        assert_eq!(r.cells[23], 3623);
        assert_eq!((r.cell_count, r.balancer_state, r.cell_delta_mv), (24, 2, 23));
        assert_eq!(r.board_temp_c, 31);
    }

    #[test]
    fn reject_bad_reply() {
        let mut f = fake_reply();
        assert_eq!(parse_realtime(&f[..100]), Err(Error::Length));
        f[50] ^= 1;
        assert_eq!(parse_realtime(&f), Err(Error::Crc));
        f[0] = 0xA5;
        assert_eq!(parse_realtime(&f), Err(Error::Header));
    }
}
