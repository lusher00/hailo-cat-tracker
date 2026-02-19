#!/bin/bash
set -e

echo "🗑️  Uninstalling Hailo Cat Tracker service..."

# Stop and disable service
echo "⏹️  Stopping service..."
sudo systemctl stop hailo-cat-tracker 2>/dev/null || true
sudo systemctl disable hailo-cat-tracker 2>/dev/null || true

# Remove service file
echo "🗑️  Removing service file..."
sudo rm -f /etc/systemd/system/hailo-cat-tracker.service

# Reload systemd
echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload

# Note: We don't remove udev rules as they might be used by other apps
echo ""
echo "✅ Service uninstalled!"
echo ""
echo "ℹ️  Note: udev rules (/etc/udev/rules.d/99-hailo.rules) were kept"
echo "   Remove manually if no longer needed: sudo rm /etc/udev/rules.d/99-hailo.rules"
echo ""
