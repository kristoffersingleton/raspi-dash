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

## Customization

### Reordering cards

Cards are assembled in `server.py` around line 1222. Change the order of the function calls:

```js
grid.innerHTML =
  cardSystem(d.system) +
  cardTemp(d.temperature) +   // moved up
  cardCpu(d.cpu) +            // moved down
  cardMemory(d.memory) +
  ...
```

### Adding a new card

**1. Add a backend data source** — add a key to the `StatsCollector.collect()` return dict in `server.py`:

```python
def collect(self):
    ...
    return {
        ...
        'uptime_raw': open('/proc/uptime').read().split()[0],  # new key
    }
```

**2. Expose it from `/api/stats`** — it's included automatically since the whole dict is returned as JSON.

**3. Write a JS card function** — add it alongside the other `cardXxx` functions in the `<script>` block:

```js
function cardUptimeRaw(val) {
  return '<div class="card">' +
    '<div class="card-title"><span class="icon">&#x23F1;</span> Uptime Raw</div>' +
    row('Seconds', val, 'cyan') +
    '</div>';
}
```

Use `span2` or `span3` on the outer div to make it wider:
```js
'<div class="card span2">'
```

**4. Add it to the grid** — append the call inside `grid.innerHTML`:

```js
grid.innerHTML =
  cardSystem(d.system) +
  ...
  cardUptimeRaw(d.uptime_raw);  // new card
```

## License

Apache 2.0
