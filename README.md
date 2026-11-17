# ClogGuard

Early detection and correction of developing nozzle clogs on a desktop FDM 3D printer running Klipper.

University of Guelph ENGG*41x0 capstone, Group 51.

## Status

Planning. No code yet, no hardware selected. Nothing here has run on a printer.

## What it will do

1. Log printer state and sensor channels during every print.
2. Compare each signal with what a healthy nozzle would show at the same flow and temperature.
3. Warn when a clog is developing, with an estimate of the time left before a visible defect.
4. Apply a fixed ladder of safe actions (slow down, raise temperature within the filament limit, purge) and verify the result.
5. Pause and alert the user if the clog cannot be recovered.

## Repository

- `docs/plan.md`: the specification, taken from the project proposal and plan.
- `MASTER_PROMPT.md`: the build prompt for the software.

## Safety

The software can send only a short, fixed list of commands to the printer, cannot exceed the filament's rated temperature, and cannot touch the firmware's thermal-runaway protection. See "Hard limits" in `docs/plan.md`.
