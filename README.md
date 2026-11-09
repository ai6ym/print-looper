# Print Looper: single-plate P2S cycle processor

Print Looper now has two deliberately separate layers:

- `plate_cycle.py` and `farmloop_timelapse.py` remain standalone, compatible
  postprocessing tools; and
- `print_queue.py` is a LAN-only queue service that calls the single-plate
  processor, manages AMS mappings and color leases, monitors the printer, and
  verifies that the plate cleared before dispatching another job.

## Queue manager

The normal workflow is:

```text
slice -> export .gcode.3mf -> print_queue.py add -> automatic dispatch
```

Queue intake never changes the exported file. It makes a validated processed
artifact, and dispatch makes another temporary copy containing the active AMS
color leases. The existing processors can still be run directly exactly as
described later in this document.

### Runtime requirements

- Python 3.11 or newer;
- `paho-mqtt` for the printer's local MQTT connection;
- Pillow for clean-plate image comparison;
- `ffmpeg` for a frame from the printer's local RTSPS camera; and
- a P2S in LAN-only Developer Mode, with its local access code.

Install the two Python runtime dependencies in the environment that will run
the service:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

All queue state is local. The default state directory is `.print-looper` in
the current directory; set `PRINT_LOOPER_HOME` or pass `--home` to put it
elsewhere.

### Initial setup

```sh
.venv/bin/python print_queue.py init
```

Edit `.print-looper/config.json` and set `printer.host` and `printer.serial`.
Supply the access code without putting it in shell history:

```sh
export PRINT_LOOPER_ACCESS_CODE='your-local-access-code'
```

With the printer idle, seed the manager's physical inventory from the current
non-RFID AMS configuration and capture an empty-plate reference:

```sh
.venv/bin/python print_queue.py ams import-live
.venv/bin/python print_queue.py camera baseline
```

Review the camera ROI and thresholds in `config.json`. The reference should be
captured with the normal lighting, camera position, empty build plate, and the
printer in its post-cycle parked position.

Start the long-running manager under the local service supervisor of your
choice:

```sh
.venv/bin/python print_queue.py run
```

`run --once` performs one state-machine iteration and is useful for setup and
diagnostics. Only one manager process should use a queue database.

### Queue controls

All controls are ordinary CLI commands and therefore work naturally over SSH:

```sh
# Postprocess and queue one exported plate
.venv/bin/python print_queue.py add plate.gcode.3mf

# Add five copies; each copy remains an independent printer job
.venv/bin/python print_queue.py add plate.gcode.3mf --copies 5

# Use one mapped spool and turn AMS Filament Backup off for this job
.venv/bin/python print_queue.py add plate.gcode.3mf --strict --map 1=A2

.venv/bin/python print_queue.py list
.venv/bin/python print_queue.py move JOB_ID 1
.venv/bin/python print_queue.py delete JOB_ID
.venv/bin/python print_queue.py repeat JOB_ID 4
.venv/bin/python print_queue.py pause
.venv/bin/python print_queue.py resume
.venv/bin/python print_queue.py events
```

`pause` lets the active print finish and still performs plate verification, but
does not send another job. A camera or printer failure blocks and pauses the
queue. After physically inspecting and correcting the printer, use `unblock`;
the queue deliberately remains paused until a separate `resume`.

`list` always shows the current queue in dispatch order, including each job's
state and refill/strict mode plus the global running, paused, or blocked state.
Use `--json list` for scripts.

### Non-RFID AMS inventory and reloads

The queue stores two colors for every slot:

- **real color** is the physical filament color declared by the operator; and
- **printer color** is the current AMS metadata, which may be an imperceptibly
  different leased RGB value.

Declare a newly loaded spool before expecting it to participate in a future
job:

```sh
.venv/bin/python print_queue.py ams load A3 \
  --preset Pd848248 --material PETG --color 000000 \
  --temp-min 220 --temp-max 260
```

During a print this records a pending change only. The manager will not write
AMS metadata until the printer is idle. A slot locked into the current job's
refill cohort cannot be redeclared as different filament; use an outside slot
or wait. This refusal has no force bypass.

`setting_id` and the pressure-advance calibration index are inherited from an
existing exact physical peer when possible. For a new identity, pass
`--setting-id` and/or `--cali-idx`; otherwise the printer's default calibration
(`-1`) is selected deliberately. Metadata temperature bounds and calibration
selection are both verified from fresh AMS telemetry before dispatch.

Before the next dispatch, pending contents are written and read back. Exact
physical matches are grouped automatically. For example, if A1/A2 already use
a black-PETG lease of `#000001`, a newly declared black `#000000` spool in A3
is changed to `#000001` and joins that refill cohort. Slots outside the cohort
retain a different printer color and cannot be selected by firmware refill.

The dispatched 3MF copy receives the same lease colors in all three relevant
locations: `project_settings.config`, `slice_info.config`, and the sliced
G-code header. Alpha is never used as a discriminator. The manager preserves
the exact sliced plate member name (for example `Metadata/plate_5.gcode`) when
starting the job.

Leases persist between jobs until regrouping is necessary. To stop managed
operation, restore physical colors, and hand ordinary AMS metadata back to
BambuStudio:

```sh
.venv/bin/python print_queue.py release-printer
```

At job completion, `tray_exist_bits` is used as the authoritative physical
presence signal. Any leased slot that has run empty is removed from the cohort
and the manager attempts to restore its real color before another dispatch.
If firmware refuses a metadata write to an empty tray, the slot is quarantined
and its stale lease becomes a collision; the next active cohort is moved to a
different lease color. This preserves safe reloading even on that firmware.

### Plate-clear verification and recovery

After every managed job, the next item remains blocked behind a camera check.
The current frame is brightness-normalized and compared only inside the
configured build-plate ROI. If it is not sufficiently close to the clean
reference, the manager repeats the complete 16-pass slow/fast clearing sweep.

Recovery pass height is:

```text
max(original clear height - recovery_attempt * z_drop_mm, 5 mm)
```

The original height is still `max(last printed Z - 10 mm, 5 mm)`. After the
configured number of unsuccessful recovery attempts, the job is marked failed
and the queue is blocked and paused. It never dispatches on an ambiguous image
or camera failure.

### Safety and current hardware-validation status

The archive pipeline and queue state machine are covered by unit tests and
have been exercised against the supplied two-filament P2S plate, including its
non-default `Metadata/plate_5.gcode` member. MQTT commands, FTPS upload, exact
AMS color readback, auto-refill grouping, camera thresholds, recovery motion,
and start-job acceptance still require a supervised test on the actual P2S.

Run the test suite with:

```sh
python3 -m unittest discover -v
```

`plate_cycle.py` turns one normally sliced Bambu P2S plate into one
independently queueable print job. It applies the useful FarmLoop-style setup
and teardown changes without concatenating multiple prints:

- replaces the on-bed nozzle load line with a non-bed prime;
- preserves the sliced file's resolved `G29.1` bed-compensation command;
- cools the bed before part removal;
- performs six P2S flex cycles;
- performs 16 slow/fast forward clearing passes;
- parks the printer in a clean, heaters-off, motors-enabled state for the next
  queue item or recovery clearing pass; and
- runs the timelapse correction pass described below, including synthetic
  teardown frames, continuous layer numbering, and one final timelapse close.

The default clearing Z is:

```text
max(last printed Z - 10 mm, 5 mm)
```

The resolved height and all cycle counts are included in the processing
report. Use `--clear-height` to override the automatic result for unusual part
geometry.

## Single-plate usage

```sh
python3 plate_cycle.py sliced-plate.gcode.3mf -o queued-job.gcode.3mf
python3 plate_cycle.py sliced-plate.gcode.3mf -o stage2.gcode.3mf --stage 2
python3 plate_cycle.py sliced-plate.gcode.3mf --dry-run
python3 plate_cycle.py queued-job.gcode.3mf --validate
```

Add `--json` for a machine-readable report. Other cycle settings can be
inspected with `python3 plate_cycle.py --help`. Inputs and existing outputs are
never overwritten. For 3MF inputs, all non-G-code members and ZIP metadata are
preserved and the G-code MD5 sidecar is recomputed.

Stage 1 is the default. `--stage 2` opts into the external-actuator sequence:
startup reset/arm, MQTT temperature sentinels, physical switch taps for the
door/fan/bender controller, external bending waits, the normal clearing sweep,
and the final safety/door-close triggers. Queue intake exposes the same choice
as `print_queue.py add ... --stage 2`; omitting it keeps Stage 1.

The processor recognizes resolved BambuStudio P2S start/end structures and
fails closed if required anchors are missing, duplicated, or out of order. It
also rejects already processed input instead of applying setup or teardown a
second time.

> P2S flex/sweep motion and synthetic timelapse layer behavior have not yet
> been verified on hardware. Inspect the report/diff and supervise the first
> printer test.

## Existing FarmLoop timelapse repair tool

`farmloop_timelapse.py` repairs layer numbering across concatenated FarmLoop
Stage 2 prints and adds logical timelapse layers around the P2S ejection
motions. It never reruns FarmLoop and does not rewrite motion, extrusion,
temperature, or acceleration commands.

> The P2S firmware response to synthetic layer changes has not yet been
> verified on hardware. Treat the generated file as a testable implementation,
> inspect its report/diff, and supervise the first printer test.

### FarmLoop clear-routine recognition

Each reset/ejection routine must be bounded by FarmLoop's
`@fl:section:start/end "p2s.clear.motion"` markers.

Inside that boundary the tool requires:

- the `BENDING MOTION` marker and matched, numbered `bend-up stroke` /
  `bend-down stroke` Z commands;
- a higher absolute Z target for `bend-up`, which increases nozzle-to-bed
  distance and therefore lowers the physical P2S bed, followed by a lower Z
  target that raises the bed again;
- the `PUSH SECTION` marker and a `central sweeps` or `rake` subsection; and
- absolute rear-to-front (decreasing Y) moves in those sweep subsections.

It does not classify Y travel elsewhere as part clearing. An incomplete,
relative-mode, mismatched, or structurally unfamiliar clear routine is rejected
instead of being guessed at.

The synthetic test fixtures exercise multiple concatenated blocks, bend cycles,
forward clearing passes, archive member preservation, and MD5 sidecar updates.
Original timelapse finish commands are neutralized and one conditional finish
is added after the last synthetic frame.

### Usage

```sh
python3 farmloop_timelapse.py input.gcode -o output.gcode
python3 farmloop_timelapse.py input.gcode.3mf -o output.gcode.3mf
python3 farmloop_timelapse.py input.3mf --dry-run
python3 farmloop_timelapse.py patched.gcode --validate
```

Add `--json` for a machine-readable report. A normal transformation also runs
the full validation pass. Output files are never overwritten. If `-o` is
omitted, `.timelapse` is inserted before the input suffix, preserving names such
as `FL_S2_...`.

For archives, the original member order and ZIP metadata are retained,
the unambiguous sliced G-code member is changed, and its uppercase MD5 sidecar
is recomputed. Other members are copied unchanged.

### Validation guarantees

The validator checks consecutive `M73 L` / `M991 P` pairs, one final finish
after all generated snapshots, updated header totals, one capture per synthetic
layer, balanced `M622`/`M623` conditionals, preservation of existing snapshots,
and a digest of every non-target original line. Generated blocks are visibly
marked with `FARMLOOP_TIMELAPSE`; already-patched inputs are rejected by the
transformer.

Run the tests with:

```sh
python3 -m unittest discover -v
```
