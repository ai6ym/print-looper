#!/usr/bin/env python3
"""SSH-friendly local queue controls for Print Looper."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from bambu_client import PrinterError
from color_lease import LeaseError, normalize_color, slot_label_to_id
from job_archive import ArchiveError
from queue_manager import (
    QueueError, add_to_queue, initialize, load_config, make_client, run_daemon,
)
from queue_store import QueueStore, StoreError


def default_home() -> Path:
    value = os.environ.get("PRINT_LOOPER_HOME")
    return Path(value).expanduser() if value else Path.cwd() / ".print-looper"


def parse_mappings(values: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for value in values:
        try:
            left, right = value.split("=", 1)
            index = int(left)
            slot_label_to_id(right)
        except (ValueError, LeaseError) as exc:
            raise QueueError(f"invalid mapping {value!r}; expected FILAMENT=A1") from exc
        if index in result:
            raise QueueError(f"filament {index} is mapped more than once")
        result[index] = right.upper()
    return result


def print_jobs(store: QueueStore, *, all_jobs: bool = False, json_output: bool = False) -> None:
    rows = store.jobs(include_finished=all_jobs)
    payload = [dict(row) for row in rows]
    queue_state = "blocked" if store.blocked else "paused" if store.paused else "running"
    reason = store.setting("block_reason", "") if store.blocked else ""
    if json_output:
        print(
            json.dumps(
                {"queue_state": queue_state, "block_reason": reason, "jobs": payload},
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"Queue: {queue_state.upper()}" + (f" — {reason}" if reason else ""))
    print("ID  POS  STATE       MODE    NAME")
    for row in rows:
        mode = "strict" if row["strict"] else "refill"
        print(f"{row['id']:<3} {row['position'] // 10:<4} {row['status']:<11} {mode:<7} {row['name']}")


def ams_load(store: QueueStore, args) -> None:
    label = args.slot.upper()
    slot_label_to_id(label)
    existing = {slot.label: slot for slot in store.slots()}.get(label)
    active = store.active_job()
    new_identity = (args.preset, normalize_color(args.color))
    peer = next((slot for slot in store.slots() if slot.identity == new_identity), None)
    if active and active["lease_json"]:
        lease = json.loads(active["lease_json"])
        locked = set(lease.get("locked_slots", []))
        if label in locked and (existing is None or existing.identity != new_identity):
            raise QueueError(
                f"{label} is locked into active job {active['id']}'s refill cohort; "
                "use an outside slot or wait for the job to become idle"
            )
    store.upsert_slot(
        label=label, preset_id=args.preset, material=args.material,
        real_color=args.color,
        reported_color=existing.reported_color if existing else args.reported_color or args.color,
        nozzle_temp_min=args.temp_min, nozzle_temp_max=args.temp_max,
        setting_id=args.setting_id if args.setting_id is not None else (peer.setting_id if peer else ""),
        cali_idx=args.cali_idx if args.cali_idx is not None else (peer.cali_idx if peer else -1),
        state="pending",
    )
    print(f"Recorded {label}; metadata will be applied and verified when the printer is idle.")


def ams_list(store: QueueStore, *, json_output: bool = False) -> None:
    slots = store.slots()
    if json_output:
        print(json.dumps([slot.__dict__ for slot in slots], indent=2, sort_keys=True))
        return
    print("SLOT  STATE      PRESET       TYPE       REAL     PRINTER")
    for slot in slots:
        print(
            f"{slot.label:<5} {slot.state:<10} {slot.preset_id:<12} {slot.material:<10} "
            f"#{slot.real_color}  #{slot.reported_color}"
        )


def import_live(home: Path, store: QueueStore, *, replace: bool) -> None:
    config = load_config(home)
    client = make_client(config)
    with client:
        live = [slot for slot in client.live_slots() if slot.present is True]
    existing = {slot.label for slot in store.slots()}
    for slot in live:
        if slot.label in existing and not replace:
            continue
        store.upsert_slot(
            label=slot.label, preset_id=slot.preset_id, material=slot.material,
            real_color=slot.reported_color, reported_color=slot.reported_color,
            nozzle_temp_min=slot.nozzle_temp_min, nozzle_temp_max=slot.nozzle_temp_max,
            setting_id=slot.setting_id, cali_idx=slot.cali_idx,
            state="verified",
        )
    print(f"Imported {len(live)} live AMS slots" + (" (replacing local truth)" if replace else ""))


def capture_baseline(home: Path, store: QueueStore) -> None:
    config = load_config(home)
    reference = Path(config["camera"]["reference"])
    if not reference.is_absolute():
        reference = home / reference
    client = make_client(config)
    with client:
        if not client.idle:
            raise QueueError("clean-plate baseline requires an idle printer")
        client.capture_frame(reference)
    store.set_setting("plate_verified", 1)
    print(f"Saved clean-plate camera reference: {reference}")


def release_printer(home: Path, store: QueueStore) -> None:
    if store.active_job():
        raise QueueError("cannot release the printer while a managed job is active")
    config = load_config(home)
    client = make_client(config)
    with client:
        if not client.idle:
            raise QueueError("cannot release a busy printer")
        for slot in store.slots():
            client.set_slot(
                tray_id=slot.tray_id, preset_id=slot.preset_id, material=slot.material,
                color=slot.real_color, nozzle_temp_min=slot.nozzle_temp_min,
                nozzle_temp_max=slot.nozzle_temp_max,
                setting_id=slot.setting_id, cali_idx=slot.cali_idx,
            )
            store.set_slot_state(slot.label, "verified", reported_color=slot.real_color)
    store.set_paused(True)
    print("Restored real AMS colours and paused the queue; printer is released for manual use.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=default_home(), help="queue state directory")
    parser.add_argument("--json", action="store_true", help="machine-readable output where supported")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create config and queue database")

    add = commands.add_parser("add", help="postprocess a sliced plate and add it to the queue")
    add.add_argument("file", type=Path)
    add.add_argument("--name")
    add.add_argument("--copies", type=int, default=1, help="total number of queued copies")
    add.add_argument("--strict", action="store_true", help="disable auto-refill for this job")
    add.add_argument("--map", action="append", default=[], metavar="N=A1")

    listing = commands.add_parser("list", help="show the queue")
    listing.add_argument("--all", action="store_true")
    commands.add_parser("pause", help="finish current job but do not dispatch another")
    commands.add_parser("resume", help="resume queue dispatch")
    commands.add_parser("unblock", help="clear a reviewed failure block; does not resume")
    move = commands.add_parser("move", help="move a queued job to a 1-based position")
    move.add_argument("job", type=int)
    move.add_argument("position", type=int)
    delete = commands.add_parser("delete", help="delete a queued job")
    delete.add_argument("job", type=int)
    repeat = commands.add_parser("repeat", help="append N more copies of a job")
    repeat.add_argument("job", type=int)
    repeat.add_argument("count", type=int)
    events = commands.add_parser("events", help="show recent daemon/queue events")
    events.add_argument("--limit", type=int, default=30)

    daemon = commands.add_parser("run", help="run queue manager")
    daemon.add_argument("--once", action="store_true")

    ams = commands.add_parser("ams", help="manage physical AMS inventory")
    ams_commands = ams.add_subparsers(dest="ams_command", required=True)
    ams_commands.add_parser("list")
    load = ams_commands.add_parser("load", help="declare newly loaded non-RFID filament")
    load.add_argument("slot")
    load.add_argument("--preset", required=True, help="exact tray_info_idx/preset ID")
    load.add_argument("--material", required=True)
    load.add_argument("--color", required=True)
    load.add_argument("--reported-color", help="stale color currently reported by printer")
    load.add_argument("--temp-min", type=int, required=True)
    load.add_argument("--temp-max", type=int, required=True)
    load.add_argument("--setting-id", help="printer setting_id; inherited from an exact peer when omitted")
    load.add_argument("--cali-idx", type=int, help="K-profile index; inherited from an exact peer when omitted")
    clear = ams_commands.add_parser("clear")
    clear.add_argument("slot")
    live = ams_commands.add_parser("import-live", help="seed inventory from idle printer")
    live.add_argument("--replace", action="store_true")

    camera = commands.add_parser("camera", help="camera verification setup")
    camera_commands = camera.add_subparsers(dest="camera_command", required=True)
    camera_commands.add_parser("baseline", help="capture an empty clean plate reference")
    commands.add_parser("release-printer", help="restore real AMS colors and pause managed dispatch")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    if args.command == "init":
        initialize(home)
        print(f"Initialized queue at {home}")
        return 0
    if args.command == "run":
        run_daemon(home, once=args.once)
        return 0
    load_config(home)
    store = QueueStore(home / "queue.db")
    try:
        if args.command == "add":
            if args.copies < 1:
                raise QueueError("--copies must be at least one")
            ids = add_to_queue(
                store, home, args.file, name=args.name, strict=args.strict,
                preferred=parse_mappings(args.map), copies=args.copies,
            )
            print("Queued job IDs: " + ", ".join(map(str, ids)))
        elif args.command == "list":
            print_jobs(store, all_jobs=args.all, json_output=args.json)
        elif args.command == "pause":
            store.set_paused(True)
            print("Queue paused; an active print will finish and be verified.")
        elif args.command == "resume":
            if store.blocked:
                raise QueueError("queue is blocked; review the failure and run unblock first")
            store.set_paused(False)
            print("Queue resumed.")
        elif args.command == "unblock":
            store.set_blocked(False, "")
            print("Failure block cleared; queue remains paused.")
        elif args.command == "move":
            store.move(args.job, args.position)
        elif args.command == "delete":
            store.delete(args.job)
        elif args.command == "repeat":
            print("Queued job IDs: " + ", ".join(map(str, store.repeat(args.job, args.count))))
        elif args.command == "events":
            rows = store.recent_events(args.limit)
            if args.json:
                print(json.dumps([dict(row) for row in rows], indent=2, sort_keys=True))
            else:
                for row in reversed(rows):
                    suffix = f" job={row['job_id']}" if row["job_id"] else ""
                    print(f"{row['created_at']} {row['kind']}{suffix}: {row['message']}")
        elif args.command == "ams":
            if args.ams_command == "list":
                ams_list(store, json_output=args.json)
            elif args.ams_command == "load":
                ams_load(store, args)
            elif args.ams_command == "clear":
                store.clear_slot(args.slot)
            elif args.ams_command == "import-live":
                import_live(home, store, replace=args.replace)
        elif args.command == "camera" and args.camera_command == "baseline":
            capture_baseline(home, store)
        elif args.command == "release-printer":
            release_printer(home, store)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QueueError, StoreError, LeaseError, ArchiveError, PrinterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
