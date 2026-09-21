# Endoscope-Hacking
<img width="512" height="538" alt="image" src="https://github.com/user-attachments/assets/642f7275-0fe8-499d-b42d-1f93bf88bc6f" />

`camera.py` relays this WiFi endoscope family's proprietary UDP stream to a
browser-friendly MJPEG viewer. It is based on packet captures from an
`iTiMO-0877` and static analysis of iTiMO 6.4
(`com.molink.john.itimo`).

The relay provides robust video reassembly, camera-sourced orientation
stabilization, browser capture controls, battery status, and safe device
metadata without requiring third-party Python packages.

## Usage

```
python camera.py [--camera-ip 192.168.10.123] [--camera-port 8031] [--command-port 50000] [--port 8080]
```

Open `http://localhost:8080` after connecting the computer to the
endoscope's WiFi network. The relay requires Python 3.7 or newer and only
uses the standard library.

| Option | Default | Purpose |
| --- | --- | --- |
| `--camera-ip` | `192.168.10.123` | Camera IP address on its WiFi network |
| `--camera-port` | `8031` | UDP video and heartbeat port |
| `--command-port` | `50000` | UDP read-only status/control port |
| `--port` | `8080` | Local HTTP viewer port |

## Browser features

The viewer exposes these controls:

- **Otoscope mode**: circular, camera-stabilized presentation.
- **Dental mirror mode**: raw, full-frame presentation.
- **Take photo**: downloads the current viewer image as a PNG.
- **Start/stop recording**: records the current viewer mode locally and
  downloads a browser-supported WebM or MP4 file. The camera stream has no
  audio.
- **Fullscreen**: expands the local viewer without changing the camera.
- **Battery status**: shows an estimate derived from the camera's reported
  battery-voltage field.
- **Device status**: displays only safe fields such as model and firmware.

Photos and recordings are created and saved by the browser. They are not
uploaded to, or stored on, the endoscope.

The local HTTP API is intentionally small:

| Endpoint | Purpose |
| --- | --- |
| `/` | Viewer and controls |
| `/stream.mjpg` | Multipart MJPEG video stream |
| `/api/orientation` | Current camera-derived rotation angle |
| `/api/status` | Read-only battery estimate |
| `/api/device` | Whitelisted, non-sensitive device metadata |

## App behavior reproduced locally

APK analysis found that the official app renders and records decoded frames
locally:

- `MainActivity` binds photo, video, fullscreen, circular-view, playback,
  settings, rotation, zoom, and split-screen controls.
- `StreamSelf.takePhoto()`, `takeRecord()`, `startRecordThread()`, and
  `setRecordData()` handle media capture after stream decoding.
- The supplied phone captures contain no camera-side photo or video command.
  Browser-side capture is therefore the appropriate equivalent.
- `MySurfaceView1.setRotate()` and `setRotate1()` are renderer operations, not
  hardware mode commands.

The APK includes a `seekbar` and a command transport for device settings, but
the supplied evidence does not prove an LED/PWM write command or its safe value
range. The relay deliberately does not guess or expose a brightness control.

## Viewer modes

The iTiMO labels refer to local renderer behavior:

- **Otoscope mode** uses the circular view and dynamic camera orientation.
- **Dental mirror mode** uses the uncropped view without dynamic rotation.

The browser remembers this selection with `localStorage`. Switching modes does
not send a command to the endoscope.

## Video protocol

The captured camera uses:

- Camera IP address: `192.168.10.123`
- Video transport: UDP `8031`
- Video format: fragmented JPEG/MJPEG, about 17 fps
- Observed complete frame sizes: about 15-54 KiB
- Keep-alive: `999901000000000000000000000000000000000000000000`
  every approximately 0.5 seconds

Each UDP video datagram begins with a 24-byte little-endian header:

| Bytes | Meaning |
| --- | --- |
| `0-1` | Fragment type: `0x0166` first, `0x0366` middle, `0x0266` last |
| `2-3` | Frame counter |
| `4-7` | Declared total JPEG size |
| `8-11` | Reserved; observed as zero |
| `12-13` | Zero-based fragment sequence number |
| `14-15` | Fragment payload size |
| `16-19` | Packed camera accelerometer sample |
| `20-23` | Reserved; observed as zero |

The relay reassembles frames by their declared size and fragment sequence
numbers. It de-duplicates fragments, restores their order, expires incomplete
frames, and withholds malformed, truncated, oversized, or invalid JPEG data
instead of presenting corruption in the browser.

## Camera orientation stabilization

The iTiMO APK loads `mlcamera-2.5` and calls
`NativeLibs.nativeGetAccelerometer()` from `StreamSelf.doExecuteMJPEG()` before
rendering each JPEG. The value originates from the camera stream pipeline, not
from the phone sensor or Bluetooth.

For the captured iTiMO-0877:

1. Header bytes `16-19` hold three packed 10-bit accelerometer values.
2. The app converts the Y/Z values into a quadrant-corrected angle.
3. The browser applies that angle only in Otoscope mode.
4. Near-zero/indeterminate vectors preserve the last valid angle rather than
   resetting to `0` degrees.

That last rule matters at an unsigned axis wrap boundary. A valid transition
such as `z=1010 -> 50 -> 114` must remain a continuous rotation; treating one
low axis as invalid causes the temporary southeast-facing jump that this relay
previously exhibited.

Some device variants use a high-bit-marked direct milli-degree rotation value.
The decoder supports that format as well.

## UDP/50000 status protocol

The camera uses a 24-byte little-endian command header:

| Field | Type |
| --- | --- |
| Magic | `uint16`, `0x9999` |
| Command | `uint16` |
| Transaction index | `uint32` |
| Argument 1 | `uint32` |
| Payload length | `uint32` |
| Reserved/unknown | `uint64` |

The verified read-only commands are:

| Command | Wire prefix | Finding |
| --- | --- | --- |
| `0x1017` | `99991710` | Returns a battery-voltage field used for the displayed charge estimate |
| `0x1060` | `99996010` | Returns JSON camera configuration |
| `0x1002` | `99990210` | Information/version query; it is **not** a viewer-mode toggle |

The captured configuration identifies the tested unit as an `iTiMO-0877` with
an `TX816` SoC, MJPEG stream type, and MoLink Technology manufacturer.

Configuration responses can contain WiFi credentials, SSID data, and a device
MAC address. `/api/device` explicitly whitelists only non-sensitive fields:
brand, model, hardware, firmware, firmware date, manufacturer, configured
camera brightness, stream type, and WiFi channel.

The official app also sends `SETCMD` traffic and probes UDP `8030`, but the
supplied captures do not establish safe, useful write semantics for those
messages. The relay leaves them untouched.

## Verification

Run the focused regression suite with:

```
python -m unittest -v test_camera.py
```

The tests cover packet assembly, accelerometer decoding and retention,
read-only control request parsing, local controller responses, safe metadata
filtering, and HTTP endpoints. Packet replay of the supplied captures confirms
that complete JPEG frames remain valid while their camera-derived angles stay
within the expected 0-360 degree range.

## Limitations

- The relay supports the captured MJPEG protocol family; other firmware may
  use different packet layouts or command semantics.
- Battery percentage is a vendor-style voltage estimate, not a calibrated fuel
  gauge.
- The relay intentionally avoids unproven device write commands such as LED
  brightness, WiFi configuration, firmware update, shutdown, and reboot.
- Browser recording depends on `MediaRecorder` and `canvas.captureStream`
  support in the chosen browser.
