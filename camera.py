import argparse
import json
import logging
import math
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

# --- Configuration ---
CAMERA_IP = "192.168.10.123"
CAMERA_PORT = 8031
CONTROL_PORT = 50000
LOCAL_PROXY_PORT = 8080

# Keep-alive heartbeat. The vendor app sends this every ~0.5s.
PAYLOAD = bytes.fromhex("999901000000000000000000000000000000000000000000")
HEARTBEAT_INTERVAL = 0.5

# Reverse-engineered 24-byte packet header (all integers little-endian):
#   0-1   packet type: 0x0166 first, 0x0366 middle, 0x0266 last
#   2-3   frame counter (8-bit effective)
#   4-7   declared total frame size in bytes
#   8-11  unknown (always 0)
#   12-13 fragment sequence number (0-based, contiguous)
#   14-15 payload length of this packet
#   16-19 packed accelerometer sample (constant within a frame)
#   20-23 unknown (always 0)
HEADER_SIZE = 24
PKT_FIRST, PKT_MIDDLE, PKT_LAST = 0x0166, 0x0366, 0x0266
MAX_FRAME_SIZE = 2 * 1024 * 1024  # sanity cap; observed frames are 23-54 KB
FRAME_TIMEOUT = 2.0  # drop incomplete frames after this many seconds

JPEG_SOI = b'\xff\xd8'
JPEG_EOI = b'\xff\xd9'

CONTROL_MAGIC = 0x9999
CONTROL_HEADER = struct.Struct("<HHIIIQ")
CMD_GET_BATTERY = 0x1017
CMD_GET_DEVICE_INFO = 0x1060

log = logging.getLogger("endoscope")


class ControlProtocolError(ValueError):
    """Raised when a camera command packet is malformed."""


class CameraControlError(RuntimeError):
    """Raised when the camera does not complete a control transaction."""


def build_control_packet(command: int, sequence: int = 0, arg1: int = 0,
                         payload: bytes = b"", unknown: int = 0) -> bytes:
    """Build a command packet for the camera's UDP/50000 control protocol."""
    if not 0 <= command <= 0xffff:
        raise ValueError("command must fit in an unsigned 16-bit field")
    if not 0 <= sequence <= 0xffffffff:
        raise ValueError("sequence must fit in an unsigned 32-bit field")
    if not 0 <= arg1 <= 0xffffffff:
        raise ValueError("arg1 must fit in an unsigned 32-bit field")
    if not 0 <= unknown <= 0xffffffffffffffff:
        raise ValueError("unknown must fit in an unsigned 64-bit field")
    return CONTROL_HEADER.pack(
        CONTROL_MAGIC, command, sequence, arg1, len(payload), unknown
    ) + payload


def parse_control_packet(packet: bytes) -> Tuple[int, int, int, bytes]:
    """Return a control response's command, sequence, arg1, and payload."""
    if len(packet) < CONTROL_HEADER.size:
        raise ControlProtocolError("control response is shorter than its header")

    magic, command, sequence, arg1, payload_length, _ = CONTROL_HEADER.unpack_from(packet)
    if magic != CONTROL_MAGIC:
        raise ControlProtocolError("control response has an invalid magic value")
    end = CONTROL_HEADER.size + payload_length
    if len(packet) < end:
        raise ControlProtocolError("control response payload is truncated")
    return command, sequence, arg1, packet[CONTROL_HEADER.size:end]


def battery_percentage(encoded: int) -> int:
    """Convert the camera's battery-voltage field to the vendor's percentage scale."""
    millivolts = encoded & 0xffff
    if millivolts >= 4080:
        return 100

    modifiers = (
        (3750, 3750, 0.15, 50.0),
        (3520, 3530, 0.135, 20.0),
        (3450, 3450, 0.14, 10.0),
        (3390, 3390, 0.15, 1.0),
    )
    for threshold, baseline, multiplier, offset in modifiers:
        if millivolts >= threshold:
            return min(100, int((millivolts - baseline) * multiplier + offset))
    return 1


SAFE_DEVICE_INFO_FIELDS = (
    "brand",
    "model",
    "hardware",
    "firmware",
    "fw_date",
    "manufacturer",
    "cam_brightness",
    "stream_type",
    "wifi_channel",
)


def safe_device_info(config: Dict[str, object]) -> Dict[str, object]:
    """Return only non-sensitive fields from the camera configuration."""
    return {
        field: config[field]
        for field in SAFE_DEVICE_INFO_FIELDS
        if isinstance(config.get(field), (str, int, float, bool))
    }


class CameraController:
    """Read-only client for the endoscope's UDP/50000 command protocol."""

    def __init__(self, camera_ip: str, command_port: int = CONTROL_PORT,
                 timeout: float = 1.0):
        self.camera_ip = camera_ip
        self.command_port = command_port
        self.timeout = timeout
        self._lock = threading.Lock()
        self._sequence = 0

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xffffffff
        return self._sequence

    def _query(self, command: int) -> Tuple[int, bytes]:
        with self._lock:
            sequence = self._next_sequence()
            request = build_control_packet(command, sequence)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout)
                try:
                    sock.sendto(request, (self.camera_ip, self.command_port))
                    response, sender = sock.recvfrom(65535)
                except socket.timeout as exc:
                    raise CameraControlError("camera control request timed out") from exc
                except OSError as exc:
                    raise CameraControlError("camera control request failed: %s" % exc) from exc

        if sender != (self.camera_ip, self.command_port):
            raise CameraControlError("camera control response came from an unexpected address")

        try:
            response_command, _, arg1, payload = parse_control_packet(response)
        except ControlProtocolError as exc:
            raise CameraControlError(str(exc)) from exc
        if response_command != command:
            raise CameraControlError("camera control response has an unexpected command")
        return arg1, payload

    def get_battery(self) -> Dict[str, int]:
        encoded, _ = self._query(CMD_GET_BATTERY)
        return {"battery_percent": battery_percentage(encoded)}

    def get_device_info(self) -> Dict[str, object]:
        _, payload = self._query(CMD_GET_DEVICE_INFO)
        try:
            config = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CameraControlError("camera device-info response is not valid JSON") from exc
        if not isinstance(config, dict):
            raise CameraControlError("camera device-info response is not an object")
        return safe_device_info(config)


class FrameStore:
    """Thread-safe holder for the newest complete frame."""

    def __init__(self):
        self._cond = threading.Condition()
        self.frame = b''
        self.version = 0
        self.timestamp = 0.0
        self.orientation = 0.0

    def publish(self, frame, orientation: Optional[float]):
        with self._cond:
            self.frame = frame
            self.version += 1
            self.timestamp = time.monotonic()
            if orientation is not None:
                self.orientation = orientation
            self._cond.notify_all()

    def wait_new(self, last_version, timeout=5.0):
        """Block until a new frame is published; returns (frame, version)."""
        with self._cond:
            self._cond.wait_for(lambda: self.version != last_version, timeout=timeout)
            return self.frame, self.version

    def orientation_status(self):
        with self._cond:
            return {
                "degrees": self.orientation,
                "version": self.version,
            }


store = FrameStore()


def accelerometer_degrees(packed: int) -> Optional[float]:
    """Decode a camera sample, or return None when the app keeps its last angle."""
    if packed >> 31:
        return (packed & 0x3fffffff) / 1000.0

    x = (packed >> 20) & 0x3ff
    y = (packed >> 10) & 0x3ff
    z = packed & 0x3ff
    x_magnitude = 1024 - x if x >= 512 else x
    y_magnitude = 1024 - y if y >= 512 else y
    z_magnitude = 1024 - z if z >= 512 else z
    if y_magnitude < 64 and z_magnitude < 64:
        return None

    angle = math.atan2(y_magnitude, z_magnitude)
    if z > 512:
        angle = math.pi - angle
    if y > 512:
        angle = (2 * math.pi) - angle

    # StreamSelf ignores tiny angles rather than resetting MySurfaceView1.
    deadband = 0.09 if x_magnitude < 36 else 0.005
    if abs(angle) <= deadband:
        return None
    return math.degrees(angle)


class FrameAssembler:
    """Reassembles frames from fragments using the header's declared size
    and sequence numbers, so lost or reordered fragments never reach clients."""

    def __init__(self):
        self.frames = {}  # (frame counter, accelerometer) -> fragment state
        self.complete = 0
        self.dropped = 0

    def add(self, data):
        if len(data) < HEADER_SIZE:
            return
        ptype, frame_counter, declared = struct.unpack('<HHI', data[0:8])
        seq, dlen = struct.unpack('<HH', data[12:16])
        accelerometer = struct.unpack('<I', data[16:20])[0]
        frame_key = (frame_counter, accelerometer)
        payload = data[HEADER_SIZE:HEADER_SIZE + dlen]

        if ptype not in (PKT_FIRST, PKT_MIDDLE, PKT_LAST):
            return
        if not 0 < declared <= MAX_FRAME_SIZE:
            if frame_key not in self.frames:
                log.warning("bogus declared size %d; dropping frame", declared)
            self.frames.pop(frame_key, None)
            return

        entry = self.frames.get(frame_key)
        if entry is None:
            entry = self.frames[frame_key] = {
                "fragments": {}, "declared": declared, "received": 0,
                "seen": time.monotonic(), "accelerometer": accelerometer,
            }
        entry["seen"] = time.monotonic()
        if seq not in entry["fragments"]:
            entry["fragments"][seq] = payload
            entry["received"] += len(payload)

        if entry["received"] >= declared:
            del self.frames[frame_key]
            self._finish(entry)

    def _finish(self, entry):
        frame = b''.join(entry["fragments"][seq] for seq in sorted(entry["fragments"]))
        if len(frame) != entry["declared"]:
            self.dropped += 1
            log.debug("size mismatch %d != %d; dropped", len(frame), entry["declared"])
            return
        eoi = frame.find(JPEG_EOI)
        if not frame.startswith(JPEG_SOI) or eoi == -1:
            self.dropped += 1
            log.debug("missing JPEG markers; dropped")
            return
        store.publish(frame[:eoi + 2], accelerometer_degrees(entry["accelerometer"]))
        self.complete += 1
        if self.complete % 100 == 0:
            log.info("frames: %d complete, %d dropped", self.complete, self.dropped)

    def expire(self):
        now = time.monotonic()
        stale = [fid for fid, e in self.frames.items() if now - e["seen"] > FRAME_TIMEOUT]
        for fid in stale:
            del self.frames[fid]
            self.dropped += 1
            log.debug("incomplete frame timed out (lost fragment); dropped")


def receive_stream(camera_ip, camera_port, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.bind(('0.0.0.0', 0))

    assembler = FrameAssembler()
    last_ping = 0.0
    log.info("connecting to %s:%d ...", camera_ip, camera_port)

    while not stop.is_set():
        now = time.monotonic()
        if now - last_ping >= HEARTBEAT_INTERVAL:
            try:
                sock.sendto(PAYLOAD, (camera_ip, camera_port))
            except OSError as exc:
                log.warning("heartbeat failed: %s", exc)
            last_ping = now

        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            assembler.expire()
            continue
        except OSError as exc:
            log.warning("recv failed: %s", exc)
            continue

        if addr[0] == camera_ip:
            assembler.add(data)

    sock.close()
    log.info("receive thread stopped")


class CamHandler(BaseHTTPRequestHandler):
    controller = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == '/stream.mjpg':
            self._stream_mjpeg()
        elif path == '/api/orientation':
            self._send_json(200, store.orientation_status())
        elif path == '/api/status':
            self._send_control_json("battery", "get_battery")
        elif path == '/api/device':
            self._send_control_json("device information", "get_device_info")
        elif path == '/':
            body = b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Endoscope Stream</title>
<style>
html { --camera-angle: 0deg; }
body { background: #000; color: #fff; font-family: sans-serif; margin: 0; text-align: center; }
main { margin: 1rem auto; max-width: 960px; }
#viewer { align-items: center; display: flex; justify-content: center; margin: auto; max-width: 100%; overflow: hidden; }
#stream { display: block; max-height: calc(100vh - 10rem); max-width: 100%; object-fit: contain; transform-origin: center; }
body.otoscope #viewer { border-radius: 50%; height: min(90vw, calc(100vh - 10rem));
                         width: min(90vw, calc(100vh - 10rem)); }
body.otoscope #stream { height: 142%; max-height: none; max-width: none; object-fit: cover;
                         transform: rotate(var(--camera-angle)); width: 142%; }
body.dental #stream { transform: none; }
.controls { display: grid; gap: 0.5rem; justify-content: center; margin: 1rem 0 0.5rem; }
.button-row { display: flex; justify-content: center; }
button { background: #111; border: 1px solid #ef9ba4; color: #fff; cursor: pointer;
         font-size: 0.9rem; font-weight: 700; padding: 0.75rem 1.25rem; }
.button-row button:first-child { border-radius: 2rem 0 0 2rem; }
.button-row button:last-child { border-radius: 0 2rem 2rem 0; }
button.active { background: #ef9ba4; color: #111; }
#record-button.recording { background: #b42318; border-color: #f97066; }
.camera-status { color: #b7c7d8; min-height: 1.5rem; }
#status { min-height: 1.5rem; }
</style>
</head>
<body>
<main>
<div id="viewer">
  <img id="stream" src="/stream.mjpg" alt="Live endoscope stream">
</div>
<div class="controls">
  <div class="button-row mode-controls" role="group" aria-label="Viewer mode">
    <button id="otoscope-mode" type="button">Otoscope mode</button>
    <button id="dental-mode" type="button">Dental mirror mode</button>
  </div>
  <div class="button-row capture-controls" role="group" aria-label="Capture controls">
    <button id="photo-button" type="button">Take photo</button>
    <button id="record-button" type="button">Start recording</button>
    <button id="fullscreen-button" type="button">Fullscreen</button>
  </div>
</div>
<div id="battery-status" class="camera-status" aria-live="polite">Battery: loading...</div>
<div id="device-info" class="camera-status" aria-live="polite">Device information: loading...</div>
<div id="status" role="status"></div>
</main>
<script>
const otoscopeButton = document.getElementById("otoscope-mode");
const dentalButton = document.getElementById("dental-mode");
const photoButton = document.getElementById("photo-button");
const recordButton = document.getElementById("record-button");
const fullscreenButton = document.getElementById("fullscreen-button");
const status = document.getElementById("status");
const batteryStatus = document.getElementById("battery-status");
const deviceInfo = document.getElementById("device-info");
const videoElement = document.getElementById("stream");
let viewerMode = "otoscope";
let cameraAngle = 0;
let recordingSession = null;
let orientationErrorReported = false;

function setCaptureStatus(message) {
  status.textContent = message;
}

function setMode(mode) {
  const otoscope = mode === "otoscope";
  viewerMode = mode;
  document.body.classList.toggle("otoscope", otoscope);
  document.body.classList.toggle("dental", !otoscope);
  otoscopeButton.classList.toggle("active", otoscope);
  otoscopeButton.setAttribute("aria-pressed", String(otoscope));
  dentalButton.classList.toggle("active", !otoscope);
  dentalButton.setAttribute("aria-pressed", String(!otoscope));
  setCaptureStatus(otoscope
    ? "Otoscope mode: stabilized circular viewer"
    : "Dental mirror mode: full-frame viewer");
  localStorage.setItem("endoscope-viewer-mode", mode);
}
otoscopeButton.addEventListener("click", () => setMode("otoscope"));
dentalButton.addEventListener("click", () => setMode("dental"));
setMode(localStorage.getItem("endoscope-viewer-mode") || "otoscope");

function timestampForFilename() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function downloadBlob(blob, filename) {
  const link = document.createElement("a");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function drawRotatedCover(context, width, height, angle) {
  const sourceWidth = videoElement.naturalWidth;
  const sourceHeight = videoElement.naturalHeight;
  const radians = angle * Math.PI / 180;
  const cosine = Math.abs(Math.cos(radians));
  const sine = Math.abs(Math.sin(radians));
  const rotatedWidth = sourceWidth * cosine + sourceHeight * sine;
  const rotatedHeight = sourceWidth * sine + sourceHeight * cosine;
  const scale = Math.max(width / rotatedWidth, height / rotatedHeight);

  context.translate(width / 2, height / 2);
  context.rotate(radians);
  context.scale(scale, scale);
  context.drawImage(
    videoElement, -sourceWidth / 2, -sourceHeight / 2, sourceWidth, sourceHeight
  );
}

function renderCapturedView(canvas, mode = viewerMode) {
  if (!videoElement.naturalWidth || !videoElement.naturalHeight) {
    throw new Error("the camera frame is not ready");
  }

  const circular = mode === "otoscope";
  const width = circular
    ? Math.min(videoElement.naturalWidth, videoElement.naturalHeight)
    : videoElement.naturalWidth;
  const height = circular ? width : videoElement.naturalHeight;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }

  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("canvas rendering is unavailable");
  }
  context.clearRect(0, 0, width, height);
  if (circular) {
    context.save();
    context.beginPath();
    context.arc(width / 2, height / 2, width / 2, 0, 2 * Math.PI);
    context.clip();
    drawRotatedCover(context, width, height, cameraAngle);
    context.restore();
  } else {
    context.drawImage(videoElement, 0, 0, width, height);
  }
}

function takePhoto() {
  try {
    const canvas = document.createElement("canvas");
    renderCapturedView(canvas);
    canvas.toBlob((blob) => {
      if (!blob) {
        setCaptureStatus("Photo capture failed.");
        return;
      }
      downloadBlob(blob, `endoscope-photo-${timestampForFilename()}.png`);
      setCaptureStatus("Photo downloaded.");
    }, "image/png");
  } catch (error) {
    setCaptureStatus(`Photo capture failed: ${error.message}`);
  }
}

function recordingFormat() {
  const formats = [
    { mimeType: "video/webm;codecs=vp9,opus", extension: "webm" },
    { mimeType: "video/webm;codecs=vp8,opus", extension: "webm" },
    { mimeType: "video/webm", extension: "webm" },
    { mimeType: "video/mp4", extension: "mp4" },
  ];
  if (typeof MediaRecorder.isTypeSupported !== "function") {
    return { mimeType: "", extension: "webm" };
  }
  return formats.find((format) => MediaRecorder.isTypeSupported(format.mimeType))
    || { mimeType: "", extension: "webm" };
}

function updateRecordButton() {
  const recording = recordingSession !== null;
  recordButton.textContent = recording ? "Stop recording" : "Start recording";
  recordButton.classList.toggle("recording", recording);
}

function startRecording() {
  if (!window.MediaRecorder || !("captureStream" in HTMLCanvasElement.prototype)) {
    setCaptureStatus("This browser does not support video recording.");
    return;
  }

  try {
    const canvas = document.createElement("canvas");
    const mode = viewerMode;
    renderCapturedView(canvas, mode);
    const captureStream = canvas.captureStream(20);
    const format = recordingFormat();
    const recorder = format.mimeType
      ? new MediaRecorder(captureStream, { mimeType: format.mimeType })
      : new MediaRecorder(captureStream);
    const session = {
      canvas,
      captureStream,
      chunks: [],
      extension: format.extension,
      mode,
      recorder,
      renderErrorReported: false,
      frameId: null,
    };

    recorder.ondataavailable = (event) => {
      if (event.data.size) {
        session.chunks.push(event.data);
      }
    };
    recorder.onerror = () => {
      setCaptureStatus("Video recording failed.");
    };
    recorder.onstop = () => {
      if (session.frameId !== null) {
        cancelAnimationFrame(session.frameId);
      }
      session.captureStream.getTracks().forEach((track) => track.stop());
      if (session.chunks.length) {
        downloadBlob(
          new Blob(session.chunks, { type: recorder.mimeType || "video/webm" }),
          `endoscope-video-${timestampForFilename()}.${session.extension}`
        );
        setCaptureStatus("Video downloaded.");
      } else {
        setCaptureStatus("Video recording stopped without frames.");
      }
      if (recordingSession === session) {
        recordingSession = null;
      }
      updateRecordButton();
    };

    const drawFrame = () => {
      if (recordingSession !== session) {
        return;
      }
      try {
        renderCapturedView(canvas, mode);
      } catch (error) {
        if (!session.renderErrorReported) {
          setCaptureStatus(`Video rendering paused: ${error.message}`);
          session.renderErrorReported = true;
        }
      }
      session.frameId = requestAnimationFrame(drawFrame);
    };

    recordingSession = session;
    recorder.start(1000);
    drawFrame();
    updateRecordButton();
    setCaptureStatus("Recording started.");
  } catch (error) {
    setCaptureStatus(`Video recording failed: ${error.message}`);
  }
}

function toggleRecording() {
  if (recordingSession) {
    if (recordingSession.recorder.state !== "inactive") {
      recordingSession.recorder.stop();
      setCaptureStatus("Finishing video...");
    }
    return;
  }
  startRecording();
}

photoButton.addEventListener("click", takePhoto);
recordButton.addEventListener("click", toggleRecording);

async function toggleFullscreen() {
  if (!document.fullscreenElement && !document.documentElement.requestFullscreen) {
    setCaptureStatus("This browser does not support fullscreen mode.");
    return;
  }
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else {
      await document.documentElement.requestFullscreen();
    }
  } catch (error) {
    setCaptureStatus(`Fullscreen change failed: ${error.message}`);
  }
}

function updateFullscreenButton() {
  fullscreenButton.textContent = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
}

fullscreenButton.addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", updateFullscreenButton);

async function refreshOrientation() {
  try {
    const response = await fetch("/api/orientation", { cache: "no-store" });
    if (response.ok) {
      const orientation = await response.json();
      const degrees = Number(orientation.degrees);
      if (Number.isFinite(degrees)) {
        cameraAngle = degrees;
        document.documentElement.style.setProperty("--camera-angle", `${degrees}deg`);
        orientationErrorReported = false;
      }
    }
  } catch (error) {
    if (!orientationErrorReported) {
      setCaptureStatus("Orientation updates are temporarily unavailable.");
      orientationErrorReported = true;
    }
  }
  window.setTimeout(refreshOrientation, 50);
}

async function refreshCameraStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "status request failed");
    }
    batteryStatus.textContent = `Battery: ${payload.battery_percent}%`;
  } catch (error) {
    batteryStatus.textContent = "Battery: unavailable";
  }
  window.setTimeout(refreshCameraStatus, 15000);
}

async function loadDeviceInfo() {
  try {
    const response = await fetch("/api/device", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "device-info request failed");
    }
    const details = [
      payload.model,
      payload.firmware && `Firmware ${payload.firmware}`,
    ].filter(Boolean);
    if (details.length) {
      deviceInfo.textContent = details.join(" | ");
    } else {
      deviceInfo.textContent = "Device information: unavailable";
    }
  } catch (error) {
    deviceInfo.textContent = "Device information: unavailable";
  }
}

refreshOrientation();
refreshCameraStatus();
loadDeviceInfo();
</script>
</body>
</html>"""
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "Not found")

    def _send_json(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_control_json(self, label, method_name):
        if self.controller is None:
            self._send_json(503, {"error": "camera control is not configured"})
            return
        try:
            result = getattr(self.controller, method_name)()
        except CameraControlError as exc:
            log.warning("%s request failed: %s", label, exc)
            self._send_json(502, {"error": str(exc)})
            return
        self._send_json(200, result)

    def _stream_mjpeg(self):
        self.send_response(200)
        self.send_header('Content-type', 'multipart/x-mixed-replace; boundary=--jpgboundary')
        self.end_headers()
        version = -1
        while True:
            frame, version = store.wait_new(version)
            if not frame:
                continue  # timeout with no frame yet; loop to detect disconnect
            try:
                self.wfile.write(b"--jpgboundary\r\n")
                self.send_header('Content-type', 'image/jpeg')
                self.send_header('Content-length', str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                break

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)


def main():
    parser = argparse.ArgumentParser(description="Endoscope UDP-to-MJPEG relay")
    parser.add_argument('--camera-ip', default=CAMERA_IP)
    parser.add_argument('--camera-port', type=int, default=CAMERA_PORT)
    parser.add_argument('--command-port', type=int, default=CONTROL_PORT,
                        help="camera UDP control port (default 50000)")
    parser.add_argument('--port', type=int, default=LOCAL_PROXY_PORT,
                        help="local HTTP port (default 8080)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    stop = threading.Event()
    t = threading.Thread(target=receive_stream,
                         args=(args.camera_ip, args.camera_port, stop),
                         daemon=True)
    t.start()

    CamHandler.controller = CameraController(args.camera_ip, args.command_port)
    server = ThreadingHTTPServer(('0.0.0.0', args.port), CamHandler)
    log.info("stream started: http://localhost:%d", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        t.join(timeout=2.0)


if __name__ == '__main__':
    main()