//! Hardware-independent logic of the interface board.
//!
//! Everything here is `no_std` and runs unchanged on the STM32 and on the PC, so it is
//! tested with a plain `cargo test` (see `firmware/README.md`).
//!
//! - [`sbms`]: Lithium Balance s-BMS slave-bus framing and CRC
//!   (`LithiumBalance_sBMS_RS485_Protocol.md` §2–§3).
//! - [`lmu`]: the 10 emulated LMUs answering the BMCU (§5–§7).
//! - [`daly`]: DALY balancer D2 protocol (`DALY_Active_Balancer_UART_Protocol.md` §4).
//! - [`cells`]: balancer channels → LMU cells (README "Cell mapping").
//! - [`temps`]: NTC inputs and balancer PCB sensors → LMU temperatures (README "Temperature mapping").
//! - [`fan`]: fan speed from the module temperatures (README "Fans").
//! - [`storage`]: flash record of the LMU settings.
#![cfg_attr(not(test), no_std)]

pub mod cells;
pub mod daly;
pub mod fan;
pub mod lmu;
pub mod sbms;
pub mod storage;
pub mod temps;

/// Number of emulated LMUs (s-BMS slave addresses 0x01…0x0A).
pub const N_LMU: usize = 10;
/// Cells per LMU.
pub const CELLS_PER_LMU: usize = 8;
/// Number of DALY balancers.
pub const N_BALANCERS: usize = 4;
/// Channels read from each balancer (#4 is a 20S unit and only uses the first 20).
pub const MAX_CHANNELS: usize = 24;
/// Cells per battery module.
pub const CELLS_PER_MODULE: usize = 20;
