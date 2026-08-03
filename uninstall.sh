#!/bin/bash
set -e

SERVICE_NAME="hailo-tracker"

echo "Uninstalling ${SERVICE_NAME}..."

sudo systemctl stop ${SERVICE_NAME} 2>/dev/null || true
sudo systemctl disable ${SERVICE_NAME} 2>/dev/null || true
sudo rm -f /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload

echo ""
echo "Service removed."
echo "Kept (remove manually if you no longer need them):"
echo "  /etc/udev/rules.d/99-hailo.rules   — may be used by other Hailo apps"
echo "  ./hailo-tracker.env                — your settings"
echo ""
