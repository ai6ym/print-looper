from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import farmloop_timelapse as timelapse
import plate_cycle as cycle


def native_plate(max_z: float = 31.16) -> str:
    return f"""; HEADER_BLOCK_START
; BambuStudio fixture
; total layer number: 2
; max_z_height: {max_z}
; HEADER_BLOCK_END
; CONFIG_BLOCK_START
; CONFIG_BLOCK_END
; EXECUTABLE_BLOCK_START
G90
;======== P2S start gcode==========
;===== fixture =====
M140 S70
;===== for Textured PEI Plate , lower the nozzle as the nozzle was touching topmost of the texture when homing ==
G29.1 Z0.03
;===== nozzle load line ===============================
M1002 gcode_claim_action : 51
G90
M83
M109 S255
G0 X100 Y0 F24000
G130 O0 X100 Y-0.2 Z0.6 F3.1 L40 E12 D4
G1 Z1
;===== noozle load line end ===========================
M1002 gcode_claim_action : 0
; layer num/total_layer_count: 1/2
M73 L1
M991 S0 P0 ;notify layer change
M1002 judge_flag timelapse_record_flag
M622 J1
M971 S11 C10 O0 T0
M623
G1 X1 E.1
; layer num/total_layer_count: 2/2
M73 L2
M991 S0 P1 ;notify layer change
M1002 judge_flag timelapse_record_flag
M622 J1
M971 S11 C10 O0 T0
M623
G1 X2 E.1
; MACHINE_END_GCODE_START
M1003 S0
M73 P100 R0
M400
G92 E0
M211 Z1
G90
G1 Z31.56 F900
M1002 judge_flag timelapse_record_flag
M622 J1
G150.3
M400
M991 S0 P-1 ;end smooth timelapse at safe pos
M400 S5
M623
G90
G1 Z41.16 F900
M140 S0
M106 S0
M106 P2 S0
M106 P3 S0
M106 P10 S0
; pull back filament to AMS
M620 S65535
T65535
G150.1 F8000
M621 S65535
G150.3
M104 S0 ; turn off hotend
M400 ; wait all motion done
M17 S
M17 Z0.4 ; lower z motor current
G1 Z95.58 F600
G1 Z93.58
M400 P100
M17 R
M220 S100
M201.2 K1.0
M73.2 R1.0
M1015.3 S0 ;disable clog detect
M1015.4 S0 K0 ;disable air printing detect
M400
M18
M73 P100 R0
; EXECUTABLE_BLOCK_END
"""


class PlateCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_smart_clear_height_uses_floor(self) -> None:
        self.assertEqual(cycle.default_clear_height(31.16), 21.16)
        self.assertEqual(cycle.default_clear_height(15.0), 5.0)
        self.assertEqual(cycle.default_clear_height(4.0), 5.0)

    def test_complete_single_plate_pipeline(self) -> None:
        source = self.root / "plate.gcode"
        stage1 = self.root / "stage1.gcode"
        output = self.root / "output.gcode"
        source.write_text(native_plate(), encoding="utf-8")

        native = cycle.scan_native_gcode(source)
        config = cycle.CycleConfig(
            clear_height=cycle.default_clear_height(native.max_z_height),
            cooldown_waits=2,
        )
        cycle.transform_native_gcode(source, stage1, native, config)
        cycle.validate_cycle_markers(stage1)

        stage1_text = stage1.read_text(encoding="utf-8")
        self.assertNotIn("G130 O0", stage1_text)
        self.assertIn("G1 E50 F200", stage1_text)
        self.assertIn("G29.1 Z0.03", stage1_text)
        self.assertNotIn("G29.1 Z+0.00", stage1_text)
        self.assertIn("G1 Z21.16 F10000", stage1_text)
        self.assertEqual(stage1_text.count("M190 S25 ; cooldown wait"), 2)
        self.assertIn("G1 Z20.2 F600 ; cycle-ready Z park", stage1_text)
        self.assertNotRegex(stage1_text, r"(?m)^\s*M18(?:\s|;|$)")

        scan = timelapse.scan_gcode(stage1)
        self.assertEqual(scan.physical_layers, 2)
        self.assertEqual(scan.bend_operations, 6)
        self.assertEqual(scan.clearing_passes, 16)
        self.assertEqual(scan.synthetic_layers, 44)
        self.assertEqual(scan.final_layers, 46)

        timelapse.transform_gcode(stage1, output, scan)
        validation = timelapse.validate_patched(output, expected=scan)
        cycle.validate_cycle_markers(output)
        self.assertEqual(validation.logical_layers, 46)
        self.assertEqual(validation.generated_snapshots, 44)
        self.assertEqual(validation.original_snapshots, 2)
        self.assertEqual(validation.active_finish_commands, 1)

    def test_stage2_is_opt_in_and_uses_external_actuators(self) -> None:
        source = self.root / "plate.gcode"
        stage2 = self.root / "stage2.gcode"
        output = self.root / "output.gcode"
        source.write_text(native_plate(), encoding="utf-8")

        native = cycle.scan_native_gcode(source)
        config = cycle.CycleConfig(
            clear_height=cycle.default_clear_height(native.max_z_height),
            stage=2,
            cooldown_waits=2,
        )
        cycle.transform_native_gcode(source, stage2, native, config)
        cycle.validate_cycle_markers(stage2)

        stage2_text = stage2.read_text(encoding="utf-8")
        self.assertIn("G29.1 Z0.03", stage2_text)
        self.assertIn("M104 S1 ; Stage 2 RESET sentinel", stage2_text)
        self.assertIn("M104 S2 ; Stage 2 T1 sentinel", stage2_text)
        self.assertIn("M104 S3 ; Stage 2 T2 sentinel", stage2_text)
        self.assertIn("M104 S4 ; Stage 2 safety sentinel", stage2_text)
        self.assertIn("M104 S5 ; Stage 2 T3 sentinel", stage2_text)
        self.assertIn("; EXTERNAL BENDING", stage2_text)
        self.assertNotIn("bend-up stroke", stage2_text)
        self.assertNotIn("cycle-ready Z park", stage2_text)
        self.assertNotRegex(stage2_text, r"(?m)^\s*M18(?:\s|;|$)")
        self.assertLess(stage2_text.index("G28\nM104 S1"), stage2_text.index("M140 S70"))

        scan = timelapse.scan_gcode(stage2)
        self.assertEqual(scan.physical_layers, 2)
        self.assertEqual(scan.bend_operations, 0)
        self.assertEqual(scan.clearing_passes, 16)
        self.assertEqual(scan.synthetic_layers, 32)

        timelapse.transform_gcode(stage2, output, scan)
        validation = timelapse.validate_patched(output, expected=scan)
        self.assertEqual(validation.logical_layers, 34)
        self.assertEqual(validation.generated_snapshots, 32)
        self.assertNotRegex(
            output.read_text(encoding="utf-8"), r"(?m)^\s*M18(?:\s|;|$)"
        )

    def test_stage1_remains_the_default(self) -> None:
        self.assertEqual(cycle.CycleConfig(clear_height=20).stage, 1)
        self.assertEqual(cycle.build_parser().parse_args(["input.gcode"]).stage, 1)

    def test_rejects_already_processed_input(self) -> None:
        source = self.root / "plate.gcode"
        source.write_text(native_plate() + f"; {cycle.CYCLE_TAG}\n", encoding="utf-8")
        with self.assertRaisesRegex(cycle.CycleError, "already processor-marked"):
            cycle.scan_native_gcode(source)


if __name__ == "__main__":
    unittest.main()
