#!/usr/bin/env python3
"""
Orbbec Gemini 335L RGB Camera Test Tool

Tests RGB stream: resolution modes, actual FPS, image quality, snapshots.
Uses plain OpenCV UVC — no Orbbec SDK required.

Usage:
    python tools/orbbec_335l_test.py                  # auto-detect
    python tools/orbbec_335l_test.py --device 6       # specify /dev/video6

Author: Liang ZHU
"""

import cv2
import numpy as np
import time
import os
import argparse
import glob
import subprocess

# ==================== Gemini 335L Specs (from datasheet) ====================
CAMERA_SPECS = {
    "model": "Orbbec Gemini 335L",
    "sensor": "OV9782 (Global Shutter)",
    "sensor_size": '1/4"',
    "shutter": "Global Shutter",
    "fov": {
        "color": {"h": 94.0, "v": 68.0, "d": 115.0},  # Color FOV
        "depth": {"h": 90.0, "v": 65.0, "d": 108.0},   # Depth FOV
    },
    "baseline_mm": 95.0,
    "depth_range": "0.17m - 20m+",
    "ip_rating": "IP65",
}

# RGB resolution modes (from v4l2-ctl --list-formats-ext)
RGB_MODES = {
    "1280x800":  {"w": 1280, "h": 800,  "yuyv_fps": [30, 15, 10, 5], "mjpg_fps": [60, 30, 15, 10, 5]},
    "1280x720":  {"w": 1280, "h": 720,  "yuyv_fps": [30, 15, 10, 5], "mjpg_fps": [60, 30, 15, 10, 5]},
    "848x480":   {"w": 848,  "h": 480,  "yuyv_fps": [60, 30, 15, 10, 5], "mjpg_fps": [60, 30, 15, 10, 5]},
    "640x480":   {"w": 640,  "h": 480,  "yuyv_fps": [90, 60, 30, 15, 10, 5], "mjpg_fps": [90, 60, 30, 15, 10, 5]},
    "640x400":   {"w": 640,  "h": 400,  "yuyv_fps": [90, 60, 30, 15, 10, 5], "mjpg_fps": [90, 60, 30, 15, 10, 5]},
    "640x360":   {"w": 640,  "h": 360,  "yuyv_fps": [90, 60, 30, 15, 10, 5], "mjpg_fps": [90, 60, 30, 15, 10, 5]},
    "480x270":   {"w": 480,  "h": 270,  "yuyv_fps": [90, 60, 30, 15, 10, 5], "mjpg_fps": [90, 60, 30, 15, 10, 5]},
    "424x240":   {"w": 424,  "h": 240,  "yuyv_fps": [90, 60, 30, 15, 10, 5], "mjpg_fps": [90, 60, 30, 15, 10, 5]},
}

# ==========================================================================


def auto_detect_rgb_device():
    """Find the RGB video device for Gemini 335L by checking pixel formats."""
    for path in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(path) as f:
                name = f.read().strip().lower()
            if "orbbec" not in name and "gemini" not in name:
                continue
            idx = int(path.split("video4linux/video")[1].split("/")[0])
            # Check if this device supports YUYV (RGB stream)
            result = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{idx}", "--list-formats"],
                capture_output=True, text=True, timeout=3
            )
            if "YUYV" in result.stdout or "MJPG" in result.stdout:
                # Prefer the YUYV+MJPG device (RGB), skip Z16 (depth) and GREY (IR)
                if "Z16" not in result.stdout and "GREY" not in result.stdout and "BA81" not in result.stdout:
                    print(f"[AUTO] Found RGB stream at /dev/video{idx}")
                    return idx
        except Exception:
            continue
    print("[WARN] Auto-detect failed, trying /dev/video6")
    return 6


def measure_fps(cap, num_frames=60, warmup=10):
    """Measure actual FPS by capturing frames."""
    # Warmup
    for _ in range(warmup):
        cap.read()

    # Measure
    t0 = time.time()
    captured = 0
    for _ in range(num_frames):
        ret, frame = cap.read()
        if ret:
            captured += 1
    elapsed = time.time() - t0

    if elapsed > 0 and captured > 0:
        return captured / elapsed, captured, elapsed
    return 0, 0, elapsed


def test_resolution(device_index, width, height, target_fps, use_mjpg=True, num_frames=90):
    """Test a specific resolution + FPS combination."""
    cap = cv2.VideoCapture(device_index)
    if not cap.isOpened():
        return None

    # Set format
    if use_mjpg:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    else:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('Y', 'U', 'Y', 'V'))

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, target_fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Read actual values
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps_reported = cap.get(cv2.CAP_PROP_FPS)

    # Test read
    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        return None

    # Measure actual FPS
    fps_actual, frames_ok, duration = measure_fps(cap, num_frames=num_frames)

    # Grab a sample frame for snapshot
    ret, sample_frame = cap.read()

    cap.release()

    codec = "MJPG" if use_mjpg else "YUYV"
    return {
        "requested": f"{width}x{height} @{target_fps}fps ({codec})",
        "actual_resolution": f"{actual_w}x{actual_h}",
        "actual_w": actual_w,
        "actual_h": actual_h,
        "fps_target": target_fps,
        "fps_reported": actual_fps_reported,
        "fps_actual": round(fps_actual, 1),
        "frames_captured": frames_ok,
        "duration_s": round(duration, 2),
        "codec": codec,
        "sample_frame": sample_frame if ret else frame,
    }


def save_snapshot(frame, label, snapshot_dir):
    """Save a snapshot PNG."""
    os.makedirs(snapshot_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"Gemini335L_{label}_{ts}.png"
    filepath = os.path.join(snapshot_dir, filename)
    cv2.imwrite(filepath, frame)
    return filepath


def print_header():
    print()
    print("=" * 70)
    print("  Orbbec Gemini 335L — RGB Camera Test")
    print("=" * 70)
    print(f"  Model     : {CAMERA_SPECS['model']}")
    print(f"  Sensor    : {CAMERA_SPECS['sensor']}")
    print(f"  Shutter   : {CAMERA_SPECS['shutter']}")
    print(f"  Color FOV : H{CAMERA_SPECS['fov']['color']['h']}° x V{CAMERA_SPECS['fov']['color']['v']}° (D{CAMERA_SPECS['fov']['color']['d']}°)")
    print(f"  Depth FOV : H{CAMERA_SPECS['fov']['depth']['h']}° x V{CAMERA_SPECS['fov']['depth']['v']}° (D{CAMERA_SPECS['fov']['depth']['d']}°)")
    print(f"  Baseline  : {CAMERA_SPECS['baseline_mm']}mm")
    print(f"  IP Rating : {CAMERA_SPECS['ip_rating']}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Orbbec Gemini 335L RGB Test")
    parser.add_argument("--device", "-d", type=int, default=None,
                        help="Video device index (auto-detect if omitted)")
    parser.add_argument("--quick", action="store_true",
                        help="Quick test: only max resolution")
    args = parser.parse_args()

    device_index = args.device if args.device is not None else auto_detect_rgb_device()
    snapshot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")

    print_header()
    print(f"\n  Device    : /dev/video{device_index}")
    print()

    # ---- Test key resolution modes ----
    test_modes = [
        # (width, height, target_fps, use_mjpg)
        (1280, 800, 60, True),    # Max res, MJPG, 60fps
        (1280, 800, 30, True),    # Max res, MJPG, 30fps
        (1280, 800, 30, False),   # Max res, YUYV, 30fps
        (1280, 720, 60, True),    # 720p, MJPG, 60fps
        (848,  480, 60, True),    # 480p wide, MJPG, 60fps
        (640,  480, 90, True),    # VGA, MJPG, 90fps
    ]

    if args.quick:
        test_modes = [
            (1280, 800, 60, True),
            (1280, 800, 30, True),
        ]

    results = []
    print(f"{'Mode':<32} {'Actual Res':<14} {'Target':>6} {'Report':>6} {'Actual':>8} {'Status'}")
    print("-" * 90)

    for w, h, fps, mjpg in test_modes:
        codec = "MJPG" if mjpg else "YUYV"
        label = f"{w}x{h} @{fps}fps ({codec})"
        print(f"  {label:<30}", end="", flush=True)

        result = test_resolution(device_index, w, h, fps, use_mjpg=mjpg, num_frames=60)
        if result is None:
            print(f"{'FAIL':>14} {'':>6} {'':>6} {'':>8} FAILED")
            continue

        fps_ok = result["fps_actual"] >= result["fps_target"] * 0.85
        status = "OK" if fps_ok else "LOW"

        print(f"{result['actual_resolution']:>14} {result['fps_target']:>6} {result['fps_reported']:>6.0f} {result['fps_actual']:>7.1f}  {status}")
        results.append(result)

    # ---- Save best snapshot ----
    print()
    print("-" * 70)
    print("  Saving snapshots...")

    for r in results:
        if r["sample_frame"] is not None:
            label = f"{r['actual_w']}x{r['actual_h']}_{r['codec']}_{r['fps_target']}fps"
            path = save_snapshot(r["sample_frame"], label, snapshot_dir)
            print(f"    {path}")

    # ---- Summary ----
    print()
    print("=" * 70)
    print("  Summary")
    print("=" * 70)

    # Find best modes
    if results:
        max_res = max(results, key=lambda r: r["actual_w"] * r["actual_h"])
        max_fps = max(results, key=lambda r: r["fps_actual"])
        print(f"  Max Resolution : {max_res['actual_resolution']} ({max_res['codec']} @{max_res['fps_actual']}fps actual)")
        print(f"  Max FPS        : {max_fps['fps_actual']}fps @ {max_fps['actual_resolution']} ({max_fps['codec']})")

    print(f"  Shutter Type   : Global Shutter (OV9782)")
    print(f"  Color FOV      : H94° x V68° (D115°)")
    print(f"  Snapshots      : {snapshot_dir}")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
