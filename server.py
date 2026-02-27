#!/usr/bin/env python3
"""RaspiDash — single-pane monitoring dashboard for Raspberry Pi 5. Port: 8766"""

import argparse, json, os, pathlib, re, socket, subprocess, sys, threading, time
from flask import Flask, Response, render_template, request as flask_request

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
        self._prev_cpu_stat = {}
        self._prev_cpu_stat_time = 0

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

        ntp_synced = False
        timezone = 'Unknown'
        try:
            result = subprocess.run(
                ['timedatectl', 'show', '--property', 'NTPSynchronized', '--property', 'Timezone'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split('\n'):
                if line.startswith('NTPSynchronized='):
                    ntp_synced = line.split('=', 1)[1].strip().lower() == 'yes'
                elif line.startswith('Timezone='):
                    timezone = line.split('=', 1)[1].strip()
        except Exception:
            pass

        return {
            'hostname': hostname,
            'model': model,
            'os': os_name,
            'kernel': kernel,
            'uptime': uptime,
            'cpu_count': cpu_count,
            'ntp_synced': ntp_synced,
            'timezone': timezone,
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

        cores = None
        try:
            now_stat = time.time()
            current_stat = {}
            with open('/proc/stat', 'r') as f:
                for line in f:
                    m = re.match(r'^(cpu\d+)\s+(.*)', line)
                    if m:
                        core_id = m.group(1)
                        vals = list(map(int, m.group(2).split()))
                        user    = vals[0] if len(vals) > 0 else 0
                        nice    = vals[1] if len(vals) > 1 else 0
                        sys_    = vals[2] if len(vals) > 2 else 0
                        idle    = vals[3] if len(vals) > 3 else 0
                        iowait  = vals[4] if len(vals) > 4 else 0
                        irq     = vals[5] if len(vals) > 5 else 0
                        softirq = vals[6] if len(vals) > 6 else 0
                        steal   = vals[7] if len(vals) > 7 else 0
                        current_stat[core_id] = (user, nice, sys_, idle, iowait, irq, softirq, steal)
            dt = now_stat - self._prev_cpu_stat_time if self._prev_cpu_stat_time > 0 else 0
            if dt > 0 and self._prev_cpu_stat:
                cores = []
                for core_id in sorted(current_stat.keys(), key=lambda x: int(x[3:])):
                    if core_id in self._prev_cpu_stat:
                        curr = current_stat[core_id]
                        prev = self._prev_cpu_stat[core_id]
                        busy_delta = ((curr[0]-prev[0]) + (curr[1]-prev[1]) + (curr[2]-prev[2]) +
                                      (curr[5]-prev[5]) + (curr[6]-prev[6]) + (curr[7]-prev[7]))
                        idle_delta = (curr[3]-prev[3]) + (curr[4]-prev[4])
                        total_delta = busy_delta + idle_delta
                        pct = (busy_delta / total_delta * 100) if total_delta > 0 else 0.0
                        cores.append({'id': int(core_id[3:]), 'pct': round(pct, 1)})
            self._prev_cpu_stat = current_stat
            self._prev_cpu_stat_time = now_stat
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
            'cores': cores,
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

        rp1_c = None
        try:
            for i in range(10):
                try:
                    with open(f'/sys/class/hwmon/hwmon{i}/name', 'r') as f:
                        if f.read().strip() == 'rp1_adc':
                            with open(f'/sys/class/hwmon/hwmon{i}/temp1_input', 'r') as tf:
                                rp1_c = int(tf.read().strip()) / 1000.0
                            break
                except Exception:
                    continue
        except Exception:
            pass
        rp1_f = round(rp1_c * 9 / 5 + 32, 1) if rp1_c is not None else None

        return {'cpu_c': cpu_c, 'cpu_f': cpu_f, 'rp1_c': rp1_c, 'rp1_f': rp1_f}

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
        current_errors = {}
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
                    rx_errs = int(parts[3]) if len(parts) > 3 else 0
                    rx_drop = int(parts[4]) if len(parts) > 4 else 0
                    tx_errs = int(parts[11]) if len(parts) > 11 else 0
                    tx_drop = int(parts[12]) if len(parts) > 12 else 0
                    current_errors[iface] = (rx_errs, rx_drop, tx_errs, tx_drop)
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
            errs = current_errors.get(iface, (0, 0, 0, 0))
            result_list.append({
                'iface': iface,
                'ips': iface_ip['ips'],
                'rx_bytes_total': rx_bytes,
                'tx_bytes_total': tx_bytes,
                'rx_kbs': iface_rates['rx_kbs'],
                'tx_kbs': iface_rates['tx_kbs'],
                'up': iface_ip['up'],
                'rx_errors': errs[0],
                'rx_dropped': errs[1],
                'tx_errors': errs[2],
                'tx_dropped': errs[3],
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

    def _gpu(self):
        v3d_mhz = None
        hevc_mhz = None
        gpu_mem_mb = None

        try:
            result = subprocess.run(['vcgencmd', 'measure_clock', 'v3d'], capture_output=True, text=True, timeout=5)
            m = re.search(r'frequency\(\d+\)=(\d+)', result.stdout)
            if m:
                v3d_mhz = int(m.group(1)) / 1e6
        except Exception:
            pass

        try:
            result = subprocess.run(['vcgencmd', 'measure_clock', 'hevc'], capture_output=True, text=True, timeout=5)
            m = re.search(r'frequency\(\d+\)=(\d+)', result.stdout)
            if m:
                hevc_mhz = int(m.group(1)) / 1e6
        except Exception:
            pass

        try:
            result = subprocess.run(['vcgencmd', 'get_mem', 'gpu'], capture_output=True, text=True, timeout=5)
            m = re.search(r'gpu=(\d+)M', result.stdout)
            if m:
                gpu_mem_mb = int(m.group(1))
        except Exception:
            pass

        return {'v3d_mhz': v3d_mhz, 'hevc_mhz': hevc_mhz, 'gpu_mem_mb': gpu_mem_mb}

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

        try:
            data['gpu'] = self._gpu()
        except Exception:
            data['gpu'] = {}

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

_config_path = pathlib.Path(__file__).parent / 'cards_config.json'
_cards = [c for c in json.loads(_config_path.read_text()) if c.get('enabled', True)]

app = Flask(__name__)
collector = StatsCollector()


@app.route('/')
def index():
    return render_template('base.html', cards=_cards)


@app.route('/logo.png')
def logo():
    logo_path = pathlib.Path(__file__).parent / 'assets' / 'raspi_dash_pony.png'
    if logo_path.exists():
        return Response(logo_path.read_bytes(), mimetype='image/png')
    return Response('', status=404)


_FAVICON_FILES = {
    'favicon.ico': 'image/x-icon',
    'favicon-16x16.png': 'image/png',
    'favicon-32x32.png': 'image/png',
    'apple-touch-icon.png': 'image/png',
    'android-chrome-192x192.png': 'image/png',
    'android-chrome-512x512.png': 'image/png',
    'site.webmanifest': 'application/manifest+json',
}

@app.route('/favicon.ico')
@app.route('/favicon-16x16.png')
@app.route('/favicon-32x32.png')
@app.route('/apple-touch-icon.png')
@app.route('/android-chrome-192x192.png')
@app.route('/android-chrome-512x512.png')
@app.route('/site.webmanifest')
def favicon():
    from flask import request as _req
    filename = _req.path.lstrip('/')
    mime = _FAVICON_FILES.get(filename, 'application/octet-stream')
    asset_path = pathlib.Path(__file__).parent / 'assets' / filename
    if asset_path.exists():
        return Response(asset_path.read_bytes(), mimetype=mime)
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
