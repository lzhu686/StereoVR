# StereoVR Tools

统一相机监控工具，支持多种深度/立体相机的实时预览、分辨率切换、热拔插重连。

## 架构概览

```
tools/
├── monitor.py          # 后端：相机检测 + 采集 + WebSocket/HTTPS 服务
├── monitor.html        # 前端：自适应 Web UI（单页应用）
├── server.crt / .key   # 自签名 TLS 证书（首次运行自动生成，已 gitignore）
└── snapshots/          # 截图保存目录（已 gitignore）
```

### monitor.py 核心模块

| 模块 | 职责 |
|------|------|
| **Configuration** | 各相机规格参数、已验证的分辨率/帧率模式 |
| **Auto-Detection** | USB VID:PID + sysfs + udev 自动识别相机类型 |
| **ZEDBackend** | 立体双目相机（ZED Mini / HBVCAM 双目头部相机） |
| **SingleCameraBackend** | 单目 RGB 相机（Orbbec Gemini 335L / RealSense D405） |
| **MonitorServer** | WebSocket 帧推送 + 模式切换 + 热拔插重连 + 截图 |

### 自动检测优先级

```
HBVCAM (VID:PID 1bcf:2d4f)
  → Orbbec (sysfs name "Gemini")
    → RealSense D405 (VID:PID 8086:0b5b)
      → ZED (sysfs/udev /dev/stereo_camera)
        → Test Mode (无相机时的占位模式)
```

### 热拔插机制

采集线程连续 30 帧读取失败后触发 `_reconnect()` 循环：
- 关闭当前设备 → 等待 2 秒 → 尝试重新打开
- 拒绝 test_mode 回退（确保真实相机恢复后才退出重连）
- 前端通过 WebSocket `status` 消息显示断连/重连状态

## 支持的相机

| 相机 | 类型 | 接口 | 格式 | 检测方式 | SDK 需求 |
|------|------|------|------|----------|----------|
| **HBVCAM-F2439GS** | 双目立体 | USB 2.0 | MJPG | VID:PID `1bcf:2d4f` | 无需 SDK |
| **ZED Mini** | 双目立体 | USB 3.0 | YUYV | udev + sysfs | 无需 SDK (UVC) |
| **ZED 2i** | 双目立体 | USB 3.0 | YUYV | udev + sysfs | 无需 SDK (UVC) |
| **Orbbec Gemini 335L** | 单目 RGB | USB 3.0 | MJPG | sysfs name | 无需 SDK |
| **Orbbec Gemini 305** | 单目 RGB | USB 3.0 | MJPG/YUYV | sysfs name | 无需 SDK |
| **RealSense D405** | 单目 RGB | USB 3.0 | YUYV | VID:PID `8086:0b5b` | 无需 SDK |

> 所有相机通过标准 **V4L2/UVC** 协议访问，不依赖厂商 SDK。
> 如需深度流、IMU、IR 投射等高级功能，需另外安装对应 SDK。

## 依赖安装

### Python 依赖

```bash
pip install opencv-python websockets numpy
```

### 系统依赖

```bash
# Ubuntu / Debian
sudo apt install v4l-utils openssl

# v4l-utils: 提供 v4l2-ctl（相机自动检测需要）
# openssl:   自动生成自签名 TLS 证书（HTTPS + WSS 加密传输）
```

### 依赖检查

运行前可手动验证：

```bash
python3 -c "import cv2, websockets, numpy; print('Python deps OK')"
which v4l2-ctl && echo "v4l2-ctl OK" || echo "MISSING: sudo apt install v4l-utils"
which openssl  && echo "openssl OK"  || echo "MISSING: sudo apt install openssl"
```

### 相机厂商 SDK（可选，monitor 不需要）

| SDK | 安装方式 | 用途 |
|-----|----------|------|
| **ZED SDK** | [stereolabs.com/developers/release](https://www.stereolabs.com/developers/release) | 深度计算、AI、固件更新 |
| **Orbbec SDK** | [github.com/orbbec/OrbbecSDK](https://github.com/orbbec/OrbbecSDK) | 深度流、IR 控制、设备管理 |
| **RealSense SDK** | `sudo apt install librealsense2-dkms librealsense2-utils` | 深度流、IMU、固件更新 |

## 使用方法

### 相机监控

```bash
cd tools/

# 自动检测相机
python3 monitor.py

# 强制指定相机类型
python3 monitor.py --type hbvcam --device 0     # HBVCAM 双目头部相机
python3 monitor.py --type zed --device 0         # ZED Mini / ZED 2i
python3 monitor.py --type orbbec --rgb 6         # Orbbec (指定 RGB 设备号)
python3 monitor.py --type realsense --device 6   # RealSense D405
```

启动后访问终端输出的 URL：
- 本机：`https://localhost:8447/monitor.html`
- 局域网：`https://<IP>:8447/monitor.html`

### 已验证的分辨率模式

**HBVCAM-F2439GS** (94/95 PASS, MJPG)
```
3840x1200  fps: [50, 30, 25, 20, 15]
3840x1080  fps: [50, 30, 25, 20, 15]      # 60fps 超时已排除
2560x720   fps: [60, 50, 30, 25, 20]
1920x1200  fps: [60, 50, 30, 25, 20, 15]
1920x1080  fps: [60, 50, 30, 25, 20, 15]
1280x720   fps: [60, 50, 30, 25, 20, 15]
1280x480   fps: [60, 50, 30, 25, 20]
640x480    fps: [60, 50, 30, 25, 20, 15]
```

**Orbbec Gemini 335L** (45/45 PASS, MJPG)
```
1280x800   fps: [60, 30, 15]
1280x720   fps: [60, 30, 15]
848x480    fps: [60, 30, 15]
640x480    fps: [90, 60, 30, 15]
640x400    fps: [90, 60, 30, 15]
640x360    fps: [90, 60, 30, 15]
480x270    fps: [90, 60, 30, 15]
424x240    fps: [90, 60, 30, 15]
```

**RealSense D405** (15/15 PASS, YUYV)
```
1280x720   fps: [15]
848x480    fps: [10]
640x480    fps: [30, 15]
480x270    fps: [30, 15]
424x240    fps: [60, 30, 15]
```

## USB 端口注意事项

- **ZED Mini / ZED 2i**: 必须使用 **USB 3.0** 端口，否则只暴露 HID 接口无视频
- **Orbbec Gemini 335L/305**: 推荐 USB 3.0，USB 2.0 下带宽受限帧率降低
- **RealSense D405**: 推荐 USB 3.0
- **HBVCAM**: USB 2.0 即可（MJPG 压缩，带宽需求低）

查看哪些物理端口支持 USB 3.0：
```bash
# 有 peer 的端口 = 支持 USB 3.0
for i in $(seq 1 12); do
  peer=$(readlink /sys/bus/usb/devices/usb1/1-0:1.0/usb1-port${i}/peer 2>/dev/null)
  [ -n "$peer" ] && echo "Port $i: USB 3.0 ($peer)"
done
```

## 前端功能 (monitor.html)

- 实时立体/单目预览（Canvas 渲染）
- 视图切换：Stereo / Left / Right（单目模式：Full / Left Half / Right Half）
- 分辨率/帧率在线切换（后端热切换，无需重启）
- JPEG 质量滑块（10-100%）
- 实时统计：采集 FPS、显示 FPS、编码耗时、帧大小
- 相机信息面板：型号、传感器、快门、FOV、基线距离
- 连接状态指示：Connected / Switching / Disconnected
- 截图保存
- 自适应布局：根据后端返回的 camera_info 自动匹配 UI

## WebSocket 协议

| 方向 | type | 说明 |
|------|------|------|
| S→C | `camera_info` | 相机信息（启动时发送） |
| S→C | `frame` | JPEG 帧（base64 left + right） |
| S→C | `mode_changed` | 模式切换完成通知 |
| S→C | `switching` | 正在切换模式 |
| S→C | `status` | `disconnected` / `reconnected` |
| S→C | `snapshot_result` | 截图结果路径 |
| C→S | `switch_mode` | 请求切换分辨率/帧率 |
| C→S | `set_quality` | 设置 JPEG 质量 |
| C→S | `snapshot` | 请求截图 |
| C→S | `set_camera_model` | 切换相机型号（仅 ZED） |

## 新增相机适配指南

1. 在 `monitor.py` Configuration 区添加 `XXX_SPECS` 和 `XXX_MODES`
2. 用 `v4l2-ctl -d /dev/videoN --list-formats-ext` 查询支持的分辨率/帧率，逐一验证后填入
3. 添加 USB VID:PID 常量和 `auto_detect_xxx()` 函数
4. 在 `detect_camera()` 中添加 force_type 和自动检测分支
5. 选择合适的 Backend：
   - 双目侧拼输出 → `ZEDBackend(idx, model, modes_dict=XXX_MODES)`
   - 单目 RGB → `SingleCameraBackend(idx, XXX_SPECS, XXX_MODES)`
6. 在 `monitor.html` 的 `detectType()` 中添加型号关键词匹配
7. 更新 CLI `--type` choices
