#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Camera Monitor — StereoVR Tool

Auto-detects connected camera and serves a live preview via WebSocket + HTTPS.
Supports HBVCAM / ZED / Orbbec / RealSense cameras. Uses monitor.html frontend
which auto-adapts to camera type (stereo / single).

Usage:
    python tools/monitor.py                              # auto-detect
    python tools/monitor.py --type hbvcam --device 0     # force HBVCAM
    python tools/monitor.py --type zed --device 0        # force ZED
    python tools/monitor.py --type orbbec --rgb 6        # force Orbbec
    python tools/monitor.py --type realsense --device 6  # force RealSense D405

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
    "model": "Gemini 335L",
    "sensor": "RGB: OV9782 GS / IR: OV9282 GS",
    "shutter": "Global Shutter",
    "h_fov": 94.0, "v_fov": 68.0, "d_fov": 115.0,
    "fourcc": "MJPG",
}

ORBBEC_MODES = {
    # Verified by test_camera_modes.py — 45/45 PASS
    "1280x800": {"width": 1280, "height": 800, "fps_options": [60, 30, 15]},
    "1280x720": {"width": 1280, "height": 720, "fps_options": [60, 30, 15]},
    "848x480":  {"width": 848,  "height": 480, "fps_options": [60, 30, 15]},
    "640x480":  {"width": 640,  "height": 480, "fps_options": [90, 60, 30, 15]},
    "640x400":  {"width": 640,  "height": 400, "fps_options": [90, 60, 30, 15]},
    "640x360":  {"width": 640,  "height": 360, "fps_options": [90, 60, 30, 15]},
    "480x270":  {"width": 480,  "height": 270, "fps_options": [90, 60, 30, 15]},
    "424x240":  {"width": 424,  "height": 240, "fps_options": [90, 60, 30, 15]},
}

# RealSense D405 specs (depth camera with RGB from depth module)
D405_USB_VID = "8086"
D405_USB_PID = "0b5b"

D405_SPECS = {
    "model": "RealSense D405",
    "sensor": "Global Shutter OV9282",
    "shutter": "Global Shutter",
    "h_fov": 87.0, "v_fov": 58.0, "d_fov": 0,
    "fourcc": "YUYV",
}

D405_MODES = {
    # Verified by test_camera_modes.py — 15/15 PASS (YUYV only, no MJPG)
    "1280x720": {"width": 1280, "height": 720, "fps_options": [15]},
    "848x480":  {"width": 848,  "height": 480, "fps_options": [10]},
    "640x480":  {"width": 640,  "height": 480, "fps_options": [30, 15]},
    "480x270":  {"width": 480,  "height": 270, "fps_options": [30, 15]},
    "424x240":  {"width": 424,  "height": 240, "fps_options": [60, 30, 15]},
}

# HBVCAM Head Stereo Camera specs (AR0234 Global Shutter, side-by-side output)
HBVCAM_USB_VID = "1bcf"
HBVCAM_USB_PID = "2d4f"

HBVCAM_CAMERAS = {
    "HBVCAM-F2439GS": {
        "baseline_mm": 60.0,
        "sensor": "AR0234 2MP CMOS Global Shutter",
        "fov": {
            "WUXGA":  {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "HD1080": {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "HD720":  {"h_fov": 0, "v_fov": 0, "d_fov": 0},
            "VGA":    {"h_fov": 0, "v_fov": 0, "d_fov": 0},
        },
    },
}

HBVCAM_MODES = {
    # Verified by test_camera_modes.py — only 3840x1080@60fps fails (V4L2 timeout)
    # Stereo double-width (per-eye = width/2)
    "WUXGA":    {"width": 3840, "height": 1200, "fps_options": [50, 30, 25, 20, 15]},
    "HD1080":   {"width": 3840, "height": 1080, "fps_options": [50, 30, 25, 20, 15]},
    "HD720":    {"width": 2560, "height": 720,  "fps_options": [60, 50, 30, 25, 20]},
    # Half-width stereo (per-eye = width/2, smaller per-eye)
    "960x1200": {"width": 1920, "height": 1200, "fps_options": [60, 50, 30, 25, 20, 15]},
    "960x1080": {"width": 1920, "height": 1080, "fps_options": [60, 50, 30, 25, 20, 15]},
    "640x720":  {"width": 1280, "height": 720,  "fps_options": [60, 50, 30, 25, 20, 15]},
    "VGA":      {"width": 1280, "height": 480,  "fps_options": [60, 50, 30, 25, 20]},
    "320x480":  {"width":  640, "height": 480,  "fps_options": [60, 50, 30, 25, 20, 15]},
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

def _check_usb_vid_pid(video_idx, expected_vid, expected_pid):
    """Check if a video device matches the expected USB VID:PID via sysfs."""
    device_link = f"/sys/class/video4linux/video{video_idx}/device"
    if not os.path.exists(device_link):
        return False
    real_path = os.path.realpath(device_link)
    path = real_path
    for _ in range(5):
        vid_file = os.path.join(path, "idVendor")
        pid_file = os.path.join(path, "idProduct")
        if os.path.exists(vid_file) and os.path.exists(pid_file):
            try:
                with open(vid_file) as f:
                    vid = f.read().strip()
                with open(pid_file) as f:
                    pid = f.read().strip()
                return vid == expected_vid and pid == expected_pid
            except Exception:
                return False
        path = os.path.dirname(path)
    return False


def auto_detect_hbvcam():
    """Find HBVCAM head stereo camera by USB VID:PID. Returns device_index or None."""
    # Method 1: udev symlink
    if os.path.exists("/dev/stereo_camera"):
        real = os.path.realpath("/dev/stereo_camera")
        try:
            idx = int(real.replace("/dev/video", ""))
            if _check_usb_vid_pid(idx, HBVCAM_USB_VID, HBVCAM_USB_PID):
                logger.info(f"Found HBVCAM via /dev/stereo_camera -> /dev/video{idx}")
                return idx
        except ValueError:
            pass
    # Method 2: scan all video devices by VID:PID
    for path in sorted(globmod.glob("/sys/class/video4linux/video*/name")):
        try:
            idx = int(path.split("video4linux/video")[1].split("/")[0])
            if not _check_usb_vid_pid(idx, HBVCAM_USB_VID, HBVCAM_USB_PID):
                continue
            result = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{idx}", "--list-formats"],
                capture_output=True, text=True, timeout=3
            )
            if "MJPG" in result.stdout or "YUYV" in result.stdout:
                logger.info(f"Auto-detected HBVCAM at /dev/video{idx}")
                return idx
        except Exception:
            continue
    return None


def auto_detect_zed():
    """Find ZED device index. Returns (device_index, model_name) or None."""
    # Check udev symlink first (skip if it's an HBVCAM)
    if os.path.exists("/dev/stereo_camera"):
        real = os.path.realpath("/dev/stereo_camera")
        try:
            idx = int(real.replace("/dev/video", ""))
            if not _check_usb_vid_pid(idx, HBVCAM_USB_VID, HBVCAM_USB_PID):
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
    orbbec_indices = []
    for path in sorted(globmod.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if "orbbec" not in name and "gemini" not in name:
                continue
            idx = int(path.split("video4linux/video")[1].split("/")[0])
            orbbec_indices.append(idx)
        except Exception:
            continue

    if not orbbec_indices:
        return None

    # Try v4l2-ctl first (if available)
    for idx in orbbec_indices:
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{idx}", "--list-formats"],
                capture_output=True, text=True, timeout=3
            )
            fmts = result.stdout
            if ("YUYV" in fmts or "MJPG" in fmts) and "Z16" not in fmts and "GREY" not in fmts and "BA81" not in fmts:
                logger.info(f"Auto-detected Orbbec RGB=/dev/video{idx}")
                return idx
        except FileNotFoundError:
            break  # v4l2-ctl not installed, fall through
        except Exception:
            continue

    # Fallback: probe with OpenCV in a subprocess with timeout
    import concurrent.futures

    def _probe_orbbec(idx):
        """Probe a single device in subprocess to avoid V4L2 select() hangs."""
        try:
            result = subprocess.run(
                ["python3", "-c",
                 f"import cv2; cap=cv2.VideoCapture({idx},cv2.CAP_V4L2); "
                 f"ok=cap.isOpened(); ret,f=cap.read() if ok else (False,None); cap.release(); "
                 f"print('RGB' if ret and f is not None and len(f.shape)==3 and f.shape[2]==3 else 'NO')"],
                capture_output=True, text=True, timeout=5
            )
            return "RGB" in result.stdout
        except Exception:
            return False

    for idx in orbbec_indices:
        if _probe_orbbec(idx):
            logger.info(f"Auto-detected Orbbec RGB=/dev/video{idx} (probe)")
            return idx

    return None


def auto_detect_d405():
    """Find RealSense D405 RGB device index. Returns device_idx or None."""
    for path in sorted(globmod.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if "realsense" not in name:
                continue
            idx = int(path.split("video4linux/video")[1].split("/")[0])
            if not _check_usb_vid_pid(idx, D405_USB_VID, D405_USB_PID):
                continue
            result = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{idx}", "--list-formats"],
                capture_output=True, text=True, timeout=3
            )
            fmts = result.stdout
            # D405 RGB device has YUYV but not depth formats (GREY, Z16)
            if "YUYV" in fmts and "GREY" not in fmts and "Z16" not in fmts:
                logger.info(f"Auto-detected RealSense D405 RGB=/dev/video{idx}")
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
        return SingleCameraBackend(rgb, ORBBEC_SPECS, ORBBEC_MODES)

    if force_type == 'realsense':
        idx = zed_device  # reuse --device flag
        if idx is None:
            idx = auto_detect_d405()
            if idx is None:
                idx = 6  # fallback default
        return SingleCameraBackend(idx, D405_SPECS, D405_MODES)

    if force_type == 'zed':
        idx = zed_device if zed_device is not None else 0
        result = auto_detect_zed()
        model = result[1] if result else "Generic Stereo"
        return ZEDBackend(idx, model)

    if force_type == 'hbvcam':
        idx = zed_device  # reuse --device flag
        if idx is None:
            idx = auto_detect_hbvcam()
            if idx is None:
                idx = 0  # fallback default
        return ZEDBackend(idx, "HBVCAM-F2439GS", modes_dict=HBVCAM_MODES)

    # Auto-detect priority: HBVCAM > Orbbec > D405 > ZED > test mode
    hbvcam_idx = auto_detect_hbvcam()
    if hbvcam_idx is not None:
        return ZEDBackend(hbvcam_idx, "HBVCAM-F2439GS", modes_dict=HBVCAM_MODES)

    orbbec_rgb_idx = auto_detect_orbbec()
    if orbbec_rgb_idx is not None:
        return SingleCameraBackend(orbbec_rgb_idx, ORBBEC_SPECS, ORBBEC_MODES)

    d405_idx = auto_detect_d405()
    if d405_idx is not None:
        return SingleCameraBackend(d405_idx, D405_SPECS, D405_MODES)

    zed = auto_detect_zed()
    if zed:
        return ZEDBackend(zed[0], zed[1])

    logger.warning("No camera detected, starting in test mode")
    return None


# ==================== Camera Backends ====================

class ZEDBackend:
    """Side-by-side stereo camera (ZED / HBVCAM / generic UVC stereo)."""
    camera_type = "stereo"

    def __init__(self, device_index=0, model="Generic Stereo", modes_dict=None):
        self.device_index = device_index
        self.camera_model = model
        if model in ZED_CAMERAS:
            self.camera_specs = ZED_CAMERAS[model]
        elif model in HBVCAM_CAMERAS:
            self.camera_specs = HBVCAM_CAMERAS[model]
        else:
            self.camera_specs = ZED_CAMERAS["Generic Stereo"]
        self.modes_dict = modes_dict if modes_dict is not None else ZED_MODES
        self.cap = None
        self.test_mode = False
        # Default: HD720 if available, else first mode
        if "HD720" in self.modes_dict:
            self.current_resolution = "HD720"
            self.current_fps = self.modes_dict["HD720"]["fps_options"][0]
        else:
            first = next(iter(self.modes_dict))
            self.current_resolution = first
            self.current_fps = self.modes_dict[first]["fps_options"][0]

    def get_modes(self):
        return {k: v["fps_options"] for k, v in self.modes_dict.items()}

    def open(self, resolution, fps):
        mode = self.modes_dict.get(resolution)
        if not mode:
            return False
        self.cap = cv2.VideoCapture(self.device_index)
        if not self.cap.isOpened():
            logger.warning(f"Cannot open {self.camera_model} camera, using test mode")
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
            logger.warning(f"{self.camera_model} read failed, using test mode")
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
        fallback = next(iter(self.modes_dict.values()))
        mode = self.modes_dict.get(self.current_resolution, fallback)
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
        if model in HBVCAM_CAMERAS:
            self.camera_model = model
            self.camera_specs = HBVCAM_CAMERAS[model]
            return True
        return False

    def validate_mode(self, res, fps):
        mode = self.modes_dict.get(res)
        return mode and fps in mode["fps_options"]

    def generate_test_frames(self):
        fallback = next(iter(self.modes_dict.values()))
        mode = self.modes_dict.get(self.current_resolution, fallback)
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
        lines = [
            f"  Camera : {self.camera_model}",
            f"  Sensor : {self.camera_specs['sensor']}",
            f"  Mode   : {self.current_resolution} @ {self.current_fps}fps",
            f"  Device : /dev/video{self.device_index}",
        ]
        return lines


class SingleCameraBackend:
    """Generic single-camera (RGB) backend — Orbbec Gemini 335L, RealSense D405, etc."""
    camera_type = "single"

    def __init__(self, device_idx, specs, modes_dict):
        self.rgb_device = device_idx
        self.camera_model = specs["model"]
        self.specs = specs
        self.modes_dict = modes_dict
        self.fourcc_str = specs.get("fourcc", "MJPG")
        self.rgb_cap = None
        self.test_mode = False
        # Default to first mode
        first = next(iter(modes_dict))
        self.current_resolution = first
        self.current_fps = modes_dict[first]["fps_options"][0]

    def get_modes(self):
        return {k: v["fps_options"] for k, v in self.modes_dict.items()}

    def open(self, resolution, fps):
        mode = self.modes_dict.get(resolution)
        if not mode:
            return False

        rgb_path = f"/dev/video{self.rgb_device}"
        self.rgb_cap = cv2.VideoCapture(rgb_path, cv2.CAP_V4L2)
        if not self.rgb_cap.isOpened():
            # Fallback: integer index with V4L2 backend
            self.rgb_cap = cv2.VideoCapture(self.rgb_device, cv2.CAP_V4L2)
        if not self.rgb_cap.isOpened():
            logger.warning(f"Cannot open {self.camera_model}, using test mode")
            self.test_mode = True
            self.current_resolution = resolution
            self.current_fps = fps
            return True

        cc = cv2.VideoWriter_fourcc(*self.fourcc_str)
        self.rgb_cap.set(cv2.CAP_PROP_FOURCC, cc)
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
        self.rgb_cap.set(cv2.CAP_PROP_FPS, fps)
        self.rgb_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ret, _ = self.rgb_cap.read()
        if not ret:
            logger.warning(f"{self.camera_model} read failed, using test mode")
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
        """Returns (rgb, rgb) — both channels show same RGB frame."""
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
        fallback = next(iter(self.modes_dict.values()))
        mode = self.modes_dict.get(self.current_resolution, fallback)
        if self.rgb_cap and not self.test_mode:
            w = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self.rgb_cap.get(cv2.CAP_PROP_FPS)
        else:
            w, h, actual_fps = mode["width"], mode["height"], self.current_fps
        model = self.camera_model if not self.test_mode else f"Test Mode ({self.camera_model})"
        info = {
            "model": model,
            "sensor": self.specs["sensor"],
            "h_fov": self.specs.get("h_fov", 0),
            "v_fov": self.specs.get("v_fov", 0),
            "d_fov": self.specs.get("d_fov", 0),
            "width": w, "height": h,
            "stereo_width": w * 2,
            "resolution": self.current_resolution,
            "fps_target": self.current_fps,
            "fps_reported": actual_fps,
            "available_modes": self.get_modes(),
            "test_mode": self.test_mode,
        }
        if self.specs.get("shutter"):
            info["shutter"] = self.specs["shutter"]
        return info

    def set_camera_model(self, model):
        return False  # fixed model

    def validate_mode(self, res, fps):
        mode = self.modes_dict.get(res)
        return mode and fps in mode["fps_options"]

    def generate_test_frames(self):
        fallback = next(iter(self.modes_dict.values()))
        mode = self.modes_dict.get(self.current_resolution, fallback)
        w, h = mode["width"], mode["height"]
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :] = (40, 120, 80)
        ts = time.strftime("%H:%M:%S")
        info = f"{self.current_resolution} @{self.current_fps}fps"
        cv2.putText(frame, "RGB (Test)", (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
        cv2.putText(frame, info, (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
        cv2.putText(frame, ts, (50, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame, f"{self.camera_model} | {self.specs.get('shutter', '')}", (50, 240),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 2)
        return frame, frame

    def snapshot_names(self, prefix, ts):
        return {
            "left": f"{prefix}_rgb_{ts}.png",
            "right": f"{prefix}_rgb2_{ts}.png",
            "combo": f"{prefix}_combo_{ts}.png",
        }

    def startup_info(self):
        lines = [
            f"  Camera : {self.camera_model}",
            f"  Sensor : {self.specs['sensor']}",
        ]
        if self.specs.get("shutter"):
            lines.append(f"  Shutter: {self.specs['shutter']}")
        lines.append(f"  Mode   : {self.current_resolution} @ {self.current_fps}fps")
        if self.specs.get("h_fov"):
            lines.append(f"  FOV    : H{self.specs['h_fov']}° x V{self.specs['v_fov']}°")
        lines.append(f"  Device : /dev/video{self.rgb_device}")
        return lines


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

        # Hot-plug reconnect state
        self.disconnected = False

    # ---- Capture Thread ----

    def camera_thread_fn(self):
        logger.info("Capture thread started")
        fps_counter = 0
        fps_timer = time.time()
        fail_count = 0
        MAX_FAIL = 30  # ~0.5s of consecutive failures triggers reconnect

        while self.is_running:
            loop_start = time.time()

            if self.is_switching:
                time.sleep(0.05)
                continue

            try:
                result = self.backend.read_frames()
            except Exception as e:
                logger.debug(f"read_frames exception: {e}")
                result = None

            if result is None:
                fail_count += 1
                if fail_count > MAX_FAIL:
                    self._reconnect()
                    fail_count = 0
                    fps_counter = 0
                    fps_timer = time.time()
                else:
                    time.sleep(0.01)
                continue

            fail_count = 0
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

    def _reconnect(self):
        """Attempt to reconnect — tries same camera first, then auto-detects others."""
        self.disconnected = True
        logger.warning("Camera disconnected, attempting reconnect...")

        res = self.backend.current_resolution
        fps = self.backend.current_fps
        same_camera_attempts = 0
        REDETECT_AFTER = 3  # try same camera N times, then scan for any camera

        while self.is_running:
            try:
                self.backend.close()
            except Exception:
                pass

            time.sleep(2)

            if not self.is_running:
                break

            # --- Phase 1: try to reopen the SAME camera ---
            if same_camera_attempts < REDETECT_AFTER:
                same_camera_attempts += 1
                try:
                    opened = self.backend.open(res, fps)
                    if opened and not getattr(self.backend, 'test_mode', False):
                        test = self.backend.read_frames()
                        if test is not None:
                            with self.frame_lock:
                                self.latest_left = test[0].copy()
                                self.latest_right = test[1].copy()
                                self.frame_id += 1
                            self.disconnected = False
                            self.camera_info = self.backend.build_camera_info()
                            logger.info("Camera reconnected successfully")
                            return
                        else:
                            self.backend.close()
                    else:
                        if opened:
                            self.backend.close()
                except Exception as e:
                    logger.warning(f"Reconnect attempt failed: {e}")
                logger.info(f"Same camera retry {same_camera_attempts}/{REDETECT_AFTER} failed...")
                continue

            # --- Phase 2: auto-detect ANY camera (cross-camera hot-plug) ---
            try:
                new_backend = detect_camera()
                if new_backend is not None:
                    # Found a camera — may be the same type or different
                    new_res = new_backend.current_resolution
                    new_fps = new_backend.current_fps
                    if new_backend.open(new_res, new_fps):
                        if not getattr(new_backend, 'test_mode', False):
                            test = new_backend.read_frames()
                            if test is not None:
                                old_model = self.backend.camera_model
                                self.backend = new_backend
                                with self.frame_lock:
                                    self.latest_left = test[0].copy()
                                    self.latest_right = test[1].copy()
                                    self.frame_id += 1
                                self.disconnected = False
                                self.camera_info = self.backend.build_camera_info()
                                if self.backend.camera_model != old_model:
                                    logger.info(f"Switched camera: {old_model} -> {self.backend.camera_model}")
                                else:
                                    logger.info("Camera reconnected successfully")
                                return
                            else:
                                new_backend.close()
                        else:
                            new_backend.close()
            except Exception as e:
                logger.warning(f"Auto-detect attempt failed: {e}")

            logger.info("Reconnect failed (scanning all cameras), retrying in 2s...")

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
            was_disconnected = False

            while True:
                # Send camera disconnect/reconnect status
                if self.disconnected and not was_disconnected:
                    was_disconnected = True
                    try:
                        await websocket.send(json.dumps({
                            "type": "status", "status": "disconnected"
                        }))
                    except Exception:
                        break
                elif not self.disconnected and was_disconnected:
                    was_disconnected = False
                    try:
                        await websocket.send(json.dumps({
                            "type": "status", "status": "reconnected"
                        }))
                        # Re-send updated camera info after reconnect
                        await websocket.send(json.dumps({
                            "type": "camera_info", "data": self.camera_info
                        }))
                    except Exception:
                        break

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
        title = f"{self.backend.camera_model} Monitor"
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
        # First Ctrl+C: graceful shutdown. Second: force exit.
        self._got_signal = False
        loop = asyncio.get_event_loop()
        def _on_signal():
            if self._got_signal:
                os._exit(0)
            self._got_signal = True
            self.ws_server.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)
        await self.ws_server.wait_closed()

    def cleanup(self):
        print("\n  Shutting down...")
        self.is_running = False
        if hasattr(self, 'ws_server') and self.ws_server:
            self.ws_server.close()
        # Release camera first — this unblocks cap.read() in capture thread
        self.backend.close()
        if self.camera_thread:
            self.camera_thread.join(timeout=2)
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
        description='Unified Camera Monitor — auto-detects HBVCAM / ZED / Orbbec / RealSense',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python monitor.py                              # auto-detect camera
  python monitor.py --type hbvcam --device 0     # force HBVCAM head stereo
  python monitor.py --type zed --device 0        # force ZED mode
  python monitor.py --type orbbec --rgb 6        # force Orbbec with specific device
  python monitor.py --type realsense --device 6  # force RealSense D405
""")
    parser.add_argument('--type', '-t', choices=['zed', 'orbbec', 'hbvcam', 'realsense'],
                        help='Force camera type (auto-detect if omitted)')
    parser.add_argument('--device', '-d', type=int, default=None,
                        help='Device index (for ZED/HBVCAM/RealSense)')
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
