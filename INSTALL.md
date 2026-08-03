# Installation

This assumes HailoRT and the PCIe driver are already working. If they aren't, start with [SETUP.md](SETUP.md).

Check first:

```bash
hailortcli fw-control identify     # should print Hailo-8 or Hailo-8L
rpicam-vid --list-cameras          # should list your camera
```

## Quick install

```bash
cd hailo-tracker
./download_model.sh
./install.sh
```

Then open `http://<pi-ip>:8080`.

## What install.sh does

| Step | Detail |
|------|--------|
| Preflight | Warns if `rpicam-vid`, `hailo_platform`, or a `.hef` is missing — it won't stop, so you can install in any order |
| udev rule | `/etc/udev/rules.d/99-hailo.rules` sets `/dev/hailo0` to mode 0666, so the service doesn't need root |
| Driver reload | `rmmod` + `modprobe hailo_pci` so the new permissions apply now rather than after a reboot |
| Config file | Creates `hailo-tracker.env` with every setting commented out. An existing file is left alone |
| systemd unit | `/etc/systemd/system/hailo-tracker.service`, running as your user, with `EnvironmentFile` pointing at the config |
| Enable + start | Auto-start on boot, and start immediately |

It refuses to run under `sudo` — it needs to know which user the service should run as, and it calls `sudo` itself where required.

## Model file

The `.hef` is ~23MB and is gitignored, so it doesn't ship with the repo.

```bash
./download_model.sh           # yolov8s, the default
./download_model.sh yolov8m   # bigger, slower, more accurate
```

The script checks `/usr/share/hailo-models/` first (where `hailo-all` puts models) before hitting the Hailo model zoo S3 bucket.

If you have a Hailo-8 rather than a Hailo-8L, edit the URL in the script — swap `hailo8l` for `hailo8`.

## Verify

```bash
sudo systemctl status hailo-tracker
sudo journalctl -u hailo-tracker -f
curl -s http://localhost:8080/healthz
```

A healthy startup logs:

```
[hailo] Loading /home/pi/hailo-tracker/yolov8s.hef ...
[hailo] Ready — in yolov8s/input_layer1 (640, 640, 3), out yolov8s/yolov8_nms_postprocess
[camera] rpicam-vid --codec mjpeg --inline --nopreview ...
[camera] capture started

  Hailo Tracker
  Model:    yolov8s.hef
  Camera:   1280x720 @ 30fps, AF=continuous, rotate=0deg
  Tracking: all 80 classes  (conf >= 40%)
  IDs:      on   Events: on   Snapshots: off

  http://192.168.1.139:8080
```

## Configuration

Edit `hailo-tracker.env`, then restart. Every setting is listed there with a comment; see the [README](README.md#configuration) for the ones that matter most.

```bash
nano hailo-tracker.env
sudo systemctl restart hailo-tracker
```

Confidence, the class filter and the display toggles are also adjustable live in the web UI — handy for finding the right threshold. Those changes don't survive a restart; put anything permanent in the env file.

## Uninstall

```bash
./uninstall.sh
```

Removes the service. Deliberately keeps the udev rule (other Hailo apps may want it), your `hailo-tracker.env`, `events.db`, and `snapshots/`.

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u hailo-tracker -n 50
```

**`HAILO_OUT_OF_PHYSICAL_DEVICES` (error 74)**

The message says "requested: 1, found: 0". That's a count of *free* devices, so it covers two unrelated causes. Split them first:

```bash
ls -l /dev/hailo0
```

*Missing* → the driver isn't loaded. On Ubuntu this is nearly always a kernel update that outran the out-of-tree module:

```bash
uname -r
find /lib/modules -name 'hailo_pci*'      # built for which kernel?
sudo apt install -y linux-headers-$(uname -r)
sudo dkms autoinstall -k $(uname -r)
sudo modprobe hailo_pci
```

*Present* → another process holds it. Only one can:

```bash
sudo fuser -v /dev/hailo0
systemctl is-active hailo-cat-tracker
```

Kill whatever it reports, or stop and **disable** the competing service — if both are enabled they race at boot and the loser fails in a way that looks intermittent.

A useful non-signal: `sudo hailortcli fw-control identify` works even when another process owns the device, because it opens it only momentarily. Passing that check does not mean the device is free.

**Permission denied on /dev/hailo0**

```bash
ls -l /dev/hailo0     # want crw-rw-rw-
sudo rmmod hailo_pci && sudo modprobe hailo_pci
```

If the node was recreated by something other than a normal boot — a DKMS rebuild, say — the udev rule may not have fired:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**`ModuleNotFoundError: No module named 'hailo_platform'`**

The service uses `/usr/bin/python3`. Confirm the module is visible to *that* interpreter:

```bash
/usr/bin/python3 -c "import hailo_platform; print(hailo_platform.__file__)"
```

If you built HailoRT from source, the bindings usually land in `/usr/lib/aarch64-linux-gnu/python3.*/site-packages` — `hailo_tracker.py` adds that path automatically. If they're somewhere else, add it to the env file:

```bash
PYTHONPATH=/path/to/site-packages
```

Don't install into a venv. The Hailo package targets system site-packages, and pip-installed `numpy`/`opencv` will shadow the system builds and break it.

**`FileNotFoundError: No .hef model found`** — run `./download_model.sh`, or set `HEF_PATH` in the env file.

**Camera not found**

```bash
rpicam-vid --list-cameras
rpicam-vid -t 3000 -o /tmp/test.jpg --encoding jpg
```

If the camera works standalone but not in the service, something else is holding it — check for another instance:

```bash
sudo systemctl stop hailo-tracker
pgrep -a rpicam
```

### Service runs but the page is unreachable

```bash
sudo ss -tlnp | grep 8080          # is it listening?
curl http://localhost:8080/healthz # does it answer locally?
sudo ufw status                    # firewall in the way?
```

If it answers locally but not from another machine, it's a firewall or a network isolation setting on your AP.

### Page loads but the video is frozen

Check the frame counter is moving:

```bash
curl -s http://localhost:8080/stats | python3 -m json.tool
```

- `capture_errors` climbing means the camera pipeline is restarting — check `journalctl` for the reason.
- `frames` static with `healthy: false` means capture has stalled; restart the service.
- Lots of `dropped` is normal under load and only means the renderer is behind; the newest frame always wins.

### Boxes are in the wrong place

- Mirrored across the diagonal → set `BOX_ORDER=yxyx`.
- Uniformly offset or scaled → `NN_SIZE` doesn't match the model's input; the startup log warns about this.
- Rotated → set `ROTATE_DEGREES`, not `CAM_HFLIP`/`CAM_VFLIP`.

### Testing changes without a Pi

```bash
pip install flask opencv-python numpy
./tests/run_tests.sh
```

Fakes the camera and the NPU, runs everything else for real.
