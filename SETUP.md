# Hailo Cat Tracker - Complete Setup Guide

From fresh Raspberry Pi 5 to working AI cat detection in ~2 hours.

## Hardware Requirements
- Raspberry Pi 5 (4GB+ recommended)
- Hailo-8L AI Accelerator (M.2 or HAT)
- Raspberry Pi Camera (tested with IMX477)
- MicroSD card (32GB+)
- Power supply (27W official recommended)

## Software Requirements
- Ubuntu 24.04 for Raspberry Pi 5
- Internet connection

---

## Step 1: Flash Ubuntu to SD Card

1. Download [Ubuntu 24.04 for Raspberry Pi](https://ubuntu.com/download/raspberry-pi)
2. Flash to SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/) or Balena Etcher
3. Boot Raspberry Pi 5 and complete initial setup

---

## Step 2: Enable PCIe Gen 3 for Hailo
```bash
sudo raspi-config
```

Navigate to:
- **6 Advanced Options** → **A8 PCIe Speed** → **Yes** (Enable PCIe Gen 3)
- Reboot when prompted

Verify Hailo is detected:
```bash
lspci | grep Hailo
# Should show: Co-processor: Hailo Technologies Ltd. Hailo-8 AI Processor
```

---

## Step 3: Install System Dependencies
```bash
sudo apt update
sudo apt install -y \
    build-essential cmake git \
    python3-dev python3-pip python3-opencv python3-flask \
    libzmq3-dev rsync \
    v4l-utils
```

---

## Step 4: Build and Install HailoRT (Runtime Library)
```bash
cd ~
mkdir hailo && cd hailo
git clone https://github.com/hailo-ai/hailort.git
cd hailort
git checkout v4.23.0

mkdir build && cd build
cmake -H.. -B. -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc)
sudo cmake --build . --config Release --target install
sudo ldconfig
```

Verify:
```bash
hailortcli --version
# Should show: 4.23.0
```

---

## Step 5: Build and Install Hailo PCIe Driver
```bash
cd ~/hailo
git clone https://github.com/hailo-ai/hailort-drivers.git
cd hailort-drivers/linux/pcie
git checkout hailo8

make
sudo make install
sudo modprobe hailo_pci
```

Verify driver loaded:
```bash
lsmod | grep hailo
dmesg | grep -i hailo
# Should see: "Firmware was loaded successfully"
```

---

## Step 6: Install Hailo Firmware

Download correct firmware version:
```bash
cd ~
curl -o hailo8_fw.bin https://hailo-hailort.s3.eu-west-2.amazonaws.com/Hailo8/4.23.0/FW/hailo8_fw.4.23.0.bin
sudo mkdir -p /lib/firmware/hailo
sudo cp hailo8_fw.bin /lib/firmware/hailo/
```

Reload driver to load firmware:
```bash
sudo rmmod hailo_pci
sudo modprobe hailo_pci
```

Verify device works:
```bash
sudo hailortcli fw-control identify
# Should show device info, firmware version 4.23.0
```

---

## Step 7: Install Python HailoRT Bindings
```bash
cd ~/hailo/hailort/hailort/libhailort/bindings/python/platform
sudo pip3 install . --break-system-packages
```

Test:
```bash
python3 -c "from hailo_platform import VDevice; print('✅ HailoRT Python bindings work!')"
```

---

## Step 8: Clone Cat Tracker Project
```bash
cd ~
git clone https://github.com/lusher00/hailo-cat-tracker.git
cd hailo-cat-tracker
```

---

## Step 9: Download YOLOv8 Model
```bash
wget https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.14.0/hailo8l/yolov8s.hef
```

---

## Step 10: Test Manually (Optional)
```bash
python3 cat_tracker_live.py
```

Open browser to `http://<raspberry-pi-ip>:8080`

Press Ctrl+C to stop.

---

## Step 11: Install as System Service
```bash
./install.sh
```

This automatically:
- Creates udev rules for device permissions
- Reloads Hailo driver
- Installs systemd service
- Enables auto-start on boot
- Starts the service

Verify:
```bash
sudo systemctl status hailo-cat-tracker
```

Access: `http://<raspberry-pi-ip>:8080`

---

## Troubleshooting

### Hailo not detected by lspci
- Check PCIe is enabled: `sudo raspi-config` → Advanced → PCIe Speed → Gen 3
- Check hardware connection
- Reboot

### Permission denied on /dev/hailo0
```bash
sudo rmmod hailo_pci
sudo modprobe hailo_pci
ls -l /dev/hailo0  # Should show: crw-rw-rw-
```

### Camera not working
Test camera:
```bash
rpicam-vid --list-cameras
rpicam-vid -t 5000 -o test.h264
```

### Service won't start
View logs:
```bash
sudo journalctl -u hailo-cat-tracker -n 50
```

### Web interface unreachable
Check service is listening:
```bash
sudo netstat -tlnp | grep 8080
```

---

## Quick Commands Reference

**Service Management:**
```bash
sudo systemctl start hailo-cat-tracker    # Start
sudo systemctl stop hailo-cat-tracker     # Stop
sudo systemctl restart hailo-cat-tracker  # Restart
sudo systemctl status hailo-cat-tracker   # Status
sudo journalctl -u hailo-cat-tracker -f   # Live logs
```

**Device Info:**
```bash
lspci | grep Hailo                        # Check PCIe
lsmod | grep hailo                        # Check driver
ls -l /dev/hailo0                         # Check permissions
sudo hailortcli fw-control identify       # Device info
```

**Camera:**
```bash
rpicam-vid --list-cameras                 # List cameras
v4l2-ctl --list-devices                   # V4L2 devices
```

---

## Performance

- **Inference**: ~30ms per frame
- **FPS**: 25-30 FPS live video
- **Latency**: 50-100ms camera to display
- **Power**: ~3W for Hailo-8L NPU

---

## Next Steps

- Customize detection classes in `cat_tracker_live.py`
- Integrate with robot controller (BeagleBone Blue)
- Add recording/logging features
- Try different models from [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo)

---

## Complete Timeline

Estimated setup time on fresh system:

- Step 1-3: 30 minutes (OS install + updates)
- Step 4: 30 minutes (HailoRT build)
- Step 5-7: 15 minutes (Driver + firmware + Python)
- Step 8-11: 5 minutes (Clone + install service)

**Total: ~1.5 hours** (mostly compilation time)

---

## Support

Issues? Check:
1. [Hailo Community Forum](https://community.hailo.ai/)
2. [GitHub Issues](https://github.com/lusher00/hailo-cat-tracker/issues)
3. System logs: `sudo journalctl -xe`
