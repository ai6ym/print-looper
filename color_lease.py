"""AMS colour leasing and exact filament-to-slot planning.

The printer's filament-backup grouping is controlled by filament preset ID and
RGB colour.  The queue keeps the real colour in its own inventory and assigns
an almost-identical, synthetic RGB colour to the slots that are allowed to
back one another up for a job.  Slots outside that cohort retain a different
colour and cannot be selected by the printer as a backup.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Iterable, Mapping


class LeaseError(ValueError):
    """The live/inventory state cannot produce an unambiguous safe mapping."""


def normalize_color(value: str) -> str:
    """Return uppercase RRGGBB, deliberately discarding the AMS alpha byte."""
    text = value.strip().lstrip("#").upper()
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6 or any(char not in "0123456789ABCDEF" for char in text):
        raise LeaseError(f"invalid RGB colour: {value!r}")
    return text


def slot_label_to_id(label: str) -> int:
    text = label.strip().upper()
    if len(text) < 2 or not text[0].isalpha() or not text[1:].isdigit():
        raise LeaseError(f"invalid AMS slot label: {label!r}")
    ams = ord(text[0]) - ord("A")
    slot = int(text[1:]) - 1
    if not 0 <= ams <= 25 or not 0 <= slot <= 3:
        raise LeaseError(f"invalid AMS slot label: {label!r}")
    return ams * 4 + slot


def slot_id_to_label(tray_id: int) -> str:
    if tray_id < 0 or tray_id >= 104:
        raise LeaseError(f"unsupported standard AMS tray id: {tray_id}")
    return f"{chr(ord('A') + tray_id // 4)}{tray_id % 4 + 1}"


@dataclass(frozen=True)
class FilamentRequirement:
    index: int
    preset_id: str
    material: str
    color: str
    nozzle_temp_min: int = 0
    nozzle_temp_max: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "color", normalize_color(self.color))

    @property
    def identity(self) -> tuple[str, str]:
        return self.preset_id, self.color


@dataclass(frozen=True)
class Slot:
    label: str
    tray_id: int
    preset_id: str
    material: str
    real_color: str
    reported_color: str
    nozzle_temp_min: int = 0
    nozzle_temp_max: int = 0
    setting_id: str = ""
    cali_idx: int = -1
    present: bool | None = None
    state: str = "verified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", self.label.upper())
        object.__setattr__(self, "real_color", normalize_color(self.real_color))
        object.__setattr__(self, "reported_color", normalize_color(self.reported_color))

    @property
    def identity(self) -> tuple[str, str]:
        return self.preset_id, self.real_color


@dataclass(frozen=True)
class SlotUpdate:
    label: str
    tray_id: int
    preset_id: str
    material: str
    real_color: str
    lease_color: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    setting_id: str
    cali_idx: int


@dataclass(frozen=True)
class LeasePlan:
    filament_colors: dict[int, str]
    ams_mapping: list[int]
    cohorts: dict[int, list[str]]
    updates: list[SlotUpdate]
    locked_slots: list[str]
    auto_refill: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _candidate_colors(real: str, seed: str) -> Iterable[str]:
    rgb = [int(real[i : i + 2], 16) for i in (0, 2, 4)]
    candidates: list[str] = []
    # Stay within two 8-bit steps on one channel.  This is invisible in the UI
    # but is a real RGB mismatch.  Alpha is not used because firmware tooling
    # normalises it.
    for delta in (1, -1, 2, -2):
        for channel in range(3):
            value = rgb[channel] + delta
            if 0 <= value <= 255:
                changed = list(rgb)
                changed[channel] = value
                candidates.append("".join(f"{part:02X}" for part in changed))
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    start = int.from_bytes(digest[:2], "big") % len(candidates)
    for offset in range(len(candidates)):
        yield candidates[(start + offset) % len(candidates)]


def allocate_lease_color(real: str, occupied: Iterable[str], *, seed: str) -> str:
    real = normalize_color(real)
    used = {normalize_color(color) for color in occupied}
    used.add(real)
    for candidate in _candidate_colors(real, seed):
        if candidate not in used:
            return candidate
    raise LeaseError(f"no imperceptibly close lease colour is available for #{real}")


def plan_leases(
    requirements: Iterable[FilamentRequirement],
    slots: Iterable[Slot],
    *,
    strict: bool = False,
    preferred: Mapping[int, str] | None = None,
    job_seed: str = "queue",
) -> LeasePlan:
    """Plan exact mappings and the metadata writes required before dispatch.

    With backup enabled, every verified physical slot with the exact preset ID
    and real RGB joins the cohort.  A newly-loaded matching spool therefore
    inherits the existing cohort lease automatically.  In strict mode only the
    mapped slot is leased and firmware auto-refill must be disabled.
    """
    reqs = sorted(requirements, key=lambda req: req.index)
    if not reqs:
        raise LeaseError("job has no filament requirements")
    inventory = sorted(slots, key=lambda slot: slot.tray_id)
    preferred = {int(key): value.upper() for key, value in (preferred or {}).items()}
    usable = [slot for slot in inventory if slot.state == "verified"]
    occupied = {slot.reported_color for slot in inventory}
    assigned: dict[tuple[str, str], str] = {}
    colors: dict[int, str] = {}
    mappings: list[int] = [-1] * max(req.index for req in reqs)
    cohorts: dict[int, list[str]] = {}
    updates_by_tray: dict[int, SlotUpdate] = {}
    locked: set[str] = set()

    for req in reqs:
        matches = [
            slot for slot in usable
            if slot.identity == req.identity and slot.material == req.material
        ]
        if not matches:
            raise LeaseError(
                f"filament {req.index} has no verified exact AMS match for "
                f"preset {req.preset_id!r}, colour #{req.color}"
            )
        selected = matches[0]
        if req.index in preferred:
            wanted = preferred[req.index]
            selected_matches = [slot for slot in matches if slot.label == wanted]
            if not selected_matches:
                raise LeaseError(
                    f"preferred mapping {req.index}={wanted} is not an exact verified match"
                )
            selected = selected_matches[0]

        cohort = [selected] if strict else matches
        identity = req.identity
        if identity in assigned:
            lease = assigned[identity]
        else:
            # Preserve a coherent existing lease where possible.  The selected
            # slot wins; otherwise the most common non-real colour wins.  This
            # is what lets a newly loaded real-black slot join a black cohort
            # that is already leased as #000001.
            cohort_ids = {slot.tray_id for slot in cohort}

            def isolated(candidate: str) -> bool:
                return not any(
                    slot.tray_id not in cohort_ids
                    and slot.preset_id == req.preset_id
                    and slot.reported_color == candidate
                    for slot in inventory
                )

            existing = [
                slot.reported_color
                for slot in cohort
                if slot.reported_color != req.color and isolated(slot.reported_color)
            ]
            if selected.reported_color != req.color and isolated(selected.reported_color):
                lease = selected.reported_color
            elif existing:
                lease = max(sorted(set(existing)), key=existing.count)
            else:
                lease = allocate_lease_color(
                    req.color,
                    occupied | set(assigned.values()),
                    seed=f"{job_seed}:{req.preset_id}:{req.color}",
                )
            assigned[identity] = lease
            occupied.add(lease)

        colors[req.index] = lease
        mappings[req.index - 1] = selected.tray_id
        cohorts[req.index] = [slot.label for slot in cohort]
        for slot in cohort:
            locked.add(slot.label)
            if slot.reported_color != lease:
                if not 150 <= slot.nozzle_temp_min <= slot.nozzle_temp_max <= 350:
                    raise LeaseError(
                        f"{slot.label} has invalid nozzle temperature bounds "
                        f"{slot.nozzle_temp_min}-{slot.nozzle_temp_max}; redeclare the spool"
                    )
                updates_by_tray[slot.tray_id] = SlotUpdate(
                    label=slot.label,
                    tray_id=slot.tray_id,
                    preset_id=slot.preset_id,
                    material=slot.material,
                    real_color=slot.real_color,
                    lease_color=lease,
                    nozzle_temp_min=slot.nozzle_temp_min,
                    nozzle_temp_max=slot.nozzle_temp_max,
                    setting_id=slot.setting_id,
                    cali_idx=slot.cali_idx,
                )

    return LeasePlan(
        filament_colors=colors,
        ams_mapping=mappings,
        cohorts=cohorts,
        updates=sorted(updates_by_tray.values(), key=lambda item: item.tray_id),
        locked_slots=sorted(locked, key=slot_label_to_id),
        auto_refill=not strict,
    )
