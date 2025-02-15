# Homelab Power Manager

## Service Setup

Create the systemd service:
```bash
sudo nano /etc/systemd/system/device-controller.service
```

Start the monitoring service:
```bash
sudo systemctl start device-monitor.service
```