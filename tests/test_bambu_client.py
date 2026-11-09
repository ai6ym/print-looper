from __future__ import annotations

import ftplib
import unittest
from unittest import mock

from bambu_client import BambuClient, ImplicitFTP_TLS


class BambuClientTelemetryTests(unittest.TestCase):
    def test_connect_accepts_paho_v2_reason_code_object(self):
        class ReasonCode:
            value = 0

        class Mqtt:
            def __init__(self):
                self.subscriptions = []

            def subscribe(self, topic, qos):
                self.subscriptions.append((topic, qos))

        client = BambuClient(host="printer", serial="serial", access_code="code")
        mqtt = Mqtt()
        client._on_connect(mqtt, None, None, ReasonCode())
        self.assertTrue(client._connected.is_set())
        self.assertEqual(mqtt.subscriptions, [("device/serial/report", 1)])

    def test_implicit_ftps_reuses_control_tls_session_for_data_socket(self):
        class ControlSocket:
            session = object()

        class Context:
            def __init__(self):
                self.calls = []

            def wrap_socket(self, conn, **kwargs):
                self.calls.append((conn, kwargs))
                return "wrapped"

        ftp = ImplicitFTP_TLS(context=Context())
        ftp.host = "printer"
        ftp.sock = ControlSocket()
        ftp._prot_p = True
        with mock.patch.object(ftplib.FTP, "ntransfercmd", return_value=("plain", 12)):
            conn, size = ftp.ntransfercmd("LIST")
        self.assertEqual((conn, size), ("wrapped", 12))
        self.assertEqual(
            ftp.context.calls,
            [("plain", {"server_hostname": "printer", "session": ftp.sock.session})],
        )

    def test_tray_exist_bits_are_authoritative_even_with_stale_metadata(self):
        client = BambuClient(host="printer", serial="serial", access_code="code")
        trays = [
            {
                "id": str(index), "tray_info_idx": "GFG02", "tray_type": "PETG",
                "tray_color": "000001FF", "nozzle_temp_min": "220",
                "nozzle_temp_max": "260", "cali_idx": "0",
            }
            for index in range(4)
        ]
        client._state = {
            "print": {"ams": {"ams": [{"id": "0", "tray": trays}], "tray_exist_bits": "6"}}
        }
        slots = client.live_slots()
        self.assertEqual([slot.present for slot in slots], [False, True, True, False])
        self.assertEqual(slots[0].reported_color, "000001")
        self.assertEqual(slots[1].cali_idx, 0)

    def test_shutdown_zero_mask_is_not_treated_as_all_empty(self):
        client = BambuClient(host="printer", serial="serial", access_code="code")
        client._state = {
            "print": {
                "ams": {
                    "ams": [{"id": "0", "tray": [{
                        "id": "0", "tray_info_idx": "GFG02", "tray_type": "PETG",
                        "tray_color": "000001FF",
                    }]}],
                    "tray_exist_bits": "0", "power_on_flag": False,
                }
            }
        }
        self.assertIsNone(client.live_slots()[0].present)


if __name__ == "__main__":
    unittest.main()
