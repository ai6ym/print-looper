"""Queue orchestration: intake, leasing, dispatch, monitoring and plate checks."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
import uuid
import zipfile

import farmloop_timelapse
import plate_cycle
from bambu_client import BambuClient, PrinterError
from color_lease import LeaseError, Slot, plan_leases
from job_archive import ArchiveError, archive_gcode_member, create_leased_archive, read_requirements
from plate_verifier import VerificationError, compare_to_reference, recovery_sweep_gcode
from queue_store import QueueStore, StoreError


class QueueError(RuntimeError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "printer": {
        "host": "CHANGE-ME",
        "serial": "CHANGE-ME",
        "access_code_env": "PRINT_LOOPER_ACCESS_CODE",
        "mqtt_port": 8883,
        "ftps_port": 990,
    },
    "dispatch": {
        "remote_directory": "/cache/print-looper",
        "bed_type": "auto",
        "bed_leveling": True,
        "flow_calibration": False,
        "timelapse": True,
        "vibration_calibration": False,
        "layer_inspection": True,
        "poll_seconds": 5,
    },
    "camera": {
        "reference": "camera/clean-plate.png",
        "roi": [0.08, 0.18, 0.84, 0.70],
        "mean_threshold": 0.045,
        "changed_fraction_threshold": 0.08,
        "settle_seconds": 8,
    },
    "recovery": {"attempts": 3, "z_drop_mm": 3.0, "park_z": 20.2},
}


def load_config(home: Path) -> dict[str, Any]:
    path = home / "config.json"
    if not path.is_file():
        raise QueueError(f"queue is not initialized: run init first ({path})")
    with path.open(encoding="utf-8") as source:
        config = json.load(source)
    return config


def initialize(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for directory in ("artifacts", "dispatch", "camera"):
        (home / directory).mkdir(exist_ok=True)
    config_path = home / "config.json"
    if config_path.exists():
        raise QueueError(f"refusing to overwrite existing config: {config_path}")
    config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    store = QueueStore(home / "queue.db")
    store.close()


def make_client(config: dict[str, Any]) -> BambuClient:
    printer = config["printer"]
    env_name = printer.get("access_code_env", "PRINT_LOOPER_ACCESS_CODE")
    access_code = os.environ.get(env_name) or printer.get("access_code")
    if not access_code:
        raise QueueError(f"printer access code is missing; set {env_name}")
    if printer.get("host") == "CHANGE-ME" or printer.get("serial") == "CHANGE-ME":
        raise QueueError("set printer.host and printer.serial in config.json")
    return BambuClient(
        host=printer["host"], serial=printer["serial"], access_code=access_code,
        mqtt_port=int(printer.get("mqtt_port", 8883)),
        ftps_port=int(printer.get("ftps_port", 990)),
    )


def _is_processed(path: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="print-looper-intake-") as temp:
        gcode, _ = farmloop_timelapse.extract_input(path, Path(temp))
        with gcode.open("r", encoding="utf-8", errors="replace") as source:
            return any(plate_cycle.CYCLE_TAG in line for line in source)


def process_for_queue(source: Path, destination: Path) -> float:
    if not zipfile.is_zipfile(source):
        raise QueueError("queue intake requires a sliced .gcode.3mf/.3mf archive")
    if _is_processed(source):
        with tempfile.TemporaryDirectory(prefix="print-looper-validate-") as temp:
            gcode, _ = farmloop_timelapse.extract_input(source, Path(temp))
            plate_cycle.validate_cycle_markers(gcode)
            farmloop_timelapse.validate_patched(gcode)
            max_z = None
            with gcode.open("r", encoding="utf-8", errors="replace") as input_file:
                for line in input_file:
                    match = plate_cycle.MAX_Z_RE.match(line.rstrip("\r\n"))
                    if match:
                        max_z = float(match.group(1))
                        break
            if max_z is None:
                raise QueueError("processed job has no max_z_height header")
        shutil.copy2(source, destination)
        return plate_cycle.default_clear_height(max_z)

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            result = plate_cycle.main([str(source), "-o", str(destination), "--json"])
    except farmloop_timelapse.FarmLoopError as exc:
        raise QueueError(f"postprocessing failed: {exc}") from exc
    if result != 0:
        raise QueueError(f"postprocessing failed with exit status {result}")
    try:
        report = json.loads(buffer.getvalue())
        return float(report["clear_height"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise QueueError("postprocessor returned an invalid report") from exc


def add_to_queue(
    store: QueueStore,
    home: Path,
    source: Path,
    *,
    name: str | None = None,
    strict: bool = False,
    preferred: dict[int, str] | None = None,
    copies: int = 1,
) -> list[int]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise QueueError(f"input does not exist: {source}")
    artifact_id = uuid.uuid4().hex
    suffix = ".gcode.3mf" if source.name.lower().endswith(".gcode.3mf") else ".3mf"
    destination = home / "artifacts" / f"{artifact_id}{suffix}"
    clear_height = process_for_queue(source, destination)
    try:
        requirements = read_requirements(destination)
        first = store.add_job(
            name=name or source.name, source_path=source, processed_path=destination,
            requirements=requirements, clear_height=clear_height, strict=strict,
            preferred=preferred,
        )
        result = [first]
        if copies > 1:
            result.extend(store.repeat(first, copies - 1))
        return result
    except Exception:
        destination.unlink(missing_ok=True)
        raise


class QueueEngine:
    def __init__(self, home: Path, store: QueueStore, config: dict[str, Any], client):
        self.home = home
        self.store = store
        self.config = config
        self.client = client

    def _sync_reported_colors(self) -> None:
        live = {slot.label: slot for slot in self.client.live_slots()}
        for declared in self.store.slots():
            actual = live.get(declared.label)
            if (
                actual and actual.present is not False
                and actual.preset_id == declared.preset_id
                and actual.material == declared.material
            ):
                self.store.update_reported_color(declared.label, actual.reported_color)

    def _apply_pending_slots(self) -> None:
        for slot in self.store.slots():
            if slot.state != "pending":
                continue
            self.client.set_slot(
                tray_id=slot.tray_id, preset_id=slot.preset_id, material=slot.material,
                color=slot.real_color, nozzle_temp_min=slot.nozzle_temp_min,
                nozzle_temp_max=slot.nozzle_temp_max,
                setting_id=slot.setting_id, cali_idx=slot.cali_idx,
            )
            self.store.set_slot_state(slot.label, "verified", reported_color=slot.real_color)
            self.store.log("ams", f"verified pending contents for {slot.label}")

    def _planning_slots(self) -> list[Slot]:
        """Combine physical truth with live metadata and quarantine unknowns."""
        declared = {slot.label: slot for slot in self.store.slots()}
        live = {slot.label: slot for slot in self.client.live_slots()}
        result: list[Slot] = []
        for label, actual in live.items():
            physical = declared.get(label)
            if (
                physical is not None
                and physical.state == "verified"
                and actual.present is True
                and physical.preset_id == actual.preset_id
                and physical.material == actual.material
            ):
                result.append(
                    Slot(
                        label=label, tray_id=actual.tray_id,
                        preset_id=physical.preset_id, material=physical.material,
                        real_color=physical.real_color, reported_color=actual.reported_color,
                        nozzle_temp_min=physical.nozzle_temp_min,
                        nozzle_temp_max=physical.nozzle_temp_max,
                        setting_id=physical.setting_id, cali_idx=physical.cali_idx,
                        present=actual.present,
                        state="verified",
                    )
                )
            else:
                # It may be empty-but-configured or manually changed.  Either
                # way, include its live identity only as a collision guard; it
                # can never satisfy a queue mapping.
                result.append(
                    Slot(
                        label=label, tray_id=actual.tray_id,
                        preset_id=actual.preset_id, material=actual.material,
                        real_color=actual.reported_color, reported_color=actual.reported_color,
                        nozzle_temp_min=actual.nozzle_temp_min,
                        nozzle_temp_max=actual.nozzle_temp_max,
                        setting_id=actual.setting_id, cali_idx=actual.cali_idx,
                        present=actual.present,
                        state="unmanaged",
                    )
                )
        return result

    def _remove_empty_slots_from_lease(self, job) -> None:
        if not job["lease_json"]:
            return
        lease = json.loads(job["lease_json"])
        locked = set(lease.get("locked_slots", []))
        live = {slot.label: slot for slot in self.client.live_slots()}
        for physical in self.store.slots():
            actual = live.get(physical.label)
            if (
                physical.state != "verified"
                or physical.label not in locked
                or actual is None
                or actual.present is not False
            ):
                continue
            restored = False
            try:
                self.client.set_slot(
                    tray_id=physical.tray_id, preset_id=physical.preset_id,
                    material=physical.material, color=physical.real_color,
                    nozzle_temp_min=physical.nozzle_temp_min,
                    nozzle_temp_max=physical.nozzle_temp_max,
                    setting_id=physical.setting_id, cali_idx=physical.cali_idx,
                    select_calibration=False,
                )
                restored = True
            except PrinterError as exc:
                # Some firmware refuses metadata writes to a physically empty
                # tray.  It is still removed locally, and the next planner sees
                # its stale reported lease as a collision and rotates the live
                # cohort away from it before dispatch.
                self.store.log(
                    "lease-cleanup",
                    f"{physical.label} is empty; printer refused real-color restore: {exc}",
                    job_id=job["id"],
                )
            reported = physical.real_color if restored else actual.reported_color
            self.store.set_slot_state(physical.label, "empty", reported_color=reported)
            self.store.log(
                "lease-cleanup",
                f"removed empty {physical.label} from lease"
                + (" and restored its real color" if restored else " using collision quarantine"),
                job_id=job["id"],
                data={"real_color": physical.real_color, "reported_color": reported},
            )

    def _verify_finished_job(self, job) -> bool:
        camera = self.config["camera"]
        recovery = self.config["recovery"]
        reference = Path(camera["reference"])
        if not reference.is_absolute():
            reference = self.home / reference
        attempts = int(recovery.get("attempts", 3))
        for attempt in range(0, attempts + 1):
            image = self.home / "camera" / f"job-{job['id']}-check-{attempt}.png"
            self.client.capture_frame(image)
            result = compare_to_reference(
                reference, image, roi=camera.get("roi"),
                mean_threshold=float(camera.get("mean_threshold", 0.045)),
                changed_fraction_threshold=float(camera.get("changed_fraction_threshold", 0.08)),
            )
            self.store.log("camera", "plate check", job_id=job["id"], data=result.to_dict())
            if result.clear:
                self.store.set_setting("plate_verified", 1)
                self.store.mark_completed(job["id"])
                return True
            if attempt == attempts:
                break
            height, gcode = recovery_sweep_gcode(
                float(job["clear_height"]), drop_mm=float(recovery.get("z_drop_mm", 3.0)),
                attempt=attempt + 1, park_z=float(recovery.get("park_z", 20.2)),
            )
            self.store.log(
                "recovery", f"running recovery clearing pass at Z{height:g}",
                job_id=job["id"], data={"attempt": attempt + 1, "height": height},
            )
            self.client.send_gcode(gcode)
            time.sleep(float(camera.get("settle_seconds", 8)))
        message = "plate still appears occupied after all recovery clearing passes"
        self.store.mark_failed(job["id"], message)
        self.store.set_blocked(True, message)
        self.store.set_paused(True)
        return False

    def _verify_idle_plate(self) -> bool:
        camera = self.config["camera"]
        reference = Path(camera["reference"])
        if not reference.is_absolute():
            reference = self.home / reference
        image = self.home / "camera" / f"idle-preflight-{int(time.time())}.png"
        self.client.capture_frame(image)
        result = compare_to_reference(
            reference, image, roi=camera.get("roi"),
            mean_threshold=float(camera.get("mean_threshold", 0.045)),
            changed_fraction_threshold=float(camera.get("changed_fraction_threshold", 0.08)),
        )
        self.store.log("camera", "idle pre-dispatch plate check", data=result.to_dict())
        if result.clear:
            self.store.set_setting("plate_verified", 1)
            return True
        message = "idle plate is not clear; refusing to dispatch"
        self.store.set_blocked(True, message)
        self.store.set_paused(True)
        return False

    def _dispatch(self, job) -> None:
        slots = self._planning_slots()
        plan = plan_leases(
            self.store.requirements(job), slots, strict=bool(job["strict"]),
            preferred=self.store.preferred(job), job_seed=f"job-{job['id']}",
        )
        self.client.set_auto_refill(plan.auto_refill)
        for update in plan.updates:
            self.client.set_slot(
                tray_id=update.tray_id, preset_id=update.preset_id,
                material=update.material, color=update.lease_color,
                nozzle_temp_min=update.nozzle_temp_min, nozzle_temp_max=update.nozzle_temp_max,
                setting_id=update.setting_id, cali_idx=update.cali_idx,
            )
            self.store.update_reported_color(update.label, update.lease_color)

        dispatch_name = f"job-{job['id']}-{uuid.uuid4().hex[:8]}.gcode.3mf"
        local = self.home / "dispatch" / dispatch_name
        create_leased_archive(Path(job["processed_path"]), local, plan.filament_colors)
        remote_dir = self.config["dispatch"].get("remote_directory", "/cache/print-looper")
        remote = remote_dir.rstrip("/") + "/" + dispatch_name
        self.client.upload(local, remote)
        digest = hashlib.md5()
        with local.open("rb") as archive_file:
            for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
                digest.update(chunk)
        self.client.start_print(
            remote_path=remote, ams_mapping=plan.ams_mapping, name=job["name"],
            gcode_member=archive_gcode_member(local),
            md5=digest.hexdigest().upper(),
            bed_type=self.config["dispatch"].get("bed_type", "auto"),
            bed_leveling=bool(self.config["dispatch"].get("bed_leveling", True)),
            flow_cali=bool(self.config["dispatch"].get("flow_calibration", False)),
            timelapse=bool(self.config["dispatch"].get("timelapse", True)),
            vibration_cali=bool(self.config["dispatch"].get("vibration_calibration", False)),
            layer_inspect=bool(self.config["dispatch"].get("layer_inspection", True)),
        )
        self.store.set_setting("plate_verified", 0)
        self.store.mark_running(job["id"], plan.to_dict(), remote)

    def tick(self) -> str:
        state = self.client.gcode_state
        active = self.store.active_job()
        if active:
            if state in {"RUNNING", "PREPARE", "PAUSE", "SLICING"}:
                return f"job {active['id']} is {state.lower()}"
            if state in {"FAILED"}:
                message = "printer reported job failure"
                self.store.mark_failed(active["id"], message)
                self.store.set_blocked(True, message)
                self.store.set_paused(True)
                return message
            if not self.client.idle:
                return f"waiting for unambiguous printer idle state (reported {state})"
            if active["status"] == "running":
                self.store.mark_verifying(active["id"])
                active = self.store.job(active["id"])
            self._remove_empty_slots_from_lease(active)
            if not self._verify_finished_job(active):
                return "plate verification failed; queue blocked"

        if not self.client.idle:
            self.store.set_setting("plate_verified", 0)
            return f"printer is busy with an unmanaged job ({state})"
        self._sync_reported_colors()
        self._apply_pending_slots()
        if self.store.paused:
            return "queue is paused"
        if self.store.blocked:
            return "queue is blocked"
        job = self.store.next_job()
        if not job:
            return "queue is empty"
        if self.store.setting("plate_verified", "0") != "1" and not self._verify_idle_plate():
            return "idle plate verification failed; queue blocked"
        self._dispatch(job)
        return f"dispatched job {job['id']}"


def run_daemon(home: Path, *, once: bool = False) -> None:
    config = load_config(home)
    store = QueueStore(home / "queue.db")
    # An idle daemon restart rechecks the plate before trusting old state.  If
    # a managed job is active, completion handling owns the verification.
    if store.active_job() is None:
        store.set_setting("plate_verified", 0)
    client = make_client(config)
    try:
        with client:
            engine = QueueEngine(home, store, config, client)
            while True:
                try:
                    print(engine.tick(), flush=True)
                except (PrinterError, LeaseError, ArchiveError, VerificationError, StoreError) as exc:
                    store.log("daemon-error", str(exc))
                    print(f"error: {exc}", flush=True)
                if once:
                    return
                time.sleep(float(config["dispatch"].get("poll_seconds", 5)))
    finally:
        store.close()
