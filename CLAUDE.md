# Claude notes

## Hardware tests: the shared defmt log

The interface board is on an ST-LINK attached to this PC. Claude owns the probe and the log
file `firmware/rtt.log` (git-ignored); the user follows it from another terminal with
`tail -F firmware/rtt.log`. Claude may start, stop and clear the log at any time without asking.

- **Log everything to `firmware/rtt.log`.** Run every probe-rs recipe (`just run`, `just run-sim`,
  `just log`) from `firmware/` in the background, with its output redirected to
  the file: `just run > rtt.log 2>&1`. `>` clears the file, `>>` appends to it. Do not filter
  the log.
- **One probe-rs at a time.** The ST-LINK can only be used by one process: stop the running log
  (stop the background task) before flashing or attaching again, or probe-rs fails with
  "probe in use".
- **Flash the firmware under test.** `just run*` flash, reset, then log.
  `just log` only attaches to what is already on the chip (no flash, no reset) and needs the
  ELF that was flashed, so after a rebuild use `just run*` instead.
- **Leave a log running** after a test, so the user keeps seeing the board.
