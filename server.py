#!/usr/bin/env python3
"""RaspiDash — single-pane monitoring dashboard for Raspberry Pi 5. Port: 8766"""

import argparse, json, os, pathlib, re, socket, subprocess, sys, threading, time
from flask import Flask, Response, request as flask_request

# (physical_pin, type, bcm_or_label)
# type: 'gpio', 'pwr', 'gnd'
PHYS_PINS = [
    (1,'pwr','3.3V'),(2,'pwr','5V'),
    (3,'gpio',2),(4,'pwr','5V'),
    (5,'gpio',3),(6,'gnd','GND'),
    (7,'gpio',4),(8,'gpio',14),
    (9,'gnd','GND'),(10,'gpio',15),
    (11,'gpio',17),(12,'gpio',18),
    (13,'gpio',27),(14,'gnd','GND'),
    (15,'gpio',22),(16,'gpio',23),
    (17,'pwr','3.3V'),(18,'gpio',24),
    (19,'gpio',10),(20,'gnd','GND'),
    (21,'gpio',9),(22,'gpio',25),
    (23,'gpio',11),(24,'gpio',8),
    (25,'gnd','GND'),(26,'gpio',7),
    (27,'gpio',0),(28,'gpio',1),
    (29,'gpio',5),(30,'gnd','GND'),
    (31,'gpio',6),(32,'gpio',12),
    (33,'gpio',13),(34,'gnd','GND'),
    (35,'gpio',19),(36,'gpio',16),
    (37,'gpio',26),(38,'gpio',20),
    (39,'gnd','GND'),(40,'gpio',21),
]


class StatsCollector:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()
        self._prev_netdev = {}
        self._prev_netdev_time = 0
        self._prev_diskstats = {}
        self._prev_diskstats_time = 0

    def _system(self):
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = 'unknown'

        try:
            with open('/proc/device-tree/model', 'r') as f:
                model = f.read().replace('\x00', '').strip()
        except Exception:
            model = 'Unknown'

        os_name = 'Unknown'
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        os_name = line.split('=', 1)[1].strip().strip('"')
                        break
        except Exception:
            pass

        kernel = 'Unknown'
        try:
            result = subprocess.run(['uname', '-r'], capture_output=True, text=True, timeout=5)
            kernel = result.stdout.strip()
        except Exception:
            pass

        uptime = 'Unknown'
        try:
            with open('/proc/uptime', 'r') as f:
                secs = float(f.read().split()[0])
            days = int(secs // 86400)
            secs %= 86400
            hours = int(secs // 3600)
            secs %= 3600
            mins = int(secs // 60)
            s = int(secs % 60)
            uptime = f'{days}d {hours}h {mins}m {s}s'
        except Exception:
            pass

        cpu_count = os.cpu_count() or 0

        return {
            'hostname': hostname,
            'model': model,
            'os': os_name,
            'kernel': kernel,
            'uptime': uptime,
            'cpu_count': cpu_count,
        }

    def _cpu(self):
        freq_mhz = None
        try:
            result = subprocess.run(['vcgencmd', 'measure_clock', 'arm'], capture_output=True, text=True, timeout=5)
            m = re.search(r'frequency\(\d+\)=(\d+)', result.stdout)
            if m:
                freq_mhz = int(m.group(1)) / 1e6
        except Exception:
            pass

        voltage = None
        try:
            result = subprocess.run(['vcgencmd', 'measure_volts', 'core'], capture_output=True, text=True, timeout=5)
            m = re.search(r'volt=([\d.]+)V', result.stdout)
            if m:
                voltage = float(m.group(1))
        except Exception:
            pass

        throttled_hex = '0x0'
        throttled_int = 0
        try:
            result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=5)
            m = re.search(r'throttled=(0x[0-9a-fA-F]+)', result.stdout)
            if m:
                throttled_hex = m.group(1)
                throttled_int = int(throttled_hex, 16)
        except Exception:
            pass

        load_avg = [0.0, 0.0, 0.0]
        try:
            with open('/proc/loadavg', 'r') as f:
                parts = f.read().split()
                load_avg = [float(parts[0]), float(parts[1]), float(parts[2])]
        except Exception:
            pass

        return {
            'freq_mhz': freq_mhz,
            'voltage': voltage,
            'throttled_hex': throttled_hex,
            'throttled_int': throttled_int,
            'under_voltage': bool(throttled_int & (1 << 0)),
            'freq_capped': bool(throttled_int & (1 << 1)),
            'throttled': bool(throttled_int & (1 << 2)),
            'soft_temp': bool(throttled_int & (1 << 3)),
            'under_voltage_occurred': bool(throttled_int & (1 << 16)),
            'freq_capped_occurred': bool(throttled_int & (1 << 17)),
            'throttled_occurred': bool(throttled_int & (1 << 18)),
            'soft_temp_occurred': bool(throttled_int & (1 << 19)),
            'load_avg': load_avg,
        }

    def _temperature(self):
        cpu_c = 0.0
        try:
            result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=5)
            m = re.search(r"temp=([\d.]+)'C", result.stdout)
            if m:
                cpu_c = float(m.group(1))
        except Exception:
            pass

        cpu_f = round(cpu_c * 9 / 5 + 32, 1)
        return {'cpu_c': cpu_c, 'cpu_f': cpu_f}

    def _memory(self):
        info = {}
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(':')
                        val = int(parts[1])
                        info[key] = val
        except Exception:
            pass

        total_kb = info.get('MemTotal', 0)
        avail_kb = info.get('MemAvailable', 0)
        free_kb = info.get('MemFree', 0)
        buffers_kb = info.get('Buffers', 0)
        cached_kb = info.get('Cached', 0) + info.get('SReclaimable', 0)
        used_kb = total_kb - avail_kb

        total_mb = total_kb / 1024
        used_mb = used_kb / 1024
        available_mb = avail_kb / 1024
        buffers_mb = buffers_kb / 1024
        cached_mb = cached_kb / 1024
        pct = (used_mb / total_mb * 100) if total_mb > 0 else 0.0

        swap_total_kb = info.get('SwapTotal', 0)
        swap_free_kb = info.get('SwapFree', 0)
        swap_used_kb = swap_total_kb - swap_free_kb
        swap_total_mb = swap_total_kb / 1024
        swap_used_mb = swap_used_kb / 1024
        swap_pct = (swap_used_mb / swap_total_mb * 100) if swap_total_mb > 0 else 0.0

        return {
            'total_mb': total_mb,
            'used_mb': used_mb,
            'available_mb': available_mb,
            'buffers_mb': buffers_mb,
            'cached_mb': cached_mb,
            'pct': pct,
            'swap_total_mb': swap_total_mb,
            'swap_used_mb': swap_used_mb,
            'swap_pct': swap_pct,
        }

    def _diskio_rates(self):
        VALID_FSTYPES = {'ext4','ext3','ext2','vfat','fat32','exfat','f2fs','xfs','btrfs','ntfs','fuseblk'}
        now = time.time()
        current = {}
        try:
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    devname = parts[2]
                    if re.match(r'^(ram|loop|zram)', devname):
                        continue
                    sectors_read = int(parts[5])
                    sectors_written = int(parts[9])
                    current[devname] = (sectors_read, sectors_written)
        except Exception:
            pass

        rates = {}
        dt = now - self._prev_diskstats_time if self._prev_diskstats_time > 0 else 0
        if dt > 0 and self._prev_diskstats:
            for devname, (sr, sw) in current.items():
                if devname in self._prev_diskstats:
                    prev_sr, prev_sw = self._prev_diskstats[devname]
                    read_sectors = max(0, sr - prev_sr)
                    write_sectors = max(0, sw - prev_sw)
                    read_kbs = (read_sectors * 512) / 1024 / dt
                    write_kbs = (write_sectors * 512) / 1024 / dt
                    rates[devname] = {'read_kbs': read_kbs, 'write_kbs': write_kbs}
                else:
                    rates[devname] = {'read_kbs': 0.0, 'write_kbs': 0.0}

        self._prev_diskstats = current
        self._prev_diskstats_time = now
        return rates

    def _disk(self):
        VALID_FSTYPES = {'ext4','ext3','ext2','vfat','fat32','exfat','f2fs','xfs','btrfs','ntfs','fuseblk'}
        io_rates = self._diskio_rates()
        disks = []
        try:
            result = subprocess.run(['df', '-T', '-k'], capture_output=True, text=True, timeout=10)
            lines = result.stdout.strip().split('\n')
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 7:
                    continue
                source = parts[0]
                fstype = parts[1]
                total_kb = int(parts[2])
                used_kb = int(parts[3])
                avail_kb = int(parts[4])
                pct_str = parts[5].replace('%', '')
                mount = parts[6]

                if fstype not in VALID_FSTYPES:
                    continue

                pct = float(pct_str) if pct_str.isdigit() else 0.0
                total_gb = total_kb / 1024 / 1024
                used_gb = used_kb / 1024 / 1024
                avail_gb = avail_kb / 1024 / 1024

                # Match mount source to diskstats device name
                # e.g. /dev/mmcblk0p2 -> mmcblk0, /dev/sda1 -> sda, /dev/nvme0n1p1 -> nvme0n1
                dev_base = os.path.basename(source)
                matched_dev = None
                # Try exact match first
                if dev_base in io_rates:
                    matched_dev = dev_base
                else:
                    # Strip partition suffix: mmcblk0p2 -> mmcblk0, sda1 -> sda, nvme0n1p1 -> nvme0n1
                    m = re.match(r'^(nvme\d+n\d+)p\d+$', dev_base)
                    if m:
                        matched_dev = m.group(1)
                    else:
                        m = re.match(r'^(mmcblk\d+)p\d+$', dev_base)
                        if m:
                            matched_dev = m.group(1)
                        else:
                            m = re.match(r'^([a-z]+)\d+$', dev_base)
                            if m:
                                matched_dev = m.group(1)

                read_kbs = 0.0
                write_kbs = 0.0
                if matched_dev and matched_dev in io_rates:
                    read_kbs = io_rates[matched_dev]['read_kbs']
                    write_kbs = io_rates[matched_dev]['write_kbs']

                disks.append({
                    'mount': mount,
                    'fstype': fstype,
                    'total_gb': total_gb,
                    'used_gb': used_gb,
                    'avail_gb': avail_gb,
                    'pct': pct,
                    'read_kbs': read_kbs,
                    'write_kbs': write_kbs,
                })
        except Exception:
            pass

        return disks

    def _network(self):
        now = time.time()
        current_dev = {}
        try:
            with open('/proc/net/dev', 'r') as f:
                for line in f.readlines()[2:]:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    iface = parts[0].rstrip(':')
                    rx_bytes = int(parts[1])
                    tx_bytes = int(parts[9])
                    current_dev[iface] = (rx_bytes, tx_bytes)
        except Exception:
            pass

        dt = now - self._prev_netdev_time if self._prev_netdev_time > 0 else 0
        rates = {}
        if dt > 0 and self._prev_netdev:
            for iface, (rx, tx) in current_dev.items():
                if iface in self._prev_netdev:
                    prev_rx, prev_tx = self._prev_netdev[iface]
                    rx_kbs = max(0, rx - prev_rx) / 1024 / dt
                    tx_kbs = max(0, tx - prev_tx) / 1024 / dt
                    rates[iface] = {'rx_kbs': rx_kbs, 'tx_kbs': tx_kbs}
                else:
                    rates[iface] = {'rx_kbs': 0.0, 'tx_kbs': 0.0}

        self._prev_netdev = current_dev
        self._prev_netdev_time = now

        # Get IP addresses and link state
        ip_info = {}
        try:
            result = subprocess.run(['ip', '-j', 'addr'], capture_output=True, text=True, timeout=5)
            ifaces = json.loads(result.stdout)
            for iface_data in ifaces:
                name = iface_data.get('ifname', '')
                flags = iface_data.get('flags', [])
                up = 'UP' in flags
                ips = []
                for addr in iface_data.get('addr_info', []):
                    local = addr.get('local', '')
                    if local:
                        prefix = addr.get('prefixlen', '')
                        ips.append(f'{local}/{prefix}')
                ip_info[name] = {'ips': ips, 'up': up}
        except Exception:
            pass

        result_list = []
        for iface, (rx_bytes, tx_bytes) in current_dev.items():
            if iface == 'lo':
                continue
            iface_rates = rates.get(iface, {'rx_kbs': 0.0, 'tx_kbs': 0.0})
            iface_ip = ip_info.get(iface, {'ips': [], 'up': False})
            result_list.append({
                'iface': iface,
                'ips': iface_ip['ips'],
                'rx_bytes_total': rx_bytes,
                'tx_bytes_total': tx_bytes,
                'rx_kbs': iface_rates['rx_kbs'],
                'tx_kbs': iface_rates['tx_kbs'],
                'up': iface_ip['up'],
            })

        return result_list

    def _fan(self):
        rpm = 0
        hwmon_idx = -1
        for i in range(6):
            path = f'/sys/class/hwmon/hwmon{i}/fan1_input'
            try:
                with open(path, 'r') as f:
                    rpm = int(f.read().strip())
                    hwmon_idx = i
                    break
            except Exception:
                continue
        return {'rpm': rpm, 'hwmon_idx': hwmon_idx}

    def _processes(self):
        procs = []
        try:
            result = subprocess.run(
                ['ps', 'aux', '--sort=-%cpu', '--no-headers', '-ww'],
                capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split('\n')
            count = 0
            for line in lines:
                if count >= 15:
                    break
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue
                user = parts[0]
                pid_str = parts[1]
                cpu_pct_str = parts[2]
                mem_pct_str = parts[3]
                rss_str = parts[5]
                command = parts[10][:80]

                # Skip the ps process itself
                if 'ps aux' in command:
                    continue

                try:
                    pid = int(pid_str)
                    cpu_pct = float(cpu_pct_str)
                    mem_pct = float(mem_pct_str)
                    rss_kb = float(rss_str)
                    rss_mb = rss_kb / 1024
                except ValueError:
                    continue

                procs.append({
                    'pid': pid,
                    'user': user,
                    'cpu_pct': cpu_pct,
                    'mem_pct': mem_pct,
                    'rss_mb': rss_mb,
                    'command': command,
                })
                count += 1
        except Exception:
            pass

        return procs

    def _services(self):
        running = []
        failed = []
        inactive = []
        try:
            result = subprocess.run(
                ['systemctl', 'list-units', '--type=service', '--all', '--no-pager', '--no-legend'],
                capture_output=True, text=True, timeout=15
            )
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 4)
                if len(parts) < 4:
                    continue
                name_raw = parts[0]
                load_state = parts[1]
                active_state = parts[2]
                sub_state = parts[3]
                description = parts[4] if len(parts) > 4 else ''

                # Filter to only loaded services
                if load_state != 'loaded':
                    continue
                if 'not-found' in load_state:
                    continue

                name = name_raw.replace('.service', '')
                entry = {
                    'name': name,
                    'sub_state': sub_state,
                    'description': description,
                }

                if active_state == 'active' and sub_state == 'running':
                    running.append(entry)
                elif active_state == 'failed' or sub_state == 'failed':
                    failed.append(entry)
                else:
                    inactive.append(entry)
        except Exception:
            pass

        return {'running': running, 'failed': failed, 'inactive': inactive}

    def _gpio(self):
        gpio_data = {}
        try:
            result = subprocess.run(['pinctrl', 'get'], capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                # Format: "  N: DIR [DRIVE] PULL | LEVEL // COMMENT"
                # e.g. " 2: ip        pu | hi // ID_SD/ID_SC"
                m = re.match(r'^\s*(\d+):\s+(\S+)\s+(.*)', line)
                if not m:
                    continue
                bcm_num = int(m.group(1))
                if bcm_num > 27:
                    continue
                dir_raw = m.group(2)
                rest = m.group(3)

                # Determine direction
                if dir_raw == 'ip':
                    direction = 'input'
                elif dir_raw == 'op':
                    direction = 'output'
                elif re.match(r'^a\d+$', dir_raw):
                    direction = 'alt'
                elif dir_raw == 'no':
                    direction = 'none'
                else:
                    direction = 'none'

                alt_num = None
                if direction == 'alt':
                    m2 = re.match(r'^a(\d+)$', dir_raw)
                    if m2:
                        alt_num = int(m2.group(1))

                # Parse pull from rest: "pu", "pd", "pn"
                pull = 'none'
                m3 = re.search(r'\b(pu|pd|pn)\b', rest)
                if m3:
                    p = m3.group(1)
                    if p == 'pu':
                        pull = 'up'
                    elif p == 'pd':
                        pull = 'down'
                    else:
                        pull = 'none'

                # Parse level
                level = '--'
                m4 = re.search(r'\|\s*(hi|lo)\b', rest)
                if m4:
                    level = m4.group(1)

                # Parse label from comment after //
                label = ''
                m5 = re.search(r'//\s*(.*)', rest)
                if m5:
                    comment = m5.group(1).strip()
                    # Extract label after '=' if present, else use comment
                    m6 = re.search(r'=(\S+)', comment)
                    if m6:
                        lbl = m6.group(1).strip()
                        if lbl.lower() not in ('input', 'output', 'none', ''):
                            label = lbl
                    elif comment.lower() not in ('input', 'output', 'none', ''):
                        label = comment

                gpio_data[bcm_num] = {
                    'bcm': bcm_num,
                    'dir_raw': dir_raw,
                    'direction': direction,
                    'pull': pull,
                    'level': level,
                    'label': label,
                    'alt_num': alt_num,
                }
        except Exception:
            pass

        # Build ordered list matching PHYS_PINS
        pins_list = []
        for phys, ptype, bcm_or_label in PHYS_PINS:
            if ptype == 'gpio':
                bcm = bcm_or_label
                gpio = gpio_data.get(bcm)
                pins_list.append({
                    'phys': phys,
                    'type': 'gpio',
                    'bcm': bcm,
                    'gpio': gpio,
                })
            else:
                pins_list.append({
                    'phys': phys,
                    'type': ptype,
                    'label': bcm_or_label,
                    'gpio': None,
                })

        return pins_list

    def _usb(self):
        devices = []
        try:
            result = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                # Skip root hubs
                if 'Linux Foundation' in line:
                    continue
                # Strip "Bus XXX Device XXX: " prefix
                m = re.sub(r'^Bus\s+\d+\s+Device\s+\d+:\s+', '', line.strip())
                devices.append(m)
        except Exception:
            pass
        return devices

    def _docker(self):
        try:
            result = subprocess.run(['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return {'available': False, 'containers': []}
            containers = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) < 4:
                    continue
                containers.append({
                    'name': parts[0],
                    'image': parts[1],
                    'status': parts[2],
                    'state': parts[3],
                })
            return {'available': True, 'containers': containers}
        except FileNotFoundError:
            return {'available': False, 'containers': []}
        except Exception:
            return {'available': False, 'containers': []}

    def collect(self):
        data = {'ts': time.time()}

        try:
            data['system'] = self._system()
        except Exception as e:
            data['system'] = None

        try:
            data['cpu'] = self._cpu()
        except Exception:
            data['cpu'] = None

        try:
            data['temperature'] = self._temperature()
        except Exception:
            data['temperature'] = None

        try:
            data['memory'] = self._memory()
        except Exception:
            data['memory'] = None

        try:
            data['disk'] = self._disk()
        except Exception:
            data['disk'] = []

        try:
            data['network'] = self._network()
        except Exception:
            data['network'] = []

        try:
            data['fan'] = self._fan()
        except Exception:
            data['fan'] = {'rpm': 0, 'hwmon_idx': -1}

        try:
            data['processes'] = self._processes()
        except Exception:
            data['processes'] = []

        try:
            data['services'] = self._services()
        except Exception:
            data['services'] = {'running': [], 'failed': [], 'inactive': []}

        try:
            data['gpio'] = self._gpio()
        except Exception:
            data['gpio'] = []

        try:
            data['usb'] = self._usb()
        except Exception:
            data['usb'] = []

        try:
            data['docker'] = self._docker()
        except Exception:
            data['docker'] = {'available': False, 'containers': []}

        with self._lock:
            self._data = data

    def run(self):
        while True:
            try:
                self.collect()
            except Exception:
                pass
            time.sleep(2)

    def get(self):
        with self._lock:
            return dict(self._data)


_fan_mode = 3  # default balanced

DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RaspiDash</title>
  <style>
    :root {
      --bg: #09101f;
      --card: #0f1829;
      --card-border: #1e2d45;
      --card-hover: #141e32;
      --text: #e8eef8;
      --muted: #5a7090;
      --label: #8da8c8;
      --green: #3dd68c;
      --yellow: #f5c542;
      --red: #f26b6b;
      --blue: #5ca8ff;
      --purple: #a98cfa;
      --cyan: #3dd9d9;
      --orange: #f5934e;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; min-height: 100vh; }

    .header { position: sticky; top: 0; z-index: 100; background: rgba(9,16,31,0.92); backdrop-filter: blur(12px); border-bottom: 1px solid var(--card-border); padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
    .logo { font-size: 22px; font-weight: 700; color: var(--blue); letter-spacing: -0.5px; }
    .logo span { color: var(--green); }
    .badges { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .badge { display: flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 500; background: rgba(255,255,255,0.05); border: 1px solid var(--card-border); }
    .badge.ok { background: rgba(61,214,140,0.1); border-color: rgba(61,214,140,0.3); color: var(--green); }
    .badge.warn { background: rgba(245,197,66,0.1); border-color: rgba(245,197,66,0.3); color: var(--yellow); }
    .badge.err { background: rgba(242,107,107,0.1); border-color: rgba(242,107,107,0.3); color: var(--red); }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    .dot.pulse { animation: pulse 2s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 0 0 currentColor} 50%{opacity:.7;box-shadow:0 0 6px 2px currentColor} }
    .updated { font-size: 11px; color: var(--muted); }

    .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 20px 24px; max-width: 1600px; margin: 0 auto; }
    @media (max-width: 1100px) { .grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 680px) { .grid { grid-template-columns: 1fr; } }
    .span2 { grid-column: span 2; }
    .span3 { grid-column: span 3; }
    @media (max-width: 1100px) { .span3 { grid-column: span 2; } }
    @media (max-width: 680px) { .span2, .span3 { grid-column: span 1; } }

    .card { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 18px; transition: border-color .2s, box-shadow .2s; }
    .card:hover { border-color: rgba(92,168,255,0.3); box-shadow: 0 0 20px rgba(92,168,255,0.08); }
    .card-title { font-size: 11px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }
    .card-title .icon { font-size: 14px; }

    .row { display: flex; justify-content: space-between; align-items: baseline; padding: 4px 0; border-bottom: 1px solid rgba(30,45,69,0.5); }
    .row:last-child { border-bottom: none; }
    .lbl { color: var(--label); font-size: 12px; }
    .val { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 13px; color: var(--text); }
    .val.ok { color: var(--green); }
    .val.warn { color: var(--yellow); }
    .val.err { color: var(--red); }
    .val.blue { color: var(--blue); }
    .val.purple { color: var(--purple); }
    .val.cyan { color: var(--cyan); }
    .val.orange { color: var(--orange); }
    .val.mu { color: var(--muted); }
    .big-val { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 36px; font-weight: 700; line-height: 1; }
    .big-unit { font-size: 14px; color: var(--muted); margin-left: 4px; }

    .bar-wrap { background: rgba(255,255,255,0.06); border-radius: 4px; height: 6px; margin: 8px 0 4px; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 4px; transition: width .5s; }
    .bar-fill.fill-green { background: linear-gradient(90deg, var(--green), #2ab570); }
    .bar-fill.fill-yellow { background: linear-gradient(90deg, var(--yellow), #e0a800); }
    .bar-fill.fill-red { background: linear-gradient(90deg, var(--red), #c84040); }
    .bar-fill.fill-blue { background: linear-gradient(90deg, var(--blue), #3a7fd4); }
    .bar-fill.fill-purple { background: linear-gradient(90deg, var(--purple), #7b6bc4); }

    .proc-table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .proc-table th { color: var(--muted); text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--card-border); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; }
    .proc-table td { padding: 4px 8px; border-bottom: 1px solid rgba(30,45,69,0.4); font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; }
    .proc-table tr:last-child td { border-bottom: none; }
    .proc-table tr:hover td { background: rgba(255,255,255,0.03); }
    .proc-bar { display: inline-block; height: 6px; background: var(--blue); border-radius: 3px; vertical-align: middle; margin-right: 4px; max-width: 60px; }

    .svc-section { margin-bottom: 14px; }
    .svc-section:last-child { margin-bottom: 0; }
    .svc-heading { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .8px; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
    .svc-list { display: flex; flex-wrap: wrap; gap: 6px; }
    .svc-tag { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 20px; font-size: 11px; font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; border: 1px solid; }
    .svc-tag.running { background: rgba(61,214,140,0.08); border-color: rgba(61,214,140,0.25); color: var(--green); }
    .svc-tag.failed { background: rgba(242,107,107,0.08); border-color: rgba(242,107,107,0.25); color: var(--red); }
    .svc-tag.inactive { background: rgba(90,112,144,0.08); border-color: rgba(90,112,144,0.2); color: var(--muted); }

    .gpio-table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .gpio-table td { padding: 3px 6px; }
    .gpio-table tr:hover td { background: rgba(255,255,255,0.02); }
    .pin-num { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 11px; color: var(--muted); width: 28px; text-align: right; }
    .pin-dot { width: 16px; height: 16px; border-radius: 50%; display: inline-block; vertical-align: middle; margin: 0 4px; border: 2px solid; }
    .pin-dot.pwr { background: rgba(245,147,78,0.3); border-color: var(--orange); }
    .pin-dot.gnd { background: rgba(90,112,144,0.2); border-color: var(--muted); }
    .pin-dot.gpio-input { background: rgba(61,214,140,0.2); border-color: var(--green); }
    .pin-dot.gpio-output { background: rgba(92,168,255,0.2); border-color: var(--blue); }
    .pin-dot.gpio-alt { background: rgba(169,140,250,0.2); border-color: var(--purple); }
    .pin-dot.gpio-none { background: rgba(90,112,144,0.1); border-color: rgba(90,112,144,0.3); }
    .pin-label { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 11px; }
    .pin-label.pwr { color: var(--orange); }
    .pin-label.gnd { color: var(--muted); }
    .pin-label.gpio-input { color: var(--green); }
    .pin-label.gpio-output { color: var(--blue); }
    .pin-label.gpio-alt { color: var(--purple); }
    .pin-label.gpio-none { color: var(--muted); font-style: italic; }
    .pin-level { width: 8px; height: 8px; border-radius: 50%; display: inline-block; vertical-align: middle; margin-left: 4px; }
    .pin-level.hi { background: var(--green); box-shadow: 0 0 4px var(--green); }
    .pin-level.lo { background: var(--muted); }
    .pin-level.na { background: transparent; }
    .gpio-divider { width: 2px; background: var(--card-border); }

    .fan-modes { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
    .fan-btn { padding: 4px 10px; border-radius: 6px; border: 1px solid var(--card-border); background: transparent; color: var(--text); font-size: 11px; cursor: pointer; transition: all .15s; }
    .fan-btn:hover { border-color: var(--blue); color: var(--blue); }
    .fan-btn.active { background: rgba(92,168,255,0.15); border-color: var(--blue); color: var(--blue); }

    .footer { text-align: center; padding: 16px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--card-border); margin-top: 8px; }

    .iface-up { color: var(--green); }
    .iface-down { color: var(--red); }
    .net-rate { font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: 12px; }
  </style>
</head>
<body>
  <header class="header">
    <div class="logo" style="display:flex;align-items:center;gap:10px"><img src="/logo.png" width="40" height="40" style="border-radius:50%">Raspi<span>Dash</span></div>
    <div class="badges">
      <div id="tempBadge" class="badge ok"><span class="dot pulse"></span>--&#176;C</div>
      <div id="throttleBadge" class="badge ok"><span class="dot pulse"></span>Nominal</div>
      <div class="badge"><span id="updated" class="updated">Loading...</span></div>
    </div>
  </header>
  <div class="grid" id="grid">
    <div style="color:var(--muted);padding:40px;grid-column:span 3;text-align:center">Collecting data...</div>
  </div>
  <div class="footer">RaspiDash &middot; Auto-refreshes every 2s &middot; <span id="pollCount"></span></div>
  <script>
    let _polls = 0;
    let _fanMode = 3;
    let _startTime = Date.now();

    function fmtBytes(b, decimals) {
      if (decimals === undefined) decimals = 1;
      if (b === null || b === undefined) return 'N/A';
      const units = ['B','KB','MB','GB','TB'];
      let i = 0;
      while (b >= 1024 && i < units.length-1) { b /= 1024; i++; }
      return b.toFixed(decimals) + ' ' + units[i];
    }

    function fmtRate(kbs) {
      if (kbs === null || kbs === undefined || kbs < 0) return '0 KB/s';
      if (kbs >= 1024) return (kbs/1024).toFixed(1) + ' MB/s';
      return kbs.toFixed(0) + ' KB/s';
    }

    function barCls(pct) {
      if (pct >= 90) return 'fill-red';
      if (pct >= 70) return 'fill-yellow';
      return 'fill-blue';
    }

    function tempCls(c) {
      if (c >= 80) return 'err';
      if (c >= 60) return 'warn';
      return 'ok';
    }

    function row(lbl, val, cls) {
      cls = cls || '';
      return '<div class="row"><span class="lbl">' + lbl + '</span><span class="val ' + cls + '">' + val + '</span></div>';
    }

    function barHtml(pct, cls) {
      return '<div class="bar-wrap"><div class="bar-fill ' + cls + '" style="width:' + Math.min(pct,100).toFixed(1) + '%"></div></div>';
    }

    function cardSystem(s) {
      if (!s) return '';
      return '<div class="card">' +
        '<div class="card-title"><span class="icon">&#x1F5A5;</span> System</div>' +
        row('Hostname', s.hostname, 'blue') +
        row('Model', s.model) +
        row('OS', s.os) +
        row('Kernel', s.kernel, 'mu') +
        row('Uptime', s.uptime, 'cyan') +
        row('CPU Cores', s.cpu_count) +
        '</div>';
    }

    function cardCpu(c) {
      if (!c) return '';
      const flags = [];
      if (c.under_voltage) flags.push('<span style="color:var(--red);font-size:11px">&#x26A1; Under-voltage</span>');
      if (c.freq_capped) flags.push('<span style="color:var(--yellow);font-size:11px">&#x26A0; Freq capped</span>');
      if (c.throttled) flags.push('<span style="color:var(--red);font-size:11px">&#x1F525; Throttled</span>');
      if (c.soft_temp) flags.push('<span style="color:var(--yellow);font-size:11px">&#x1F321; Soft temp limit</span>');
      const throttleHtml = flags.length ? flags.join(' ') : '<span class="val ok">&#x2713; OK</span>';
      const loadCls = c.load_avg[0] > 3 ? 'err' : c.load_avg[0] > 1.5 ? 'warn' : 'ok';
      let hist = '';
      if (c.under_voltage_occurred || c.throttled_occurred || c.freq_capped_occurred || c.soft_temp_occurred) {
        const parts = [];
        if (c.under_voltage_occurred) parts.push('&#x26A1;UV');
        if (c.throttled_occurred) parts.push('&#x1F525;Thr');
        if (c.freq_capped_occurred) parts.push('&#x26A0;FC');
        if (c.soft_temp_occurred) parts.push('&#x1F321;ST');
        hist = row('Historical', parts.join(' '), 'mu');
      }
      return '<div class="card">' +
        '<div class="card-title"><span class="icon">&#x2699;</span> CPU</div>' +
        row('Frequency', c.freq_mhz ? c.freq_mhz.toFixed(0)+' MHz' : 'N/A', 'blue') +
        row('Core Voltage', c.voltage ? c.voltage.toFixed(4)+' V' : 'N/A', 'cyan') +
        row('Load 1/5/15', c.load_avg.map(function(v){return v.toFixed(2);}).join(' / '), loadCls) +
        '<div class="row"><span class="lbl">Throttle</span><span>' + throttleHtml + '</span></div>' +
        hist +
        '</div>';
    }

    function cardTemp(t) {
      if (!t) return '';
      const cls = tempCls(t.cpu_c);
      const colorMap = {ok:'var(--green)',warn:'var(--yellow)',err:'var(--red)'};
      const color = colorMap[cls] || 'var(--text)';
      const emoji = t.cpu_c < 60 ? '&#x2744; Cool' : t.cpu_c < 80 ? '&#x2668; Warm' : '&#x1F525; Hot';
      return '<div class="card">' +
        '<div class="card-title"><span class="icon">&#x1F321;</span> Temperature</div>' +
        '<div style="text-align:center;padding:12px 0 8px">' +
        '<span class="big-val" style="color:' + color + '">' + t.cpu_c.toFixed(1) + '</span><span class="big-unit">&#176;C</span>' +
        '<div style="color:var(--muted);font-size:12px;margin-top:4px">' + t.cpu_f.toFixed(1) + ' &#176;F</div>' +
        '</div>' +
        '<div style="text-align:center;font-size:11px;color:var(--muted);margin-top:8px">' + emoji + '</div>' +
        '</div>';
    }

    function cardMemory(m) {
      if (!m) return '';
      const ramCls = barCls(m.pct);
      const swapCls = barCls(m.swap_pct);
      const swapLabel = m.swap_total_mb >= 1024 ? (m.swap_total_mb/1024).toFixed(1)+' GB' : m.swap_total_mb.toFixed(0)+' MB';
      return '<div class="card span2">' +
        '<div class="card-title"><span class="icon">&#x1F4BE;</span> Memory</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">' +
        '<div>' +
        '<div style="font-size:11px;color:var(--label);margin-bottom:4px">RAM &middot; LPDDR4X</div>' +
        '<div style="font-family:monospace">' + (m.used_mb/1024).toFixed(2) + ' GB used</div>' +
        '<div style="font-size:11px;color:var(--muted)">' + (m.used_mb/1024).toFixed(2) + ' / ' + (m.total_mb/1024).toFixed(2) + ' GB</div>' +
        barHtml(m.pct, ramCls) +
        '<div style="font-size:11px;color:var(--muted)">' + m.pct.toFixed(1) + '% &middot; ' + (m.available_mb/1024).toFixed(2) + ' GB free</div>' +
        '<div style="font-size:11px;color:var(--muted);margin-top:4px">Buffers ' + (m.buffers_mb/1024).toFixed(2) + ' GB &middot; Cache ' + (m.cached_mb/1024).toFixed(2) + ' GB</div>' +
        '</div>' +
        '<div>' +
        '<div style="font-size:11px;color:var(--label);margin-bottom:4px">Swap &middot; ' + swapLabel + '</div>' +
        '<div style="font-family:monospace">' + m.swap_used_mb.toFixed(0) + ' MB used</div>' +
        '<div style="font-size:11px;color:var(--muted)">' + m.swap_used_mb.toFixed(0) + ' / ' + m.swap_total_mb.toFixed(0) + ' MB</div>' +
        barHtml(m.swap_pct, swapCls) +
        '<div style="font-size:11px;color:' + (m.swap_pct > 50 ? 'var(--yellow)' : 'var(--muted)') + '">' + m.swap_pct.toFixed(1) + '% used</div>' +
        '</div>' +
        '</div>' +
        '</div>';
    }

    function cardDisk(disks) {
      if (!disks || !disks.length) return '<div class="card"><div class="card-title"><span class="icon">&#x1F4BF;</span> Disk</div><div style="color:var(--muted)">No disk data</div></div>';
      const rows = disks.map(function(d) {
        const cls = barCls(d.pct);
        return '<div style="margin-bottom:12px">' +
          '<div style="display:flex;justify-content:space-between;font-size:12px">' +
          '<span style="color:var(--blue);font-family:monospace">' + d.mount + '</span>' +
          '<span style="color:var(--muted)">' + d.fstype + '</span>' +
          '</div>' +
          barHtml(d.pct, cls) +
          '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">' +
          '<span>' + d.used_gb.toFixed(1) + ' / ' + d.total_gb.toFixed(1) + ' GB (' + d.pct.toFixed(0) + '%)</span>' +
          '<span style="font-family:monospace">&#x2193;' + fmtRate(d.read_kbs) + ' &#x2191;' + fmtRate(d.write_kbs) + '</span>' +
          '</div>' +
          '</div>';
      }).join('');
      return '<div class="card"><div class="card-title"><span class="icon">&#x1F4BF;</span> Disk</div>' + rows + '</div>';
    }

    function cardNetwork(nets) {
      if (!nets || !nets.length) return '<div class="card span2"><div class="card-title"><span class="icon">&#x1F310;</span> Network</div><div style="color:var(--muted)">No interfaces</div></div>';
      const cards = nets.map(function(n) {
        const stateColor = n.up ? 'var(--green)' : 'var(--red)';
        const ips = n.ips.length ? n.ips.join(', ') : 'No IP';
        return '<div style="padding:10px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid var(--card-border)">' +
          '<div style="display:flex;justify-content:space-between;margin-bottom:8px">' +
          '<span style="font-weight:600;color:var(--blue);font-family:monospace">' + n.iface + '</span>' +
          '<span style="font-size:11px;color:' + stateColor + '">' + (n.up ? '&#x25CF; UP' : '&#x25CB; DOWN') + '</span>' +
          '</div>' +
          '<div style="font-size:11px;color:var(--muted);margin-bottom:8px;font-family:monospace">' + ips + '</div>' +
          '<div style="display:flex;gap:16px;font-size:12px;font-family:monospace">' +
          '<div><div style="color:var(--muted);font-size:10px">&#x2193; RX</div><div style="color:var(--green)">' + fmtRate(n.rx_kbs) + '</div><div style="font-size:10px;color:var(--muted)">' + fmtBytes(n.rx_bytes_total) + ' total</div></div>' +
          '<div><div style="color:var(--muted);font-size:10px">&#x2191; TX</div><div style="color:var(--cyan)">' + fmtRate(n.tx_kbs) + '</div><div style="font-size:10px;color:var(--muted)">' + fmtBytes(n.tx_bytes_total) + ' total</div></div>' +
          '</div>' +
          '</div>';
      }).join('');
      const gridStyle = nets.length > 1 ? 'display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px' : '';
      return '<div class="card span2"><div class="card-title"><span class="icon">&#x1F310;</span> Network</div><div style="' + gridStyle + '">' + cards + '</div></div>';
    }

    function cardFan(fan) {
      if (!fan) return '';
      const rpm = fan.rpm || 0;
      const spinning = rpm > 100;
      const emoji = spinning ? '&#x1F300;' : '&#x1F4A4;';
      const borderColor = spinning ? 'var(--green)' : 'var(--card-border)';
      const rpmColor = spinning ? 'var(--green)' : 'var(--muted)';
      const shadow = spinning ? 'box-shadow:0 0 20px rgba(61,214,140,0.3)' : '';
      return '<div class="card">' +
        '<div class="card-title"><span class="icon">&#x1F4A8;</span> Fan</div>' +
        '<div style="text-align:center;padding:8px 0">' +
        '<div style="width:80px;height:80px;border-radius:50%;border:3px solid ' + borderColor + ';display:inline-flex;align-items:center;justify-content:center;font-size:32px;margin-bottom:8px;' + shadow + '">' + emoji + '</div>' +
        '<div class="big-val" style="font-size:28px;color:' + rpmColor + '">' + rpm + '</div>' +
        '<div style="font-size:12px;color:var(--muted);margin-top:4px">' + (spinning ? 'RPM' : 'Off / Idle') + '</div>' +
        '</div>' +
        '</div>';
    }

    function cardProcesses(procs) {
      if (!procs || !procs.length) return '<div class="card span3"><div class="card-title"><span class="icon">&#x1F4CA;</span> Processes</div><div style="color:var(--muted)">No data</div></div>';
      const rows = procs.map(function(p) {
        const cpuWidth = Math.min(p.cpu_pct * 2, 60);
        const cpuColor = p.cpu_pct > 50 ? 'var(--red)' : p.cpu_pct > 20 ? 'var(--yellow)' : 'var(--blue)';
        const memColor = p.mem_pct > 20 ? 'var(--yellow)' : 'var(--text)';
        return '<tr>' +
          '<td style="color:var(--muted)">' + p.pid + '</td>' +
          '<td style="color:var(--label)">' + p.user.substring(0,12) + '</td>' +
          '<td><span class="proc-bar" style="width:' + cpuWidth + 'px;background:' + cpuColor + '"></span><span style="color:' + cpuColor + '">' + p.cpu_pct.toFixed(1) + '%</span></td>' +
          '<td style="color:' + memColor + '">' + p.mem_pct.toFixed(1) + '%</td>' +
          '<td style="color:var(--muted)">' + p.rss_mb.toFixed(0) + ' MB</td>' +
          '<td style="color:var(--text);font-size:11px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + p.command.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</td>' +
          '</tr>';
      }).join('');
      return '<div class="card span3">' +
        '<div class="card-title"><span class="icon">&#x1F4CA;</span> Top Processes <span style="color:var(--muted);font-weight:400;font-size:10px">by CPU</span></div>' +
        '<div style="overflow-x:auto">' +
        '<table class="proc-table">' +
        '<thead><tr><th>PID</th><th>User</th><th>CPU%</th><th>MEM%</th><th>RSS</th><th>Command</th></tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
        '</table>' +
        '</div>' +
        '</div>';
    }

    function cardServices(svcs) {
      if (!svcs) return '';
      const runningTags = (svcs.running || []).map(function(s) {
        return '<span class="svc-tag running" title="' + s.description.replace(/"/g,'&quot;') + '"><span class="dot"></span>' + s.name + '</span>';
      }).join('');
      const failedTags = (svcs.failed || []).map(function(s) {
        return '<span class="svc-tag failed" title="' + s.description.replace(/"/g,'&quot;') + '"><span class="dot"></span>' + s.name + '</span>';
      }).join('');
      const inactiveTags = (svcs.inactive || []).map(function(s) {
        return '<span class="svc-tag inactive" title="' + s.description.replace(/"/g,'&quot;') + '">' + s.name + '</span>';
      }).join('');
      const failedSection = (svcs.failed||[]).length ?
        '<div class="svc-section"><div class="svc-heading" style="color:var(--red)"><span class="dot" style="background:var(--red)"></span>Failed</div><div class="svc-list">' + failedTags + '</div></div>' : '';
      return '<div class="card span3">' +
        '<div class="card-title">' +
        '<span class="icon">&#x2699;</span> Services' +
        '<span style="margin-left:auto;display:flex;gap:10px;font-size:11px;font-weight:400">' +
        '<span style="color:var(--green)">' + (svcs.running||[]).length + ' running</span>' +
        ((svcs.failed||[]).length ? '<span style="color:var(--red)">' + svcs.failed.length + ' failed</span>' : '') +
        '<span style="color:var(--muted)">' + (svcs.inactive||[]).length + ' inactive</span>' +
        '</span>' +
        '</div>' +
        failedSection +
        '<div class="svc-section"><div class="svc-heading" style="color:var(--green)"><span class="dot" style="background:var(--green)"></span>Running</div><div class="svc-list">' + runningTags + '</div></div>' +
        '<details style="margin-top:12px"><summary style="cursor:pointer;font-size:11px;color:var(--muted);user-select:none">&#x25B6; Inactive / exited (' + (svcs.inactive||[]).length + ')</summary>' +
        '<div class="svc-list" style="margin-top:8px">' + inactiveTags + '</div></details>' +
        '</div>';
    }

    function pinDotClass(pin) {
      if (pin.type === 'pwr') return 'pwr';
      if (pin.type === 'gnd') return 'gnd';
      const dir = pin.gpio ? pin.gpio.direction : 'none';
      if (dir === 'input') return 'gpio-input';
      if (dir === 'output') return 'gpio-output';
      if (dir === 'alt') return 'gpio-alt';
      return 'gpio-none';
    }

    function pinLabelText(pin) {
      if (pin.type === 'pwr') return pin.label;
      if (pin.type === 'gnd') return 'GND';
      const g = pin.gpio;
      if (!g) return 'GPIO' + pin.bcm;
      if (g.label && g.label !== 'input' && g.label !== 'output' && g.label !== 'none') return 'BCM' + pin.bcm + ' &middot; ' + g.label;
      if (g.direction === 'output') return 'BCM' + pin.bcm + ' &rarr; OUT';
      if (g.direction === 'input') return 'BCM' + pin.bcm + ' &larr; IN';
      if (g.direction === 'alt') return 'BCM' + pin.bcm + ' &middot; ALT' + (g.alt_num !== null ? g.alt_num : '');
      return 'BCM' + pin.bcm;
    }

    function pinLevelDot(pin) {
      if (!pin.gpio || pin.type !== 'gpio') return '';
      const lvl = pin.gpio.level;
      if (lvl === 'hi') return '<span class="pin-level hi" title="HIGH"></span>';
      if (lvl === 'lo') return '<span class="pin-level lo" title="LOW"></span>';
      return '<span class="pin-level na"></span>';
    }

    function cardGpio(pins) {
      if (!pins || !pins.length) return '<div class="card span3"><div class="card-title"><span class="icon">&#x1F4CC;</span> GPIO Header</div><div style="color:var(--muted)">GPIO data unavailable (pinctrl not found)</div></div>';
      let tableRows = '';
      for (let i = 0; i < 40; i += 2) {
        const left = pins[i];
        const right = pins[i+1];
        if (!left || !right) continue;
        const leftDotCls = pinDotClass(left);
        const rightDotCls = pinDotClass(right);
        const leftLblCls = left.type === 'pwr' ? 'pwr' : left.type === 'gnd' ? 'gnd' : (left.gpio ? 'gpio-' + left.gpio.direction : 'gpio-none');
        const rightLblCls = right.type === 'pwr' ? 'pwr' : right.type === 'gnd' ? 'gnd' : (right.gpio ? 'gpio-' + right.gpio.direction : 'gpio-none');
        tableRows += '<tr>' +
          '<td style="text-align:right"><span class="pin-label ' + leftLblCls + '">' + pinLabelText(left) + '</span>' + pinLevelDot(left) + '</td>' +
          '<td style="text-align:right" class="pin-num">' + left.phys + '</td>' +
          '<td><span class="pin-dot ' + leftDotCls + '"></span></td>' +
          '<td class="gpio-divider">&nbsp;</td>' +
          '<td><span class="pin-dot ' + rightDotCls + '"></span></td>' +
          '<td class="pin-num">' + right.phys + '</td>' +
          '<td><span class="pin-label ' + rightLblCls + '">' + pinLabelText(right) + '</span>' + pinLevelDot(right) + '</td>' +
          '</tr>';
      }
      const legend = '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:11px">' +
        '<span><span class="pin-dot gpio-output" style="display:inline-block"></span> <span style="color:var(--blue)">Output</span></span>' +
        '<span><span class="pin-dot gpio-input" style="display:inline-block"></span> <span style="color:var(--green)">Input</span></span>' +
        '<span><span class="pin-dot gpio-alt" style="display:inline-block"></span> <span style="color:var(--purple)">Alt Function</span></span>' +
        '<span><span class="pin-dot pwr" style="display:inline-block"></span> <span style="color:var(--orange)">Power</span></span>' +
        '<span><span class="pin-dot gnd" style="display:inline-block"></span> <span style="color:var(--muted)">GND</span></span>' +
        '<span><span class="pin-level hi" style="display:inline-block"></span> <span style="color:var(--muted)">HI</span></span>' +
        '<span><span class="pin-level lo" style="display:inline-block"></span> <span style="color:var(--muted)">LO</span></span>' +
        '</div>';
      return '<div class="card span3">' +
        '<div class="card-title"><span class="icon">&#x1F4CC;</span> GPIO 40-Pin Header</div>' +
        '<div style="overflow-x:auto"><table class="gpio-table"><tbody>' + tableRows + '</tbody></table></div>' +
        legend +
        '</div>';
    }

    function cardUsb(usb) {
      const items = usb || [];
      if (!items.length) return '<div class="card"><div class="card-title"><span class="icon">&#x1F50C;</span> USB Devices</div><div style="color:var(--muted)">No USB devices</div></div>';
      const list = items.map(function(d) {
        return '<div style="padding:4px 0;border-bottom:1px solid rgba(30,45,69,0.4);font-size:12px;font-family:monospace">' + d.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';
      }).join('');
      return '<div class="card"><div class="card-title"><span class="icon">&#x1F50C;</span> USB Devices <span style="margin-left:auto;font-size:11px;color:var(--muted);font-weight:400">' + items.length + '</span></div>' + list + '</div>';
    }

    function cardDocker(docker) {
      if (!docker || !docker.available) {
        return '<div class="card"><div class="card-title"><span class="icon">&#x1F433;</span> Docker</div><div style="color:var(--muted);font-size:12px">Docker not available</div></div>';
      }
      const containers = docker.containers || [];
      if (!containers.length) {
        return '<div class="card"><div class="card-title"><span class="icon">&#x1F433;</span> Docker</div><div style="color:var(--muted);font-size:12px">No containers running</div></div>';
      }
      const items = containers.map(function(c) {
        const stateColor = c.state === 'running' ? 'var(--green)' : 'var(--muted)';
        return '<div style="padding:6px 0;border-bottom:1px solid rgba(30,45,69,0.4)">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<span style="color:var(--blue);font-family:monospace;font-size:12px">' + c.name + '</span>' +
          '<span style="font-size:11px;color:' + stateColor + '">&#x25CF; ' + c.state + '</span>' +
          '</div>' +
          '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + c.image + '</div>' +
          '<div style="font-size:10px;color:var(--muted);margin-top:1px">' + c.status + '</div>' +
          '</div>';
      }).join('');
      return '<div class="card"><div class="card-title"><span class="icon">&#x1F433;</span> Docker <span style="margin-left:auto;font-size:11px;color:var(--muted);font-weight:400">' + containers.length + ' container' + (containers.length !== 1 ? 's' : '') + '</span></div>' + items + '</div>';
    }

    async function refresh() {
      try {
        const res = await fetch('/api/stats');
        const d = await res.json();
        _polls++;

        const tempBadge = document.getElementById('tempBadge');
        if (d.temperature && tempBadge) {
          const t = d.temperature.cpu_c;
          const cls = t >= 80 ? 'err' : t >= 60 ? 'warn' : 'ok';
          tempBadge.className = 'badge ' + cls;
          tempBadge.innerHTML = '<span class="dot pulse"></span>' + t.toFixed(1) + '&#176;C';
        }

        const throttleBadge = document.getElementById('throttleBadge');
        if (d.cpu && throttleBadge) {
          const c = d.cpu;
          const anyThrottle = c.under_voltage || c.freq_capped || c.throttled || c.soft_temp;
          throttleBadge.className = 'badge ' + (anyThrottle ? 'warn' : 'ok');
          throttleBadge.innerHTML = anyThrottle ? '<span class="dot pulse"></span>Throttled' : '<span class="dot pulse"></span>Nominal';
        }

        document.getElementById('updated').textContent = 'Updated just now \u00b7 polls: ' + _polls;

        const pollCount = document.getElementById('pollCount');
        if (pollCount) pollCount.textContent = 'Poll #' + _polls;

        const grid = document.getElementById('grid');
        grid.innerHTML =
          cardSystem(d.system) +
          cardCpu(d.cpu) +
          cardTemp(d.temperature) +
          cardMemory(d.memory) +
          cardFan(d.fan) +
          cardDisk(d.disk) +
          cardNetwork(d.network) +
          cardProcesses(d.processes) +
          cardServices(d.services) +
          cardGpio(d.gpio) +
          cardUsb(d.usb) +
          cardDocker(d.docker);

      } catch(e) {
        console.warn('Refresh error:', e);
      }
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>'''

app = Flask(__name__)
collector = StatsCollector()


@app.route('/')
def index():
    return Response(DASHBOARD_HTML, mimetype='text/html')


@app.route('/logo.png')
def logo():
    logo_path = pathlib.Path(__file__).parent / 'assets' / 'raspi_dash_pony.png'
    if logo_path.exists():
        return Response(logo_path.read_bytes(), mimetype='image/png')
    return Response('', status=404)


@app.route('/api/stats')
def api_stats():
    data = collector.get()
    return Response(json.dumps(data), mimetype='application/json')


@app.route('/api/fan-mode', methods=['POST'])
def api_fan_mode():
    global _fan_mode
    try:
        body = flask_request.get_json(force=True)
        mode = int(body.get('mode', _fan_mode))
        _fan_mode = mode
        return Response(json.dumps({'ok': True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({'ok': False, 'error': str(e)}), mimetype='application/json', status=400)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()

    t = threading.Thread(target=collector.run, daemon=True)
    t.start()
    time.sleep(1)

    app.run(host='0.0.0.0', port=args.port, threaded=True)
