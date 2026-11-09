"""Read filament requirements and create colour-leased dispatch archives."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from color_lease import FilamentRequirement, LeaseError, normalize_color


class ArchiveError(ValueError):
    pass


GCODE_COLOR_RE = re.compile(rb"^(;\s*filament_colour\s*=\s*)([^\r\n]*)(\r?\n?)$", re.I)


def _archive_members(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    gcodes = [name for name in names if name.lower().endswith(".gcode") and not name.endswith("/")]
    if len(gcodes) != 1:
        raise ArchiveError(f"expected exactly one sliced G-code member, found {len(gcodes)}")
    slice_members = [name for name in names if name.lower() == "metadata/slice_info.config"]
    if len(slice_members) != 1:
        raise ArchiveError("archive has no unambiguous Metadata/slice_info.config")
    return gcodes[0], slice_members[0]


def archive_gcode_member(path: Path) -> str:
    """Return the exact plate member the printer must receive in `param`."""
    return _archive_members(path)[0]


def read_requirements(path: Path) -> list[FilamentRequirement]:
    _, slice_member = _archive_members(path)
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read(slice_member))
    result: list[FilamentRequirement] = []
    for node in root.findall(".//plate/filament"):
        try:
            result.append(
                FilamentRequirement(
                    index=int(node.attrib["id"]),
                    preset_id=node.attrib.get("tray_info_idx", ""),
                    material=node.attrib.get("type", ""),
                    color=node.attrib["color"],
                )
            )
        except (KeyError, ValueError, LeaseError) as exc:
            raise ArchiveError(f"invalid filament entry in slice_info.config: {exc}") from exc
    if not result:
        raise ArchiveError("slice_info.config contains no plate filament entries")
    indices = [item.index for item in result]
    if indices != sorted(set(indices)):
        raise ArchiveError(f"filament IDs are duplicated or unordered: {indices}")
    return result


def _leased_color_string(original: str, colors: dict[int, str]) -> str:
    separator = ";" if ";" in original else "," if "," in original else None
    values = original.strip().split(separator) if separator else [original.strip()]
    for index, color in colors.items():
        offset = index - 1
        if not 0 <= offset < len(values):
            raise ArchiveError(f"filament {index} is outside G-code colour list")
        values[offset] = "#" + normalize_color(color)
    return (separator or "").join(values)


def _rewrite_gcode(data: bytes, colors: dict[int, str]) -> bytes:
    output: list[bytes] = []
    found = 0
    for line in data.splitlines(keepends=True):
        match = GCODE_COLOR_RE.match(line)
        if match:
            original = match.group(2).decode("ascii")
            replacement = _leased_color_string(original, colors).encode("ascii")
            line = match.group(1) + replacement + match.group(3)
            found += 1
        output.append(line)
    if not found:
        raise ArchiveError("G-code has no filament_colour header to lease")
    return b"".join(output)


def _rewrite_project(data: bytes, colors: dict[int, str]) -> bytes:
    try:
        project = json.loads(data)
        values = project["filament_colour"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ArchiveError("project_settings.config has no usable filament_colour list") from exc
    if not isinstance(values, list):
        raise ArchiveError("project filament_colour is not a list")
    for index, color in colors.items():
        offset = index - 1
        if not 0 <= offset < len(values):
            raise ArchiveError(f"filament {index} is outside project colour list")
        values[offset] = "#" + normalize_color(color)
    return (json.dumps(project, indent=4, ensure_ascii=False) + "\n").encode("utf-8")


def _rewrite_slice(data: bytes, colors: dict[int, str]) -> bytes:
    root = ET.fromstring(data)
    seen: set[int] = set()
    for node in root.findall(".//plate/filament"):
        index = int(node.attrib["id"])
        if index in colors:
            node.set("color", "#" + normalize_color(colors[index]))
            seen.add(index)
    if seen != set(colors):
        raise ArchiveError(f"slice_info.config did not contain filament IDs {sorted(set(colors) - seen)}")
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def create_leased_archive(source: Path, destination: Path, colors: dict[int, str]) -> None:
    """Copy a processed 3MF and make its filament metadata match leased slots."""
    if destination.exists():
        raise ArchiveError(f"refusing to overwrite leased archive: {destination}")
    gcode_member, slice_member = _archive_members(source)
    project_member = "Metadata/project_settings.config"
    md5_member = gcode_member + ".md5"
    normalized = {int(index): normalize_color(color) for index, color in colors.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".3mf", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(source) as src, zipfile.ZipFile(temp_path, "w") as dst:
            if project_member not in src.namelist():
                raise ArchiveError(f"archive has no {project_member}")
            rewritten_gcode: bytes | None = None
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == gcode_member:
                    data = rewritten_gcode = _rewrite_gcode(data, normalized)
                elif info.filename == slice_member:
                    data = _rewrite_slice(data, normalized)
                elif info.filename == project_member:
                    data = _rewrite_project(data, normalized)
                elif info.filename == md5_member:
                    if rewritten_gcode is None:
                        rewritten_gcode = _rewrite_gcode(src.read(gcode_member), normalized)
                    data = hashlib.md5(rewritten_gcode).hexdigest().upper().encode("ascii")
                dst.writestr(info, data)
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
