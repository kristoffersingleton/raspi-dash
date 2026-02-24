# RaspiDash

A real-time "single pane of glass" monitoring dashboard for Raspberry Pi 5.

![RaspiDash](assets/raspi_dash_pony.png)

## Features

- **System** — hostname, Pi model, OS, kernel, uptime
- **CPU** — frequency, core voltage, throttle flags, load averages
- **Temperature** — CPU gauge in °C / °F
- **Memory** — RAM and swap usage bars
- **Disk** — per-mount usage bars with live I/O read/write rates
- **Network** — per-interface IPs and live RX/TX rates
- **Fan** — RPM readout
- **Processes** — top 15 by CPU usage
- **Services** — systemd units grouped by running / failed / inactive
- **GPIO Pin Map** — visual 40-pin header with direction, function, pull, and level
- **USB Devices** — lsusb output
- **Docker** — running containers (graceful if Docker is absent)

All data is sourced from Pi-native interfaces (`/proc`, `vcgencmd`, `pinctrl`, etc.) — no external dependencies beyond Flask.

## Requirements

- Raspberry Pi 5 running Raspberry Pi OS (Debian Bookworm/Trixie)
- Python 3.11+
- Flask (`pip install flask`)

## Install & Run

```bash
git clone https://github.com/kristoffersingleton/raspi-dash.git
cd raspi-dash
pip install -r requirements.txt
python server.py
```

Open `http://<your-pi-ip>:8766` in a browser.

## Options

```
python server.py --port 8766
```

## License

Apache 2.0
