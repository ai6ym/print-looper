from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from color_lease import FilamentRequirement, Slot
from queue_manager import QueueEngine
from queue_store import QueueStore


class FakeClient:
    gcode_state = "IDLE"
    idle = True

    def __init__(self):
        self.auto_refill = None
        self.slot_updates = []
        self.uploads = []
        self.started = None
        self.live = [
            Slot(
                label=label, tray_id=index, preset_id="GFG02", material="PETG",
                real_color=color, reported_color=color, nozzle_temp_min=220,
                nozzle_temp_max=260, present=True,
            )
            for index, (label, color) in enumerate(
                (("A1", "000001"), ("A2", "000001"), ("A3", "000000"))
            )
        ]

    def live_slots(self):
        return self.live

    def set_auto_refill(self, enabled):
        self.auto_refill = enabled

    def set_slot(self, **kwargs):
        self.slot_updates.append(kwargs)
        self.live = [
            Slot(
                **{
                    **slot.__dict__,
                    "reported_color": kwargs["color"],
                    "real_color": kwargs["color"],
                }
            )
            if slot.tray_id == kwargs["tray_id"] else slot
            for slot in self.live
        ]

    def upload(self, local, remote):
        self.uploads.append((local, remote))

    def start_print(self, **kwargs):
        self.started = kwargs
        self.gcode_state = "RUNNING"
        self.idle = False


class QueueEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "dispatch").mkdir()
        self.store = QueueStore(self.root / "queue.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def archive(self) -> Path:
        path = self.root / "processed.gcode.3mf"
        gcode = b"; filament_colour = #000000\nG1 X1\n"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Metadata/plate_5.gcode", gcode)
            archive.writestr("Metadata/plate_5.gcode.md5", hashlib.md5(gcode).hexdigest().upper())
            archive.writestr("Metadata/project_settings.config", json.dumps({"filament_colour": ["#000000"]}))
            archive.writestr(
                "Metadata/slice_info.config",
                '<config><plate><filament id="1" tray_info_idx="GFG02" type="PETG" color="#000000"/></plate></config>',
            )
        return path

    def test_dispatch_adds_new_matching_spool_to_existing_lease(self):
        archive = self.archive()
        job_id = self.store.add_job(
            name="plate", source_path=archive, processed_path=archive,
            requirements=[FilamentRequirement(1, "GFG02", "PETG", "000000")],
            clear_height=21.16,
        )
        for label, reported in (("A1", "000001"), ("A2", "000001"), ("A3", "000000")):
            self.store.upsert_slot(
                label=label, preset_id="GFG02", material="PETG", real_color="000000",
                reported_color=reported, nozzle_temp_min=220, nozzle_temp_max=260,
            )
        config = {
            "dispatch": {"remote_directory": "/cache/print-looper"},
            "camera": {}, "recovery": {},
        }
        client = FakeClient()
        engine = QueueEngine(self.root, self.store, config, client)
        engine._dispatch(self.store.job(job_id))
        self.assertTrue(client.auto_refill)
        self.assertEqual([item["tray_id"] for item in client.slot_updates], [2])
        self.assertEqual(client.slot_updates[0]["color"], "000001")
        self.assertEqual(client.started["ams_mapping"], [0])
        self.assertEqual(client.started["gcode_member"], "Metadata/plate_5.gcode")
        self.assertEqual(len(client.started["md5"]), 32)
        lease = json.loads(self.store.job(job_id)["lease_json"])
        self.assertEqual(lease["locked_slots"], ["A1", "A2", "A3"])

    def test_empty_slot_is_restored_and_removed_from_lease_between_jobs(self):
        archive = self.archive()
        job_id = self.store.add_job(
            name="plate", source_path=archive, processed_path=archive,
            requirements=[FilamentRequirement(1, "GFG02", "PETG", "000000")],
            clear_height=21.16,
        )
        for label in ("A1", "A2"):
            self.store.upsert_slot(
                label=label, preset_id="GFG02", material="PETG", real_color="000000",
                reported_color="000001", nozzle_temp_min=220, nozzle_temp_max=260,
            )
        self.store.mark_running(
            job_id,
            {"locked_slots": ["A1", "A2"], "filament_colors": {"1": "000001"}},
            "/cache/job.3mf",
        )
        client = FakeClient()
        client.live = [
            Slot(**{**client.live[0].__dict__, "present": False}),
            client.live[1],
        ]
        config = {"dispatch": {}, "camera": {}, "recovery": {}}
        engine = QueueEngine(self.root, self.store, config, client)
        engine._remove_empty_slots_from_lease(self.store.job(job_id))
        slots = {slot.label: slot for slot in self.store.slots()}
        self.assertEqual(slots["A1"].state, "empty")
        self.assertEqual(slots["A1"].reported_color, "000000")
        self.assertEqual(slots["A2"].state, "verified")
        self.assertFalse(client.slot_updates[-1]["select_calibration"])


if __name__ == "__main__":
    unittest.main()
