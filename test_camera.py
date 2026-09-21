import json
import socket
import struct
import threading
import unittest
from urllib.request import urlopen

import camera


class ControlProtocolTest(unittest.TestCase):
    def test_builds_and_parses_a_battery_request(self):
        request = camera.build_control_packet(
            camera.CMD_GET_BATTERY, sequence=42, arg1=7
        )

        self.assertEqual(len(request), camera.CONTROL_HEADER.size)
        self.assertEqual(
            camera.parse_control_packet(request),
            (camera.CMD_GET_BATTERY, 42, 7, b""),
        )

    def test_rejects_a_truncated_control_payload(self):
        packet = camera.CONTROL_HEADER.pack(
            camera.CONTROL_MAGIC, camera.CMD_GET_BATTERY, 0, 0, 1, 0
        )

        with self.assertRaises(camera.ControlProtocolError):
            camera.parse_control_packet(packet)

    def test_decodes_captured_battery_value(self):
        self.assertEqual(camera.battery_percentage(0x05000F10), 65)
        self.assertEqual(camera.battery_percentage(4080), 100)

    def test_excludes_sensitive_device_fields(self):
        info = camera.safe_device_info({
            "model": "iTiMO-0877",
            "firmware": "1.0.0",
            "key": "wifi-password",
            "sta_key": "network-password",
            "mac": "444a44362e84",
        })

        self.assertEqual(info, {"model": "iTiMO-0877", "firmware": "1.0.0"})


class CameraControllerTest(unittest.TestCase):
    def test_reads_battery_and_safe_device_info(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.settimeout(1.0)
        commands = []
        errors = []

        def respond():
            try:
                for _ in range(2):
                    request, sender = server.recvfrom(65535)
                    command, sequence, _, _ = camera.parse_control_packet(request)
                    commands.append(command)
                    if command == camera.CMD_GET_BATTERY:
                        response = camera.build_control_packet(
                            command, sequence=sequence, arg1=0x05000F10
                        )
                    else:
                        response = camera.build_control_packet(
                            command,
                            sequence=sequence,
                            payload=json.dumps({
                                "firmware": "1.0.0",
                                "key": "wifi-password",
                                "model": "iTiMO-0877",
                            }).encode("utf-8"),
                        )
                    server.sendto(response, sender)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()

        thread = threading.Thread(target=respond)
        thread.start()
        controller = camera.CameraController(
            "127.0.0.1", command_port=server.getsockname()[1], timeout=1.0
        )
        try:
            self.assertEqual(controller.get_battery(), {"battery_percent": 65})
            self.assertEqual(
                controller.get_device_info(),
                {"firmware": "1.0.0", "model": "iTiMO-0877"},
            )
        finally:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(commands, [camera.CMD_GET_BATTERY, camera.CMD_GET_DEVICE_INFO])


class AccelerometerTest(unittest.TestCase):
    def test_decodes_captured_three_axis_sample(self):
        self.assertAlmostEqual(
            camera.accelerometer_degrees(0x3C33D3B2),
            107.73,
            places=2,
        )

    def test_decodes_direct_millidegree_sample(self):
        self.assertEqual(
            camera.accelerometer_degrees(0x8001E240),
            123.456,
        )

    def test_decodes_single_low_axis_as_a_valid_quadrant_transition(self):
        self.assertAlmostEqual(
            camera.accelerometer_degrees(0x3F7C1032),
            281.22,
            places=2,
        )

    def test_rejects_only_an_indeterminate_two_axis_sample(self):
        self.assertIsNone(camera.accelerometer_degrees(1))

    def test_rejects_the_vendor_deadband_near_zero_degrees(self):
        self.assertIsNone(camera.accelerometer_degrees(0x3FF001F4))


class FrameAssemblerTest(unittest.TestCase):
    def test_publishes_orientation_from_frame_header(self):
        frame = b"\xff\xd8test\xff\xd9"
        packed = 0x3C33D3B2
        packet = (
            struct.pack("<HHI", camera.PKT_FIRST, 1, len(frame))
            + b"\0" * 4
            + struct.pack("<HHI", 0, len(frame), packed)
            + b"\0" * 4
            + frame
        )
        version = camera.store.orientation_status()["version"]

        camera.FrameAssembler().add(packet)

        published, published_version = camera.store.wait_new(version, timeout=0)
        self.assertEqual(published, frame)
        self.assertGreater(published_version, version)
        self.assertAlmostEqual(
            camera.store.orientation_status()["degrees"],
            camera.accelerometer_degrees(packed),
        )

    def test_preserves_last_angle_for_an_ignored_sample(self):
        camera.store.publish(b"first", 123.456)
        camera.store.publish(b"second", None)

        self.assertEqual(camera.store.orientation_status()["degrees"], 123.456)


class FakeCameraController:
    def get_battery(self):
        return {"battery_percent": 65}

    def get_device_info(self):
        return {"firmware": "1.0.0", "model": "iTiMO-0877"}


class CamHandlerTest(unittest.TestCase):
    def setUp(self):
        self.original_controller = camera.CamHandler.controller
        camera.CamHandler.controller = FakeCameraController()
        self.server = camera.ThreadingHTTPServer(("127.0.0.1", 0), camera.CamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        camera.CamHandler.controller = self.original_controller

    def test_home_page_exposes_the_mode_control(self):
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")

        self.assertIn("Otoscope mode", page)
        self.assertIn("Dental mirror mode", page)
        self.assertIn("Take photo", page)
        self.assertIn("Start recording", page)
        self.assertIn("Fullscreen", page)
        self.assertIn("MediaRecorder", page)
        self.assertIn("/api/orientation", page)
        self.assertIn("/api/status", page)
        self.assertIn("/api/device", page)
        self.assertIn("--camera-angle", page)
        self.assertIn("localStorage", page)
        self.assertNotIn("/api/mode/toggle", page)

    def test_orientation_endpoint_returns_current_angle(self):
        camera.store.publish(b"frame", 123.456)

        with urlopen(self.base_url + "/api/orientation") as response:
            payload = json.loads(response.read())

        self.assertEqual(payload["degrees"], 123.456)
        self.assertIn("version", payload)

    def test_status_endpoints_return_controller_data(self):
        with urlopen(self.base_url + "/api/status") as response:
            battery = json.loads(response.read())
        with urlopen(self.base_url + "/api/device") as response:
            device = json.loads(response.read())

        self.assertEqual(battery, {"battery_percent": 65})
        self.assertEqual(device, {"firmware": "1.0.0", "model": "iTiMO-0877"})


if __name__ == "__main__":
    unittest.main()
