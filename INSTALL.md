# Installation Guide

## Quick Install
```bash
cd hailo-cat-tracker
./install.sh
```

This will:
- Create udev rules for device permissions
- Reload the Hailo PCIe driver
- Install systemd service
- Enable auto-start on boot
- Start the service immediately

## What Gets Installed

- **udev rule**: `/etc/udev/rules.d/99-hailo.rules` - Sets `/dev/hailo0` permissions to 0666
- **systemd service**: `/etc/systemd/system/hailo-cat-tracker.service` - Manages the application

## Verify Installation

Check service status:
```bash
sudo systemctl status hailo-cat-tracker
```

View live logs:
```bash
sudo journalctl -u hailo-cat-tracker -f
```

Access web interface:
```
http://<raspberry-pi-ip>:8080
```

## Uninstall
```bash
./uninstall.sh
```

## Manual Installation

If the automatic script doesn't work, follow the manual steps in [README.md](README.md).

## Troubleshooting

### Service won't start
Check logs:
```bash
sudo journalctl -u hailo-cat-tracker -n 50
```

Common issues:
- **Permission denied on /dev/hailo0**: Run `ls -l /dev/hailo0`. Should be `crw-rw-rw-`. If not:
```bash
  sudo rmmod hailo_pci
  sudo modprobe hailo_pci
```

- **Camera not found**: Check `rpicam-vid --list-cameras`

- **Port 8080 in use**: Change port in `cat_tracker_live.py` line 95

### Service starts but website unreachable
- Check if service is listening: `sudo netstat -tlnp | grep 8080`
- Check firewall: `sudo ufw status`
- Verify from Pi itself: `curl http://localhost:8080`
