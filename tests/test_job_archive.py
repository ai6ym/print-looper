from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from job_archive import archive_gcode_member, create_leased_archive, read_requirements


class JobArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(self) -> Path:
        path = self.root / "job.gcode.3mf"
        gcode = b"; filament_colour = #000000;#FFFFFF\nG1 X1\n"
        project = {"filament_colour": ["#000000", "#FFFFFF"], "other": "unchanged"}
        slice_info = b'''<?xml version="1.0"?><config><plate>
<filament id="1" tray_info_idx="GFG02" type="PETG" color="#000000"/>
<filament id="2" tray_info_idx="GFG02" type="PETG" color="#FFFFFF"/>
</plate></config>'''
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode)
            archive.writestr("Metadata/plate_1.gcode.md5", hashlib.md5(gcode).hexdigest().upper())
            archive.writestr("Metadata/project_settings.config", json.dumps(project))
            archive.writestr("Metadata/slice_info.config", slice_info)
            archive.writestr("Metadata/unrelated.bin", b"same")
        return path

    def test_reads_requirements_and_rewrites_all_color_metadata(self) -> None:
        source = self.fixture()
        requirements = read_requirements(source)
        self.assertEqual(archive_gcode_member(source), "Metadata/plate_1.gcode")
        self.assertEqual([(item.index, item.preset_id, item.color) for item in requirements],
                         [(1, "GFG02", "000000"), (2, "GFG02", "FFFFFF")])
        output = self.root / "leased.gcode.3mf"
        create_leased_archive(source, output, {1: "000001", 2: "FFFFFE"})
        with zipfile.ZipFile(output) as archive:
            gcode = archive.read("Metadata/plate_1.gcode")
            self.assertIn(b"#000001;#FFFFFE", gcode)
            self.assertEqual(
                archive.read("Metadata/plate_1.gcode.md5").decode(),
                hashlib.md5(gcode).hexdigest().upper(),
            )
            project = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertEqual(project["filament_colour"], ["#000001", "#FFFFFE"])
            root = ET.fromstring(archive.read("Metadata/slice_info.config"))
            self.assertEqual([node.attrib["color"] for node in root.findall(".//filament")],
                             ["#000001", "#FFFFFE"])
            self.assertEqual(archive.read("Metadata/unrelated.bin"), b"same")


if __name__ == "__main__":
    unittest.main()
