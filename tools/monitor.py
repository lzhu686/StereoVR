#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Camera Monitor — StereoVR Tool

Auto-detects connected camera (ZED stereo / Orbbec Gemini 335L) and serves
a live preview via WebSocket + HTTPS. Uses monitor.html frontend which
auto-adapts to camera type.

Usage:
    python tools/monitor.py                              # auto-detect
    python tools/monitor.py --type zed --device 0        # force ZED
    python tools/monitor.py --type orbbec --rgb 6        # force Orbbec

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
import signal
import glob as globmod
from typing import Set

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== Configuration ====================
MONITOR_WSS_PORT = 8767
MONITOR_HTTPS_PORT = 8447
JPEG_QUALITY = 95

# ZED camera specs
ZED_CAMERAS = {
    "ZED Mini": {
        "baseline_mm": 63.0,
        "sensor": '1/3" 4MP CMOS',
        "fov": {
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

ZED_MODES = {
    "VGA":    {"width": 1344, "height": 376,  "fps_options": [100, 60, 30, 15]},
    "HD720":  {"width": 2560, "height": 720,  "fps_options": [60, 30, 15]},
    "HD1080": {"width": 3840, "height": 1080, "fps_options": [30, 15]},
    "HD2K":   {"width": 4416, "height": 1242, "fps_options": [15]},
}

# Orbbec Gemini 335L specs
ORBBEC_SPECS = {
    "baseline_mm": 95.0,
    "sensor": "RGB: OV9782 GS / IR: OV9282 GS",
    "shutter": "Global Shutter",
    "fov": {
        "color": {"h_fov": 94.0, "v_fov": 68.0, "d_fov": 115.0},
        "depth": {"h_fov": 90.0, "v_fov": 65.0, "d_fov": 108.0},
    },
}

ORBBEC_MODES = {
    "1280x800":  {"width": 1280, "height": 800, "fps_options": [60, 30, 15]},
    "1280x720":  {"width": 1280, "height": 720, "fps_options": [60, 30, 15]},
    "848x480":   {"width": 848,  "height": 480, "fps_options": [60, 30, 15]},
    "640x480":   {"width": 640,  "height": 480, "fps_options": [90, 60, 30]},
    "640x360":   {"width": 640,  "height": 360, "fps_options": [90, 60, 30]},
}


# ==================== Utilities ====================

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def get_ssl_context(script_dir):
    cert_file = os.path.join(script_dir, "server.crt")
    key_file = os.path.join(script_dir, "server.key")
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


def cleanup_ports(*ports):
    """Kill any process occupying the given ports so we can bind immediately."""
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            result = s.connect_ex(('127.0.0.1', port))
            s.close()
            if result == 0:
                logger.info(f"Port {port} in use, cleaning up...")
                subprocess.run(['fuser', '-k', f'{port}/tcp'],
                               capture_output=True, timeout=5)
                time.sleep(0.5)
        except Exception:
            pass


def start_https_server(serve_dir, cert_file, key_file, port):
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    os.chdir(serve_dir)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    HTTPServer.allow_reuse_address = True
    httpd = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


# ==================== Auto-Detection ====================

def auto_detect_zed():
    """Find ZED device index. Returns (device_index, model_name) or None."""
    # Check udev symlink first
    if os.path.exists("/dev/stereo_camera"):
        real = os.path.realpath("/dev/stereo_camera")
        try:
            idx = int(real.replace("/dev/video", ""))
            model = _detect_zed_model()
            logger.info(f"Found udev symlink /dev/stereo_camera -> /dev/video{idx}")
            return idx, model
        except ValueError:
            pass
    # Scan sysfs
    for path in sorted(globmod.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if 'zed' in name:
                idx = int(path.split("video4linux/video")[1].split("/")[0])
                model = _detect_zed_model()
                logger.info(f"Auto-detected ZED at /dev/video{idx} ({name})")
                return idx, model
        except Exception:
            continue
    return None


def _detect_zed_model():
    try:
        result = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
        out = result.stdout.lower()
        if 'zed-m' in out or 'zed mini' in out:
            return "ZED Mini"
        elif 'zed2i' in out or 'zed-2i' in out or 'zed 2i' in out:
            return "ZED 2i (4mm)"
        elif 'zed' in out and 'stereolabs' in out:
            return "ZED 2i (4mm)"
    except Exception:
        pass
    return "Generic Stereo"


def auto_detect_orbbec():
    """Find Orbbec RGB device index. Returns rgb_idx or None."""
    for path in sorted(globmod.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if "orbbec" not in name and "gemini" not in name:
                continue
            idx = int(path.split("video4linux/video")[1].split("/")[0])
            result = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{idx}", "--list-formats"],
                capture_output=True, text=True, timeout=3
            )
            fmts = result.stdout
            if ("YUYV" in fmts or "MJPG" in fmts) and "Z16" not in fmts and "GREY" not in fmts and "BA81" not in fmts:
                logger.info(f"Auto-detected Orbbec RGB=/dev/video{idx}")
                return idx
        except Exception:
            continue
    return None


def detect_camera(force_type=None, zed_device=None, orbbec_rgb=None):
    """Detect connected camera and return appropriate backend, or None for test mode."""
    if force_type == 'orbbec':
        rgb = orbbec_rgb
        if rgb is None:
            rgb = auto_detect_orbbec()
            if rgb is None:
                rgb = 6  # fallback default
        return OrbbecBackend(rgb)

    if force_type == 'zed':
        idx = zed_device if zed_device is not None else 0
        result = auto_detect_zed()
        model = result[1] if result else "Generic Stereo"
        return ZEDBackend(idx, model)

    # Auto-detect: try Orbbec first (more specific), then ZED
    orbbec_rgb_idx = auto_detect_orbbec()
    if orbbec_rgb_idx is not None:
        return OrbbecBackend(orbbec_rgb_idx)

    zed = auto_detect_zed()
    if zed:
        return ZEDBackend(zed[0], zed[1])

    logger.warning("No camera detected, starting in test mode")
    return None


# ==================== Camera Backends ====================

class ZEDBackend:
    """ZED stereo camera (side-by-side output via single UVC device)."""
    camera_type = "stereo"

    def __init__(self, device_index=0, model="Generic Stereo"):
        self.device_index = device_index
        self.camera_model = model
        self.camera_specs = ZED_CAMERAS.get(model, ZED_CAMERAS["Generic Stereo"])
        self.cap = None
        self.test_mode = False
        self.current_resolution = "HD720"
        self.current_fps = 60

    def get_modes(self):
        return {k: v["fps_options"] for k, v in ZED_MODES.items()}

    def open(self, resolution, fps):
        mode = ZED_MODES.get(resolution)
        if not mode:
            return False
        self.cap = cv2.VideoCapture(self.device_index)
        if not self.cap.isOpened():
            logger.warning("Cannot open ZED camera, using test mode")
            self.test_mode = True
            self.current_resolution = resolution
            self.current_fps = fps
            return True
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ret, _ = self.cap.read()
        if not ret:
            logger.warning("ZED read failed, using test mode")
            self.cap.release()
            self.cap = None
            self.test_mode = True
            self.current_resolution = resolution
            self.current_fps = fps
            return True
        self.test_mode = False
        self.current_resolution = resolution
        self.current_fps = fps
        return True

    def close(self):
        if self.cap:
            self.cap.release()
            self.cap = None

    def read_frames(self):
        """Returns (left, right) or None on failure."""
        if self.test_mode:
            return self.generate_test_frames()
        ret, stereo = self.cap.read()
        if not ret or stereo is None:
            return None
        mid = stereo.shape[1] // 2
        return stereo[:, :mid], stereo[:, mid:]

    def on_reopen(self):
        """Flush stale frames after mode switch."""
        if self.cap and not self.test_mode:
            for _ in range(5):
                self.cap.read()
            ret, stereo = self.cap.read()
            if ret and stereo is not None:
                mid = stereo.shape[1] // 2
                return stereo[:, :mid].copy(), stereo[:, mid:].copy()
        return None

    def build_camera_info(self):
        mode = ZED_MODES.get(self.current_resolution, ZED_MODES["HD720"])
        fov = self.camera_specs["fov"].get(self.current_resolution, {"h_fov": 0, "v_fov": 0, "d_fov": 0})
        if self.cap and not self.test_mode:
            actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        else:
            actual_w, actual_h, actual_fps = mode["width"], mode["height"], self.current_fps
        single_w = actual_w // 2
        return {
            "model": self.camera_model if not self.test_mode else f"Test Mode ({self.camera_model})",
            "baseline": self.camera_specs["baseline_mm"],
            "sensor": self.camera_specs["sensor"],
            "h_fov": fov["h_fov"], "v_fov": fov["v_fov"], "d_fov": fov["d_fov"],
            "width": single_w, "height": actual_h,
            "stereo_width": actual_w,
            "resolution": self.current_resolution,
            "fps_target": self.current_fps,
            "fps_reported": actual_fps,
            "available_modes": self.get_modes(),
            "test_mode": self.test_mode,
        }

    def set_camera_model(self, model):
        if model in ZED_CAMERAS:
            self.camera_model = model
            self.camera_specs = ZED_CAMERAS[model]
            return True
        return False

    def validate_mode(self, res, fps):
        mode = ZED_MODES.get(res)
        return mode and fps in mode["fps_options"]

    def generate_test_frames(self):
        mode = ZED_MODES.get(self.current_resolution, ZED_MODES["HD720"])
        w, h = mode["width"] // 2, mode["height"]
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

    def snapshot_names(self, prefix, ts):
        return {
            "left": f"{prefix}_left_{ts}.png",
            "right": f"{prefix}_right_{ts}.png",
            "combo": f"{prefix}_stereo_{ts}.png",
        }

    def startup_info(self):
        return [
            f"  Camera : {self.camera_model}",
            f"  Mode   : {self.current_resolution} @ {self.current_fps}fps",
            f"  Device : /dev/video{self.device_index}",
        ]


class OrbbecBackend:
    """Orbbec Gemini 335L (separate RGB + IR UVC devices).
    IR runs in a dedicated thread to avoid blocking RGB capture."""
    camera_type = "orbbec"
    camera_model = "Gemini 335L"

    def __init__(self, rgb_device=6):
        self.rgb_device = rgb_device
        self.rgb_cap = None
        self.test_mode = False
        self.current_resolution = "1280x800"
        self.current_fps = 30

    def get_modes(self):
        return {k: v["fps_options"] for k, v in ORBBEC_MODES.items()}

    def open(self, resolution, fps):
        mode = ORBBEC_MODES.get(resolution)
        if not mode:
            return False

        rgb_path = f"/dev/video{self.rgb_device}"
        self.rgb_cap = cv2.VideoCapture(rgb_path, cv2.CAP_V4L2)
        if not self.rgb_cap.isOpened():
            logger.warning("Cannot open Orbbec RGB camera, using test mode")
            self.test_mode = True
            self.current_resolution = resolution
            self.current_fps = fps
            return True

        self.rgb_cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
        self.rgb_cap.set(cv2.CAP_PROP_FPS, fps)
        self.rgb_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ret, _ = self.rgb_cap.read()
        if not ret:
            logger.warning("Orbbec RGB read failed, using test mode")
            self.rgb_cap.release()
            self.rgb_cap = None
            self.test_mode = True
            self.current_resolution = resolution
            self.current_fps = fps
            return True

        self.test_mode = False
        self.current_resolution = resolution
        self.current_fps = fps
        return True

    def close(self):
        if self.rgb_cap:
            self.rgb_cap.release()
            self.rgb_cap = None

    def read_frames(self):
        """Returns (rgb, rgb) — both channels show RGB."""
        if self.test_mode:
            return self.generate_test_frames()
        ret, rgb = self.rgb_cap.read()
        if not ret or rgb is None:
            return None
        return rgb, rgb

    def on_reopen(self):
        if self.rgb_cap and not self.test_mode:
            ret, frame = self.rgb_cap.read()
            if ret and frame is not None:
                return frame.copy(), frame.copy()
        return None

    def build_camera_info(self):
        mode = ORBBEC_MODES.get(self.current_resolution, ORBBEC_MODES["1280x800"])
        color_fov = ORBBEC_SPECS["fov"]["color"]
        if self.rgb_cap and not self.test_mode:
            w = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self.rgb_cap.get(cv2.CAP_PROP_FPS)
        else:
            w, h, actual_fps = mode["width"], mode["height"], self.current_fps
        return {
            "model": "Gemini 335L" if not self.test_mode else "Test Mode (Gemini 335L)",
            "baseline": ORBBEC_SPECS["baseline_mm"],
            "sensor": ORBBEC_SPECS["sensor"],
            "shutter": ORBBEC_SPECS["shutter"],
            "h_fov": color_fov["h_fov"], "v_fov": color_fov["v_fov"], "d_fov": color_fov["d_fov"],
            "width": w, "height": h,
            "stereo_width": w * 2,
            "resolution": self.current_resolution,
            "fps_target": self.current_fps,
            "fps_reported": actual_fps,
            "available_modes": self.get_modes(),
            "test_mode": self.test_mode,
        }

    def set_camera_model(self, model):
        return False  # Orbbec model is fixed

    def validate_mode(self, res, fps):
        mode = ORBBEC_MODES.get(res)
        return mode and fps in mode["fps_options"]

    def generate_test_frames(self):
        mode = ORBBEC_MODES.get(self.current_resolution, ORBBEC_MODES["1280x800"])
        w, h = mode["width"], mode["height"]
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :] = (40, 120, 80)
        ts = time.strftime("%H:%M:%S")
        info = f"{self.current_resolution} @{self.current_fps}fps"
        cv2.putText(frame, "RGB (Test)", (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
        cv2.putText(frame, info, (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
        cv2.putText(frame, ts, (50, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame, "Gemini 335L | Global Shutter", (50, 240),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 2)
        return frame, frame

    def snapshot_names(self, prefix, ts):
        return {
            "left": f"{prefix}_rgb_{ts}.png",
            "right": f"{prefix}_rgb2_{ts}.png",
            "combo": f"{prefix}_combo_{ts}.png",
        }

    def startup_info(self):
        return [
            f"  Camera  : Gemini 335L",
            f"  Sensor  : {ORBBEC_SPECS['sensor']}",
            f"  Shutter : {ORBBEC_SPECS['shutter']}",
            f"  Mode    : {self.current_resolution} @ {self.current_fps}fps",
            f"  RGB FOV : H{ORBBEC_SPECS['fov']['color']['h_fov']}° x V{ORBBEC_SPECS['fov']['color']['v_fov']}°",
            f"  RGB Dev : /dev/video{self.rgb_device}",
        ]


# ==================== Monitor Server ====================

class MonitorServer:
    """Shared WebSocket/HTTPS server that works with any camera backend."""

    def __init__(self, backend, port=MONITOR_WSS_PORT):
        self.backend = backend
        self.port = port
        self.connected_clients: Set = set()

        self.jpeg_quality = JPEG_QUALITY
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

    # ---- Capture Thread ----

    def camera_thread_fn(self):
        logger.info("Capture thread started")
        fps_counter = 0
        fps_timer = time.time()

        while self.is_running:
            loop_start = time.time()

            if self.is_switching:
                time.sleep(0.05)
                continue

            result = self.backend.read_frames()
            if result is None:
                time.sleep(0.001)
                continue
            left, right = result

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

            target_interval = 1.0 / max(self.backend.current_fps, 1)
            elapsed = time.time() - loop_start
            if elapsed < target_interval:
                time.sleep(target_interval - elapsed)

        logger.info("Capture thread stopped")

    # ---- Snapshot ----

    def save_snapshot(self):
        with self.frame_lock:
            left = self.latest_left.copy() if self.latest_left is not None else None
            right = self.latest_right.copy() if self.latest_right is not None else None
        if left is None:
            return None

        os.makedirs(self.snapshot_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        model_tag = self.backend.camera_model.replace(' ', '_')
        prefix = f"{model_tag}_{self.backend.current_resolution}_{self.backend.current_fps}fps"
        names = self.backend.snapshot_names(prefix, ts)

        lp = os.path.join(self.snapshot_dir, names["left"])
        rp = os.path.join(self.snapshot_dir, names["right"])
        sp = os.path.join(self.snapshot_dir, names["combo"])
        cv2.imwrite(lp, left)
        if right is not None:
            cv2.imwrite(rp, right)
        combo = np.hstack((left, right)) if right is not None else left
        cv2.imwrite(sp, combo)
        logger.info(f"Snapshot: {lp}")
        return {"left": lp, "right": rp, "stereo": sp}

    # ---- WebSocket ----

    @staticmethod
    def _encode_frame(img, quality):
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

                if fid == last_sent_id or left is None:
                    await asyncio.sleep(0.005)
                    continue

                t0 = time.time()
                left_b64, left_sz = self._encode_frame(left, self.jpeg_quality)
                if right is not None:
                    right_b64, right_sz = self._encode_frame(right, self.jpeg_quality)
                else:
                    right_b64, right_sz = left_b64, left_sz
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
                        "resolution": self.backend.current_resolution,
                        "fps_target": self.backend.current_fps,
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
                    if self.backend.validate_mode(res, fps):
                        await self._switch_mode(res, fps)
                elif t == "set_quality":
                    self.jpeg_quality = max(10, min(100, int(msg.get("quality", 85))))
                elif t == "snapshot":
                    result = self.save_snapshot()
                    await websocket.send(json.dumps({"type": "snapshot_result", "data": result}))
                elif t == "set_camera_model":
                    model = msg.get("model", "")
                    if self.backend.set_camera_model(model):
                        self.camera_info = self.backend.build_camera_info()
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

            self.camera_info = self.backend.build_camera_info()
            info_msg = json.dumps({"type": "mode_changed", "data": self.camera_info})
            for c in list(self.connected_clients):
                try:
                    await c.send(info_msg)
                except Exception:
                    pass
            logger.info(f"Switched to {resolution} @ {fps}fps")

    def _reopen_camera(self, resolution, fps):
        self.backend.close()
        time.sleep(0.5)
        with self.frame_lock:
            self.latest_left = None
            self.latest_right = None
        self.backend.open(resolution, fps)
        result = self.backend.on_reopen()
        if result:
            with self.frame_lock:
                self.latest_left = result[0]
                self.latest_right = result[1]
                self.frame_id += 1

    # ---- Lifecycle ----

    async def start(self):
        if not self.backend.open(self.backend.current_resolution, self.backend.current_fps):
            logger.error("Camera init failed")
            return
        self.camera_info = self.backend.build_camera_info()

        self.is_running = True
        self.camera_thread = threading.Thread(target=self.camera_thread_fn, daemon=True)
        self.camera_thread.start()
        time.sleep(1)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        ssl_ctx = get_ssl_context(script_dir)

        local_ip = get_local_ip()
        title = "Orbbec Gemini 335L Monitor" if self.backend.camera_type == "orbbec" else "Camera Monitor"
        print("\n" + "=" * 60)
        print(f"  {title}")
        print("=" * 60)
        for line in self.backend.startup_info():
            print(line)
        print(f"  WSS    : wss://localhost:{self.port}")
        print(f"  Access : https://localhost:{MONITOR_HTTPS_PORT}/monitor.html")
        print(f"  WiFi   : https://{local_ip}:{MONITOR_HTTPS_PORT}/monitor.html")
        print("=" * 60 + "\n")

        self.ws_server = await websockets.serve(
            self.handle_client, "0.0.0.0", self.port,
            ssl=ssl_ctx, ping_interval=20, ping_timeout=10,
        )
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.ws_server.close)
        await self.ws_server.wait_closed()

    def cleanup(self):
        print("\n  Shutting down...")
        self.is_running = False
        if hasattr(self, 'ws_server') and self.ws_server:
            self.ws_server.close()
        if self.camera_thread:
            self.camera_thread.join(timeout=3)
        self.backend.close()
        print("  Camera released, ports freed. Ready to restart.")
        logger.info("Cleanup done")


# ==================== Main ====================

async def run_server(backend):
    server = MonitorServer(backend)
    try:
        await server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description='Unified Camera Monitor — auto-detects ZED stereo or Orbbec Gemini 335L',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python monitor.py                              # auto-detect camera
  python monitor.py --type zed --device 0        # force ZED mode
  python monitor.py --type orbbec --rgb 6        # force Orbbec with specific device
""")
    parser.add_argument('--type', '-t', choices=['zed', 'orbbec'],
                        help='Force camera type (auto-detect if omitted)')
    parser.add_argument('--device', '-d', type=int, default=None,
                        help='ZED device index')
    parser.add_argument('--rgb', type=int, default=None,
                        help='Orbbec RGB device index')
    args = parser.parse_args()

    # Clean up stale ports
    cleanup_ports(MONITOR_WSS_PORT, MONITOR_HTTPS_PORT)

    # Detect or create backend
    backend = detect_camera(
        force_type=args.type,
        zed_device=args.device,
        orbbec_rgb=args.rgb,
    )
    if backend is None:
        # Test mode with generic stereo
        backend = ZEDBackend(0, "Generic Stereo")
        backend.test_mode = True

    # Start HTTPS file server
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cert_file = os.path.join(script_dir, "server.crt")
    key_file = os.path.join(script_dir, "server.key")
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

    threading.Thread(
        target=start_https_server,
        args=(script_dir, cert_file, key_file, MONITOR_HTTPS_PORT),
        daemon=True
    ).start()

    asyncio.run(run_server(backend))


if __name__ == "__main__":
    main()
