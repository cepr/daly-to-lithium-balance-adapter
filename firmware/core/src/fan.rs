//! Fan speed from the module temperatures (README "Fans").
//!
//! The duty cycle is the fan speed: 0 % stops the fans, 100 % is full speed. The hardware
//! inverts the signal and stops the fans while the MCU is in reset.

use crate::temps::Temp;

/// Below this the fans are off.
pub const ON_C: i8 = 35;
/// The fans stop again only below `ON_C - HYSTERESIS_C`.
pub const HYSTERESIS_C: i8 = 3;
/// At and above this the fans run at full speed.
pub const FULL_C: i8 = 45;
/// Lowest duty the fans are run at. Most PC fans don't start reliably below about 20–30 %.
pub const MIN_DUTY: u8 = 30;

/// Fan duty in percent for the 4 module temperatures. `running` is whether the fans are
/// on now (for the hysteresis). A faulty sensor runs the fans at full speed.
pub fn duty(temps: [Temp; 4], running: bool) -> u8 {
    let mut hottest = i8::MIN;
    for t in temps {
        match t {
            Temp::Ok(c) => hottest = hottest.max(c),
            Temp::Open | Temp::Short => return 100,
        }
    }
    let threshold = if running { ON_C - HYSTERESIS_C } else { ON_C };
    if hottest < threshold {
        return 0;
    }
    if hottest >= FULL_C {
        return 100;
    }
    // Linear from MIN_DUTY at ON_C to 100 % at FULL_C (MIN_DUTY within the hysteresis band).
    let above = (hottest.max(ON_C) - ON_C) as u16;
    let span = (FULL_C - ON_C) as u16;
    MIN_DUTY + ((100 - MIN_DUTY) as u16 * above / span) as u8
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all(c: i8) -> [Temp; 4] {
        [Temp::Ok(c); 4]
    }

    #[test]
    fn curve() {
        assert_eq!(duty(all(20), false), 0);
        assert_eq!(duty(all(34), false), 0);
        assert_eq!(duty(all(35), false), MIN_DUTY);
        assert_eq!(duty(all(40), false), 65);
        assert_eq!(duty(all(45), false), 100);
        assert_eq!(duty(all(60), true), 100);
    }

    #[test]
    fn hysteresis() {
        assert_eq!(duty(all(33), false), 0);
        assert_eq!(duty(all(33), true), MIN_DUTY);
        assert_eq!(duty(all(32), true), MIN_DUTY);
        assert_eq!(duty(all(31), true), 0);
    }

    #[test]
    fn hottest_module_wins() {
        assert_eq!(duty([Temp::Ok(20), Temp::Ok(45), Temp::Ok(20), Temp::Ok(20)], false), 100);
    }

    #[test]
    fn sensor_fault_runs_full_speed() {
        assert_eq!(duty([Temp::Ok(20), Temp::Open, Temp::Ok(20), Temp::Ok(20)], false), 100);
        assert_eq!(duty([Temp::Short, Temp::Ok(20), Temp::Ok(20), Temp::Ok(20)], false), 100);
    }
}
