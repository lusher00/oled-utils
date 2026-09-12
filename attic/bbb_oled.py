#!/usr/bin/env python3
import argparse
import json
import socket
import subprocess
import time

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont

I2C_ADDR = 0x3C
BATT_STATUS_PATH = "/run/batt_status.json"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
REFRESH_SEC = 1.0


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "no ip"


def get_service_status(service_name):
    try:
        out = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def get_battery_voltage():
    try:
        with open(BATT_STATUS_PATH, "r") as fh:
            data = json.load(fh)
        for key in ("voltage", "voltage_v", "v"):
            if key in data:
                return float(data[key])
    except Exception:
        pass
    return None


def connect_display(port):
    serial = i2c(port=port, address=I2C_ADDR)
    return ssd1306(serial)


def parse_args():
    p = argparse.ArgumentParser(description="OLED status display")
    p.add_argument("--service",  default="balance_bot",
                   help="systemd service name to monitor (default: balance_bot)")
    p.add_argument("--i2c-port", type=int, default=1,
                   help="I2C port number (default: 1)")
    p.add_argument("--refresh",  type=float, default=REFRESH_SEC,
                   help="refresh interval in seconds (default: 1.0)")
    return p.parse_args()


def main():
    args = parse_args()
    i2c_port    = args.i2c_port
    service_name = args.service
    refresh_sec  = args.refresh

    f_sm = ImageFont.truetype(FONT_PATH, 9)
    device = None
    beat = False

    while True:
        if device is None:
            try:
                device = connect_display(i2c_port)
            except Exception:
                time.sleep(2)
                continue

        try:
            ip = get_ip()
            svc = get_service_status(service_name)
            batt = get_battery_voltage()
            batt_str = f"{batt:.1f}V" if batt is not None else "------"
            now = time.strftime("%H:%M:%S")

            with canvas(device) as draw:
                draw.text((3, 0), f"IP:   {ip}", font=f_sm, fill="white")
                draw.text((3, 12), f"BBOT: {svc}", font=f_sm, fill="white")
                draw.text((3, 24), f"BATT: {batt_str}", font=f_sm, fill="white")
                draw.text((3, 36), f"TIME: {now}", font=f_sm, fill="white")

                if svc == "active":
                    draw.ellipse((3, 50) + (13, 60), outline="white", fill="white" if beat else "black")
                else:
                    draw.ellipse((3, 50) + (13, 60), outline="white", fill="black")
                    draw.line((3, 50, 13, 60), fill="white")
                    draw.line((3, 60, 13, 50), fill="white")

            beat = not beat
            time.sleep(refresh_sec)

        except Exception:
            # Likely an I2C hiccup -- drop and reconnect on next loop
            device = None
            time.sleep(2)


if __name__ == "__main__":
    main()