"""Small LAN-only Bambu client used by the queue daemon.

MQTT support is imported lazily from paho-mqtt so all archive, queue and test
commands remain usable without network dependencies.  The printer protocol is
not a public Bambu API; every mutating operation is followed by live-state
verification and callers are expected to fail closed.
"""

from __future__ import annotations

import copy
import ftplib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote

from color_lease import Slot, normalize_color, slot_id_to_label


class PrinterError(RuntimeError):
    pass


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS variant for the printer's implicit-TLS port 990."""

    def connect(self, host: str = "", port: int = 0, timeout: float = -999, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout, source_address=source_address
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd: str, rest=None):
        """Open a protected data channel reusing the control TLS session.

        The printer's vsFTPd enables ``require_ssl_reuse``.  Python's stock
        FTP_TLS opens a new TLS session for PASV data sockets, which the
        printer rejects with status 522 even though login succeeded.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            kwargs = {"server_hostname": self.host}
            session = getattr(self.sock, "session", None)
            if session is not None:
                kwargs["session"] = session
            conn = self.context.wrap_socket(conn, **kwargs)
        return conn, size


class BambuClient:
    def __init__(
        self,
        *,
        host: str,
        serial: str,
        access_code: str,
        mqtt_port: int = 8883,
        ftps_port: int = 990,
        timeout: float = 15.0,
    ):
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.mqtt_port = mqtt_port
        self.ftps_port = ftps_port
        self.timeout = timeout
        self._mqtt = None
        self._connected = threading.Event()
        self._changed = threading.Condition()
        self._state: dict[str, Any] = {}
        self._sequence = 10000

    def connect(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise PrinterError(
                "network operation requires paho-mqtt; install requirements.txt in the service environment"
            ) from exc
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="print-looper")
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id="print-looper")
        client.username_pw_set("bblp", self.access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.host, self.mqtt_port, keepalive=30)
        client.loop_start()
        self._mqtt = client
        if not self._connected.wait(self.timeout):
            self.close()
            raise PrinterError(f"timed out connecting to printer MQTT at {self.host}")
        self.request_status()
        self.wait_for(lambda state: bool(state.get("print")), "initial printer status")

    def close(self) -> None:
        if self._mqtt is not None:
            try:
                self._mqtt.disconnect()
                self._mqtt.loop_stop()
            finally:
                self._mqtt = None
                self._connected.clear()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_args):
        self.close()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        # paho-mqtt 2.2 passes a ReasonCode object whose numeric value is
        # available through ``value`` but which no longer implements int().
        # Older callback APIs still pass an integer, so accept both forms.
        value = getattr(reason_code, "value", reason_code)
        if int(value) != 0:
            return
        client.subscribe(f"device/{self.serial}/report", qos=1)
        self._connected.set()

    def _on_message(self, _client, _userdata, message):
        try:
            payload = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        with self._changed:
            _deep_merge(self._state, payload)
            self._changed.notify_all()

    def _next_sequence(self) -> str:
        self._sequence += 1
        return str(self._sequence)

    def publish(self, payload: dict[str, Any]) -> str:
        if self._mqtt is None:
            raise PrinterError("printer client is not connected")
        section = payload.get("print") or payload.get("pushing")
        if isinstance(section, dict) and "sequence_id" not in section:
            section["sequence_id"] = self._next_sequence()
        result = self._mqtt.publish(
            f"device/{self.serial}/request", json.dumps(payload, separators=(",", ":")), qos=1
        )
        if result.rc != 0:
            raise PrinterError(f"MQTT publish failed with result {result.rc}")
        return str(section.get("sequence_id", "")) if isinstance(section, dict) else ""

    def request_status(self) -> None:
        self.publish({"pushing": {"command": "pushall"}})

    @property
    def state(self) -> dict[str, Any]:
        with self._changed:
            return copy.deepcopy(self._state)

    def wait_for(self, predicate, description: str, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.timeout)
        with self._changed:
            while True:
                state = copy.deepcopy(self._state)
                if predicate(state):
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PrinterError(f"timed out waiting for {description}")
                self._changed.wait(min(remaining, 1.0))

    @property
    def gcode_state(self) -> str:
        return str(self.state.get("print", {}).get("gcode_state", "UNKNOWN")).upper()

    @property
    def idle(self) -> bool:
        return self.gcode_state in {"IDLE", "FINISH"}

    @staticmethod
    def _auto_refill_from_state(state: dict[str, Any]) -> bool | None:
        value = state.get("print", {}).get("cfg")
        if value is None:
            return None
        try:
            number = int(str(value), 16)
        except (TypeError, ValueError):
            return None
        return bool(number & (1 << 18))

    @property
    def auto_refill_enabled(self) -> bool | None:
        return self._auto_refill_from_state(self.state)

    def live_slots(self) -> list[Slot]:
        result: list[Slot] = []
        ams_root = self.state.get("print", {}).get("ams", {})
        presence_raw = ams_root.get("tray_exist_bits")
        try:
            presence_bits = (
                int(presence_raw, 16) if isinstance(presence_raw, str) else int(presence_raw)
            )
        except (TypeError, ValueError):
            presence_bits = None
        if presence_bits == 0 and ams_root.get("power_on_flag") is False:
            # Firmware also emits this shape while the AMS is powering down;
            # it is not authoritative evidence that every tray was removed.
            presence_bits = None
        for ams in ams_root.get("ams", []) or []:
            try:
                ams_id = int(ams.get("id", 0))
            except (TypeError, ValueError):
                continue
            for tray in ams.get("tray", []) or []:
                try:
                    local_id = int(tray.get("id", 0))
                    tray_id = ams_id * 4 + local_id
                    color = normalize_color(str(tray.get("tray_color") or "000000"))
                    cali_value = tray.get("cali_idx")
                except (TypeError, ValueError):
                    continue
                preset = str(tray.get("tray_info_idx") or "")
                material = str(tray.get("tray_type") or "")
                if not preset or not material:
                    continue
                result.append(
                    Slot(
                        label=slot_id_to_label(tray_id), tray_id=tray_id,
                        preset_id=preset, material=material,
                        real_color=color, reported_color=color,
                        nozzle_temp_min=int(tray.get("nozzle_temp_min") or 0),
                        nozzle_temp_max=int(tray.get("nozzle_temp_max") or 0),
                        setting_id=str(tray.get("setting_id") or ""),
                        cali_idx=int(cali_value) if cali_value not in (None, "") else -1,
                        present=(bool(presence_bits & (1 << tray_id)) if presence_bits is not None else None),
                    )
                )
        return sorted(result, key=lambda slot: slot.tray_id)

    def set_slot(
        self,
        *,
        tray_id: int,
        preset_id: str,
        material: str,
        color: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        setting_id: str = "",
        cali_idx: int = -1,
        select_calibration: bool = True,
    ) -> None:
        if not self.idle:
            raise PrinterError("refusing to change AMS metadata while printer is not idle")
        color = normalize_color(color)
        if not 150 <= nozzle_temp_min <= nozzle_temp_max <= 350:
            raise PrinterError(
                f"invalid nozzle temperature bounds {nozzle_temp_min}-{nozzle_temp_max} for tray {tray_id}"
            )
        ams_id, slot_id = divmod(tray_id, 4)
        self.publish(
            {
                "print": {
                    "command": "ams_filament_setting", "ams_id": ams_id,
                    "slot_id": slot_id, "tray_id": slot_id,
                    "tray_info_idx": preset_id, "setting_id": setting_id,
                    "tray_type": material, "tray_color": color + "FF",
                    "nozzle_temp_min": int(nozzle_temp_min),
                    "nozzle_temp_max": int(nozzle_temp_max),
                }
            }
        )
        self.request_status()

        def matches(_state: dict[str, Any]) -> bool:
            return any(
                slot.tray_id == tray_id
                and slot.preset_id == preset_id
                and slot.material == material
                and slot.reported_color == color
                and (not setting_id or slot.setting_id == setting_id)
                and slot.nozzle_temp_min == int(nozzle_temp_min)
                and slot.nozzle_temp_max == int(nozzle_temp_max)
                for slot in self.live_slots()
            )

        self.wait_for(matches, f"AMS tray {tray_id} metadata readback")
        if select_calibration:
            self.select_calibration(tray_id, cali_idx)

    def select_calibration(self, tray_id: int, cali_idx: int) -> None:
        ams_id, slot_id = divmod(tray_id, 4)
        self.publish(
            {"print": {"command": "extrusion_cali_sel", "ams_id": ams_id,
                       "tray_id": slot_id, "slot_id": slot_id, "cali_idx": int(cali_idx)}}
        )
        self.request_status()
        self.wait_for(
            lambda _state: any(
                slot.tray_id == tray_id and slot.cali_idx == int(cali_idx)
                for slot in self.live_slots()
            ),
            f"AMS tray {tray_id} calibration profile readback",
        )

    def set_auto_refill(self, enabled: bool) -> None:
        self.publish({"print": {"command": "print_option", "auto_switch_filament": bool(enabled)}})
        self.request_status()
        self.wait_for(
            lambda state: self._auto_refill_from_state(state) is bool(enabled),
            f"AMS filament backup to report {'on' if enabled else 'off'}",
        )

    def send_gcode(self, lines: str) -> None:
        if not lines.endswith("\n"):
            lines += "\n"
        self.publish({"print": {"command": "gcode_line", "param": lines}})

    def upload(self, local_path: Path, remote_path: str) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        ftp = ImplicitFTP_TLS(context=context, timeout=self.timeout)
        try:
            ftp.connect(self.host, self.ftps_port)
            ftp.login("bblp", self.access_code)
            ftp.prot_p()
            parent, name = remote_path.rsplit("/", 1)
            if parent:
                current = ""
                for component in parent.strip("/").split("/"):
                    current += "/" + component
                    try:
                        ftp.mkd(current)
                    except ftplib.error_perm as exc:
                        if not str(exc).startswith("550"):
                            raise
            with local_path.open("rb") as source:
                ftp.storbinary(f"STOR {remote_path}", source, blocksize=1024 * 256)
        except (OSError, ftplib.Error) as exc:
            raise PrinterError(f"FTPS upload failed: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def start_print(
        self,
        *,
        remote_path: str,
        ams_mapping: list[int],
        name: str,
        gcode_member: str = "Metadata/plate_1.gcode",
        md5: str = "",
        bed_type: str = "auto",
        bed_leveling: bool = True,
        flow_cali: bool = False,
        timelapse: bool = True,
        vibration_cali: bool = False,
        layer_inspect: bool = True,
    ) -> None:
        mapping2 = [
            {"ams_id": tray // 4, "slot_id": tray % 4} if tray >= 0
            else {"ams_id": 255, "slot_id": 255}
            for tray in ams_mapping
        ]
        payload = {
            "print": {
                "command": "project_file", "file": remote_path,
                "url": "ftp:///" + remote_path.lstrip("/"),
                "subtask_name": name, "bed_type": bed_type,
                "use_ams": True, "ams_mapping": ams_mapping, "ams_mapping2": mapping2,
                "bed_leveling": bed_leveling, "flow_cali": flow_cali,
                "timelapse": timelapse, "layer_inspect": layer_inspect,
                "vibration_cali": vibration_cali, "param": gcode_member, "md5": md5,
                "auto_bed_leveling": 1 if bed_leveling else 0,
                "cfg": "4" if timelapse else "0",
                "extrude_cali_flag": 1 if flow_cali else 0,
                "extrude_cali_manual_mode": 0,
                "nozzle_offset_cali": 2,
                "profile_id": "0", "project_id": "0", "task_id": "0", "subtask_id": "0",
            }
        }
        self.publish(payload)
        self.wait_for(
            lambda state: str(state.get("print", {}).get("gcode_state", "")).upper()
            in {"RUNNING", "PREPARE"},
            "printer to accept queued job",
            timeout=max(self.timeout, 30),
        )

    def capture_frame(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        password = quote(self.access_code, safe="")
        url = f"rtsps://bblp:{password}@{self.host}:322/streaming/live/1"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1", str(destination),
        ]
        try:
            subprocess.run(command, check=True, timeout=30, capture_output=True)
        except FileNotFoundError as exc:
            raise PrinterError("camera verification requires ffmpeg") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise PrinterError(f"camera snapshot failed: {str(detail).strip() or exc}") from exc
