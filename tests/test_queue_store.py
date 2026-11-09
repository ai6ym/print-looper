from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest

from color_lease import FilamentRequirement
from queue_store import QueueStore
from print_queue import print_jobs


class QueueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root / "queue.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def add(self, name: str) -> int:
        return self.store.add_job(
            name=name, source_path=self.root / "in.3mf", processed_path=self.root / "out.3mf",
            requirements=[FilamentRequirement(1, "GFG02", "PETG", "000000")],
            clear_height=21.5,
        )

    def test_reorder_delete_and_repeat(self) -> None:
        first = self.add("first")
        second = self.add("second")
        self.store.move(second, 1)
        self.assertEqual(self.store.next_job()["id"], second)
        copies = self.store.repeat(first, 2)
        self.assertEqual(len(copies), 2)
        self.store.delete(second)
        self.assertEqual(self.store.next_job()["id"], first)

    def test_slot_tracks_real_and_leased_color_separately(self) -> None:
        self.store.upsert_slot(
            label="A1", preset_id="GFG02", material="PETG", real_color="000000",
            reported_color="000001", nozzle_temp_min=220, nozzle_temp_max=260,
        )
        item = self.store.slots()[0]
        self.assertEqual(item.real_color, "000000")
        self.assertEqual(item.reported_color, "000001")

    def test_cli_list_reports_queue_state_and_current_jobs(self) -> None:
        job_id = self.add("visible")
        output = io.StringIO()
        with redirect_stdout(output):
            print_jobs(self.store, json_output=True)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["queue_state"], "running")
        self.assertEqual([job["id"] for job in payload["jobs"]], [job_id])


if __name__ == "__main__":
    unittest.main()
