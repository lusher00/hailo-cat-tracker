# 🐱 Hailo Cat Tracker

Real-time AI-powered cat detection running on Raspberry Pi 5 with Hailo-8L NPU.

## Hardware
- Raspberry Pi 5
- Hailo-8L AI Accelerator (13 TOPS)
- Raspberry Pi Camera Module (IMX477)

## Features
- 🚀 Real-time object detection at 30 FPS
- 🐱 YOLO-based cat detection
- 🌐 Live web streaming interface
- ⚡ Hardware-accelerated inference on Hailo NPU

## Installation

### 1. Install Hailo Driver & Runtime
```bash
# Install HailoRT
git clone https://github.com/hailo-ai/hailort.git
cd hailort
git checkout v4.23.0
mkdir build && cd build
cmake -H.. -B. -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc)
sudo cmake --build . --config Release --target install
sudo ldconfig
```

### 2. Install Hailo PCIe Driver
```bash
cd ~/hailort-drivers
git clone https://github.com/hailo-ai/hailort-drivers.git
cd hailort-drivers/linux/pcie
git checkout hailo8
make
sudo make install
sudo modprobe hailo_pci
```

### 3. Install Firmware
```bash
curl -o hailo8_fw.bin https://hailo-hailort.s3.eu-west-2.amazonaws.com/Hailo8/4.23.0/FW/hailo8_fw.4.23.0.bin
sudo mkdir -p /lib/firmware/hailo
sudo cp hailo8_fw.bin /lib/firmware/hailo/
```

### 4. Install Python Dependencies
```bash
sudo apt install -y python3-opencv python3-flask
sudo pip3 install --break-system-packages \
    ~/hailort/hailort/libhailort/bindings/python/platform
```

### 5. Run the Tracker
```bash
python3 cat_tracker_live.py
```

Open browser to `http://<raspberry-pi-ip>:8080`

## How It Works

1. **Camera Stream**: `rpicam-vid` captures 640x640 MJPEG frames at 30 FPS
2. **AI Inference**: Each frame is processed by YOLOv8s model on Hailo-8L NPU
3. **Detection**: Cats are identified with bounding boxes and confidence scores
4. **Web Display**: Annotated frames stream to browser in real-time

## Architecture
```
Camera (IMX477) 
    ↓ MJPEG stream
rpicam-vid 
    ↓ JPEG frames
Python decoder
    ↓ NumPy arrays
Hailo-8L NPU (YOLOv8s)
    ↓ Detections
OpenCV annotation
    ↓ Annotated JPEG
Flask web server
    ↓ HTTP stream
Browser
```

## Performance
- **Inference**: ~30ms per frame on Hailo-8L
- **Total latency**: ~50-100ms camera to display
- **Power**: ~3W for NPU

## Model
- YOLOv8s compiled for Hailo-8L
- 80-class COCO dataset (class 15 = cat)
- Input: 640x640 RGB
- Output: Bounding boxes + confidence scores

## Future Improvements
- [ ] Send cat position to robot controller (BeagleBone Blue)
- [ ] Add tracking ID for multiple cats
- [ ] Record detection events
- [ ] Mobile-responsive UI

## License
MIT

## Acknowledgments
- Hailo for the AI accelerator and HailoRT SDK
- Ultralytics for YOLOv8
- Raspberry Pi Foundation
