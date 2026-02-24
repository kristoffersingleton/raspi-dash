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

Raspberry Pi OS (Bookworm and later) enforces PEP 668 — it blocks `pip install` outside of a virtual environment to protect system Python packages. Create a venv first:

```bash
git clone https://github.com/kristoffersingleton/raspi-dash.git
cd raspi-dash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py
```

If you're using an existing venv (e.g. shared across Pi projects), just activate it before the `pip install` step. Remember to point the `ExecStart` path in your systemd unit to the venv's Python binary.

Open `http://<your-pi-ip>:8766` in a browser.

## Options

```
python server.py --port 8766
```

## Customization

Cards are driven by two files: `cards_config.json` controls which cards are active and their order; `templates/cards/` holds one HTML file per card.

### Reordering or disabling cards

Edit `cards_config.json` — change the order of entries or set `"enabled": false`:

```json
[
  { "key": "system",      "fn": "cardSystem",  "template": "system",  "enabled": true },
  { "key": "temperature", "fn": "cardTemp",    "template": "temperature", "enabled": true },
  { "key": "cpu",         "fn": "cardCpu",     "template": "cpu",     "enabled": true },
  { "key": "fan",         "fn": "cardFan",     "template": "fan",     "enabled": false },
  ...
]
```

Restart the server — no code changes needed.

### Adding a new card

**1. Add a backend data source** — add a key to `StatsCollector.collect()` in `server.py`:

```python
data['uptime_raw'] = float(open('/proc/uptime').read().split()[0])
```

It's included in `/api/stats` automatically.

**2. Create a card template** — add `templates/cards/uptime_raw.html`:

```html
<script>
  function cardUptimeRaw(val) {
    return '<div class="card">' +
      '<div class="card-title"><span class="icon">&#x23F1;</span> Uptime Raw</div>' +
      row('Seconds', val, 'cyan') +
      '</div>';
  }
</script>
```

Use `span2` or `span3` on the outer div to make it wider.

**3. Register it in `cards_config.json`:**

```json
{ "key": "uptime_raw", "fn": "cardUptimeRaw", "template": "uptime_raw", "enabled": true }
```

That's it — position it anywhere in the list to control where it appears in the grid.

## Running as a service

To have RaspiDash start automatically on boot, create a systemd service unit.

**1. Create the service file:**

```bash
sudo nano /etc/systemd/system/raspidash.service
```

```ini
[Unit]
Description=RaspiDash monitoring dashboard
After=network.target

[Service]
ExecStart=/path/to/your/venv/bin/python /home/pi/raspi-dash/server.py
WorkingDirectory=/home/pi/raspi-dash
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```

Replace `/path/to/your/venv` with your Python environment and adjust the `User` and paths to match your setup.

**2. Enable and start it:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable raspidash
sudo systemctl start raspidash
```

**3. Check status:**

```bash
sudo systemctl status raspidash
```

The dashboard will now start on every boot and restart automatically if it crashes.

## License

Apache 2.0
