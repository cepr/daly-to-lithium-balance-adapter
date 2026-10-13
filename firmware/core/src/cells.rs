//! Balancer channels → LMU cell registers (README "Cell mapping").
//!
//! Global cell g (0-based) is in module m = g / 20, position k = g % 20. Its primary
//! reading is balancer m, channel k. Cells 1–4 of modules 2–4 are also read by the
//! balancer below (channel 20 + k), and the two readings are compared.

use crate::{CELLS_PER_LMU, CELLS_PER_MODULE, MAX_CHANNELS, N_BALANCERS, N_LMU};

/// Cell measurement fault (§7.1).
pub const CELL_FAULT: u16 = 0x4000;
/// "Flagged" bit (§7.1): the BMCU raises a fault and still uses the lower 15 bits.
pub const CELL_FLAGGED: u16 = 0x8000;
/// Readings at or above this cannot be a real cell and would collide with the §7.1 codes.
const MAX_PLAUSIBLE_MV: u16 = 0x2000;
/// Largest allowed difference between the two readings of an overlap cell.
pub const OVERLAP_TOLERANCE_MV: u16 = 50;

/// Latest cell voltages of each balancer, `None` when missing or stale.
pub type Balancers = [Option<[u16; MAX_CHANNELS]>; N_BALANCERS];

/// Register value of global cell `g` (0-based).
pub fn cell_word(bal: &Balancers, g: usize) -> u16 {
    let (m, k) = (g / CELLS_PER_MODULE, g % CELLS_PER_MODULE);
    let Some(primary) = bal[m].map(|b| b[k]) else {
        return CELL_FAULT;
    };
    if primary >= MAX_PLAUSIBLE_MV {
        return CELL_FAULT;
    }
    let second = (m > 0 && k < MAX_CHANNELS - CELLS_PER_MODULE)
        .then(|| bal[m - 1].map(|b| b[CELLS_PER_MODULE + k]))
        .flatten();
    match second {
        Some(s) if primary.abs_diff(s) > OVERLAP_TOLERANCE_MV => primary | CELL_FLAGGED,
        _ => primary,
    }
}

/// Cell registers of all 10 LMUs.
pub fn lmu_cells(bal: &Balancers) -> [[u16; CELLS_PER_LMU]; N_LMU] {
    core::array::from_fn(|lmu| core::array::from_fn(|i| cell_word(bal, lmu * CELLS_PER_LMU + i)))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Balancer b, channel c reads 3000 + 100·b + c, so every value shows its source.
    fn all() -> Balancers {
        core::array::from_fn(|b| Some(core::array::from_fn(|c| 3000 + 100 * b as u16 + c as u16)))
    }

    #[test]
    fn mapping_matches_readme_table() {
        let mut bal = all();
        // Make the overlap channels agree with the primary readings.
        for b in 0..3 {
            for k in 0..4 {
                bal[b].as_mut().unwrap()[20 + k] = bal[b + 1].unwrap()[k];
            }
        }
        let c = lmu_cells(&bal);
        assert_eq!(c[0], [3000, 3001, 3002, 3003, 3004, 3005, 3006, 3007]); // #1 ch 1–8
        assert_eq!(c[2], [3016, 3017, 3018, 3019, 3100, 3101, 3102, 3103]); // #1 ch 17–20 + #2 ch 1–4
        assert_eq!(c[3][0], 3104); // #2 ch 5
        assert_eq!(c[5][0], 3200); // #3 ch 1
        assert_eq!(c[7], [3216, 3217, 3218, 3219, 3300, 3301, 3302, 3303]); // #3 ch 17–20 + #4 ch 1–4
        assert_eq!(c[9][7], 3319); // #4 ch 20
    }

    #[test]
    fn overlap_mismatch_is_flagged() {
        let mut bal = all();
        bal[1].as_mut().unwrap()[0] = 3600;
        bal[0].as_mut().unwrap()[20] = 3600 + OVERLAP_TOLERANCE_MV + 1;
        assert_eq!(cell_word(&bal, 20), 3600 | CELL_FLAGGED);
        bal[0].as_mut().unwrap()[20] = 3600 + OVERLAP_TOLERANCE_MV;
        assert_eq!(cell_word(&bal, 20), 3600);
    }

    #[test]
    fn missing_balancer_faults_its_module_only() {
        let mut bal = all();
        bal[1] = None;
        let c = lmu_cells(&bal);
        assert_eq!(c[2][3], 3019);
        assert_eq!(c[2][4], CELL_FAULT); // cell 21 is primary on #2
        assert_eq!(c[4][7], CELL_FAULT); // cell 40
        assert_eq!(c[5][0], 3200); // cell 41: #3 valid, #2 second reading missing → accepted
    }

    #[test]
    fn implausible_value_is_fault() {
        let mut bal = all();
        bal[3].as_mut().unwrap()[19] = 0xFFFF;
        assert_eq!(cell_word(&bal, 79), CELL_FAULT);
    }
}
