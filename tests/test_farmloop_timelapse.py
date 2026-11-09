from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

import farmloop_timelapse as fl


def layer(number: int, total: int) -> str:
    return f"""; layer num/total_layer_count: {number}/{total}
M73 L{number}
M991 S0 P{number - 1} ;notify layer change
M622.1 S1
M1002 judge_flag timelapse_record_flag
M622 J1
M971 S11 C10 O0 T0
M1004 S5 P1
M623
G1 X{number} E.1
"""


def section(layer_count: int, bends: int = 1, passes: int = 1) -> str:
    bend_lines = []
    for number in range(1, bends + 1):
        bend_lines.extend(
            [
                f"G1 Z{230 + number} F12000 ; bend-up stroke #{number}",
                f"G1 Z{200 - number} F12000 ; bend-down stroke #{number}",
            ]
        )
    sweep_lines = ["G1 Y250 F3000"]
    for number in range(passes):
        sweep_lines.append(f"G1 Y{number} F3000 ; forward pass")
        if number != passes - 1:
            sweep_lines.append("G1 Y250 F3000")
    return "\n".join(
        [
            "; HEADER_BLOCK_START",
            f"; total layer number: {layer_count}",
            "; HEADER_BLOCK_END",
            "; EXECUTABLE_BLOCK_START",
            "G90",
            *(layer(i, layer_count).rstrip() for i in range(1, layer_count + 1)),
            "M1002 judge_flag timelapse_record_flag",
            "M622 J1",
            "M400",
            "M991 S0 P-1 ;end smooth timelapse at safe pos",
            "M400 S5",
            "M623",
            fl.CLEAR_START,
            "; BENDING MOTION",
            *bend_lines,
            "; PUSH SECTION",
            "; central sweeps",
            *sweep_lines,
            "; END SECTION",
            "G1 X65 Y245 F12000 ; park",
            "G1 Y265 F3000 ; final rear park",
            fl.CLEAR_END,
            "M18",
            "; EXECUTABLE_BLOCK_END",
            "",
        ]
    )


def movement_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith(("G0 ", "G1 ", "G2 ", "G3 "))]


class ProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_transform(self, text: str) -> tuple[fl.ScanResult, Path]:
        source = self.root / "FL_S2_input.gcode"
        output = self.root / "FL_S2_output.gcode"
        source.write_text(text, encoding="utf-8")
        scan = fl.scan_gcode(source)
        fl.transform_gcode(source, output, scan)
        fl.validate_patched(output, expected=scan)
        return scan, output

    def test_single_print_continuous_numbering_and_motion_preserved(self) -> None:
        text = section(2)
        scan, output = self.run_transform(text)
        patched = output.read_text(encoding="utf-8")
        self.assertEqual(scan.physical_layers, 2)
        self.assertEqual(scan.final_layers, 6)
        self.assertEqual(re.findall(r"^M73 L(\d+)$", patched, re.M), [str(i) for i in range(1, 7)])
        self.assertEqual(movement_lines(text), movement_lines(patched))
        self.assertEqual(len(re.findall(r"^M991 S0 P-1\b", patched, re.M)), 1)
        self.assertIn("; total layer number: 6", patched)

    def test_repeated_identical_prints(self) -> None:
        scan, output = self.run_transform(section(2) + section(2))
        self.assertEqual(scan.physical_layers, 4)
        self.assertEqual(scan.bend_operations, 2)
        self.assertEqual(scan.clearing_passes, 2)
        self.assertEqual(scan.final_layers, 12)
        result = fl.validate_patched(output, expected=scan)
        self.assertEqual(result.active_finish_commands, 1)

    def test_different_height_prints(self) -> None:
        scan, output = self.run_transform(section(1) + section(3))
        self.assertEqual(scan.physical_layers, 4)
        self.assertEqual(scan.final_layers, 12)
        self.assertEqual(fl.validate_patched(output).logical_layers, 12)

    def test_multiple_bends_and_clearing_passes(self) -> None:
        scan, output = self.run_transform(section(1, bends=2, passes=3))
        self.assertEqual(scan.bend_operations, 2)
        self.assertEqual(scan.clearing_passes, 3)
        self.assertEqual(scan.synthetic_layers, 10)
        self.assertEqual(scan.final_layers, 11)
        result = fl.validate_patched(output, expected=scan)
        self.assertEqual(result.generated_snapshots, 10)

    def test_explicit_external_bending_allows_sweep_only_routine(self) -> None:
        text = section(1).replace(
            "; BENDING MOTION\nG1 Z231 F12000 ; bend-up stroke #1\n"
            "G1 Z199 F12000 ; bend-down stroke #1",
            "; EXTERNAL BENDING",
        )
        scan, output = self.run_transform(text)
        self.assertEqual(scan.bend_operations, 0)
        self.assertEqual(scan.clearing_passes, 1)
        self.assertEqual(scan.synthetic_layers, 2)
        self.assertEqual(fl.validate_patched(output, expected=scan).generated_snapshots, 2)

    def test_rejects_mismatched_layer_pair(self) -> None:
        source = self.root / "bad.gcode"
        source.write_text(section(1).replace("M991 S0 P0", "M991 S0 P8"), encoding="utf-8")
        with self.assertRaisesRegex(fl.FarmLoopError, "does not match"):
            fl.scan_gcode(source)

    def test_rejects_incomplete_bend(self) -> None:
        source = self.root / "bad.gcode"
        source.write_text(
            section(1).replace("G1 Z199 F12000 ; bend-down stroke #1\n", ""), encoding="utf-8"
        )
        with self.assertRaisesRegex(fl.FarmLoopError, "no bend-down"):
            fl.scan_gcode(source)

    def test_rejects_unmarked_clear_routine(self) -> None:
        source = self.root / "bad.gcode"
        source.write_text(section(1).replace("; PUSH SECTION", "; unknown"), encoding="utf-8")
        with self.assertRaisesRegex(fl.FarmLoopError, "lacks BENDING MOTION or PUSH SECTION"):
            fl.scan_gcode(source)

    def test_rejects_already_patched_input(self) -> None:
        _, output = self.run_transform(section(1))
        with self.assertRaisesRegex(fl.FarmLoopError, "already marked"):
            fl.scan_gcode(output)

    def test_archive_preserves_unrelated_member_and_updates_md5(self) -> None:
        gcode = section(2).encode()
        archive_path = self.root / "FL_S2_fixture.gcode.3mf"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode)
            archive.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(gcode).hexdigest().upper())
            archive.writestr("Metadata/unrelated.bin", b"unchanged")

        temp_dir = self.root / "extract"
        temp_dir.mkdir()
        extracted, member = fl.extract_input(archive_path, temp_dir)
        assert member
        scan = fl.scan_gcode(extracted)
        transformed = self.root / "transformed.gcode"
        fl.transform_gcode(extracted, transformed, scan)
        output_archive = self.root / "out.gcode.3mf"
        fl.write_archive(archive_path, output_archive, member, transformed)

        with zipfile.ZipFile(output_archive) as archive:
            changed = archive.read(member)
            self.assertEqual(archive.read("Metadata/unrelated.bin"), b"unchanged")
            self.assertEqual(
                archive.read(member + ".md5"), hashlib.md5(changed).hexdigest().upper().encode()
            )


if __name__ == "__main__":
    unittest.main()
