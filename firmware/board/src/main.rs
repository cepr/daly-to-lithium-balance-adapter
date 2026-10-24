//! Interface board firmware: emulates the 10 Lithium Balance LMUs on the BMCU slave bus,
//! using cell voltages read from the 4 DALY balancers and the 4 module NTCs.
//!
//! Tasks:
//! - `bmcu`: USART2, RS485 (DE on PA1), 38400 8N1. Answers the BMCU.
//! - `balancer` ×4: USART1/3/5/6, 9600 8N1 through the ISO7721 isolators. Polls one DALY each.
//! - `temperatures`: ADC1, the 4 NTC inputs.
//! - `fans`: TIM1_CH1 on PA8, 25 kHz PWM for the 8 fans.
//! - `status`: status LED.
//! - `store_settings`: saves the LMU settings to flash after the diagnostic software writes them.
//!
//! Pin assignment: README "Hardware" section and `hardware/interface_board/`.
//!
//! With the `nucleo` feature the same firmware runs on a NUCLEO-G0B1RE: balancer #4 moves
//! to PC0/PC1, the status LED to the green user LED on PA5, and the clock runs from the
//! internal HSI16 instead of the 8 MHz oscillator (see `firmware/README.md`).
//!
//! With the `simulate` feature the balancers and NTCs are ignored: every cell reads 3.9 V and
//! every temperature 25 °C, to test the BMCU link alone.
#![no_std]
#![no_main]

#[cfg(all(feature = "board", feature = "nucleo"))]
compile_error!("enable only one of the features `board` and `nucleo` (use --no-default-features for `nucleo`)");
#[cfg(not(any(feature = "board", feature = "nucleo")))]
compile_error!("enable one of the features `board` or `nucleo`");

use core::cell::RefCell;

use bridge_core::{cells, daly, fan, lmu, sbms, storage, temps, CELLS_PER_LMU, MAX_CHANNELS, N_BALANCERS, N_LMU};
use defmt::{info, warn};
use embassy_executor::Spawner;
use embassy_stm32::adc::{Adc, AdcChannel, AnyAdcChannel, SampleTime};
use embassy_stm32::flash::{Blocking, Flash};
use embassy_stm32::gpio::{Level, Output, Speed};
use embassy_stm32::peripherals::{ADC1, TIM1};
use embassy_stm32::time::Hertz;
use embassy_stm32::timer::low_level::CountingMode;
use embassy_stm32::timer::simple_pwm::{PwmPin, SimplePwm};
use embassy_stm32::usart::{self, BufferedUart};
use embassy_stm32::{bind_interrupts, peripherals};
use embassy_sync::blocking_mutex::Mutex;
use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::signal::Signal;
use embassy_time::{Duration, Instant, Timer, with_deadline, with_timeout};
use embedded_io_async::{Read, Write};
use static_cell::StaticCell;
use {defmt_rtt as _, panic_probe as _};

bind_interrupts!(struct Irqs {
    USART1 => usart::BufferedInterruptHandler<peripherals::USART1>;
    USART2_LPUART2 => usart::BufferedInterruptHandler<peripherals::USART2>;
    USART3_4_5_6_LPUART1 => usart::BufferedInterruptHandler<peripherals::USART3>,
        usart::BufferedInterruptHandler<peripherals::USART5>,
        usart::BufferedInterruptHandler<peripherals::USART6>;
});

/// Expected string count of balancers #1…#4 (README "Balancer wiring").
const EXPECTED_CELLS: [u16; N_BALANCERS] = [24, 24, 24, 20];
/// Time between two polls of the same balancer. A real-time reply takes about 135 ms.
const BALANCER_POLL: Duration = Duration::from_millis(500);
/// Measured on a real balancer: the reply ends up to 510 ms after the request (it answers on
/// its own ~1 s cycle), so with the poll above each balancer is read about once a second.
const BALANCER_TIMEOUT: Duration = Duration::from_millis(1000);
/// Balancer data older than this is reported to the BMCU as a cell fault. Several polls, so a
/// single lost reply is not a fault.
const STALE_AFTER: Duration = Duration::from_millis(5000);
/// Pause between the end of a BMCU request and our reply, so its driver is released.
const TURNAROUND: Duration = Duration::from_millis(1);
/// Write "never sleep" to every balancer at start-up. Leave off until register 0x008A has
/// been confirmed on a real balancer (DALY doc §8): without it the balancers stop
/// answering after 1 h.
const DISABLE_SLEEP: bool = false;
/// Flash page holding the LMU settings (offset from the start of flash). `memory.x` keeps the
/// program out of it.
const SETTINGS_OFFSET: u32 = 126 * 1024;
const SETTINGS_PAGE: u32 = 2 * 1024;
/// Save the settings once the diagnostic software has stopped writing for this long: one Write
/// click writes all 10 LMUs in about 3 s, and is saved with a single page erase.
const SETTINGS_SAVE_DELAY: Duration = Duration::from_secs(2);
/// `simulate` feature: the balancer and temperature tasks don't run, and the BMCU reads
/// `SIM_CELL_MV` on every cell and `SIM_TEMP_C` on every temperature (fans stay off).
const SIMULATE: bool = cfg!(feature = "simulate");
const SIM_CELL_MV: u16 = 3900;
const SIM_TEMP_C: i16 = 25;

/// Latest data from one balancer.
#[derive(Clone, Copy)]
struct Balancer {
    at: Instant,
    cells: [u16; MAX_CHANNELS],
    board_temp_c: i16,
}

/// Latest measurements, written by the polling tasks and read by the BMCU task.
struct Shared {
    balancers: [Option<Balancer>; N_BALANCERS],
    temps: [temps::Temp; 4],
}

static SHARED: Mutex<CriticalSectionRawMutex, RefCell<Shared>> = Mutex::new(RefCell::new(Shared {
    balancers: [None; N_BALANCERS],
    // Until the first ADC pass, report the sensors as open so the BMCU sees a fault.
    temps: [temps::Temp::Open; 4],
}));

fn snapshot() -> lmu::Snapshot {
    if SIMULATE {
        let t = [temps::from_celsius(SIM_TEMP_C); 4];
        return lmu::Snapshot { cells: [[SIM_CELL_MV; CELLS_PER_LMU]; N_LMU], temps: temps::lmu_temp_regs(t, t) };
    }
    let now = Instant::now();
    SHARED.lock(|s| {
        let s = s.borrow();
        let fresh = s.balancers.map(|b| b.filter(|b| now - b.at < STALE_AFTER));
        let bal: cells::Balancers = fresh.map(|b| b.map(|b| b.cells));
        // A missing balancer reports its PCB sensor as open, like an NTC before the first ADC pass.
        let pcb = fresh.map(|b| b.map_or(temps::Temp::Open, |b| temps::from_celsius(b.board_temp_c)));
        lmu::Snapshot { cells: cells::lmu_cells(&bal), temps: temps::lmu_temp_regs(pcb, s.temps) }
    })
}

fn all_balancers_fresh() -> bool {
    if SIMULATE {
        return true;
    }
    let now = Instant::now();
    SHARED.lock(|s| s.borrow().balancers.iter().all(|b| matches!(b, Some(b) if now - b.at < STALE_AFTER)))
}

/// Settings changed by the diagnostic software, for `store_settings`.
static SETTINGS_CHANGED: Signal<CriticalSectionRawMutex, [lmu::Settings; N_LMU]> = Signal::new();

#[embassy_executor::task]
async fn bmcu(mut uart: BufferedUart<'static>, mut settings: [lmu::Settings; N_LMU]) {
    let mut rx = sbms::Receiver::new();
    let mut byte = [0u8; 1];
    loop {
        if uart.read_exact(&mut byte).await.is_err() {
            continue; // framing / noise error: the receiver resyncs on the next command letter
        }
        let Some(raw) = rx.push(byte[0]) else { continue };
        let Some(frame) = sbms::parse(raw) else { continue };
        let before = settings;
        let (event, reply) = lmu::handle(&frame, &snapshot(), &mut settings);
        if settings != before {
            SETTINGS_CHANGED.signal(settings);
        }
        match event {
            lmu::Event::Ignored | lmu::Event::Read { .. } => {}
            e => info!("bmcu: {}", e),
        }
        if let Some(mut reply) = reply {
            Timer::after(TURNAROUND).await;
            let _ = uart.write_all(reply.finish()).await;
            let _ = uart.flush().await;
        }
    }
}

fn load_settings(flash: &mut Flash<'static, Blocking>) -> [lmu::Settings; N_LMU] {
    let mut record = [0u8; storage::RECORD_LEN];
    match flash.blocking_read(SETTINGS_OFFSET, &mut record).ok().and_then(|_| storage::decode(&record)) {
        Some(s) => {
            info!("settings: loaded from flash");
            s
        }
        None => {
            info!("settings: none in flash, using the defaults");
            [lmu::DEFAULT_SETTINGS; N_LMU]
        }
    }
}

#[embassy_executor::task]
async fn store_settings(mut flash: Flash<'static, Blocking>) {
    loop {
        let mut settings = SETTINGS_CHANGED.wait().await;
        // Wait until the writes stop, keeping the latest settings.
        while let Ok(s) = with_timeout(SETTINGS_SAVE_DELAY, SETTINGS_CHANGED.wait()).await {
            settings = s;
        }
        // The CPU stalls for the page erase (about 40 ms): the BMCU may miss a reply and retry.
        let record = storage::encode(&settings);
        let ok = flash.blocking_erase(SETTINGS_OFFSET, SETTINGS_OFFSET + SETTINGS_PAGE).is_ok()
            && flash.blocking_write(SETTINGS_OFFSET, &record).is_ok();
        if ok {
            info!("settings: saved to flash");
        } else {
            warn!("settings: flash write FAILED");
        }
    }
}

/// Discard bytes left over from an earlier reply.
async fn drain(uart: &mut BufferedUart<'static>) {
    let mut junk = [0u8; 32];
    while let Ok(Ok(_)) = with_timeout(Duration::from_millis(5), uart.read(&mut junk)).await {}
}

/// Send `request` and read exactly `reply.len()` bytes before the timeout.
async fn transact(uart: &mut BufferedUart<'static>, request: &[u8], reply: &mut [u8]) -> bool {
    drain(uart).await;
    if uart.write_all(request).await.is_err() || uart.flush().await.is_err() {
        return false;
    }
    matches!(with_deadline(Instant::now() + BALANCER_TIMEOUT, uart.read_exact(reply)).await, Ok(Ok(())))
}

#[embassy_executor::task(pool_size = 4)]
async fn balancer(index: usize, mut uart: BufferedUart<'static>) {
    let n = index + 1;
    if DISABLE_SLEEP {
        let req = daly::write_request(daly::REG_SLEEP_TIME, 0xFFFF);
        let mut echo = [0u8; 8];
        let ok = transact(&mut uart, &req, &mut echo).await && echo == req;
        info!("balancer #{}: disable sleep {}", n, if ok { "ok" } else { "FAILED" });
    }
    let mut reply = [0u8; daly::REALTIME_REPLY_LEN];
    let mut online = false;
    loop {
        let result = if transact(&mut uart, &daly::REALTIME_REQUEST, &mut reply).await {
            daly::parse_realtime(&reply).map_err(Some)
        } else {
            Err(None) // timeout
        };
        match result {
            Ok(rt) if rt.cell_count == EXPECTED_CELLS[index] => {
                if !online {
                    info!("balancer #{}: online, {} cells, state {}", n, rt.cell_count, rt.balancer_state);
                    online = true;
                }
                let b = Balancer { at: Instant::now(), cells: rt.cells, board_temp_c: rt.board_temp_c };
                SHARED.lock(|s| s.borrow_mut().balancers[index] = Some(b));
            }
            Ok(rt) => warn!("balancer #{}: reports {} cells, expected {}", n, rt.cell_count, EXPECTED_CELLS[index]),
            Err(e) => {
                if online {
                    warn!("balancer #{}: no valid reply ({})", n, e);
                    online = false;
                }
            }
        }
        Timer::after(BALANCER_POLL).await;
    }
}

#[embassy_executor::task]
async fn temperatures(mut adc: Adc<'static, ADC1>, mut inputs: [AnyAdcChannel<'static, ADC1>; 4]) {
    loop {
        let t = core::array::from_fn(|i| {
            // Average 8 samples: the filter is 1 kΩ + 100 nF, so the long sample time is
            // there to charge the ADC capacitor from it.
            let sum: u32 = (0..8).map(|_| adc.blocking_read(&mut inputs[i], SampleTime::CYCLES160_5) as u32).sum();
            temps::from_adc((sum / 8) as u16)
        });
        SHARED.lock(|s| s.borrow_mut().temps = t);
        Timer::after_millis(500).await;
    }
}

/// PWM frequency of the 4-wire fan spec (21–28 kHz).
const FAN_PWM: Hertz = Hertz::khz(25);

#[embassy_executor::task]
async fn fans(mut pwm: SimplePwm<'static, TIM1>) {
    // Duty = fan speed. PA8 high turns Q2B off (via Q2A), so the fans' PWM line floats high (run);
    // PA8 low or floating (reset) turns Q2B on and grounds it (stop). See README "Fans".
    let mut ch = pwm.ch1();
    ch.set_duty_cycle_fully_off();
    ch.enable();
    // Wait for the first temperature pass: before it the sensors read "open" (full speed).
    Timer::after_millis(1000).await;
    let mut duty = 0;
    loop {
        let t = SHARED.lock(|s| s.borrow().temps);
        let new = fan::duty(t, duty > 0);
        if new != duty {
            info!("fans: {}%", new);
            duty = new;
        }
        ch.set_duty_cycle_percent(duty);
        Timer::after_millis(1000).await;
    }
}

#[embassy_executor::task]
async fn status(mut led: Output<'static>) {
    loop {
        led.toggle();
        // Slow blink: all 4 balancers answer. Fast blink: at least one is missing.
        Timer::after_millis(if all_balancers_fresh() { 500 } else { 100 }).await;
    }
}

/// Interface board: 8 MHz oscillator X1 (ASE-8.000MHZ-LC-T, ±50 ppm) on PC14 / OSC_IN in HSE
/// bypass mode (the LQFP-32 has no OSC_OUT, so no passive crystal), PLL to 64 MHz.
#[cfg(feature = "board")]
fn clock_config() -> embassy_stm32::Config {
    use embassy_stm32::rcc::*;
    use embassy_stm32::time::Hertz;
    let mut config = embassy_stm32::Config::default();
    config.rcc.hse = Some(Hse { freq: Hertz::mhz(8), mode: HseMode::Bypass });
    config.rcc.pll = Some(Pll {
        source: PllSource::HSE,
        prediv: PllPreDiv::DIV1,
        mul: PllMul::MUL16, // VCO 128 MHz
        divp: None,
        divq: None,
        divr: Some(PllRDiv::DIV2), // SYSCLK 64 MHz
    });
    config.rcc.sys = Sysclk::PLL1_R;
    // When CAN is implemented, clock FDCAN straight from the oscillator:
    // config.rcc.mux.fdcansel = mux::Fdcansel::HSE;
    config
}

/// Nucleo: no HSE fitted by default, stay on the internal HSI16 (±1 %, fine for the UARTs).
#[cfg(feature = "nucleo")]
fn clock_config() -> embassy_stm32::Config {
    embassy_stm32::Config::default()
}

fn uart_config(baud: u32) -> usart::Config {
    let mut c = usart::Config::default();
    c.baudrate = baud;
    c
}

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    let p = embassy_stm32::init(clock_config());
    info!("interface board start");
    if SIMULATE {
        warn!("SIMULATION: balancers and NTCs ignored, cells {} mV, temperatures {} C", SIM_CELL_MV, SIM_TEMP_C);
        // Read by the fans task: keeps the fans off.
        SHARED.lock(|s| s.borrow_mut().temps = [temps::from_celsius(SIM_TEMP_C); 4]);
    }

    // CAN transceiver stays in standby (STBY high) until CAN is implemented.
    // Leaked so the pin keeps driving after `main` returns.
    core::mem::forget(Output::new(p.PA15, Level::High, Speed::Low));

    macro_rules! buffers {
        ($tx:expr, $rx:expr) => {{
            static TX: StaticCell<[u8; $tx]> = StaticCell::new();
            static RX: StaticCell<[u8; $rx]> = StaticCell::new();
            (TX.init([0; $tx]), RX.init([0; $rx]))
        }};
    }

    // BMCU bus: USART2, TX PA2, RX PA3, driver enable PA1 (hardware DE).
    let (tx, rx) = buffers!(64, 256);
    let bmcu_uart = BufferedUart::new_with_de(p.USART2, p.PA3, p.PA2, p.PA1, Irqs, tx, rx, uart_config(38_400))
        .expect("USART2 config");
    let mut flash = Flash::new_blocking(p.FLASH);
    let settings = load_settings(&mut flash);
    spawner.spawn(bmcu(bmcu_uart, settings).unwrap());
    spawner.spawn(store_settings(flash).unwrap());

    // Balancers #1…#4: USART1 (PB6/PB7), USART5 (PB3/PB4), USART3 (PB2/PB0), USART6 (PA4/PA5).
    let (tx, rx) = buffers!(16, 256);
    let b1 = BufferedUart::new(p.USART1, p.PB7, p.PB6, tx, rx, Irqs, uart_config(9_600)).expect("USART1 config");
    let (tx, rx) = buffers!(16, 256);
    let b2 = BufferedUart::new(p.USART5, p.PB4, p.PB3, tx, rx, Irqs, uart_config(9_600)).expect("USART5 config");
    let (tx, rx) = buffers!(16, 256);
    let b3 = BufferedUart::new(p.USART3, p.PB0, p.PB2, tx, rx, Irqs, uart_config(9_600)).expect("USART3 config");
    // On the Nucleo, PA5 drives the user LED, so balancer #4 uses USART6 on PC0 (TX) / PC1 (RX).
    #[cfg(feature = "board")]
    let (b4_rx, b4_tx, led) = (p.PA5, p.PA4, p.PB5);
    #[cfg(feature = "nucleo")]
    let (b4_rx, b4_tx, led) = (p.PC1, p.PC0, p.PA5);
    let (tx, rx) = buffers!(16, 256);
    let b4 = BufferedUart::new(p.USART6, b4_rx, b4_tx, tx, rx, Irqs, uart_config(9_600)).expect("USART6 config");
    if !SIMULATE {
        for (i, uart) in [b1, b2, b3, b4].into_iter().enumerate() {
            spawner.spawn(balancer(i, uart).unwrap());
        }
    }

    // NTC inputs T1…T4: PA0, PA6, PA7, PB1.
    let adc = Adc::new(p.ADC1);
    let inputs = [p.PA0.degrade_adc(), p.PA6.degrade_adc(), p.PA7.degrade_adc(), p.PB1.degrade_adc()];
    if !SIMULATE {
        spawner.spawn(temperatures(adc, inputs).unwrap());
    }

    spawner.spawn(status(Output::new(led, Level::Low, Speed::Low)).unwrap());

    let fan_pin = PwmPin::new(p.PA8, embassy_stm32::gpio::OutputType::PushPull);
    let pwm = SimplePwm::new(p.TIM1, Some(fan_pin), None, None, None, FAN_PWM, CountingMode::EdgeAlignedUp);
    spawner.spawn(fans(pwm).unwrap());

    // Reserved, not implemented yet: FDCAN1 (PB8/PB9), debug LPUART2 TX (PC6).
}
