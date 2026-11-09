#!/usr/bin/env python3
"""FarmLoop P2S timelapse postprocessor.

This tool deliberately recognizes only structurally marked FarmLoop P2S clear
routines.  It fails instead of guessing when a routine is incomplete or has an
unexpected bend/sweep shape.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
import zipfile


PATCH_TAG = "FARMLOOP_TIMELAPSE"
CLEAR_START = '; @fl:section:start "p2s.clear.motion"'
CLEAR_END = '; @fl:section:end "p2s.clear.motion"'

M73_LAYER_RE = re.compile(r"^\s*M73\s+L\s*(-?\d+)(?=\s|;|$)", re.I)
M991_RE = re.compile(r"^\s*M991\s+S\s*0\s+P\s*(-?\d+)(?=\s|;|$)", re.I)
M971_RE = re.compile(r"^\s*M971(?:\s|$)", re.I)
M622_RE = re.compile(r"^\s*M622\s+J\s*\d+(?=\s|;|$)", re.I)
M623_RE = re.compile(r"^\s*M623(?=\s|;|$)", re.I)
HEADER_TOTAL_RE = re.compile(r"^(\s*;\s*total layer number\s*:\s*)\d+(\s*)$", re.I)
LAYER_COMMENT_RE = re.compile(
    r"^(\s*;\s*layer num/total_layer_count\s*:\s*)(\d+)\s*/\s*(\d+)(\s*)$",
    re.I,
)
MOVE_RE = re.compile(r"^\s*G(?:0|1)\b", re.I)
AXIS_RE = re.compile(r"([XYZ])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)
BEND_UP_RE = re.compile(r"bend-up\s+stroke\s*#\s*(\d+)", re.I)
BEND_DOWN_RE = re.compile(r"bend-down\s+stroke\s*#\s*(\d+)", re.I)


class FarmLoopError(RuntimeError):
    """A safe, user-facing refusal to transform ambiguous G-code."""


@dataclass(frozen=True)
class SyntheticEvent:
    line_number: int
    position: str  # before or after
    kind: str
    label: str


@dataclass
class ClearRoutine:
    start_line: int
    end_line: int = 0
    bend_operations: int = 0
    clearing_passes: int = 0
    saw_bending_marker: bool = False
    external_bending: bool = False
    saw_push_marker: bool = False


@dataclass
class ScanResult:
    physical_layers: int = 0
    original_snapshots: int = 0
    original_finish_commands: int = 0
    header_entries: int = 0
    executable_blocks: int = 0
    bend_operations: int = 0
    clearing_passes: int = 0
    newline: str = "\n"
    patched: bool = False
    events: list[SyntheticEvent] = field(default_factory=list)
    clear_routines: list[ClearRoutine] = field(default_factory=list)
    finish_lines: list[int] = field(default_factory=list)
    preserved_digest: str = ""

    @property
    def synthetic_layers(self) -> int:
        return len(self.events)

    @property
    def final_layers(self) -> int:
        return self.physical_layers + self.synthetic_layers


@dataclass
class Report:
    input: str
    output: str | None
    archive_member: str | None
    original_physical_layers: int
    final_logical_layers: int
    bend_operations: int
    bend_synthetic_frames: int
    clearing_passes: int
    clearing_synthetic_frames: int
    original_timelapse_snapshots: int
    preserved_original_snapshots: int
    original_finish_commands: int
    removed_intermediate_finish_commands: int
    relocated_final_finish: bool
    header_entries_updated: int
    validation: str
    warnings: list[str] = field(default_factory=list)


def split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def executable_text(body: str) -> str:
    return body.split(";", 1)[0].strip()


def is_ignored_for_preservation(body: str, generated: bool = False) -> bool:
    stripped = body.strip()
    if generated or PATCH_TAG in body:
        return True
    if M73_LAYER_RE.match(body) or M991_RE.match(body):
        return True
    if HEADER_TOTAL_RE.match(body) or LAYER_COMMENT_RE.match(body):
        return True
    return False


def digest_line(hasher: "hashlib._Hash", body: str, ending: str) -> None:
    hasher.update(body.encode("utf-8"))
    hasher.update(ending.encode("ascii"))


def parse_axes(body: str) -> dict[str, float]:
    code = body.split(";", 1)[0]
    return {name.upper(): float(value) for name, value in AXIS_RE.findall(code)}


def scan_gcode(path: Path, *, allow_patched: bool = False) -> ScanResult:
    result = ScanResult()
    events: list[SyntheticEvent] = []
    preserved = hashlib.sha256()
    in_exec = False
    in_clear = False
    in_bending = False
    in_push = False
    in_sweep = False
    sweep_positioned = False
    absolute = True
    current_y: float | None = None
    pending_layer: tuple[int, int] | None = None
    pending_bend: tuple[int, float, int] | None = None
    conditional_depth = 0
    routine: ClearRoutine | None = None
    first_ending = ""

    with path.open("r", encoding="utf-8", newline="") as src:
        for line_number, raw in enumerate(src, 1):
            body, ending = split_line_ending(raw)
            if ending and not first_ending:
                first_ending = ending
            stripped = body.strip()

            if PATCH_TAG in body:
                result.patched = True
                if not allow_patched:
                    raise FarmLoopError(
                        f"line {line_number}: input is already marked {PATCH_TAG}; refusing to duplicate frames"
                    )

            if HEADER_TOTAL_RE.match(body):
                result.header_entries += 1

            if stripped == "; EXECUTABLE_BLOCK_START":
                if in_exec:
                    raise FarmLoopError(f"line {line_number}: nested EXECUTABLE_BLOCK_START")
                in_exec = True
                result.executable_blocks += 1
                conditional_depth = 0
            elif stripped == "; EXECUTABLE_BLOCK_END":
                if not in_exec:
                    raise FarmLoopError(f"line {line_number}: EXECUTABLE_BLOCK_END without start")
                if in_clear:
                    raise FarmLoopError(f"line {line_number}: executable block ended inside clear routine")
                if pending_layer:
                    raise FarmLoopError(
                        f"line {pending_layer[1]}: M73 layer has no corresponding M991 notification"
                    )
                if conditional_depth:
                    raise FarmLoopError(
                        f"line {line_number}: executable block has {conditional_depth} unclosed M622 conditional(s)"
                    )
                in_exec = False

            if in_exec:
                if M622_RE.match(body):
                    conditional_depth += 1
                elif M623_RE.match(body):
                    conditional_depth -= 1
                    if conditional_depth < 0:
                        raise FarmLoopError(f"line {line_number}: M623 without a matching M622 J block")

                layer_match = M73_LAYER_RE.match(body)
                notify_match = M991_RE.match(body)
                code = executable_text(body)
                if layer_match:
                    if pending_layer:
                        raise FarmLoopError(
                            f"line {line_number}: a second M73 L appeared before the M991 for line {pending_layer[1]}"
                        )
                    pending_layer = (int(layer_match.group(1)), line_number)
                    result.physical_layers += 1
                elif notify_match and int(notify_match.group(1)) >= 0:
                    if not pending_layer:
                        raise FarmLoopError(f"line {line_number}: layer M991 has no preceding M73 L")
                    old_layer, m73_line = pending_layer
                    if int(notify_match.group(1)) != old_layer - 1:
                        raise FarmLoopError(
                            f"lines {m73_line}/{line_number}: M73 L{old_layer} does not match M991 P{notify_match.group(1)}"
                        )
                    pending_layer = None
                elif pending_layer and code:
                    raise FarmLoopError(
                        f"line {line_number}: executable command separates M73 at line {pending_layer[1]} from its M991"
                    )

                if notify_match and int(notify_match.group(1)) == -1:
                    result.original_finish_commands += 1
                    result.finish_lines.append(line_number)
                if M971_RE.match(body):
                    result.original_snapshots += 1

                if stripped == CLEAR_START:
                    if in_clear:
                        raise FarmLoopError(f"line {line_number}: nested p2s.clear.motion section")
                    in_clear = True
                    in_bending = in_push = in_sweep = False
                    sweep_positioned = False
                    current_y = None
                    routine = ClearRoutine(start_line=line_number)
                elif stripped == CLEAR_END:
                    if not in_clear or routine is None:
                        raise FarmLoopError(f"line {line_number}: clear section end without start")
                    if pending_bend:
                        raise FarmLoopError(
                            f"line {line_number}: bend-up stroke #{pending_bend[0]} has no bend-down stroke"
                        )
                    routine.end_line = line_number
                    if not routine.saw_bending_marker or not routine.saw_push_marker:
                        raise FarmLoopError(
                            f"lines {routine.start_line}-{line_number}: clear routine lacks BENDING MOTION or PUSH SECTION marker"
                        )
                    if not routine.clearing_passes or (
                        not routine.bend_operations and not routine.external_bending
                    ):
                        raise FarmLoopError(
                            f"lines {routine.start_line}-{line_number}: no unambiguous internal/external bend or forward clearing pass"
                        )
                    result.clear_routines.append(routine)
                    in_clear = in_bending = in_push = in_sweep = False
                    routine = None
                elif in_clear and routine is not None:
                    upper = stripped.upper()
                    if "BENDING MOTION" in upper:
                        routine.saw_bending_marker = True
                        in_bending = True
                        in_push = in_sweep = False
                        sweep_positioned = False
                    elif "EXTERNAL BENDING" in upper:
                        routine.saw_bending_marker = True
                        routine.external_bending = True
                        in_bending = False
                        in_push = in_sweep = False
                        sweep_positioned = False
                    elif "PUSH SECTION" in upper:
                        routine.saw_push_marker = True
                        in_bending = False
                        in_push = True
                        in_sweep = False
                        sweep_positioned = False
                    elif in_push and "CHOOSE SWEEP HEIGHT" in upper:
                        in_sweep = False
                        sweep_positioned = False
                    elif in_push and ("CENTRAL SWEEPS" in upper or "RAKE" in upper):
                        in_sweep = True
                        sweep_positioned = False
                    elif "END SECTION" in upper:
                        in_sweep = False
                        sweep_positioned = False

                    if in_bending and MOVE_RE.match(body):
                        axes = parse_axes(body)
                        up_match = BEND_UP_RE.search(body)
                        down_match = BEND_DOWN_RE.search(body)
                        if up_match or down_match:
                            if "Z" not in axes:
                                raise FarmLoopError(f"line {line_number}: labeled bend stroke has no Z target")
                            if not absolute:
                                raise FarmLoopError(f"line {line_number}: relative-mode bend stroke is ambiguous")
                            stroke = int((up_match or down_match).group(1))
                            if up_match:
                                if pending_bend:
                                    raise FarmLoopError(
                                        f"line {line_number}: bend-up #{stroke} before bend-down #{pending_bend[0]}"
                                    )
                                pending_bend = (stroke, axes["Z"], line_number)
                            else:
                                if not pending_bend or pending_bend[0] != stroke:
                                    raise FarmLoopError(f"line {line_number}: bend-down #{stroke} has no matching bend-up")
                                _, lowered_z, up_line = pending_bend
                                if lowered_z <= axes["Z"]:
                                    raise FarmLoopError(
                                        f"lines {up_line}/{line_number}: expected higher Z to lower the P2S bed, got {lowered_z:g} then {axes['Z']:g}"
                                    )
                                events.append(
                                    SyntheticEvent(up_line, "after", "bed_lowered", f"bed lowered (bend #{stroke})")
                                )
                                events.append(
                                    SyntheticEvent(line_number, "after", "bed_raised", f"bed raised (bend #{stroke})")
                                )
                                routine.bend_operations += 1
                                pending_bend = None

                    if in_push and MOVE_RE.match(body):
                        axes = parse_axes(body)
                        if "Y" in axes:
                            if not absolute:
                                raise FarmLoopError(
                                    f"line {line_number}: relative Y move inside FarmLoop push section is ambiguous"
                                )
                            target_y = axes["Y"]
                            # Each marked sweep subsection in the FarmLoop routine
                            # begins with a rear-edge staging move.  It is not a
                            # clearing pass even if the previous routine parked at
                            # a still larger Y coordinate.
                            if in_sweep and not sweep_positioned:
                                sweep_positioned = True
                            elif in_sweep and current_y is not None and target_y < current_y:
                                pass_number = routine.clearing_passes + 1
                                events.append(
                                    SyntheticEvent(
                                        line_number,
                                        "before",
                                        "clear_before",
                                        f"before forward clearing pass #{pass_number}",
                                    )
                                )
                                events.append(
                                    SyntheticEvent(
                                        line_number,
                                        "after",
                                        "clear_after",
                                        f"after forward clearing pass #{pass_number}",
                                    )
                                )
                                routine.clearing_passes += 1
                            current_y = target_y

                command = executable_text(body).upper()
                if command == "G90":
                    absolute = True
                elif command == "G91":
                    absolute = False

            if not is_ignored_for_preservation(body):
                digest_line(preserved, body, ending)

    if in_exec:
        raise FarmLoopError("file ended inside an executable block")
    if in_clear:
        raise FarmLoopError("file ended inside p2s.clear.motion")
    if result.executable_blocks == 0:
        raise FarmLoopError("no FarmLoop EXECUTABLE_BLOCK_START/END sections found")
    if result.header_entries == 0:
        raise FarmLoopError("no '; total layer number:' header entries found")
    if result.physical_layers == 0:
        raise FarmLoopError("no executable M73 L / M991 layer pairs found")
    if result.original_finish_commands == 0:
        raise FarmLoopError("no executable M991 S0 P-1 timelapse finish found")
    result.events = sorted(events, key=lambda event: (event.line_number, event.position != "before"))
    result.bend_operations = sum(item.bend_operations for item in result.clear_routines)
    result.clearing_passes = sum(item.clearing_passes for item in result.clear_routines)
    result.newline = first_ending or "\n"
    result.preserved_digest = preserved.hexdigest()
    return result


def replace_m73(body: str, number: int) -> str:
    return re.sub(r"(?i)^(\s*M73\s+)L\s*-?\d+", rf"\g<1>L{number}", body, count=1)


def replace_m991(body: str, number: int) -> str:
    return re.sub(r"(?i)^(\s*M991\s+S\s*0\s+P)\s*-?\d+", rf"\g<1>{number}", body, count=1)


def synthetic_block(event: SyntheticEvent, number: int, total: int, nl: str) -> list[str]:
    lines = [
        f"; {PATCH_TAG}_BEGIN: synthetic {event.label}",
        f"; layer num/total_layer_count: {number}/{total}",
        "M400 ; wait for motion queue before synthetic capture",
        f"M73 L{number}",
        f"M991 S0 P{number - 1} ;notify synthetic layer change",
        "; SKIPPABLE_START",
        "; SKIPTYPE: timelapse",
        "M622.1 S1 ; P2S profile default: timelapse enabled for previous firmware",
        "M1002 judge_flag timelapse_record_flag",
        "M622 J1",
        "M971 S11 C10 O0 T0",
        "M1004 S5 P1 ; external shutter, matching the P2S traditional timelapse profile",
        "M623",
        "; SKIPPABLE_END",
        f"; {PATCH_TAG}_END",
    ]
    return [line + nl for line in lines]


def final_finish_block(nl: str) -> list[str]:
    lines = [
        f"; {PATCH_TAG}_BEGIN: final timelapse finish after final synthetic snapshot",
        "M1002 judge_flag timelapse_record_flag",
        "M622 J1",
        "M400 ; wait for final synthetic snapshot commands",
        "M991 S0 P-1 ;end timelapse after final FarmLoop ejection frame",
        "M400 S5 ;wait for last picture to be taken (P2S profile behavior)",
        "M623",
        f"; {PATCH_TAG}_END",
    ]
    return [line + nl for line in lines]


def transform_gcode(source: Path, destination: Path, scan: ScanResult) -> None:
    before: dict[int, list[SyntheticEvent]] = {}
    after: dict[int, list[SyntheticEvent]] = {}
    for event in scan.events:
        (before if event.position == "before" else after).setdefault(event.line_number, []).append(event)

    counter = 0
    pending_layer: int | None = None
    last_synthetic_line = max((event.line_number for event in scan.events), default=0)
    finish_inserted = False
    last_finish_line = scan.finish_lines[-1]
    in_exec = False

    with source.open("r", encoding="utf-8", newline="") as src, destination.open(
        "w", encoding="utf-8", newline=""
    ) as dst:
        for line_number, raw in enumerate(src, 1):
            body, ending = split_line_ending(raw)
            nl = ending or scan.newline

            if body.strip() == "; EXECUTABLE_BLOCK_START":
                in_exec = True

            for event in before.get(line_number, []):
                counter += 1
                dst.writelines(synthetic_block(event, counter, scan.final_layers, nl))

            layer_match = M73_LAYER_RE.match(body) if in_exec else None
            notify_match = M991_RE.match(body) if in_exec else None
            if layer_match:
                counter += 1
                pending_layer = counter
                dst.write(replace_m73(body, counter) + ending)
            elif notify_match and int(notify_match.group(1)) >= 0:
                if pending_layer is None:
                    raise AssertionError("scan/transform disagreement: notification without layer")
                dst.write(replace_m991(body, pending_layer - 1) + ending)
                pending_layer = None
            elif notify_match and int(notify_match.group(1)) == -1:
                if scan.synthetic_layers or line_number != last_finish_line:
                    dst.write(
                        f"; {PATCH_TAG}: neutralized original finish at source line {line_number}: {body.strip()}" + nl
                    )
                else:
                    dst.write(raw)
            else:
                header_match = HEADER_TOTAL_RE.match(body)
                layer_comment = LAYER_COMMENT_RE.match(body) if in_exec else None
                if header_match:
                    dst.write(f"{header_match.group(1)}{scan.final_layers}{header_match.group(2)}" + ending)
                elif layer_comment:
                    dst.write(
                        f"{layer_comment.group(1)}{counter + 1}/{scan.final_layers}{layer_comment.group(4)}" + ending
                    )
                else:
                    dst.write(raw)

            for event in after.get(line_number, []):
                counter += 1
                dst.writelines(synthetic_block(event, counter, scan.final_layers, nl))

            if scan.synthetic_layers and line_number == last_synthetic_line:
                dst.writelines(final_finish_block(nl))
                finish_inserted = True

            if body.strip() == "; EXECUTABLE_BLOCK_END":
                in_exec = False

    if counter != scan.final_layers:
        raise AssertionError(f"transformation produced {counter} layers, expected {scan.final_layers}")
    if scan.synthetic_layers and not finish_inserted:
        raise AssertionError("final timelapse finish was not inserted")


@dataclass
class ValidationResult:
    logical_layers: int
    synthetic_layers: int
    active_finish_commands: int
    original_snapshots: int
    generated_snapshots: int
    header_entries: int
    preserved_digest: str


def validate_patched(path: Path, *, expected: ScanResult | None = None) -> ValidationResult:
    in_exec = False
    generated = False
    generated_kind = ""
    pending_layer: tuple[int, int] | None = None
    next_layer = 1
    active_finish = 0
    finish_line = 0
    last_generated_snapshot_line = 0
    original_snapshots = 0
    generated_snapshots = 0
    synthetic_layers = 0
    header_values: list[tuple[int, int]] = []
    layer_comments: list[tuple[int, int, int]] = []
    conditional_depth = 0
    preserved = hashlib.sha256()

    with path.open("r", encoding="utf-8", newline="") as src:
        for line_number, raw in enumerate(src, 1):
            body, ending = split_line_ending(raw)
            stripped = body.strip()
            if stripped.startswith(f"; {PATCH_TAG}_BEGIN:"):
                if generated:
                    raise FarmLoopError(f"line {line_number}: nested generated block")
                generated = True
                generated_kind = stripped
            elif stripped == f"; {PATCH_TAG}_END":
                if not generated:
                    raise FarmLoopError(f"line {line_number}: generated block end without start")
                generated = False
                generated_kind = ""

            header = HEADER_TOTAL_RE.match(body)
            if header:
                value_match = re.search(r"(\d+)\s*$", body)
                assert value_match
                header_values.append((line_number, int(value_match.group(1))))
            layer_comment = LAYER_COMMENT_RE.match(body)
            if layer_comment:
                layer_comments.append(
                    (line_number, int(layer_comment.group(2)), int(layer_comment.group(3)))
                )

            if stripped == "; EXECUTABLE_BLOCK_START":
                in_exec = True
                conditional_depth = 0
            elif stripped == "; EXECUTABLE_BLOCK_END":
                if pending_layer:
                    raise FarmLoopError(f"line {pending_layer[1]}: layer lacks notification")
                if conditional_depth:
                    raise FarmLoopError(f"line {line_number}: unclosed M622 block")
                in_exec = False

            if in_exec:
                if M622_RE.match(body):
                    conditional_depth += 1
                elif M623_RE.match(body):
                    conditional_depth -= 1
                    if conditional_depth < 0:
                        raise FarmLoopError(f"line {line_number}: M623 without M622")

                layer = M73_LAYER_RE.match(body)
                notify = M991_RE.match(body)
                code = executable_text(body)
                if layer:
                    number = int(layer.group(1))
                    if number != next_layer:
                        raise FarmLoopError(
                            f"line {line_number}: expected strictly increasing M73 L{next_layer}, found L{number}"
                        )
                    pending_layer = (number, line_number)
                    next_layer += 1
                    if generated and "synthetic" in generated_kind:
                        synthetic_layers += 1
                elif notify and int(notify.group(1)) >= 0:
                    if not pending_layer or int(notify.group(1)) != pending_layer[0] - 1:
                        raise FarmLoopError(f"line {line_number}: M991 does not match preceding M73")
                    pending_layer = None
                elif pending_layer and code:
                    raise FarmLoopError(f"line {line_number}: command between M73 and M991")

                if notify and int(notify.group(1)) == -1:
                    active_finish += 1
                    finish_line = line_number
                if M971_RE.match(body):
                    if generated:
                        generated_snapshots += 1
                        last_generated_snapshot_line = line_number
                    else:
                        original_snapshots += 1

            if not is_ignored_for_preservation(body, generated=generated):
                digest_line(preserved, body, ending)

    if generated:
        raise FarmLoopError("file ended inside a generated block")
    logical_layers = next_layer - 1
    if active_finish != 1:
        raise FarmLoopError(f"expected exactly one active M991 S0 P-1, found {active_finish}")
    if last_generated_snapshot_line and finish_line <= last_generated_snapshot_line:
        raise FarmLoopError("final timelapse finish occurs before the last synthetic snapshot")
    if not header_values:
        raise FarmLoopError("no total-layer header entries found")
    bad_headers = [(line, value) for line, value in header_values if value != logical_layers]
    if bad_headers:
        line, value = bad_headers[0]
        raise FarmLoopError(
            f"line {line}: header total {value} does not equal {logical_layers} logical layers"
        )
    bad_comments = [
        (line, number, total)
        for line, number, total in layer_comments
        if total != logical_layers or not 1 <= number <= logical_layers
    ]
    if bad_comments:
        line, number, total = bad_comments[0]
        raise FarmLoopError(
            f"line {line}: layer comment {number}/{total} is inconsistent with {logical_layers} logical layers"
        )
    if generated_snapshots != synthetic_layers:
        raise FarmLoopError(
            f"generated snapshot/layer mismatch: {generated_snapshots} snapshots, {synthetic_layers} layers"
        )
    if expected:
        if logical_layers != expected.final_layers:
            raise FarmLoopError(f"expected {expected.final_layers} logical layers, found {logical_layers}")
        if synthetic_layers != expected.synthetic_layers:
            raise FarmLoopError(f"expected {expected.synthetic_layers} synthetic layers, found {synthetic_layers}")
        if original_snapshots != expected.original_snapshots:
            raise FarmLoopError(
                f"original snapshot count changed: expected {expected.original_snapshots}, found {original_snapshots}"
            )
        if preserved.hexdigest() != expected.preserved_digest:
            raise FarmLoopError("non-target G-code changed; mechanical/control preservation digest differs")

    return ValidationResult(
        logical_layers=logical_layers,
        synthetic_layers=synthetic_layers,
        active_finish_commands=active_finish,
        original_snapshots=original_snapshots,
        generated_snapshots=generated_snapshots,
        header_entries=len(header_values),
        preserved_digest=preserved.hexdigest(),
    )


def archive_gcode_member(archive: zipfile.ZipFile) -> str:
    candidates = [
        info.filename
        for info in archive.infolist()
        if info.filename.lower().endswith(".gcode") and not info.is_dir()
    ]
    preferred = [name for name in candidates if name.lower() == "metadata/plate_1.gcode"]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    raise FarmLoopError(
        f"archive must contain one unambiguous sliced G-code member; found {len(candidates)}: {candidates}"
    )


def extract_input(input_path: Path, temp_dir: Path) -> tuple[Path, str | None]:
    if not zipfile.is_zipfile(input_path):
        return input_path, None
    with zipfile.ZipFile(input_path, "r") as archive:
        member = archive_gcode_member(archive)
        extracted = temp_dir / "input.gcode"
        with archive.open(member, "r") as src, extracted.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    return extracted, member


def write_archive(
    input_path: Path,
    temp_output: Path,
    member: str,
    transformed_gcode: Path,
) -> None:
    md5_name = member + ".md5"
    digest = hashlib.md5()
    with transformed_gcode.open("rb") as gcode:
        for chunk in iter(lambda: gcode.read(1024 * 1024), b""):
            digest.update(chunk)
    md5_bytes = digest.hexdigest().upper().encode("ascii")

    with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(temp_output, "w") as target:
        target.comment = source.comment
        names = set(source.namelist())
        if md5_name not in names:
            raise FarmLoopError(f"archive has {member} but no required checksum sidecar {md5_name}")
        for info in source.infolist():
            cloned = copy.copy(info)
            cloned.CRC = 0
            cloned.file_size = 0
            cloned.compress_size = 0
            with target.open(cloned, "w") as dst:
                if info.filename == member:
                    with transformed_gcode.open("rb") as src:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                elif info.filename == md5_name:
                    dst.write(md5_bytes)
                else:
                    with source.open(info, "r") as src:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)


def copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def default_output_path(input_path: Path) -> Path:
    name = input_path.name
    lower = name.lower()
    if lower.endswith(".gcode.3mf"):
        return input_path.with_name(name[:-10] + ".timelapse.gcode.3mf")
    if lower.endswith(".gcode"):
        return input_path.with_name(name[:-6] + ".timelapse.gcode")
    if lower.endswith(".3mf"):
        return input_path.with_name(name[:-4] + ".timelapse.3mf")
    raise FarmLoopError("input must be a .gcode, .gcode.3mf, or .3mf file")


def make_report(
    input_path: Path,
    output_path: Path | None,
    member: str | None,
    scan: ScanResult,
) -> Report:
    return Report(
        input=str(input_path),
        output=str(output_path) if output_path else None,
        archive_member=member,
        original_physical_layers=scan.physical_layers,
        final_logical_layers=scan.final_layers,
        bend_operations=scan.bend_operations,
        bend_synthetic_frames=scan.bend_operations * 2,
        clearing_passes=scan.clearing_passes,
        clearing_synthetic_frames=scan.clearing_passes * 2,
        original_timelapse_snapshots=scan.original_snapshots,
        preserved_original_snapshots=scan.original_snapshots,
        original_finish_commands=scan.original_finish_commands,
        removed_intermediate_finish_commands=max(0, scan.original_finish_commands - 1),
        relocated_final_finish=bool(scan.synthetic_layers),
        header_entries_updated=scan.header_entries,
        validation="passed",
        warnings=[
            "P2S firmware behavior for synthetic layer changes is not hardware-verified.",
        ],
    )


def print_report(report: Report, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return
    print(f"Input: {report.input}")
    if report.archive_member:
        print(f"Archive G-code: {report.archive_member}")
    if report.output:
        print(f"Output: {report.output}")
    print(f"Physical layers: {report.original_physical_layers}")
    print(f"Bend operations: {report.bend_operations} ({report.bend_synthetic_frames} frames)")
    print(f"Forward clearing passes: {report.clearing_passes} ({report.clearing_synthetic_frames} frames)")
    print(f"Final logical layers: {report.final_logical_layers}")
    print(f"Original snapshots preserved: {report.preserved_original_snapshots}")
    print(
        f"Timelapse finishes: {report.original_finish_commands} original, "
        f"{report.removed_intermediate_finish_commands} intermediate removed, 1 final active"
    )
    print(f"Layer-total headers updated: {report.header_entries_updated}")
    print(f"Validation: {report.validation}")
    for warning in report.warnings:
        print(f"Warning: {warning}")


def validate_existing(input_path: Path, *, json_output: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="farmloop-timelapse-") as temp_name:
        temp_dir = Path(temp_name)
        gcode_path, member = extract_input(input_path, temp_dir)
        result = validate_patched(gcode_path)
        payload = {
            "input": str(input_path),
            "archive_member": member,
            "logical_layers": result.logical_layers,
            "synthetic_layers": result.synthetic_layers,
            "active_finish_commands": result.active_finish_commands,
            "original_snapshots": result.original_snapshots,
            "generated_snapshots": result.generated_snapshots,
            "header_entries": result.header_entries,
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
    parser.add_argument("input", type=Path, help="FarmLoop .gcode, .gcode.3mf, or .3mf input")
    parser.add_argument("-o", "--output", type=Path, help="output path (never overwritten)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="transform and validate in a temporary file only")
    mode.add_argument("--validate", action="store_true", help="validate an already-patched input without changing it")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise FarmLoopError(f"input does not exist or is not a file: {input_path}")
    if args.validate:
        if args.output:
            raise FarmLoopError("--validate does not accept --output")
        return validate_existing(input_path, json_output=args.json)

    output_path = (args.output.expanduser().resolve() if args.output else default_output_path(input_path))
    if output_path == input_path:
        raise FarmLoopError("refusing to overwrite the input")
    if not args.dry_run and output_path.exists():
        raise FarmLoopError(f"refusing to overwrite existing output: {output_path}")

    with tempfile.TemporaryDirectory(prefix="farmloop-timelapse-") as temp_name:
        temp_dir = Path(temp_name)
        gcode_path, member = extract_input(input_path, temp_dir)
        scan = scan_gcode(gcode_path)
        transformed = temp_dir / "transformed.gcode"
        transform_gcode(gcode_path, transformed, scan)
        validate_patched(transformed, expected=scan)
        report = make_report(input_path, None if args.dry_run else output_path, member, scan)

        if not args.dry_run:
            if member:
                archive_temp = temp_dir / "output.3mf"
                write_archive(input_path, archive_temp, member, transformed)
                copy_exclusive(archive_temp, output_path)
            else:
                copy_exclusive(transformed, output_path)
        print_report(report, json_output=args.json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FarmLoopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
