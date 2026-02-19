#!/bin/bash
set -e

echo "🚀 Installing Hailo Cat Tracker as a system service..."

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
   echo "❌ Please run as regular user (not sudo)"
   exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER=$(whoami)

echo "📁 Install directory: $INSTALL_DIR"
echo "👤 Running as user: $USER"

# 1. Create udev rule for device permissions
echo "🔧 Creating udev rule for Hailo device permissions..."
sudo tee /etc/udev/rules.d/99-hailo.rules > /dev/null << UDEV
KERNEL=="hailo*", MODE="0666"
UDEV

# 2. Reload udev rules
echo "♻️  Reloading udev rules..."
sudo udevadm control --reload-rules
sudo udevadm trigger

# 3. Reload Hailo driver to apply permissions
echo "🔄 Reloading Hailo PCIe driver..."
if lsmod | grep -q hailo_pci; then
    sudo rmmod hailo_pci
    sudo modprobe hailo_pci
    echo "✅ Driver reloaded"
else
    echo "⚠️  hailo_pci driver not loaded - will load on next boot"
fi

# 4. Verify device permissions
if [ -e /dev/hailo0 ]; then
    PERMS=$(ls -l /dev/hailo0 | awk '{print $1}')
    echo "✅ Device permissions: $PERMS"
else
    echo "⚠️  /dev/hailo0 not found - device may not be connected"
fi

# 5. Create systemd service
echo "📝 Creating systemd service..."
sudo tee /etc/systemd/system/hailo-cat-tracker.service > /dev/null << SERVICE
[Unit]
Description=Hailo Cat Tracker - AI-powered cat detection
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/cat_tracker_live.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

# 6. Reload systemd
echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload

# 7. Enable service
echo "✅ Enabling service to start on boot..."
sudo systemctl enable hailo-cat-tracker

# 8. Start service
echo "🚀 Starting service..."
sudo systemctl start hailo-cat-tracker

# 9. Wait a moment for service to start
sleep 2

# 10. Check status
echo ""
echo "📊 Service status:"
sudo systemctl status hailo-cat-tracker --no-pager || true

echo ""
echo "✅ Installation complete!"
echo ""
echo "📋 Useful commands:"
echo "  View logs:    sudo journalctl -u hailo-cat-tracker -f"
echo "  Stop service: sudo systemctl stop hailo-cat-tracker"
echo "  Start:        sudo systemctl start hailo-cat-tracker"
echo "  Restart:      sudo systemctl restart hailo-cat-tracker"
echo "  Disable:      sudo systemctl disable hailo-cat-tracker"
echo ""
echo "🌐 Web interface: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
