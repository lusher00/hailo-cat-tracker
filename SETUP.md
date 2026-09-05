# Complete Setup Guide

From a bare Raspberry Pi 5 to working detection.

There are two routes, and which one you take depends on your OS. Pick one — don't mix them.

| | **Route A — Raspberry Pi OS** | **Route B — Ubuntu 24.04** |
|---|---|---|
| Method | `apt install hailo-all` | Build HailoRT from source |
| Time | ~15 minutes | ~90 minutes, mostly compiling |
| Best for | Almost everyone | You already run Ubuntu, or need a specific HailoRT version |

Route A is strongly recommended. Route B exists because `hailo-all` isn't packaged for Ubuntu.

## Hardware

- Raspberry Pi 5 (4GB+; 8GB if you also want to compile things comfortably)
- Hailo-8L AI Kit — M.2 HAT+ or AI HAT+
- One or two Raspberry Pi camera modules, one per CSI port (IMX708 / Camera Module 3, IMX477 / HQ Camera). Both ports can run at once
- MicroSD 32GB+, or an NVMe drive
- The official 27W USB-C supply. Underpowering a Pi 5 with an NPU and a camera produces failures that look like software bugs

---

# Route A — Raspberry Pi OS

## 1. Flash and boot

Flash **Raspberry Pi OS (64-bit, Bookworm)** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Set your username, WiFi and SSH in the Imager's advanced options before writing.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## 2. Enable PCIe Gen 3

```bash
sudo raspi-config
```

**6 Advanced Options → A8 PCIe Speed → Yes**, then reboot.

Gen 2 works, but Gen 3 roughly doubles the bandwidth to the NPU and costs nothing.

```bash
lspci | grep -i hailo
# 0000:01:00.0 Co-processor: Hailo Technologies Ltd. Hailo-8 AI Processor
```

Nothing listed? The HAT isn't seated, the PCIe ribbon is in backwards, or PCIe isn't enabled. The ribbon is easy to get wrong — the contacts face the board on both ends.

## 3. Install everything

```bash
sudo apt install -y hailo-all
sudo reboot
```

`hailo-all` pulls in the PCIe driver, firmware, HailoRT, the Python bindings and a set of pre-compiled models. This is the whole reason Route A is short.

## 4. Verify

```bash
hailortcli fw-control identify
```

```
Device Architecture: HAILO8L
Serial Number: HLDDLBB...
Firmware Version: 4.20.0
```

```bash
python3 -c "from hailo_platform import VDevice; print('bindings ok')"
rpicam-vid --list-cameras
```

Then continue at [Install the tracker](#install-the-tracker).

---

# Route B — Ubuntu 24.04

Longer, because you build HailoRT yourself. Everything must agree on one version — driver, firmware and runtime. Mismatches are the single most common failure here, and the error messages don't point at the real cause.

Pick your version and stick to it:

```bash
export HAILORT_VERSION=4.23.0
```

## 1. Flash Ubuntu

[Ubuntu 24.04 for Raspberry Pi](https://ubuntu.com/download/raspberry-pi), flashed with Raspberry Pi Imager or Balena Etcher. Boot and finish the first-run setup.

## 2. Enable PCIe Gen 3

Ubuntu has no `raspi-config` PCIe entry, so edit the firmware config directly:

```bash
sudo nano /boot/firmware/config.txt
```

Add:

```
dtparam=pciex1
dtparam=pciex1_gen=3
```

```bash
sudo reboot
lspci | grep -i hailo
```

## 3. Build dependencies

```bash
sudo apt update
sudo apt install -y \
    build-essential cmake git \
    python3-dev python3-pip python3-opencv python3-flask python3-numpy \
    libzmq3-dev rsync v4l-utils \
    linux-headers-$(uname -r)
```

`linux-headers` matching your running kernel is required to build the driver. If `apt` can't find them, `uname -r` and the available headers have diverged — update and reboot first.

## 4. Build HailoRT

```bash
mkdir -p ~/hailo && cd ~/hailo
git clone https://github.com/hailo-ai/hailort.git
cd hailort
git checkout v${HAILORT_VERSION}

mkdir build && cd build
cmake -H.. -B. -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j$(nproc)
sudo cmake --build . --config Release --target install
sudo ldconfig
```

This is the slow part — 30 to 45 minutes on a Pi 5. If it's killed partway through, you've run out of RAM; retry with `-j2` instead of `-j$(nproc)`.

```bash
hailortcli --version    # must match $HAILORT_VERSION
```

## 5. Build the PCIe driver

```bash
cd ~/hailo
git clone https://github.com/hailo-ai/hailort-drivers.git
cd hailort-drivers/linux/pcie

make
sudo make install
sudo modprobe hailo_pci
```

```bash
lsmod | grep hailo
dmesg | grep -i hailo
```

You want to see the driver load. It will complain about missing firmware until the next step.

### Register it with DKMS — don't skip this

This driver is out-of-tree. A plain `make install` binds it to the kernel you built it against, and the next `apt upgrade` that brings a new kernel will silently leave you without it. The symptom is unhelpful: `/dev/hailo0` vanishes and HailoRT reports `HAILO_OUT_OF_PHYSICAL_DEVICES` rather than anything about a missing module.

The repo ships a `dkms.conf`, so register it:

```bash
cd ~/hailo/hailort-drivers/linux/pcie
sudo dkms add .
sudo dkms install hailo_pci/${HAILORT_VERSION}
dkms status
```

`dkms status` should list `hailo_pci` against your current kernel. DKMS will then rebuild it automatically on every kernel update, provided the matching headers are installed — so also:

```bash
sudo apt install -y linux-headers-generic
```

If you ever land on a kernel without the module anyway:

```bash
sudo apt install -y linux-headers-$(uname -r)
sudo dkms autoinstall -k $(uname -r)
sudo modprobe hailo_pci
```

## 6. Install firmware

The firmware version must match HailoRT exactly.

```bash
cd ~/hailo
curl -o hailo8_fw.bin \
  https://hailo-hailort.s3.eu-west-2.amazonaws.com/Hailo8/${HAILORT_VERSION}/FW/hailo8_fw.${HAILORT_VERSION}.bin
sudo mkdir -p /lib/firmware/hailo
sudo cp hailo8_fw.bin /lib/firmware/hailo/

sudo rmmod hailo_pci
sudo modprobe hailo_pci
dmesg | grep -i hailo | tail
# ... "Firmware was loaded successfully"
```

```bash
sudo hailortcli fw-control identify
```

## 7. Python bindings

```bash
cd ~/hailo/hailort/hailort/libhailort/bindings/python/platform
sudo pip3 install . --break-system-packages
python3 -c "from hailo_platform import VDevice; print('bindings ok')"
```

On Ubuntu these often land in `/usr/lib/aarch64-linux-gnu/python3.12/site-packages`, which isn't on the default path. `hailo_tracker.py` adds it automatically — this is exactly why that `sys.path.insert` at the top of the file exists.

## 8. Camera

Ubuntu doesn't ship `rpicam-apps`:

```bash
sudo apt install -y rpicam-apps
rpicam-vid --list-cameras
```

If it's missing from your archive, add the Raspberry Pi PPA or use Route A. Camera support on Ubuntu for Pi lags Raspberry Pi OS, and this is the step most likely to give you trouble.

`rpicam-apps`, not the older `libcamera-apps` — the tracker selects cameras with `--camera N`, which the older package doesn't have.

### Two cameras

`--list-cameras` should show one line per module:

```
Available cameras
-----------------
0 : imx708 [4608x2592 10-bit RGGB] (/base/axi/pcie@120000/rp1/i2c@88000/imx708@1a)
1 : imx477 [4056x3040 12-bit RGGB] (/base/axi/pcie@120000/rp1/i2c@80000/imx477@1a)
```

Check each one moves pixels before involving the tracker:

```bash
rpicam-vid --camera 0 -t 3000 -o /tmp/cam0.jpg --encoding jpg
rpicam-vid --camera 1 -t 3000 -o /tmp/cam1.jpg --encoding jpg
```

If only one is listed, it's a wiring or firmware problem rather than a software one. Reseat the ribbon (contacts toward the board on a Pi 5), and check `/boot/firmware/config.txt`: `camera_auto_detect=1` handles both ports, but `camera_auto_detect=0` with a single `dtoverlay=imx708` pins one sensor and hides the other. To name both explicitly, give each its port:

```
camera_auto_detect=0
dtoverlay=imx708,cam0
dtoverlay=imx477,cam1
```

Reboot after editing.

---

# Install the tracker

```bash
cd ~
git clone https://github.com/lusher00/hailo-tracker.git
cd hailo-tracker

./download_model.sh
./install.sh
```

Open `http://<pi-ip>:8080`.

Details and troubleshooting for this step are in [INSTALL.md](INSTALL.md).

---

# Tuning

Once it's running, three things are worth spending ten minutes on.

## 1. Focus and exposure

Watch the stream and check your subject is actually sharp. The defaults assume indoor light and a moving subject:

```bash
CAM_AUTOFOCUS=continuous
CAM_SHUTTER=20000     # 1/50s
CAM_GAIN=2
```

These apply to camera 0 and are the fallback for every other camera, so with two modules pointed at the same room you set them once. Override only what differs, with a `CAM1_` prefix:

```bash
CAM1_SHUTTER=            # camera 1 is outdoors — let the ISP handle it
CAM1_GAIN=
CAM1_ROTATE=180          # mounted upside down
```

An IMX477 has no focus actuator, so the autofocus settings are dropped for it automatically — you don't need to blank `CAM1_AUTOFOCUS` yourself.

Too dark → raise `CAM_GAIN` to 4 or 6 before lengthening the shutter; a longer shutter reintroduces motion blur, which costs detections.

Outdoors → clear `CAM_SHUTTER` and `CAM_GAIN` entirely and let the ISP handle the range.

Fixed scene (a bowl, a doorway) → `CAM_AUTOFOCUS=manual` plus `CAM_LENS_POSITION` for the distance. Manual AF *without* a lens position is the most common cause of a permanently blurry stream. (This is a Camera Module 3 concern; an HQ Camera is focused by hand at the lens.)

## 2. Threshold and stability

Start at `CONF_THRESH=0.40` and adjust it in the web UI while watching real footage — the value applies to every camera, since they share one NPU and one set of detection rules.

- Missing obvious objects → lower it
- Furniture being called "cat" → raise it, and raise `TRACK_MIN_HITS` to 5

`TRACK_MIN_HITS` is often the better lever. A false positive usually appears for one or two frames; requiring five consecutive hits removes most of them without losing anything real.

## 3. Narrow what you track

Detecting all 80 classes costs nothing extra — the network computes them regardless — but it makes the stream noisy and fills the event log with chairs.

```bash
TRACKED_CLASSES=cat,dog,person
```

Add an ROI if the camera sees more than you care about:

```bash
ROI_POLYGON=[[0.05,0.4],[0.6,0.35],[0.65,0.95],[0.1,0.95]]
SHOW_ROI=true
```

## 4. Decide which cameras run the NPU

Camera 0 detects; any additional camera comes up as plain video. That's deliberate — two cameras sharing one Hailo-8L halve the inference rate each one gets, and a second angle is often worth having as video regardless.

Turn it on from the Cameras panel in the web UI to see the cost live, then pin whatever you settle on:

```bash
CAM1_DETECT=true
```

If splitting the NPU evenly isn't what you want, bias it — `CAM1_INFER_EVERY_N=2` runs inference on every second frame from camera 1 and leaves camera 0 the larger share. The tracker coasts between inferences, so this is usually invisible on a slow-moving subject.

Turn on `SHOW_ROI`, look at the stream, adjust the points, restart. Coordinates are normalised, so they survive a resolution change.

---

# Quick reference

**Service**

```bash
sudo systemctl {start,stop,restart,status} hailo-tracker
sudo journalctl -u hailo-tracker -f
```

**Device**

```bash
lspci | grep -i hailo                 # PCIe
lsmod | grep hailo                    # driver
ls -l /dev/hailo0                     # permissions — want crw-rw-rw-
sudo hailortcli fw-control identify   # firmware
hailortcli --version
```

**Camera**

```bash
rpicam-vid --list-cameras
rpicam-vid --camera 0 -t 3000 -o /tmp/t0.jpg --encoding jpg
rpicam-vid --camera 1 -t 3000 -o /tmp/t1.jpg --encoding jpg
v4l2-ctl --list-devices
python3 hailo_tracker.py --list-cameras
```

**Application**

```bash
curl -s localhost:8080/stats | python3 -m json.tool
curl -s localhost:8080/stats/1 | python3 -m json.tool     # one camera
curl -s localhost:8080/cameras | python3 -m json.tool
curl -s localhost:8080/events/summary | python3 -m json.tool
curl -s localhost:8080/healthz
```

---

# Timeline

**Route A** — 30 min OS install and updates, 10 min `hailo-all`, 5 min tracker. **Under an hour**, mostly waiting on downloads.

**Route B** — 30 min OS, 15 min dependencies, 45 min HailoRT build, 15 min driver and firmware, 5 min tracker. **About two hours**, mostly compiling.

# Support

- [Hailo Community Forum](https://community.hailo.ai/) — the most useful resource for driver and firmware problems
- [Hailo Model Zoo](https://github.com/hailo-ai/hailo_model_zoo) — other compiled models
- [GitHub Issues](https://github.com/lusher00/hailo-tracker/issues)
- `sudo journalctl -xe` for anything system-level
