"""Camera-based clean-plate verification and lowered recovery sweeps."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    clear: bool
    mean_difference: float
    changed_fraction: float
    image: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_to_reference(
    reference: Path,
    image: Path,
    *,
    roi: Sequence[float] | None = None,
    mean_threshold: float = 0.045,
    changed_fraction_threshold: float = 0.08,
) -> VerificationResult:
    try:
        from PIL import Image, ImageChops, ImageEnhance, ImageStat
    except ImportError as exc:
        raise VerificationError("camera verification requires Pillow") from exc
    if not reference.is_file():
        raise VerificationError(f"clean-plate reference does not exist: {reference}")
    with Image.open(reference) as ref_source, Image.open(image) as sample_source:
        ref = ref_source.convert("L").resize((320, 240))
        sample = sample_source.convert("L").resize((320, 240))
        if roi:
            if len(roi) != 4 or any(not 0 <= value <= 1 for value in roi):
                raise VerificationError("camera ROI must be four normalized values in [0,1]")
            x, y, width, height = roi
            box = (int(x * 320), int(y * 240), int((x + width) * 320), int((y + height) * 240))
            ref = ref.crop(box)
            sample = sample.crop(box)
        ref_mean = ImageStat.Stat(ref).mean[0]
        sample_mean = ImageStat.Stat(sample).mean[0]
        if sample_mean > 1:
            sample = ImageEnhance.Brightness(sample).enhance(ref_mean / sample_mean)
        difference = ImageChops.difference(ref, sample)
        histogram = difference.histogram()
        pixels = max(1, ref.width * ref.height)
        mean = sum(value * count for value, count in enumerate(histogram)) / (255 * pixels)
        changed = sum(histogram[21:]) / pixels
    return VerificationResult(
        clear=mean <= mean_threshold and changed <= changed_fraction_threshold,
        mean_difference=mean,
        changed_fraction=changed,
        image=str(image),
    )


def recovery_sweep_gcode(clear_height: float, *, drop_mm: float, attempt: int, park_z: float = 20.2) -> tuple[float, str]:
    if attempt < 1:
        raise VerificationError("recovery attempt numbers start at one")
    height = max(5.0, clear_height - drop_mm * attempt)
    lines = [
        "M17", "G90", "M104 S0", "M140 S0", "M106 P2 S0", "M106 P3 S0",
        f"G1 Z{height:g} F10000", "G1 X125 F3000", "G1 Y250 F3000",
        "G1 Y0 F3000", "G1 Y250 F3000", "G1 Y0 F3000",
    ]
    for x in (220, 190, 160, 130, 100, 70, 30):
        lines.extend(["G1 Y250 F3000", f"G1 X{x} F3000", "G1 Y0 F3000"])
    lines.extend(["G1 Y250 F3000", "G1 X220 F3000", f"G1 Z{height:g} F12000"])
    for x in (220, 190, 160, 130, 100, 70, 30):
        lines.extend(["G1 Y250 F12000", f"G1 X{x} F12000", "G1 Y0 F12000"])
    lines.extend(
        ["G1 X65 Y245 F12000", "G1 Y265 F3000", f"G1 Z{park_z:g} F600", "M400"]
    )
    return height, "\n".join(lines) + "\n"
