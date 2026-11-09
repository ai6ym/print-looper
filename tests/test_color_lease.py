from __future__ import annotations

import unittest

from color_lease import FilamentRequirement, Slot, normalize_color, plan_leases


def slot(label: str, real: str, reported: str, *, preset: str = "GFG02") -> Slot:
    tray = (ord(label[0]) - ord("A")) * 4 + int(label[1]) - 1
    return Slot(
        label=label, tray_id=tray, preset_id=preset, material="PETG",
        real_color=real, reported_color=reported, nozzle_temp_min=220,
        nozzle_temp_max=260,
    )


class ColorLeaseTests(unittest.TestCase):
    def test_alpha_is_not_part_of_backup_identity(self) -> None:
        self.assertEqual(normalize_color("#000001FF"), "000001")
        self.assertEqual(normalize_color("00000100"), "000001")

    def test_new_matching_black_spool_joins_existing_lease(self) -> None:
        requirement = FilamentRequirement(1, "GFG02", "PETG", "000000")
        plan = plan_leases(
            [requirement],
            [slot("A1", "000000", "000001"), slot("A2", "000000", "000001"),
             slot("B1", "000000", "000001"), slot("A3", "000000", "000000")],
            job_seed="repeat",
        )
        self.assertEqual(plan.filament_colors, {1: "000001"})
        self.assertEqual(plan.ams_mapping, [0])
        self.assertEqual(plan.cohorts[1], ["A1", "A2", "A3", "B1"])
        self.assertEqual([update.label for update in plan.updates], ["A3"])
        self.assertTrue(plan.auto_refill)

    def test_strict_job_locks_only_mapped_slot(self) -> None:
        requirement = FilamentRequirement(1, "GFG02", "PETG", "000000")
        plan = plan_leases(
            [requirement], [slot("A1", "000000", "000000"), slot("A2", "000000", "000000")],
            strict=True, preferred={1: "A2"}, job_seed="strict",
        )
        self.assertEqual(plan.ams_mapping, [1])
        self.assertEqual(plan.locked_slots, ["A2"])
        self.assertFalse(plan.auto_refill)
        self.assertNotEqual(plan.filament_colors[1], "000000")

    def test_mapping_preserves_unused_filament_index_holes(self) -> None:
        requirement = FilamentRequirement(2, "GFG02", "PETG", "000000")
        plan = plan_leases([requirement], [slot("A1", "000000", "000000")])
        self.assertEqual(plan.ams_mapping, [-1, 0])

    def test_different_physical_color_is_not_added_to_cohort(self) -> None:
        requirement = FilamentRequirement(1, "GFG02", "PETG", "000000")
        plan = plan_leases(
            [requirement],
            [slot("A1", "000000", "000001"), slot("A3", "FFFFFF", "000000")],
        )
        self.assertEqual(plan.locked_slots, ["A1"])

    def test_excluded_slot_with_old_lease_forces_new_cohort_color(self) -> None:
        requirement = FilamentRequirement(1, "GFG02", "PETG", "000000")
        excluded = slot("A3", "FFFFFF", "000001")
        excluded = Slot(**{**excluded.__dict__, "state": "unmanaged"})
        plan = plan_leases(
            [requirement], [slot("A1", "000000", "000001"), excluded], job_seed="collision"
        )
        self.assertNotEqual(plan.filament_colors[1], "000001")
        self.assertEqual([update.label for update in plan.updates], ["A1"])


if __name__ == "__main__":
    unittest.main()
