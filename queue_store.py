"""SQLite persistence for the local print queue and physical AMS inventory."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from color_lease import FilamentRequirement, Slot, normalize_color, slot_label_to_id


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreError(ValueError):
    pass


class QueueStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                processed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                position INTEGER NOT NULL,
                strict INTEGER NOT NULL DEFAULT 0,
                requirements_json TEXT NOT NULL,
                preferred_json TEXT NOT NULL DEFAULT '{}',
                lease_json TEXT,
                clear_height REAL NOT NULL,
                parent_id INTEGER,
                remote_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY(parent_id) REFERENCES jobs(id)
            );
            CREATE INDEX IF NOT EXISTS jobs_queue_order ON jobs(status, position, id);
            CREATE TABLE IF NOT EXISTS ams_slots (
                label TEXT PRIMARY KEY,
                tray_id INTEGER NOT NULL UNIQUE,
                preset_id TEXT NOT NULL,
                material TEXT NOT NULL,
                real_color TEXT NOT NULL,
                reported_color TEXT NOT NULL,
                nozzle_temp_min INTEGER NOT NULL DEFAULT 0,
                nozzle_temp_max INTEGER NOT NULL DEFAULT 0,
                setting_id TEXT NOT NULL DEFAULT '',
                cali_idx INTEGER NOT NULL DEFAULT -1,
                state TEXT NOT NULL DEFAULT 'verified',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                job_id INTEGER,
                message TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        slot_columns = {row[1] for row in self.db.execute("PRAGMA table_info(ams_slots)")}
        if "setting_id" not in slot_columns:
            self.db.execute("ALTER TABLE ams_slots ADD COLUMN setting_id TEXT NOT NULL DEFAULT ''")
        if "cali_idx" not in slot_columns:
            self.db.execute("ALTER TABLE ams_slots ADD COLUMN cali_idx INTEGER NOT NULL DEFAULT -1")
        self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('paused','0')")
        self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('blocked','0')")
        self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('plate_verified','0')")
        self.db.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: object) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    @property
    def paused(self) -> bool:
        return self.setting("paused", "0") == "1"

    @property
    def blocked(self) -> bool:
        return self.setting("blocked", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_setting("paused", int(paused))

    def set_blocked(self, blocked: bool, reason: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings(key,value) VALUES('blocked',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(int(blocked)),),
            )
            self.db.execute(
                "INSERT INTO settings(key,value) VALUES('block_reason',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (reason,),
            )

    def log(self, kind: str, message: str, *, job_id: int | None = None, data: object = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events(created_at,kind,job_id,message,data_json) VALUES(?,?,?,?,?)",
                (now(), kind, job_id, message, json.dumps(data or {}, sort_keys=True)),
            )

    def add_job(
        self,
        *,
        name: str,
        source_path: Path,
        processed_path: Path,
        requirements: list[FilamentRequirement],
        clear_height: float,
        strict: bool = False,
        preferred: dict[int, str] | None = None,
        parent_id: int | None = None,
    ) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(position),0)+10 AS p FROM jobs WHERE status='queued'").fetchone()
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO jobs(
                    name,source_path,processed_path,status,position,strict,
                    requirements_json,preferred_json,clear_height,parent_id,created_at
                ) VALUES(?,?,?,'queued',?,?,?,?,?,?,?)""",
                (
                    name,
                    str(source_path),
                    str(processed_path),
                    int(row["p"]),
                    int(strict),
                    json.dumps([asdict(item) for item in requirements], sort_keys=True),
                    json.dumps(preferred or {}, sort_keys=True),
                    float(clear_height),
                    parent_id,
                    now(),
                ),
            )
        job_id = int(cursor.lastrowid)
        self.log("queue", f"queued {name}", job_id=job_id)
        return job_id

    def jobs(self, *, include_finished: bool = False) -> list[sqlite3.Row]:
        if include_finished:
            return list(self.db.execute("SELECT * FROM jobs ORDER BY id"))
        return list(
            self.db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','verifying','failed') "
                "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'verifying' THEN 1 ELSE 2 END, position, id"
            )
        )

    def job(self, job_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise StoreError(f"no such job: {job_id}")
        return row

    def next_job(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY position,id LIMIT 1"
        ).fetchone()

    def active_job(self) -> sqlite3.Row | None:
        rows = list(self.db.execute("SELECT * FROM jobs WHERE status IN ('running','verifying') ORDER BY id"))
        if len(rows) > 1:
            raise StoreError("database contains multiple active jobs")
        return rows[0] if rows else None

    def requirements(self, job: sqlite3.Row) -> list[FilamentRequirement]:
        return [FilamentRequirement(**item) for item in json.loads(job["requirements_json"])]

    def preferred(self, job: sqlite3.Row) -> dict[int, str]:
        return {int(key): str(value) for key, value in json.loads(job["preferred_json"]).items()}

    def mark_running(self, job_id: int, lease: dict[str, object], remote_path: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='running',lease_json=?,remote_path=?,started_at=?,error=NULL WHERE id=?",
                (json.dumps(lease, sort_keys=True), remote_path, now(), job_id),
            )
        self.log("dispatch", "print accepted by printer", job_id=job_id, data=lease)

    def mark_verifying(self, job_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='verifying' WHERE id=?", (job_id,))

    def mark_completed(self, job_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='completed',finished_at=?,error=NULL WHERE id=?",
                (now(), job_id),
            )
        self.log("complete", "plate cleared and job completed", job_id=job_id)

    def mark_failed(self, job_id: int, message: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='failed',finished_at=?,error=? WHERE id=?",
                (now(), message, job_id),
            )
        self.log("failure", message, job_id=job_id)

    def move(self, job_id: int, position: int) -> None:
        queued = list(self.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY position,id"))
        ids = [int(row["id"]) for row in queued]
        if job_id not in ids:
            raise StoreError(f"job {job_id} is not queued")
        ids.remove(job_id)
        index = max(0, min(position - 1, len(ids)))
        ids.insert(index, job_id)
        with self.db:
            for order, queued_id in enumerate(ids, 1):
                self.db.execute("UPDATE jobs SET position=? WHERE id=?", (order * 10, queued_id))
        self.log("queue", f"moved to queue position {index + 1}", job_id=job_id)

    def delete(self, job_id: int) -> None:
        job = self.job(job_id)
        if job["status"] != "queued":
            raise StoreError("only queued jobs can be deleted")
        with self.db:
            self.db.execute("UPDATE jobs SET status='deleted',finished_at=? WHERE id=?", (now(), job_id))
        self.log("queue", "deleted from queue", job_id=job_id)

    def repeat(self, job_id: int, copies: int) -> list[int]:
        if copies < 1:
            raise StoreError("repeat count must be at least one")
        source = self.job(job_id)
        reqs = self.requirements(source)
        preferred = self.preferred(source)
        result = []
        root_parent = int(source["parent_id"] or source["id"])
        for _ in range(copies):
            result.append(
                self.add_job(
                    name=source["name"],
                    source_path=Path(source["source_path"]),
                    processed_path=Path(source["processed_path"]),
                    requirements=reqs,
                    clear_height=float(source["clear_height"]),
                    strict=bool(source["strict"]),
                    preferred=preferred,
                    parent_id=root_parent,
                )
            )
        return result

    def upsert_slot(
        self,
        *,
        label: str,
        preset_id: str,
        material: str,
        real_color: str,
        reported_color: str | None = None,
        nozzle_temp_min: int = 0,
        nozzle_temp_max: int = 0,
        setting_id: str = "",
        cali_idx: int = -1,
        state: str = "verified",
    ) -> None:
        label = label.upper()
        tray_id = slot_label_to_id(label)
        real = normalize_color(real_color)
        reported = normalize_color(reported_color or real)
        with self.db:
            self.db.execute(
                """INSERT INTO ams_slots(
                    label,tray_id,preset_id,material,real_color,reported_color,
                    nozzle_temp_min,nozzle_temp_max,setting_id,cali_idx,state,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(label) DO UPDATE SET
                    tray_id=excluded.tray_id,preset_id=excluded.preset_id,
                    material=excluded.material,real_color=excluded.real_color,
                    reported_color=excluded.reported_color,
                    nozzle_temp_min=excluded.nozzle_temp_min,
                    nozzle_temp_max=excluded.nozzle_temp_max,
                    setting_id=excluded.setting_id,cali_idx=excluded.cali_idx,
                    state=excluded.state,updated_at=excluded.updated_at""",
                (
                    label, tray_id, preset_id, material, real, reported,
                    int(nozzle_temp_min), int(nozzle_temp_max), setting_id, int(cali_idx), state, now(),
                ),
            )
        self.log("ams", f"{label} declared as {preset_id} #{real}")

    def slots(self) -> list[Slot]:
        return [
            Slot(
                label=row["label"], tray_id=row["tray_id"], preset_id=row["preset_id"],
                material=row["material"], real_color=row["real_color"],
                reported_color=row["reported_color"], nozzle_temp_min=row["nozzle_temp_min"],
                nozzle_temp_max=row["nozzle_temp_max"], state=row["state"],
                setting_id=row["setting_id"], cali_idx=row["cali_idx"],
            )
            for row in self.db.execute("SELECT * FROM ams_slots ORDER BY tray_id")
        ]

    def update_reported_color(self, label: str, color: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE ams_slots SET reported_color=?,updated_at=? WHERE label=?",
                (normalize_color(color), now(), label.upper()),
            )

    def set_slot_state(self, label: str, state: str, *, reported_color: str | None = None) -> None:
        with self.db:
            if reported_color is None:
                self.db.execute(
                    "UPDATE ams_slots SET state=?,updated_at=? WHERE label=?",
                    (state, now(), label.upper()),
                )
            else:
                self.db.execute(
                    "UPDATE ams_slots SET state=?,reported_color=?,updated_at=? WHERE label=?",
                    (state, normalize_color(reported_color), now(), label.upper()),
                )

    def clear_slot(self, label: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM ams_slots WHERE label=?", (label.upper(),))
        self.log("ams", f"cleared inventory declaration for {label.upper()}")

    def recent_events(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))
