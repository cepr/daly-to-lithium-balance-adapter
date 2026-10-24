//! Balancer channels → LMU cell registers (README "Cell mapping").
//!
//! Global cell g (0-based) is in module m = g / 20, position k = g % 20. Its primary
//! reading is balancer m, channel k. Cells 1–4 of modules 2–4 are also read by the
//! balancer below (channel 20 + k): the BMCU gets the two readings in turn.

use crate::{CELLS_PER_LMU, CELLS_PER_MODULE, MAX_CHANNELS, N_BALANCERS, N_LMU};

/// Cell measurement fault (§7.1).
pub const CELL_FAULT: u16 = 0x4000;
/// Readings at or above this cannot be a real cell and would collide with the §7.1 codes.
const MAX_PLAUSIBLE_MV: u16 = 0x2000;

/// Latest cell voltages of each balancer, `None` when missing or stale.
pub type Balancers = [Option<[u16; MAX_CHANNELS]>; N_BALANCERS];

/// Register value of global cell `g` (0-based). For an overlap cell, `lower` selects the
/// second reading, from the balancer below, instead of the primary one; other cells ignore it.
/// A missing or implausible reading is a cell fault, even when the other one is valid.
pub fn cell_word(bal: &Balancers, g: usize, lower: bool) -> u16 {
    let (m, k) = (g / CELLS_PER_MODULE, g % CELLS_PER_MODULE);
    let overlap = m > 0 && k < MAX_CHANNELS - CELLS_PER_MODULE;
    let reading = if lower && overlap {
        bal[m - 1].map(|b| b[CELLS_PER_MODULE + k])
    } else {
        bal[m].map(|b| b[k])
    };
    match reading {
        Some(mv) if mv < MAX_PLAUSIBLE_MV => mv,
        _ => CELL_FAULT,
    }
}

/// Cell registers of all 10 LMUs. `lower[l]` selects the second reading for the overlap
/// cells of LMU `l` (see [`cell_word`]).
pub fn lmu_cells(bal: &Balancers, lower: &[bool; N_LMU]) -> [[u16; CELLS_PER_LMU]; N_LMU] {
    core::array::from_fn(|lmu| core::array::from_fn(|i| cell_word(bal, lmu * CELLS_PER_LMU + i, lower[lmu])))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Balancer b, channel c reads 3000 + 100·b + c, so every value shows its source.
    fn all() -> Balancers {
        core::array::from_fn(|b| Some(core::array::from_fn(|c| 3000 + 100 * b as u16 + c as u16)))
    }

    const PRIMARY: [bool; N_LMU] = [false; N_LMU];
    const SECOND: [bool; N_LMU] = [true; N_LMU];

    #[test]
    fn mapping_matches_readme_table() {
        let bal = all();
        let c = lmu_cells(&bal, &PRIMARY);
        assert_eq!(c[0], [3000, 3001, 3002, 3003, 3004, 3005, 3006, 3007]); // #1 ch 1–8
        assert_eq!(c[2], [3016, 3017, 3018, 3019, 3100, 3101, 3102, 3103]); // #1 ch 17–20 + #2 ch 1–4
        assert_eq!(c[3][0], 3104); // #2 ch 5
        assert_eq!(c[5][0], 3200); // #3 ch 1
        assert_eq!(c[7], [3216, 3217, 3218, 3219, 3300, 3301, 3302, 3303]); // #3 ch 17–20 + #4 ch 1–4
        assert_eq!(c[9][7], 3319); // #4 ch 20
    }

    #[test]
    fn second_reading_of_overlap_cells() {
        let c = lmu_cells(&all(), &SECOND);
        assert_eq!(c[2], [3016, 3017, 3018, 3019, 3020, 3021, 3022, 3023]); // #1 ch 17–24
        assert_eq!(c[3][0], 3104); // #2 ch 5: not an overlap cell
        assert_eq!(c[5][..5], [3120, 3121, 3122, 3123, 3204]); // #2 ch 21–24, then #3 ch 5
        assert_eq!(c[7], [3216, 3217, 3218, 3219, 3220, 3221, 3222, 3223]); // #3 ch 17–24
        assert_eq!(c[9][7], 3319); // #4 ch 20
    }

    #[test]
    fn readings_alternate_per_lmu() {
        let mut lower = PRIMARY;
        lower[2] = true;
        let c = lmu_cells(&all(), &lower);
        assert_eq!(c[2][4], 3020); // LMU 3: #1 ch 21
        assert_eq!(c[5][0], 3200); // LMU 6: still #3 ch 1
    }

    #[test]
    fn missing_balancer_faults_its_module_only() {
        let mut bal = all();
        bal[1] = None;
        let c = lmu_cells(&bal, &PRIMARY);
        assert_eq!(c[2][3], 3019);
        assert_eq!(c[2][4], CELL_FAULT); // cell 21 is primary on #2
        assert_eq!(c[4][7], CELL_FAULT); // cell 40
        assert_eq!(c[5][0], 3200); // cell 41: primary #3
        let c = lmu_cells(&bal, &SECOND);
        assert_eq!(c[2][4], 3020); // cell 21: second reading #1 ch 21
        assert_eq!(c[5][0], CELL_FAULT); // cell 41: second reading on the missing #2
        assert_eq!(c[5][4], 3204); // cell 45: not an overlap cell
    }

    #[test]
    fn implausible_value_is_fault() {
        let mut bal = all();
        bal[3].as_mut().unwrap()[19] = 0xFFFF;
        assert_eq!(cell_word(&bal, 79, false), CELL_FAULT);
    }
}
