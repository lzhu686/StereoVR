#!/usr/bin/env python3
"""
Auto-test all resolution/fps combinations for a V4L2 camera.
Each test runs in a subprocess with timeout to handle V4L2 select() hangs.

Usage:
    python3 test_camera_modes.py /dev/video0              # test MJPG (default)
    python3 test_camera_modes.py /dev/video6 --yuyv       # test YUYV format
    python3 test_camera_modes.py                           # auto-detect via /dev/stereo_camera
"""
import subprocess
import sys
import re
import json
import time
import os

TIMEOUT_SEC = 12
DELAY_SEC = 0.3

TEST_SNIPPET = r'''
import cv2, time, json, sys
device = sys.argv[1]
w, h, fps = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
fourcc_str = sys.argv[5]  # "MJPG" or "YUYV"
cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
if not cap.isOpened():
    print(json.dumps({"ok": False, "err": "open_failed", "ms": 0}))
    sys.exit(0)
cc = cv2.VideoWriter_fourcc(*fourcc_str)
cap.set(cv2.CAP_PROP_FOURCC, cc)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
cap.set(cv2.CAP_PROP_FPS, fps)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
t0 = time.time()
ret, frame = cap.read()
ms = round((time.time() - t0) * 1000)
if ret and frame is not None:
    aw, ah = frame.shape[1], frame.shape[0]
    afps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    print(json.dumps({"ok": True, "aw": aw, "ah": ah, "afps": round(afps, 1), "ms": ms}))
else:
    cap.release()
    print(json.dumps({"ok": False, "err": "read_failed", "ms": ms}))
'''


def get_modes(device, fmt_filter):
    """Parse v4l2-ctl --list-formats-ext for a specific pixel format."""
    result = subprocess.run(
        ["v4l2-ctl", "-d", device, "--list-formats-ext"],
        capture_output=True, text=True, timeout=5
    )
    modes = []
    in_fmt = False
    current_size = None
    for line in result.stdout.splitlines():
        # Check for format header like "'MJPG'" or "'YUYV'"
        if f"'{fmt_filter}'" in line:
            in_fmt = True
            continue
        # Another format starts — stop collecting
        if in_fmt and re.search(r"^\s+\[\d+\]:", line):
            in_fmt = False
            continue
        if not in_fmt:
            continue
        size_match = re.search(r'Size: Discrete (\d+)x(\d+)', line)
        if size_match:
            current_size = (int(size_match.group(1)), int(size_match.group(2)))
            continue
        fps_match = re.search(r'([\d.]+) fps', line)
        if fps_match and current_size:
            fps = float(fps_match.group(1))
            if fps == int(fps):
                fps = int(fps)
            modes.append((current_size[0], current_size[1], fps))
    return modes


def test_one_mode(device, w, h, fps, fourcc):
    try:
        proc = subprocess.run(
            [sys.executable, "-c", TEST_SNIPPET, device, str(w), str(h), str(int(fps)), fourcc],
            capture_output=True, text=True, timeout=TIMEOUT_SEC
        )
        if proc.stdout.strip():
            return json.loads(proc.stdout.strip())
        return {"ok": False, "err": f"no_output stderr={proc.stderr[:100]}", "ms": 0}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout", "ms": TIMEOUT_SEC * 1000}
    except Exception as e:
        return {"ok": False, "err": str(e), "ms": 0}


def main():
    # Parse args
    device = None
    fourcc = "MJPG"
    for arg in sys.argv[1:]:
        if arg == "--yuyv":
            fourcc = "YUYV"
        elif arg == "--mjpg":
            fourcc = "MJPG"
        elif arg.startswith("/dev/"):
            device = arg

    if device is None:
        if os.path.exists("/dev/stereo_camera"):
            device = os.path.realpath("/dev/stereo_camera")
            print(f"Auto-detected: /dev/stereo_camera -> {device}")
        else:
            device = "/dev/video0"

    # Device name
    try:
        idx = int(device.replace("/dev/video", ""))
        with open(f"/sys/class/video4linux/video{idx}/name") as f:
            dev_name = f.read().strip()
    except Exception:
        dev_name = "Unknown"

    print(f"\nDevice : {device}")
    print(f"Name   : {dev_name}")
    print(f"Format : {fourcc}")

    # Parse modes
    modes = get_modes(device, fourcc)
    if not modes:
        print(f"No {fourcc} modes found! Trying other formats...")
        for alt in ["MJPG", "YUYV"]:
            if alt != fourcc:
                modes = get_modes(device, alt)
                if modes:
                    fourcc = alt
                    print(f"  Found {len(modes)} modes with {fourcc}")
                    break
    if not modes:
        print("No supported modes found!")
        return

    print(f"Total {fourcc} modes to test: {len(modes)}")

    # Group by resolution
    by_res = {}
    for w, h, fps in modes:
        key = f"{w}x{h}"
        by_res.setdefault(key, []).append(fps)

    print("\nResolutions found:")
    for res, fps_list in by_res.items():
        print(f"  {res:>12s} : {', '.join(str(int(f)) for f in sorted(fps_list, reverse=True))} fps")

    print(f"\n{'='*80}")
    print(f"{'Resolution':>12s} {'FPS':>5s} | {'Result':>8s} {'Actual':>16s} {'Read ms':>9s} {'Note'}")
    print(f"{'='*80}")

    results = []
    total = len(modes)
    for i, (w, h, fps) in enumerate(modes):
        label = f"{w}x{h}"
        fps_int = int(fps)
        sys.stdout.write(f"\r  Testing {i+1}/{total}: {label} @ {fps_int}fps ... ")
        sys.stdout.flush()

        r = test_one_mode(device, w, h, fps_int, fourcc)
        ok = r.get("ok", False)
        results.append({"w": w, "h": h, "fps": fps_int, "fourcc": fourcc, **r})

        if ok:
            actual = f"{r.get('aw',0)}x{r.get('ah',0)} @{r.get('afps',0)}fps"
            note = ""
            if r.get('aw') != w or r.get('ah') != h:
                note = "SIZE MISMATCH!"
            status = "\033[32m   OK   \033[0m"
        else:
            actual = ""
            note = r.get("err", "unknown")
            status = "\033[31m  FAIL  \033[0m"

        ms = r.get("ms", 0)
        print(f"\r{label:>12s} {fps_int:>5d} | {status} {actual:>16s} {ms:>8d}ms {note}")

        time.sleep(DELAY_SEC)

    # Summary
    print(f"\n{'='*80}")
    print(f"SUMMARY — Working {fourcc} modes:")
    print(f"{'='*80}")

    working = {}
    for r in results:
        if r["ok"]:
            key = f"{r['w']}x{r['h']}"
            working.setdefault(key, []).append(r["fps"])

    if not working:
        print("  No working modes found!")
    else:
        for res in sorted(working.keys(), key=lambda x: (-int(x.split('x')[0]), -int(x.split('x')[1]))):
            fps_list = sorted(working[res], reverse=True)
            print(f"  {res:>12s}  fps: {fps_list}")

    # Generate config dict
    print(f"\n{'='*80}")
    print("GENERATED CONFIG:")
    print(f"{'='*80}")
    print("\nMODES = {")
    for res in sorted(working.keys(), key=lambda x: (-int(x.split('x')[0]), -int(x.split('x')[1]))):
        fps_list = sorted(working[res], reverse=True)
        w, h = res.split('x')
        print(f'    "{res}": {{"width": {w:>4s}, "height": {h:>4s}, "fps_options": {fps_list}}},')
    print("}")

    # Save results
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"test_results_{dev_name[:20].replace(' ','_').replace('/','_')}_{fourcc}.json")
    with open(out_file, "w") as f:
        json.dump({"device": device, "name": dev_name, "fourcc": fourcc, "results": results}, f, indent=2)
    print(f"\nRaw results saved to: {out_file}")


if __name__ == "__main__":
    main()
