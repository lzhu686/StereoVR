#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Camera Performance Monitor - StereoVR Tool

Tests stereo camera capabilities via OpenCV UVC: resolution switching,
FPS measurement, FOV display, and live stereo preview with WebSocket streaming.

Supports ZED Mini, ZED 2i, and generic USB stereo cameras.

Usage:
    python tools/camera_monitor.py              # auto-detect
    python tools/camera_monitor.py --device 0   # specify device

Author: Liang ZHU
"""

import asyncio
import websockets
import json
import cv2
import numpy as np
import base64
import time
import logging
import threading
import ssl
import os
import socket
import subprocess
import argparse
from typing import Optional, Set

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== Configuration ====================
MONITOR_WSS_PORT = 8767
MONITOR_HTTPS_PORT = 8447
JPEG_QUALITY = 95

# Known camera specs (USB VID:PID -> camera info)
# ZED cameras output side-by-side stereo via UVC
KNOWN_CAMERAS = {
    "ZED Mini": {
        "baseline_mm": 63.0,
        "sensor": '1/3" 4MP CMOS',
        "fov": {  # Per resolution, from Stereolabs datasheet
            "VGA":    {"h_fov": 85.0, "v_fov": 54.0, "d_fov": 95.0},
            "HD720":  {"h_fov": 85.0, "v_fov": 54.0, "d_fov": 95.0},
            "HD1080": {"h_fov": 80.0, "v_fov": 50.0, "d_fov": 90.0},
            "HD2K":   {"h_fov": 78.0, "v_fov": 48.0, "d_fov": 88.0},
        },
    },
    "ZED 2i (2.1mm)": {
        "baseline_mm": 120.0,
        "sensor": '1/3" 4MP CMOS',
        "fov": {
            "VGA":    {"h_fov": 100.0, "v_fov": 64.0, "d_fov": 110.0},
            "HD720":  {"h_fov": 100.0, "v_fov": 64.0, "d_fov": 110.0},
            "HD1080": {"h_fov": 95.0,  "v_fov": 60.0, "d_fov": 105.0},
            "HD2K":   {"h_fov": 90.0,  "v_fov": 56.0, "d_fov": 100.0},
        },
    },
    "ZED 2i (4mm)": {
        "baseline_mm": 120.0,
        "sensor": '1/3" 4MP CMOS',
        "fov": {
            "VGA":    {"h_fov": 65.0, "v_fov": 40.0, "d_fov": 73.0},
            "HD720":  {"h_fov": 65.0, "v_fov": 40.0, "d_fov": 73.0},
            "HD1080": {"h_fov": 63.0, "v_fov": 38.0, "d_fov": 71.0},
            "HD2K":   {"h_fov": 61.0, "v_fov": 37.0, "d_fov": 69.0},
        },
    },
    "Generic Stereo": {
        "baseline_mm": 60.0,
        "sensor": "Unknown",
        "fov": {
            "VGA":    {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "HD720":  {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "HD1080": {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "HD2K":   {"h_fov": 0, "v_fov": 0, "d_fov": 0},
        },
    },
}

# Stereo side-by-side resolutions (total width x height)
STEREO_MODES = {
    "VGA":    {"width": 1344, "height": 376,  "fps_options": [100, 60, 30, 15]},
    "HD720":  {"width": 2560, "height": 720,  "fps_options": [60, 30, 15]},
    "HD1080": {"width": 3840, "height": 1080, "fps_options": [30, 15]},
    "HD2K":   {"width": 4416, "height": 1242, "fps_options": [15]},
}
# =======================================================


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def auto_detect_device_index():
    """Find the /dev/video* index for a ZED camera by reading sysfs device names.
    Also checks udev symlink /dev/stereo_camera first."""
    import glob
    # Check udev symlink first
    if os.path.exists("/dev/stereo_camera"):
        real = os.path.realpath("/dev/stereo_camera")
        try:
            idx = int(real.replace("/dev/video", ""))
            logger.info(f"Found udev symlink /dev/stereo_camera → /dev/video{idx}")
            return idx
        except ValueError:
            pass
    # Fallback: scan sysfs
    for path in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if 'zed' in name:
                idx = int(path.split("video4linux/video")[1].split("/")[0])
                logger.info(f"Auto-detected ZED camera at /dev/video{idx} ({name})")
                return idx
        except Exception:
            continue
    logger.info("No ZED device found in sysfs, falling back to /dev/video0")
    return 0


def detect_camera_model(device_index=0):
    """Detect ZED camera model from USB device info."""
    try:
        result = subprocess.run(
            ['lsusb'], capture_output=True, text=True, timeout=5
        )
        output = result.stdout.lower()
        if 'zed-m' in output or 'zed mini' in output:
            return "ZED Mini"
        elif 'zed2i' in output or 'zed-2i' in output or 'zed 2i' in output:
            return "ZED 2i (4mm)"
        elif 'zed' in output and 'stereolabs' in output:
            return "ZED 2i (4mm)"
    except Exception:
        pass
    return "Generic Stereo"


def get_ssl_context(script_dir):
    cert_file = os.path.join(script_dir, "server.crt")
    key_file = os.path.join(script_dir, "server.key")

    # Also check parent directory (StereoVR root)
    parent_dir = os.path.dirname(script_dir)
    parent_cert = os.path.join(parent_dir, "server.crt")
    parent_key = os.path.join(parent_dir, "server.key")

    if os.path.exists(parent_cert) and os.path.exists(parent_key):
        cert_file, key_file = parent_cert, parent_key
    elif not os.path.exists(cert_file) or not os.path.exists(key_file):
        logger.info("Generating SSL certificate...")
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', key_file, '-out', cert_file,
            '-days', '365', '-nodes', '-subj', '/CN=localhost'
        ], capture_output=True)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


class CameraMonitorServer:
    def __init__(self, device_index=0, port=MONITOR_WSS_PORT):
        self.device_index = device_index
        self.port = port
        self.connected_clients: Set = set()

        # Camera state
        self.cap = None
        self.test_mode = False
        self.camera_model = detect_camera_model(device_index)
        self.camera_specs = KNOWN_CAMERAS.get(self.camera_model, KNOWN_CAMERAS["Generic Stereo"])
        self.current_resolution = "HD720"
        self.current_fps = 60
        self.jpeg_quality = JPEG_QUALITY

        # Camera info
        self.camera_info = {}

        # Frame threading
        self.frame_lock = threading.Lock()
        self.latest_left = None
        self.latest_right = None
        self.frame_id = 0
        self.is_running = False
        self.camera_thread = None

        # Switch control
        self.switch_lock = asyncio.Lock()
        self.is_switching = False

        # FPS tracking
        self.fps_actual = 0.0
        self.frames_captured = 0

        # Snapshot
        self.snapshot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")

    # ---- Camera Operations ----

    def open_camera(self, resolution: str, fps: int) -> bool:
        mode = STEREO_MODES.get(resolution)
        if not mode:
            logger.error(f"Unknown resolution: {resolution}")
            return False

        self.cap = cv2.VideoCapture(self.device_index)
        if not self.cap.isOpened():
            logger.warning("Cannot open camera, using test mode")
            self.test_mode = True
            self._build_camera_info(resolution, fps)
            return True

        # Configure
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Read actual values
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)

        # Test read
        ret, frame = self.cap.read()
        if not ret:
            logger.warning("Camera read failed, using test mode")
            self.cap.release()
            self.cap = None
            self.test_mode = True
            self._build_camera_info(resolution, fps)
            return True

        self.test_mode = False
        self.current_resolution = resolution
        self.current_fps = fps
        self._build_camera_info(resolution, fps, actual_w, actual_h, actual_fps)
        logger.info(f"Camera opened: {self.camera_model} @ {resolution} actual={actual_w}x{actual_h} fps={actual_fps:.0f}")
        return True

    def close_camera(self):
        if self.cap:
            self.cap.release()
            self.cap = None

    def _build_camera_info(self, resolution, fps, actual_w=None, actual_h=None, actual_fps=None):
        mode = STEREO_MODES.get(resolution, STEREO_MODES["HD720"])
        fov_data = self.camera_specs["fov"].get(resolution, {"h_fov": 0, "v_fov": 0, "d_fov": 0})

        single_w = (actual_w or mode["width"]) // 2
        single_h = actual_h or mode["height"]

        self.current_resolution = resolution
        self.current_fps = fps
        self.camera_info = {
            "model": self.camera_model if not self.test_mode else f"Test Mode ({self.camera_model})",
            "baseline": self.camera_specs["baseline_mm"],
            "sensor": self.camera_specs["sensor"],
            "h_fov": fov_data["h_fov"],
            "v_fov": fov_data["v_fov"],
            "d_fov": fov_data["d_fov"],
            "width": single_w,
            "height": single_h,
            "stereo_width": actual_w or mode["width"],
            "resolution": resolution,
            "fps_target": fps,
            "fps_reported": actual_fps or fps,
            "available_modes": {k: v["fps_options"] for k, v in STEREO_MODES.items()},
            "test_mode": self.test_mode,
        }

    # ---- Frame Capture ----

    def camera_thread_fn(self):
        logger.info("Capture thread started")
        fps_counter = 0
        fps_timer = time.time()

        while self.is_running:
            loop_start = time.time()

            if self.is_switching:
                time.sleep(0.05)
                continue

            if self.test_mode:
                left, right = self._generate_test_frames()
            else:
                ret, stereo_frame = self.cap.read()
                if ret and stereo_frame is not None:
                    h = stereo_frame.shape[0]
                    mid = stereo_frame.shape[1] // 2
                    left = stereo_frame[:, :mid]
                    right = stereo_frame[:, mid:]
                else:
                    time.sleep(0.001)
                    continue

            with self.frame_lock:
                self.latest_left = left.copy()
                self.latest_right = right.copy()
                self.frame_id += 1

            fps_counter += 1
            self.frames_captured += 1
            now = time.time()
            if now - fps_timer >= 1.0:
                self.fps_actual = fps_counter / (now - fps_timer)
                fps_counter = 0
                fps_timer = now

            target_interval = 1.0 / max(self.current_fps, 1)
            elapsed = time.time() - loop_start
            if elapsed < target_interval:
                time.sleep(target_interval - elapsed)

        logger.info("Capture thread stopped")

    def _generate_test_frames(self):
        mode = STEREO_MODES.get(self.current_resolution, STEREO_MODES["HD720"])
        w = mode["width"] // 2
        h = mode["height"]
        left = np.zeros((h, w, 3), dtype=np.uint8)
        right = np.zeros((h, w, 3), dtype=np.uint8)
        left[:, :] = (40, 80, 160)
        right[:, :] = (160, 80, 40)

        ts = time.strftime("%H:%M:%S")
        info = f"{self.current_resolution}@{self.current_fps}fps {w}x{h}"
        for img, label in [(left, "LEFT"), (right, "RIGHT")]:
            cv2.putText(img, label, (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
            cv2.putText(img, info, (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
            cv2.putText(img, ts, (50, 210), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        return left, right

    # ---- Snapshot ----

    def save_snapshot(self):
        with self.frame_lock:
            left = self.latest_left.copy() if self.latest_left is not None else None
            right = self.latest_right.copy() if self.latest_right is not None else None
        if left is None:
            return None

        os.makedirs(self.snapshot_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        prefix = f"{self.camera_model.replace(' ', '_')}_{self.current_resolution}_{self.current_fps}fps"
        lp = os.path.join(self.snapshot_dir, f"{prefix}_left_{ts}.png")
        rp = os.path.join(self.snapshot_dir, f"{prefix}_right_{ts}.png")
        sp = os.path.join(self.snapshot_dir, f"{prefix}_stereo_{ts}.png")
        cv2.imwrite(lp, left)
        cv2.imwrite(rp, right)
        # Combined side-by-side stereo image
        stereo = np.hstack((left, right))
        cv2.imwrite(sp, stereo)
        logger.info(f"Snapshot: {lp}")
        return {"left": lp, "right": rp, "stereo": sp}

    # ---- WebSocket ----

    def _encode_frame(self, img, quality):
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf).decode('utf-8'), len(buf)

    async def handle_client(self, websocket):
        addr = websocket.remote_address
        logger.info(f"Client connected: {addr}")
        self.connected_clients.add(websocket)

        try:
            await websocket.send(json.dumps({"type": "camera_info", "data": self.camera_info}))

            listener_task = asyncio.create_task(self._client_listener(websocket))

            last_sent_id = -1
            send_interval = 1.0 / 60

            while True:
                with self.frame_lock:
                    left = self.latest_left
                    right = self.latest_right
                    fid = self.frame_id

                if fid == last_sent_id or left is None or right is None:
                    await asyncio.sleep(0.005)
                    continue

                t0 = time.time()
                left_b64, left_sz = self._encode_frame(left, self.jpeg_quality)
                right_b64, right_sz = self._encode_frame(right, self.jpeg_quality)
                encode_ms = (time.time() - t0) * 1000

                msg = {
                    "type": "frame",
                    "frame_id": fid,
                    "left": left_b64,
                    "right": right_b64,
                    "stats": {
                        "fps_actual": round(self.fps_actual, 1),
                        "encode_ms": round(encode_ms, 1),
                        "quality": self.jpeg_quality,
                        "size_kb": round((left_sz + right_sz) / 1024, 1),
                        "resolution": self.current_resolution,
                        "fps_target": self.current_fps,
                        "width": self.camera_info.get("width", 0),
                        "height": self.camera_info.get("height", 0),
                    }
                }
                await websocket.send(json.dumps(msg))
                last_sent_id = fid
                await asyncio.sleep(send_interval)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client disconnected: {addr}")
        except Exception as e:
            logger.error(f"Client error {addr}: {e}")
        finally:
            self.connected_clients.discard(websocket)
            listener_task.cancel()

    async def _client_listener(self, websocket):
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                t = msg.get("type")
                if t == "switch_mode":
                    res = msg.get("resolution", "")
                    fps = msg.get("fps", 0)
                    mode = STEREO_MODES.get(res)
                    if mode and fps in mode["fps_options"]:
                        await self._switch_mode(res, fps)
                elif t == "set_quality":
                    self.jpeg_quality = max(10, min(100, int(msg.get("quality", 85))))
                elif t == "snapshot":
                    result = self.save_snapshot()
                    await websocket.send(json.dumps({"type": "snapshot_result", "data": result}))
                elif t == "set_camera_model":
                    model = msg.get("model", "")
                    if model in KNOWN_CAMERAS:
                        self.camera_model = model
                        self.camera_specs = KNOWN_CAMERAS[model]
                        self._build_camera_info(
                            self.current_resolution, self.current_fps,
                            self.camera_info.get("stereo_width"),
                            self.camera_info.get("height"),
                            self.camera_info.get("fps_reported"),
                        )
                        info_msg = json.dumps({"type": "mode_changed", "data": self.camera_info})
                        for c in list(self.connected_clients):
                            try:
                                await c.send(info_msg)
                            except Exception:
                                pass

        except websockets.exceptions.ConnectionClosed:
            pass

    async def _switch_mode(self, resolution, fps):
        async with self.switch_lock:
            logger.info(f"Switching to {resolution} @ {fps}fps")
            self.is_switching = True

            notify = json.dumps({"type": "switching", "resolution": resolution, "fps": fps})
            for c in list(self.connected_clients):
                try:
                    await c.send(notify)
                except Exception:
                    pass

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._reopen_camera, resolution, fps)
            self.is_switching = False

            info_msg = json.dumps({"type": "mode_changed", "data": self.camera_info})
            for c in list(self.connected_clients):
                try:
                    await c.send(info_msg)
                except Exception:
                    pass
            logger.info(f"Switched to {resolution} @ {fps}fps")

    def _reopen_camera(self, resolution, fps):
        self.close_camera()
        time.sleep(0.5)

        # Clear stale frames before opening new resolution
        with self.frame_lock:
            self.latest_left = None
            self.latest_right = None

        self.open_camera(resolution, fps)

        # Flush initial frames — camera may output old-resolution frames briefly
        if self.cap and not self.test_mode:
            for _ in range(5):
                self.cap.read()
            # Store a valid new-resolution frame immediately
            ret, stereo_frame = self.cap.read()
            if ret and stereo_frame is not None:
                mid = stereo_frame.shape[1] // 2
                with self.frame_lock:
                    self.latest_left = stereo_frame[:, :mid].copy()
                    self.latest_right = stereo_frame[:, mid:].copy()
                    self.frame_id += 1

    # ---- Lifecycle ----

    async def start(self):
        if not self.open_camera(self.current_resolution, self.current_fps):
            logger.error("Camera init failed")
            return

        self.is_running = True
        self.camera_thread = threading.Thread(target=self.camera_thread_fn, daemon=True)
        self.camera_thread.start()
        time.sleep(1)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        ssl_ctx = get_ssl_context(script_dir)

        local_ip = get_local_ip()
        print("\n" + "=" * 60)
        print("  Camera Performance Monitor")
        print("=" * 60)
        print(f"  Camera : {self.camera_model}")
        print(f"  Mode   : {self.current_resolution} @ {self.current_fps}fps")
        print(f"  WSS    : wss://localhost:{self.port}")
        print(f"  Access : https://localhost:{MONITOR_HTTPS_PORT}")
        print(f"  WiFi   : https://{local_ip}:{MONITOR_HTTPS_PORT}")
        print("=" * 60 + "\n")

        server = await websockets.serve(
            self.handle_client, "0.0.0.0", self.port,
            ssl=ssl_ctx, ping_interval=20, ping_timeout=10,
        )
        await server.wait_closed()

    def cleanup(self):
        self.is_running = False
        if self.camera_thread:
            self.camera_thread.join(timeout=3)
        self.close_camera()
        logger.info("Cleanup done")


# ---- HTTPS File Server ----

def start_https_server(serve_dir, cert_file, key_file, port):
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    os.chdir(serve_dir)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    httpd = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


async def run_server(device_index=0):
    server = CameraMonitorServer(device_index=device_index)
    try:
        await server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.cleanup()


def main():
    parser = argparse.ArgumentParser(description='Camera Performance Monitor')
    parser.add_argument('--device', '-d', type=int, default=None, help='Camera device index (auto-detect if omitted)')
    args = parser.parse_args()

    device_index = args.device if args.device is not None else auto_detect_device_index()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    cert_file = os.path.join(script_dir, "server.crt")
    key_file = os.path.join(script_dir, "server.key")

    # Check parent dir for existing certs
    parent_dir = os.path.dirname(script_dir)
    parent_cert = os.path.join(parent_dir, "server.crt")
    parent_key = os.path.join(parent_dir, "server.key")
    if os.path.exists(parent_cert) and os.path.exists(parent_key):
        cert_file, key_file = parent_cert, parent_key
    elif not os.path.exists(cert_file):
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', key_file, '-out', cert_file,
            '-days', '365', '-nodes', '-subj', '/CN=localhost'
        ], capture_output=True)

    # Start HTTPS server for tools/ directory
    threading.Thread(
        target=start_https_server,
        args=(script_dir, cert_file, key_file, MONITOR_HTTPS_PORT),
        daemon=True
    ).start()

    asyncio.run(run_server(device_index))


if __name__ == "__main__":
    main()
