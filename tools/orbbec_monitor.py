#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orbbec Gemini 335L Camera Monitor — StereoVR Tool

Live RGB + IR preview via WebSocket, reusing orbbec_monitor.html frontend.
Left view = RGB color, Right view = IR grayscale.
Uses plain OpenCV UVC — no Orbbec SDK required.

Usage:
    python tools/orbbec_monitor.py                          # auto-detect
    python tools/orbbec_monitor.py --rgb 6 --ir 2           # specify devices

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
import glob
from typing import Optional, Set

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== Configuration ====================
MONITOR_WSS_PORT = 8767
MONITOR_HTTPS_PORT = 8447
JPEG_QUALITY = 95

# Gemini 335L camera specs (from datasheet)
CAMERA_SPECS = {
    "Gemini 335L": {
        "baseline_mm": 95.0,
        "sensor": "RGB: OV9782 GS / IR: OV9282 GS",
        "shutter": "Global Shutter",
        "fov": {
            "color": {"h_fov": 94.0, "v_fov": 68.0, "d_fov": 115.0},
            "depth": {"h_fov": 90.0, "v_fov": 65.0, "d_fov": 108.0},
        },
    },
}

# RGB resolution modes (from v4l2-ctl on actual device)
RGB_MODES = {
    "1280x800":  {"width": 1280, "height": 800, "fps_options": [60, 30, 15]},
    "1280x720":  {"width": 1280, "height": 720, "fps_options": [60, 30, 15]},
    "848x480":   {"width": 848,  "height": 480, "fps_options": [60, 30, 15]},
    "640x480":   {"width": 640,  "height": 480, "fps_options": [90, 60, 30]},
    "640x360":   {"width": 640,  "height": 360, "fps_options": [90, 60, 30]},
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


def auto_detect_orbbec_devices():
    """Find RGB and IR video device indices for Gemini 335L."""
    rgb_idx, ir_idx = None, None

    for path in sorted(glob.glob("/sys/class/video4linux/video*/name")):
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
                rgb_idx = idx
            elif "GREY" in fmts and "Z16" not in fmts:
                ir_idx = idx
        except Exception:
            continue

    return rgb_idx, ir_idx


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


class OrbbecMonitorServer:
    def __init__(self, rgb_device=6, ir_device=2, port=MONITOR_WSS_PORT):
        self.rgb_device = rgb_device
        self.ir_device = ir_device
        self.port = port
        self.connected_clients: Set = set()

        # Camera state
        self.rgb_cap = None
        self.ir_cap = None
        self.ir_available = False
        self.test_mode = False
        self.current_resolution = "1280x800"
        self.current_fps = 30
        self.jpeg_quality = JPEG_QUALITY

        # Camera info
        self.camera_info = {}
        self.specs = CAMERA_SPECS["Gemini 335L"]

        # Frame threading
        self.frame_lock = threading.Lock()
        self.latest_rgb = None
        self.latest_ir = None
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
        mode = RGB_MODES.get(resolution)
        if not mode:
            logger.error(f"Unknown resolution: {resolution}")
            return False

        # Open RGB stream — V4L2 backend for reliable MJPG support
        rgb_path = f"/dev/video{self.rgb_device}"
        self.rgb_cap = cv2.VideoCapture(rgb_path, cv2.CAP_V4L2)
        if not self.rgb_cap.isOpened():
            logger.warning("Cannot open RGB camera, using test mode")
            self.test_mode = True
            self._build_camera_info(resolution, fps)
            return True

        # Configure RGB — MJPG enables higher fps (60fps at 1280x800)
        self.rgb_cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
        self.rgb_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
        self.rgb_cap.set(cv2.CAP_PROP_FPS, fps)
        self.rgb_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.rgb_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.rgb_cap.get(cv2.CAP_PROP_FPS)

        ret, frame = self.rgb_cap.read()
        if not ret:
            logger.warning("RGB read failed, using test mode")
            self.rgb_cap.release()
            self.rgb_cap = None
            self.test_mode = True
            self._build_camera_info(resolution, fps)
            return True

        # Try to open IR stream (V4L2 backend, GREY format auto-converted to BGR)
        self.ir_available = False
        if self.ir_device is not None:
            ir_path = f"/dev/video{self.ir_device}"
            self.ir_cap = cv2.VideoCapture(ir_path, cv2.CAP_V4L2)
            if self.ir_cap.isOpened():
                self.ir_cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode["width"])
                self.ir_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode["height"])
                self.ir_cap.set(cv2.CAP_PROP_FPS, fps)
                self.ir_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ret_ir, _ = self.ir_cap.read()
                if ret_ir:
                    self.ir_available = True
                    logger.info(f"IR stream opened: /dev/video{self.ir_device}")
                else:
                    self.ir_cap.release()
                    self.ir_cap = None
                    logger.info("IR stream read failed, RGB-only mode")
            else:
                self.ir_cap = None
                logger.info("IR device not available, RGB-only mode")

        self.test_mode = False
        self.current_resolution = resolution
        self.current_fps = fps
        self._build_camera_info(resolution, fps, actual_w, actual_h, actual_fps)
        ir_str = "RGB+IR" if self.ir_available else "RGB only"
        logger.info(f"Camera opened: Gemini 335L ({ir_str}) @ {resolution} actual={actual_w}x{actual_h} fps={actual_fps:.0f}")
        return True

    def close_camera(self):
        if self.rgb_cap:
            self.rgb_cap.release()
            self.rgb_cap = None
        if self.ir_cap:
            self.ir_cap.release()
            self.ir_cap = None

    def _build_camera_info(self, resolution, fps, actual_w=None, actual_h=None, actual_fps=None):
        mode = RGB_MODES.get(resolution, RGB_MODES["1280x800"])
        color_fov = self.specs["fov"]["color"]

        w = actual_w or mode["width"]
        h = actual_h or mode["height"]

        self.current_resolution = resolution
        self.current_fps = fps
        self.camera_info = {
            "model": "Gemini 335L" if not self.test_mode else "Test Mode (Gemini 335L)",
            "baseline": self.specs["baseline_mm"],
            "sensor": self.specs["sensor"],
            "h_fov": color_fov["h_fov"],
            "v_fov": color_fov["v_fov"],
            "d_fov": color_fov["d_fov"],
            "width": w,
            "height": h,
            "stereo_width": w * 2,  # For compatibility with frontend
            "resolution": resolution,
            "fps_target": fps,
            "fps_reported": actual_fps or fps,
            "available_modes": {k: v["fps_options"] for k, v in RGB_MODES.items()},
            "test_mode": self.test_mode,
            "ir_available": self.ir_available,
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
                rgb, ir = self._generate_test_frames()
            else:
                ret, rgb = self.rgb_cap.read()
                if not ret or rgb is None:
                    time.sleep(0.001)
                    continue

                # Read IR if available (V4L2 returns BGR with equal channels)
                ir = None
                if self.ir_available and self.ir_cap:
                    ret_ir, ir_raw = self.ir_cap.read()
                    if ret_ir and ir_raw is not None:
                        if len(ir_raw.shape) == 2:
                            ir = cv2.cvtColor(ir_raw, cv2.COLOR_GRAY2BGR)
                        else:
                            ir = ir_raw

                # If no IR, create a dimmed/annotated version of RGB
                if ir is None:
                    ir = cv2.cvtColor(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                    cv2.putText(ir, "IR Not Available", (20, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 180, 255), 2)

            with self.frame_lock:
                self.latest_rgb = rgb.copy()
                self.latest_ir = ir.copy()
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
        mode = RGB_MODES.get(self.current_resolution, RGB_MODES["1280x800"])
        w, h = mode["width"], mode["height"]
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        ir = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[:, :] = (40, 120, 80)
        ir[:, :] = (60, 60, 60)

        ts = time.strftime("%H:%M:%S")
        info = f"{self.current_resolution} @{self.current_fps}fps"
        for img, label, color in [(rgb, "RGB (Color)", (0, 255, 0)), (ir, "IR (Grayscale)", (200, 200, 200))]:
            cv2.putText(img, label, (50, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5, color, 3)
            cv2.putText(img, info, (50, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
            cv2.putText(img, ts, (50, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.putText(img, "Gemini 335L | Global Shutter", (50, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 2)
        return rgb, ir

    # ---- Snapshot ----

    def save_snapshot(self):
        with self.frame_lock:
            rgb = self.latest_rgb.copy() if self.latest_rgb is not None else None
            ir = self.latest_ir.copy() if self.latest_ir is not None else None
        if rgb is None:
            return None

        os.makedirs(self.snapshot_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        prefix = f"Gemini335L_{self.current_resolution}_{self.current_fps}fps"
        rp = os.path.join(self.snapshot_dir, f"{prefix}_rgb_{ts}.png")
        ip = os.path.join(self.snapshot_dir, f"{prefix}_ir_{ts}.png")
        sp = os.path.join(self.snapshot_dir, f"{prefix}_combo_{ts}.png")
        cv2.imwrite(rp, rgb)
        if ir is not None:
            cv2.imwrite(ip, ir)
            stereo = np.hstack((rgb, ir))
            cv2.imwrite(sp, stereo)
        logger.info(f"Snapshot: {rp}")
        return {"left": rp, "right": ip, "stereo": sp}

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
                    rgb = self.latest_rgb
                    ir = self.latest_ir
                    fid = self.frame_id

                if fid == last_sent_id or rgb is None:
                    await asyncio.sleep(0.005)
                    continue

                t0 = time.time()
                # left = RGB, right = IR
                left_b64, left_sz = self._encode_frame(rgb, self.jpeg_quality)
                right_b64, right_sz = self._encode_frame(ir, self.jpeg_quality) if ir is not None else (left_b64, left_sz)
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
                    mode = RGB_MODES.get(res)
                    if mode and fps in mode["fps_options"]:
                        await self._switch_mode(res, fps)
                elif t == "set_quality":
                    self.jpeg_quality = max(10, min(100, int(msg.get("quality", 85))))
                elif t == "snapshot":
                    result = self.save_snapshot()
                    await websocket.send(json.dumps({"type": "snapshot_result", "data": result}))

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
        with self.frame_lock:
            self.latest_rgb = None
            self.latest_ir = None
        self.open_camera(resolution, fps)
        # Store a valid frame immediately (open_camera already verified read works)
        if self.rgb_cap and not self.test_mode:
            ret, frame = self.rgb_cap.read()
            if ret and frame is not None:
                with self.frame_lock:
                    self.latest_rgb = frame.copy()
                    ir = None
                    if self.ir_cap:
                        ret_ir, ir_raw = self.ir_cap.read()
                        if ret_ir and ir_raw is not None:
                            ir = cv2.cvtColor(ir_raw, cv2.COLOR_GRAY2BGR) if len(ir_raw.shape) == 2 else ir_raw
                    if ir is None:
                        ir = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                    self.latest_ir = ir.copy()
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
        ir_str = "RGB + IR" if self.ir_available else "RGB only"
        print("\n" + "=" * 60)
        print("  Orbbec Gemini 335L Monitor")
        print("=" * 60)
        print(f"  Camera  : Gemini 335L ({ir_str})")
        print(f"  Sensor  : {self.specs['sensor']}")
        print(f"  Shutter : {self.specs['shutter']}")
        print(f"  Mode    : {self.current_resolution} @ {self.current_fps}fps")
        print(f"  RGB FOV : H{self.specs['fov']['color']['h_fov']}° x V{self.specs['fov']['color']['v_fov']}°")
        print(f"  RGB Dev : /dev/video{self.rgb_device}")
        if self.ir_available:
            print(f"  IR Dev  : /dev/video{self.ir_device}")
        print(f"  WSS     : wss://localhost:{self.port}")
        print(f"  Access  : https://localhost:{MONITOR_HTTPS_PORT}/orbbec_monitor.html")
        print(f"  WiFi    : https://{local_ip}:{MONITOR_HTTPS_PORT}/orbbec_monitor.html")
        print("=" * 60)
        print("  View: Left = RGB Color | Right = IR Grayscale")
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


async def run_server(rgb_device, ir_device):
    server = OrbbecMonitorServer(rgb_device=rgb_device, ir_device=ir_device)
    try:
        await server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.cleanup()


def main():
    parser = argparse.ArgumentParser(description='Orbbec Gemini 335L Monitor')
    parser.add_argument('--rgb', type=int, default=None, help='RGB device index (auto-detect if omitted)')
    parser.add_argument('--ir', type=int, default=None, help='IR device index (auto-detect if omitted)')
    args = parser.parse_args()

    if args.rgb is None or args.ir is None:
        auto_rgb, auto_ir = auto_detect_orbbec_devices()
        rgb_dev = args.rgb if args.rgb is not None else (auto_rgb if auto_rgb is not None else 6)
        ir_dev = args.ir if args.ir is not None else (auto_ir if auto_ir is not None else 2)
    else:
        rgb_dev, ir_dev = args.rgb, args.ir

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

    # Start HTTPS server for tools/ directory
    threading.Thread(
        target=start_https_server,
        args=(script_dir, cert_file, key_file, MONITOR_HTTPS_PORT),
        daemon=True
    ).start()

    asyncio.run(run_server(rgb_dev, ir_dev))


if __name__ == "__main__":
    main()
