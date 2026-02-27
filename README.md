# RGB125 双目立体视觉 VR 系统

基于 USB 双目相机的实时立体视觉 VR 透视系统，支持 WebXR 标准，兼容多种 VR 设备。

## ✨ 特性

- 🎯 **一键启动**: 简化的启动流程，只需一个命令
- 🔒 **安全加密**: 自动配置 HTTPS + WSS 加密传输
- 📱 **跨设备支持**: 支持 Quest 3、PICO 4、Vision Pro 等主流 VR 设备
- ⚡ **高性能**: 60fps 实时传输，低延迟优化
- 🔌 **USB有线模式**: 自动ADB端口转发，延迟更低更稳定
- 🎨 **双目立体**: 125° 视场角，60mm 基线距离

## 🎮 支持的 VR 设备

| 设备 | 状态 |
|------|------|
| Meta Quest 3 / Quest Pro | ✅ 完全支持 |
| Meta Quest 2 | ✅ 支持 |
| PICO 4 / PICO Neo3 | ✅ 支持 |
| Apple Vision Pro | ✅ 支持 |
| HTC Vive / Vive Pro | ✅ 支持 |
| Valve Index | ✅ 支持 |

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install opencv-python websockets numpy
```

### 2. 启动服务器

```bash
python start.py
```

服务器会自动：
- ✅ 检测USB连接的VR设备并设置ADB端口转发
- ✅ 生成 SSL 证书（如果不存在）
- ✅ 启动 HTTPS 文件服务器（端口 8445）
- ✅ 启动 WSS WebSocket 服务器（端口 8765）

### 3. 在 VR 设备中访问

**USB有线模式（推荐，低延迟）**：
```
https://127.0.0.1:8445
```

**WiFi无线模式**：
```
https://你的电脑IP:8445
```

## 🔌 USB有线模式

USB有线连接比WiFi延迟更低、更稳定。

### 前置条件

1. VR设备开启**开发者模式**和**USB调试**
2. 安装 ADB 工具
3. USB线连接VR设备到电脑

### 验证连接

```bash
adb devices
# 应显示类似: PA8A10MGJ3060002D    device
```

### 工作原理

```
VR浏览器 → 127.0.0.1:8445 → USB线 → PC服务器
         (ADB reverse)
```

启动脚本会自动执行 `adb reverse` 命令，无需手动配置。

## 📁 项目结构

```
StereoVR/
├── start.py              # 🚀 主启动脚本（只需运行这个！）
├── server.py             # WebSocket 服务器核心代码
├── index.html            # 🏠 主页导航（VR设备访问入口）
├── README.md             # 📖 完整文档
├── QUICK_START.md        # ⚡ 快速上手指南
│
├── web/                  # 前端应用文件
│   ├── dual_infrared_viewer.html      # 普通2D查看器
│   └── dual_infrared_vr_viewer.html   # VR 立体查看器
│
├── tools/                # 扩展工具
│   ├── camera_monitor.py             # 📊 相机性能监测服务器
│   └── camera_monitor.html           # 📊 相机性能监测面板
│
├── server.crt            # SSL 证书（自动生成）
└── server.key            # SSL 私钥（自动生成）
```

## 📊 相机性能监测工具

独立的相机测试工具，用于测量和对比立体相机的 FOV、分辨率、帧率等参数。

### 支持的相机

| 相机型号 | 基线距离 | H-FOV (HD720) | 备注 |
|---------|---------|---------------|------|
| ZED Mini | 63 mm | 85° | 推荐桌面级遥操作 |
| ZED 2i (2.1mm) | 120 mm | 100° | 超广角 |
| ZED 2i (4mm) | 120 mm | 65° | 窄角高清 |

### 启动方式

```bash
# 默认使用 /dev/video0
python tools/camera_monitor.py

# 指定设备索引
python tools/camera_monitor.py --device 2
```

访问 `https://localhost:8447/camera_monitor.html` 打开监测面板。

### ZED 相机 USB 连接要求

**ZED 相机必须通过 USB 3.0 连接**，USB 2.0 下只会出现 HID 设备（IMU），不会注册为 UVC 视频设备。

```bash
# 检查 USB 连接状态
lsusb
# 正确: Bus 002 (USB 3.0) 上出现 STEREOLABS ZED-M
# 错误: Bus 001 (USB 2.0) 上只有 ZED-M HID Interface

# 验证视频设备已创建
ls /dev/video*

# 查看 USB 总线速度
lsusb -t
# ZED 应显示 5000M (USB 3.0)，而非 480M (USB 2.0)
```

**常见连接问题：**

| 现象 | 原因 | 解决方法 |
|------|------|---------|
| `lsusb` 显示 `ZED-M HID Interface` 但无 `/dev/video*` | 连接在 USB 2.0 端口 | 改插 USB 3.0 端口（蓝色口） |
| `lsusb` 中完全看不到 ZED | USB Hub 供电不足 | 直连主板 USB 口，不用扩展坞 |
| TypeC 扩展坞连接无反应 | Hub 芯片兼容性问题 | 直连后面板 USB-A 3.0 口 |

### OpenCV UVC 模式（无需 ZED SDK）

本工具使用 OpenCV 直接通过 UVC 协议读取 ZED 相机，**不需要安装 ZED SDK 和 NVIDIA GPU**。ZED 相机在 UVC 模式下输出左右拼接的 side-by-side 立体帧：

```
                    stereo_frame (side-by-side)
┌──────────────────────┬──────────────────────┐
│      Left Eye        │      Right Eye       │
│   (width/2 x height) │  (width/2 x height)  │
└──────────────────────┴──────────────────────┘
```

OpenCV 配置参数：

```python
cap = cv2.VideoCapture(device_index)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))  # MJPG 编码
cap.set(cv2.CAP_PROP_FRAME_WIDTH, stereo_width)   # 双目拼接宽度
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
cap.set(cv2.CAP_PROP_FPS, fps)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 最小缓冲，降低延迟
```

### 分辨率与 FOV 对照表（ZED Mini）

| 模式 | 单眼分辨率 | 双目拼接宽度 | 可选帧率 | H-FOV | V-FOV | D-FOV |
|------|-----------|------------|---------|-------|-------|-------|
| VGA | 672×376 | 1344 | 100/60/30/15 | 85° | 54° | 95° |
| **HD720** | **1280×720** | **2560** | **60/30/15** | **85°** | **54°** | **95°** |
| HD1080 | 1920×1080 | 3840 | 30/15 | 80° | 50° | 90° |
| HD2K | 2208×1242 | 4416 | 15 | 78° | 48° | 88° |

> **推荐：HD720 @ 60fps** — 最大 FOV (85°) + 足够分辨率 + 60fps 流畅度。
> VGA 和 HD720 共享相同传感器读取区域，FOV 一致；HD1080/HD2K 使用 sensor crop 模式，视角更窄。

### 监测工具端口

| 端口 | 用途 | 协议 |
|------|------|------|
| 8447 | 监测面板文件服务器 | HTTPS |
| 8767 | 监测视频流传输 | WSS |

## ⚙️ 配置参数

如需调整相机参数，编辑 `server.py` 文件顶部：

```python
STEREO_WIDTH = 2560     # 双目拼接图像宽度
STEREO_HEIGHT = 720     # 双目拼接图像高度
CAMERA_WIDTH = 1280     # 单目图像宽度
CAMERA_HEIGHT = 720     # 单目图像高度
TARGET_FPS = 60         # 目标帧率
JPEG_QUALITY = 100      # JPEG压缩质量 (1-100)
```

## 🔧 故障排除

### USB设备未检测到

**现象**: 启动时显示"未检测到USB设备"

**解决方法**:
1. 确认VR设备已开启开发者模式和USB调试
2. 运行 `adb devices` 检查设备状态
3. 若显示 `unauthorized`，在VR中确认USB调试授权

### WebSocket 连接失败

**现象**: 页面显示 "WebSocket: 未连接"

**解决方法**:
1. 确保 Python 服务器已启动
2. 检查防火墙是否允许 8445 和 8765 端口

### 证书警告

**现象**: 浏览器提示"您的连接不是私密连接"

**解决方法**: 这是正常现象（自签名证书），点击"高级"→"继续前往"

## 🌐 端口说明

| 端口 | 用途 | 协议 |
|------|------|------|
| 8445 | 文件服务器 | HTTPS |
| 8765 | 视频流传输 | WSS (WebSocket over SSL) |

## 📖 使用提示

1. **推荐USB有线**: 延迟更低，带宽更稳定
2. **远程访问**: 必须通过 HTTPS 访问才能使用 WebXR
3. **多客户端**: 支持多个设备同时连接

## 🔐 系统架构

```
USB有线模式:
┌─────────────┐  ADB reverse  ┌────────────────────┐
│ VR浏览器     │ ──────────── │ PC服务器            │
│ 127.0.0.1   │    USB线      │ HTTPS:8445         │
└─────────────┘               │ WSS:8765           │
                              └────────────────────┘

WiFi无线模式:
┌─────────────┐    WiFi      ┌────────────────────┐
│ VR浏览器     │ ──────────── │ PC服务器            │
│ 192.168.x.x │   局域网      │ HTTPS:8445         │
└─────────────┘               │ WSS:8765           │
                              └────────────────────┘
```

## 👨‍💻 作者

**Liang ZHU** - lzhu686@connect.hkust-gz.edu.cn

## 📄 许可证

MIT License
