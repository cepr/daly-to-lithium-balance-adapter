//! NTC inputs and balancer PCB temperatures → LMU temperatures (README "Temperature mapping").
//!
//! Each LMU reports four temperatures (protocol doc §7.2): T1 "Pcb 1" = balancer PCB sensor,
//! T2 "Pcb 2" = not fitted, T3 "Bat 1" = module NTC, T4 "Bat 2" = not fitted.
//!
//! Front end: 10 kΩ 1 % pull-up to 3.3 V, NTC to ground, 1 kΩ + 100 nF filter into the ADC.
//! The NTC is assumed to be 10 kΩ at 25 °C with B = 3950: change [`NTC_TABLE`] to match the
//! sensors actually fitted to the modules.

use crate::N_LMU;

/// Full-scale code of the 12-bit ADC.
pub const ADC_MAX: u16 = 4095;
/// Above this the input is open (−40 °C gives about 3996).
const OPEN_ABOVE: u16 = 4060;
/// Below this the input is shorted (+125 °C gives about 142).
const SHORT_BELOW: u16 = 60;
const PULL_UP_OHM: u32 = 10_000;

/// Codes from protocol doc §7.2.
pub const CODE_OPEN: u8 = 0xD7;
pub const CODE_SHORT: u8 = 0x79;
pub const CODE_NOT_FITTED: u8 = 0x7A;
pub const MIN_C: i16 = -40;
pub const MAX_C: i16 = 120;

/// NTC resistance in Ω from −40 °C to +125 °C in 5 °C steps (10 kΩ, B = 3950).
const NTC_TABLE: [u32; 34] = [
    401_860, 281_577, 200_204, 144_317, 105_385, 77_898, 58_246, 44_026, 33_621, 25_925, 20_175,
    15_837, 12_535, 10_000, 8_037, 6_506, 5_301, 4_348, 3_588, 2_978, 2_486, 2_086, 1_760, 1_492,
    1_270, 1_087, 934, 805, 698, 606, 529, 463, 407, 359,
];
const TABLE_START_C: i32 = -40;
const TABLE_STEP_C: i32 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum Temp {
    /// Valid reading in °C, already clamped to −40…+120.
    Ok(i8),
    /// Open sensor or broken wire.
    Open,
    /// Shorted sensor.
    Short,
}

/// Convert a raw ADC code to a temperature.
pub fn from_adc(raw: u16) -> Temp {
    if raw > OPEN_ABOVE {
        return Temp::Open;
    }
    if raw < SHORT_BELOW {
        return Temp::Short;
    }
    let r = PULL_UP_OHM * raw as u32 / (ADC_MAX - raw) as u32;
    Temp::Ok(clamp(celsius(r)))
}

/// A temperature already in °C (balancer PCB sensor), clamped to −40…+120.
pub fn from_celsius(c: i16) -> Temp {
    Temp::Ok(clamp(c as i32))
}

/// Temperature in °C (rounded) for an NTC resistance, by linear interpolation in the table.
fn celsius(r: u32) -> i32 {
    if r >= NTC_TABLE[0] {
        return TABLE_START_C;
    }
    let i = NTC_TABLE.iter().position(|&t| t <= r).unwrap_or(NTC_TABLE.len());
    if i == NTC_TABLE.len() {
        return TABLE_START_C + TABLE_STEP_C * (NTC_TABLE.len() as i32 - 1);
    }
    // Between table points i-1 (hotter resistance hi) and i (lo).
    let (hi, lo) = (NTC_TABLE[i - 1] as i32, NTC_TABLE[i] as i32);
    let base = TABLE_START_C + TABLE_STEP_C * (i as i32 - 1);
    let num = TABLE_STEP_C * (hi - r as i32);
    let den = hi - lo;
    base + (num + den / 2) / den
}

fn clamp(c: i32) -> i8 {
    c.clamp(MIN_C as i32, MAX_C as i32) as i8
}

/// Register byte for one temperature.
pub fn code(t: Temp) -> u8 {
    match t {
        Temp::Ok(c) => c as u8,
        Temp::Open => CODE_OPEN,
        Temp::Short => CODE_SHORT,
    }
}

/// Register byte of each LMU from one reading per module (module NTCs, or the PCB
/// sensors of the balancers). LMUs 3 and 8 span two modules: `lower[l]` selects the lower
/// module's reading instead of the upper one (the module of their overlap cells' primary
/// balancer, see `cells::cell_word`); the other LMUs ignore it.
pub fn lmu_temps(t: [Temp; 4], lower: &[bool; N_LMU]) -> [u8; N_LMU] {
    let pick = |lo: usize, l: usize| if lower[l] { t[lo] } else { t[lo + 1] };
    let per_lmu = [t[0], t[0], pick(0, 2), t[1], t[1], t[2], t[2], pick(2, 7), t[3], t[3]];
    per_lmu.map(code)
}

/// Registers 0x7E5–0x7E8 (T1…T4) of each LMU from the 4 balancer PCB temperatures and the
/// 4 module NTCs (`lower`: see [`lmu_temps`]).
pub fn lmu_temp_regs(pcb: [Temp; 4], ntc: [Temp; 4], lower: &[bool; N_LMU]) -> [[u8; 4]; N_LMU] {
    let (pcb, ntc) = (lmu_temps(pcb, lower), lmu_temps(ntc, lower));
    core::array::from_fn(|l| [pcb[l], CODE_NOT_FITTED, ntc[l], CODE_NOT_FITTED])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn adc_for(r: f64) -> u16 {
        (4095.0 * r / (r + 10_000.0)).round() as u16
    }

    #[test]
    fn conversion() {
        assert_eq!(from_adc(adc_for(10_000.0)), Temp::Ok(25));
        assert_eq!(from_adc(adc_for(3_588.0)), Temp::Ok(50));
        assert_eq!(from_adc(adc_for(33_621.0)), Temp::Ok(0));
        assert_eq!(from_adc(adc_for(4_348.0)), Temp::Ok(45));
        assert_eq!(from_adc(adc_for(300_000.0)), Temp::Ok(-36));
        assert_eq!(from_adc(adc_for(450_000.0)), Temp::Ok(-40)); // below the table, clamped
        assert_eq!(from_adc(adc_for(380.0)), Temp::Ok(120)); // ~123 °C, clamped
    }

    #[test]
    fn faults() {
        assert_eq!(from_adc(4095), Temp::Open);
        assert_eq!(from_adc(0), Temp::Short);
        assert_eq!(code(Temp::Open), 0xD7);
        assert_eq!(code(Temp::Short), 0x79);
        assert_eq!(code(Temp::Ok(-40)), 0xD8);
    }

    const UPPER: [bool; N_LMU] = [false; N_LMU];
    const LOWER: [bool; N_LMU] = [true; N_LMU];

    #[test]
    fn mapping() {
        let t = [Temp::Ok(20), Temp::Ok(25), Temp::Ok(-3), Temp::Ok(-6)];
        let n = |c: i8| c as u8;
        assert_eq!(lmu_temps(t, &LOWER), [20, 20, 20, 25, 25, n(-3), n(-3), n(-3), n(-6), n(-6)]);
        assert_eq!(lmu_temps(t, &UPPER), [20, 20, 25, 25, 25, n(-3), n(-3), n(-6), n(-6), n(-6)]);
        let mut lower = UPPER;
        lower[7] = true;
        assert_eq!(lmu_temps(t, &lower)[2..8], [25, 25, 25, n(-3), n(-3), n(-3)]); // only LMU 8 switched
    }

    #[test]
    fn register_layout() {
        let pcb = [Temp::Ok(30), Temp::Ok(31), Temp::Ok(32), Temp::Open];
        let ntc = [Temp::Ok(20), Temp::Ok(21), Temp::Ok(22), Temp::Ok(23)];
        let r = lmu_temp_regs(pcb, ntc, &LOWER);
        assert_eq!(r[0], [30, CODE_NOT_FITTED, 20, CODE_NOT_FITTED]);
        assert_eq!(r[9], [CODE_OPEN, CODE_NOT_FITTED, 23, CODE_NOT_FITTED]);
        assert_eq!(from_celsius(-50), Temp::Ok(-40));
        assert_eq!(from_celsius(130), Temp::Ok(120));
    }

    #[test]
    fn fault_on_its_turn() {
        let t = [Temp::Ok(20), Temp::Open, Temp::Short, Temp::Ok(30)];
        let (lower, upper) = (lmu_temps(t, &LOWER), lmu_temps(t, &UPPER));
        assert_eq!((lower[2], upper[2]), (20, CODE_OPEN));
        assert_eq!((lower[7], upper[7]), (CODE_SHORT, 30));
        assert_eq!((lower[0], upper[9]), (20, 30));
    }
}
