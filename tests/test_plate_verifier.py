from __future__ import annotations

import unittest

from plate_verifier import recovery_sweep_gcode


class PlateVerifierTests(unittest.TestCase):
    def test_recovery_passes_lower_but_never_below_five(self) -> None:
        height, gcode = recovery_sweep_gcode(21.0, drop_mm=3.0, attempt=2)
        self.assertEqual(height, 15.0)
        self.assertIn("G1 Z15 F10000", gcode)
        self.assertEqual(gcode.count("G1 Y0"), 16)
        self.assertNotIn("M18", gcode)
        floor, _ = recovery_sweep_gcode(8.0, drop_mm=3.0, attempt=4)
        self.assertEqual(floor, 5.0)


if __name__ == "__main__":
    unittest.main()
