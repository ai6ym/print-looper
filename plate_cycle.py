#!/usr/bin/env python3
"""Prepare one sliced Bambu P2S plate for an externally queued print cycle.

The processor applies the FarmLoop-style non-bed prime and flex/sweep teardown
to one ordinary sliced plate.  It then runs the existing timelapse correction
pass so the added teardown motions have continuous logical layer numbers and
the timelapse is closed after the final generated clearing frame.

This intentionally does not concatenate jobs.  One input archive remains one
print job for a queue service to submit independently.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import sys
import tempfile

import farmloop_timelapse as timelapse


CYCLE_TAG = "PRINT_LOOPER_CYCLE"
SETUP_START_TEXT = ";===== for Textured PEI Plate"
SETUP_END_TEXT = ";===== noozle load line end"
MACHINE_END_TEXT = "; MACHINE_END_GCODE_START"
PULLBACK_TEXT = "; pull back filament to AMS"

MAX_Z_RE = re.compile(
    r"^\s*;\s*max_z_height\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*$", re.I
)
M109_RE = re.compile(r"^\s*M109\s+S\s*(-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|;|$)", re.I)
G1_Z_RE = re.compile(r"^\s*G1\s+Z\s*(-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|;|$)", re.I)


class CycleError(timelapse.FarmLoopError):
    """A safe refusal to transform an unfamiliar or ambiguous sliced plate."""


@dataclass(frozen=True)
class CycleConfig:
    clear_height: float
    cooldown_temperature: float = 25.0
    cooldown_waits: int = 60
    bend_cycles: int = 6
    bend_up_z: float = 235.0
    bend_down_z: float = 200.0
    park_z: float = 20.2
    prime_length: float = 50.0
    prime_feedrate: float = 200.0


@dataclass
class NativeScan:
    max_z_height: float
    nozzle_load_temperature: float
    newline: str
    setup_start_line: int
    setup_end_line: int
    cycle_replace_start_line: int
    cycle_replace_end_line: int
    finish_z_lines: tuple[int, int]
    speed_reset_before_line: int
    final_progress_line: int
    executable_end_line: int


@dataclass
class CycleReport:
    input: str
    output: str | None
    archive_member: str | None
    max_z_height: float
    clear_height: float
    cooldown_temperature: float
    cooldown_waits: int
    bend_operations: int
    clearing_passes: int
    physical_layers: int
    synthetic_layers: int
    final_logical_layers: int
    original_snapshots: int
    generated_snapshots: int
    validation: str
    warnings: list[str]


def format_number(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def default_clear_height(max_z_height: float) -> float:
    """Return FarmLoop's smart default: 10 mm below the top, but at least Z5."""
    return max(max_z_height - 10.0, 5.0)


def _require_one(name: str, values: list[int]) -> int:
    if len(values) != 1:
        raise CycleError(f"expected exactly one {name}, found {len(values)}")
    return values[0]


def scan_native_gcode(path: Path) -> NativeScan:
    if not path.is_file():
        raise CycleError(f"input G-code does not exist: {path}")

    max_z_values: list[float] = []
    setup_starts: list[int] = []
    setup_ends: list[int] = []
    nozzle_temperatures: list[float] = []
    machine_end_lines: list[int] = []
    pullback_line = 0
    post_pullback_g150 = 0
    cycle_starts: list[int] = []
    cycle_ends: list[int] = []
    z_current_line = 0
    finish_z_lines: list[int] = []
    speed_reset_lines: list[int] = []
    final_progress_lines: list[int] = []
    executable_end_lines: list[int] = []
    first_ending = ""
    in_setup = False
    in_machine_end = False

    with path.open("r", encoding="utf-8", newline="") as src:
        for line_number, raw in enumerate(src, 1):
            body, ending = timelapse.split_line_ending(raw)
            stripped = body.strip()
            if ending and not first_ending:
                first_ending = ending

            if CYCLE_TAG in body or timelapse.PATCH_TAG in body:
                raise CycleError(
                    f"line {line_number}: input is already processor-marked; refusing to cycle it twice"
                )

            max_z = MAX_Z_RE.match(body)
            if max_z:
                max_z_values.append(float(max_z.group(1)))

            if stripped.startswith(SETUP_START_TEXT):
                setup_starts.append(line_number)
                in_setup = True
            if in_setup:
                temperature = M109_RE.match(body)
                if temperature:
                    nozzle_temperatures.append(float(temperature.group(1)))
            if stripped.startswith(SETUP_END_TEXT):
                setup_ends.append(line_number)
                in_setup = False

            if stripped == MACHINE_END_TEXT:
                machine_end_lines.append(line_number)
                in_machine_end = True
            if not in_machine_end:
                continue

            if stripped == PULLBACK_TEXT:
                pullback_line = line_number
            elif pullback_line and stripped == "G150.3":
                post_pullback_g150 = line_number
            elif post_pullback_g150 and line_number > post_pullback_g150:
                if not cycle_starts and re.match(r"^M104\s+S0(?:\s|;|$)", stripped, re.I):
                    cycle_starts.append(line_number)
                elif cycle_starts and not cycle_ends and re.match(
                    r"^M400\s*;\s*wait all motion done\s*$", stripped, re.I
                ):
                    cycle_ends.append(line_number)

            if re.match(r"^M17\s+Z0\.4(?:\s|;|$)", stripped, re.I):
                z_current_line = line_number
            elif z_current_line and line_number > z_current_line and len(finish_z_lines) < 2:
                if G1_Z_RE.match(body):
                    finish_z_lines.append(line_number)

            if re.match(r"^M1015\.3\s+S0(?:\s|;|$)", stripped, re.I):
                speed_reset_lines.append(line_number)
            if re.match(r"^M73\s+P100\s+R0(?:\s|;|$)", stripped, re.I):
                final_progress_lines.append(line_number)
            if stripped == "; EXECUTABLE_BLOCK_END":
                executable_end_lines.append(line_number)
                in_machine_end = False

    if in_setup:
        raise CycleError("file ended inside the native nozzle-load setup section")
    if len(max_z_values) != 1:
        raise CycleError(f"expected exactly one max_z_height header, found {len(max_z_values)}")
    setup_start = _require_one("native plate-offset/nozzle-load start", setup_starts)
    setup_end = _require_one("native nozzle-load end", setup_ends)
    if setup_end <= setup_start:
        raise CycleError("native nozzle-load section ends before it starts")
    if len(nozzle_temperatures) != 1:
        raise CycleError(
            f"expected one resolved M109 temperature in the nozzle-load section, found {len(nozzle_temperatures)}"
        )
    _require_one("MACHINE_END_GCODE_START", machine_end_lines)
    cycle_start = _require_one("post-unload M104 S0", cycle_starts)
    cycle_end = _require_one("post-unload motion wait", cycle_ends)
    if cycle_end != cycle_start + 1:
        raise CycleError("post-unload M104 S0 is not immediately followed by the expected M400 wait")
    if len(finish_z_lines) != 2:
        raise CycleError(f"expected two final low-current Z moves, found {len(finish_z_lines)}")
    speed_reset = _require_one("final clog-detection disable", speed_reset_lines)
    if not final_progress_lines:
        raise CycleError("no final M73 P100 R0 found")
    final_progress = final_progress_lines[-1]
    executable_end = _require_one("EXECUTABLE_BLOCK_END", executable_end_lines)
    if not (cycle_end < finish_z_lines[0] < finish_z_lines[1] < speed_reset < final_progress < executable_end):
        raise CycleError("machine-end anchors occur in an unfamiliar order")

    return NativeScan(
        max_z_height=max_z_values[0],
        nozzle_load_temperature=nozzle_temperatures[0],
        newline=first_ending or "\n",
        setup_start_line=setup_start,
        setup_end_line=setup_end,
        cycle_replace_start_line=cycle_start,
        cycle_replace_end_line=cycle_end,
        finish_z_lines=(finish_z_lines[0], finish_z_lines[1]),
        speed_reset_before_line=speed_reset,
        final_progress_line=final_progress,
        executable_end_line=executable_end,
    )


def _lines(text: str, newline: str) -> list[str]:
    return [line + newline for line in text.strip("\n").split("\n")]


def setup_block(scan: NativeScan, config: CycleConfig) -> list[str]:
    temp = format_number(scan.nozzle_load_temperature)
    prime = format_number(config.prime_length)
    feed = format_number(config.prime_feedrate)
    return _lines(
        f"""; {CYCLE_TAG}_SETUP_BEGIN
; @fl:section:start "cleaning.p2s.nozzle-load-line"
; Prime away from the build plate so teardown sweeps do not encounter a load line.
G29.1 Z+0.00 ; FarmLoop P2S cycle profile
M109 S{temp}
M975 S1
G90
M83
T1000
G92 E0
G1 E{prime} F{feed}
M400
G1 X100 F21000
M400
; @fl:section:end "cleaning.p2s.nozzle-load-line"
; {CYCLE_TAG}_SETUP_END""",
        scan.newline,
    )


def cycle_block(scan: NativeScan, config: CycleConfig) -> list[str]:
    cool = format_number(config.cooldown_temperature)
    clear = format_number(config.clear_height)
    up = format_number(config.bend_up_z)
    down = format_number(config.bend_down_z)
    block: list[str] = [
        f"; {CYCLE_TAG}_TEARDOWN_BEGIN",
        "M104 S0 ; turn off hotend",
        "M140 S0 ; turn off bed",
        "M400 ; wait for queued print motion",
        "M145 P0 ; put the P2S air duct in cooling mode",
        "M106 P2 S255 ; auxiliary cooling fan",
        "M106 P3 S255 ; chamber exhaust fan",
        f"; Cool bed to {cool} C before flexing",
    ]
    block.extend(
        f"M190 S{cool} ; cooldown wait {number}/{config.cooldown_waits}"
        for number in range(1, config.cooldown_waits + 1)
    )
    block.extend(
        [
            timelapse.CLEAR_START,
            "M140 S0 ; keep bed heater off",
            "M104 S0 ; keep hotend off",
            "; BENDING MOTION",
        ]
    )
    for number in range(1, config.bend_cycles + 1):
        block.append(f"G1 Z{up} F12000 ; bend-up stroke #{number}")
        block.append(f"G1 Z{down} F12000 ; bend-down stroke #{number}")
    block.extend(
        [
            "; PUSH SECTION",
            "; choose sweep height",
            f"G1 Z{clear} F10000 ; clear-height.smart-default",
            "M400",
            "; central sweeps",
            "G1 X125 F3000",
            "G1 Y250 F3000",
            "G1 Y0 F3000",
            "G1 Y250 F3000",
            "G1 Y0 F3000",
            "; extended right-to-left rake",
        ]
    )
    for x in (220, 190, 160, 130, 100, 70, 30):
        block.extend(["G1 Y250 F3000", f"G1 X{x} F3000", "G1 Y0 F3000"])
    block.extend(
        [
            "; choose sweep height",
            "G1 Y250 F3000",
            "M400",
            "G1 X220 F3000",
            "M400",
            f"G1 Z{clear} F12000",
            "M400",
            "; extended right-to-left rake",
        ]
    )
    for x in (220, 190, 160, 130, 100, 70, 30):
        block.extend(["G1 Y250 F12000", f"G1 X{x} F12000", "G1 Y0 F12000"])
    block.extend(
        [
            "; END SECTION",
            "G1 X65 Y245 F12000 ; safe corner before parking",
            "G1 Y265 F3000 ; rear idle position",
            timelapse.CLEAR_END,
            "M106 S0 ; part cooling fan off",
            "M106 P2 S0 ; auxiliary fan off",
            "M106 P3 S0 ; chamber exhaust fan off",
            '; @fl:section:start "fan.off.left-aux"',
            "M106 P10 S0 ; left auxiliary fan off",
            '; @fl:section:end "fan.off.left-aux"',
            f"; {CYCLE_TAG}_TEARDOWN_END",
        ]
    )
    return [line + scan.newline for line in block]


def transform_native_gcode(
    source: Path,
    destination: Path,
    scan: NativeScan,
    config: CycleConfig,
) -> None:
    if config.cooldown_waits < 1:
        raise CycleError("--cooldown-waits must be at least 1")
    if config.bend_cycles < 1:
        raise CycleError("--bend-cycles must be at least 1")
    if not 0 < config.clear_height < config.bend_down_z:
        raise CycleError(
            f"clear height must be above 0 and below bend-down Z {config.bend_down_z:g}"
        )

    skip_through = 0
    with source.open("r", encoding="utf-8", newline="") as src, destination.open(
        "w", encoding="utf-8", newline=""
    ) as dst:
        for line_number, raw in enumerate(src, 1):
            if line_number <= skip_through:
                continue
            body, ending = timelapse.split_line_ending(raw)
            nl = ending or scan.newline

            if line_number == scan.setup_start_line:
                dst.writelines(setup_block(scan, config))
                skip_through = scan.setup_end_line
                continue
            if line_number == scan.cycle_replace_start_line:
                dst.writelines(cycle_block(scan, config))
                skip_through = scan.cycle_replace_end_line
                continue
            if line_number == scan.finish_z_lines[0]:
                dst.write(f"G1 Z{format_number(config.park_z)} F600 ; cycle-ready Z park" + nl)
                continue
            if line_number == scan.finish_z_lines[1]:
                dst.write(f"G1 Z{format_number(config.park_z)} ; hold cycle-ready Z park" + nl)
                continue
            if line_number == scan.speed_reset_before_line:
                dst.write("M1002 set_gcode_claim_speed_level : 0" + nl)
            if line_number == scan.final_progress_line:
                dst.write("M104 S0 ; final hotend-off guard" + nl)
                dst.write("M140 S0 ; final bed-off guard" + nl)
            dst.write(raw)


def validate_cycle_markers(path: Path) -> None:
    counts = {
        "setup begin": 0,
        "setup end": 0,
        "teardown begin": 0,
        "teardown end": 0,
    }
    needles = {
        "setup begin": f"; {CYCLE_TAG}_SETUP_BEGIN",
        "setup end": f"; {CYCLE_TAG}_SETUP_END",
        "teardown begin": f"; {CYCLE_TAG}_TEARDOWN_BEGIN",
        "teardown end": f"; {CYCLE_TAG}_TEARDOWN_END",
    }
    with path.open("r", encoding="utf-8", newline="") as src:
        for raw in src:
            stripped = raw.strip()
            for name, needle in needles.items():
                if stripped == needle:
                    counts[name] += 1
    bad = {name: count for name, count in counts.items() if count != 1}
    if bad:
        raise CycleError(f"invalid cycle marker counts: {bad}")


def default_output_path(input_path: Path) -> Path:
    name = input_path.name
    lower = name.lower()
    if lower.endswith(".gcode.3mf"):
        return input_path.with_name(name[:-10] + ".loop.gcode.3mf")
    if lower.endswith(".gcode"):
        return input_path.with_name(name[:-6] + ".loop.gcode")
    if lower.endswith(".3mf"):
        return input_path.with_name(name[:-4] + ".loop.3mf")
    raise CycleError("input must be a .gcode, .gcode.3mf, or .3mf file")


def build_report(
    input_path: Path,
    output_path: Path | None,
    member: str | None,
    native: NativeScan,
    config: CycleConfig,
    stage1_scan: timelapse.ScanResult,
    validation: timelapse.ValidationResult,
) -> CycleReport:
    return CycleReport(
        input=str(input_path),
        output=str(output_path) if output_path else None,
        archive_member=member,
        max_z_height=native.max_z_height,
        clear_height=config.clear_height,
        cooldown_temperature=config.cooldown_temperature,
        cooldown_waits=config.cooldown_waits,
        bend_operations=stage1_scan.bend_operations,
        clearing_passes=stage1_scan.clearing_passes,
        physical_layers=stage1_scan.physical_layers,
        synthetic_layers=stage1_scan.synthetic_layers,
        final_logical_layers=stage1_scan.final_layers,
        original_snapshots=validation.original_snapshots,
        generated_snapshots=validation.generated_snapshots,
        validation="passed",
        warnings=[
            "P2S flex/sweep motion and synthetic timelapse layers must be supervised on the first hardware run.",
            "The automatic clear height is max(max_z_height minus 10 mm, 5 mm); override it if that does not contact this part safely.",
        ],
    )


def print_report(report: CycleReport, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return
    print(f"Input: {report.input}")
    if report.archive_member:
        print(f"Archive G-code: {report.archive_member}")
    if report.output:
        print(f"Output: {report.output}")
    print(f"Model max Z: {report.max_z_height:g} mm")
    print(f"Clearing height: {report.clear_height:g} mm")
    print(
        f"Cooldown: {report.cooldown_temperature:g} C "
        f"({report.cooldown_waits} wait commands)"
    )
    print(f"Bend operations: {report.bend_operations}")
    print(f"Forward clearing passes: {report.clearing_passes}")
    print(
        f"Layers: {report.physical_layers} physical + {report.synthetic_layers} synthetic "
        f"= {report.final_logical_layers} logical"
    )
    print(
        f"Timelapse snapshots: {report.original_snapshots} original + "
        f"{report.generated_snapshots} generated"
    )
    print(f"Validation: {report.validation}")
    for warning in report.warnings:
        print(f"Warning: {warning}")


def validate_existing(input_path: Path, *, json_output: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="print-looper-validate-") as temp_name:
        gcode_path, member = timelapse.extract_input(input_path, Path(temp_name))
        validate_cycle_markers(gcode_path)
        result = timelapse.validate_patched(gcode_path)
        payload = {
            "input": str(input_path),
            "archive_member": member,
            "logical_layers": result.logical_layers,
            "synthetic_layers": result.synthetic_layers,
            "active_finish_commands": result.active_finish_commands,
            "original_snapshots": result.original_snapshots,
            "generated_snapshots": result.generated_snapshots,
            "validation": "passed",
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for key, value in payload.items():
                if value is not None:
                    print(f"{key.replace('_', ' ').title()}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="single sliced .gcode, .gcode.3mf, or .3mf")
    parser.add_argument("-o", "--output", type=Path, help="output path (never overwritten)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="process and validate without writing output")
    mode.add_argument("--validate", action="store_true", help="validate an already processed output")
    parser.add_argument(
        "--clear-height",
        type=float,
        help="sweep Z height in mm (default: max(max_z_height minus 10 mm, 5 mm))",
    )
    parser.add_argument("--cooldown-temperature", type=float, default=25.0, help="bed cooldown target")
    parser.add_argument("--cooldown-waits", type=int, default=60, help="repeated M190 waits")
    parser.add_argument("--bend-cycles", type=int, default=6, help="P2S bed flex cycles")
    parser.add_argument("--park-z", type=float, default=20.2, help="final cycle-ready Z position")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise CycleError(f"input does not exist or is not a file: {input_path}")
    if args.validate:
        if args.output:
            raise CycleError("--validate does not accept --output")
        return validate_existing(input_path, json_output=args.json)

    output_path = args.output.expanduser().resolve() if args.output else default_output_path(input_path)
    if output_path == input_path:
        raise CycleError("refusing to overwrite the input")
    if not args.dry_run and output_path.exists():
        raise CycleError(f"refusing to overwrite existing output: {output_path}")

    with tempfile.TemporaryDirectory(prefix="print-looper-") as temp_name:
        temp_dir = Path(temp_name)
        source_gcode, member = timelapse.extract_input(input_path, temp_dir)
        native = scan_native_gcode(source_gcode)
        clear_height = (
            args.clear_height if args.clear_height is not None else default_clear_height(native.max_z_height)
        )
        config = CycleConfig(
            clear_height=clear_height,
            cooldown_temperature=args.cooldown_temperature,
            cooldown_waits=args.cooldown_waits,
            bend_cycles=args.bend_cycles,
            park_z=args.park_z,
        )

        stage1 = temp_dir / "stage1.gcode"
        transform_native_gcode(source_gcode, stage1, native, config)
        validate_cycle_markers(stage1)
        stage1_scan = timelapse.scan_gcode(stage1)

        final_gcode = temp_dir / "processed.gcode"
        timelapse.transform_gcode(stage1, final_gcode, stage1_scan)
        validation = timelapse.validate_patched(final_gcode, expected=stage1_scan)
        validate_cycle_markers(final_gcode)

        report = build_report(
            input_path,
            None if args.dry_run else output_path,
            member,
            native,
            config,
            stage1_scan,
            validation,
        )
        if not args.dry_run:
            if member:
                archive_temp = temp_dir / "output.3mf"
                timelapse.write_archive(input_path, archive_temp, member, final_gcode)
                timelapse.copy_exclusive(archive_temp, output_path)
            else:
                timelapse.copy_exclusive(final_gcode, output_path)
        print_report(report, json_output=args.json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except timelapse.FarmLoopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
