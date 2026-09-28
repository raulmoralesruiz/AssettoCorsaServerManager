import functools
import math
import re
import socket as _sock
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from constants import AC_HOST, CFG_DIR, RCON_PORT, SERVICE_NAME, TRACKS_DIR
from helpers.config_io import read_extra_cfg, read_server_cfg
from helpers import docker_api

# ── UDP live position state ───────────────────────────────────────────────────
_car_data: dict = {}
_udp_pkt        = [0]
_udp_err        = ["none"]
_udp_ready      = False
_udp_lock       = threading.Lock()
_udp_start_lock = threading.Lock()

# ── Split-Zeit Tracking ───────────────────────────────────────────────────────
import queue as _queue
_split_queue:  _queue.Queue = _queue.Queue()
_split_config: list         = []
_split_lock                 = threading.Lock()

def set_split_config(splits: list):
    global _split_config
    with _split_lock:
        _split_config = sorted(splits, key=lambda x: x["pos"])

def get_split_events() -> list:
    events = []
    while True:
        try:
            events.append(_split_queue.get_nowait())
        except _queue.Empty:
            break
    return events


def _udp_listener():
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    s.settimeout(1.0)
    try:
        s.bind(("127.0.0.1", 12000))
    except Exception as e:
        _udp_err[0] = f"bind: {e}"
        s.close()
        return
    while True:
        try:
            data, _ = s.recvfrom(512)
            if not data:
                continue
            _udp_pkt[0] += 1
            pkt, size = data[0], len(data)
            if pkt in (2, 53) and size >= 2:
                cid = data[1]
                with _udp_lock:
                    entry = dict(_car_data.get(cid, {}))
                if size >= 33:
                    try:
                        sp = struct.unpack_from("<f", data, 29)[0]
                        if 0.0 <= sp <= 1.0:
                            sp = round(sp, 4)
                            prev_sp = entry.get("spLine", -1.0)
                            now_t   = time.time()
                            if prev_sp > 0.85 and sp < 0.15:
                                entry["_lapStartTime"]    = now_t
                                entry["_passedSplits"]    = set()
                            if "_lapStartTime" not in entry:
                                entry["_lapStartTime"]  = now_t
                                entry["_passedSplits"]  = set()
                            if prev_sp >= 0.0 and sp > prev_sp:
                                with _split_lock:
                                    splits = list(_split_config)
                                passed = entry.get("_passedSplits", set())
                                for spl in splits:
                                    spos = spl["pos"]
                                    if spos not in passed and prev_sp < spos <= sp:
                                        elapsed_ms = int((now_t - entry["_lapStartTime"]) * 1000)
                                        _split_queue.put({
                                            "car_id": cid,
                                            "name":   spl["name"],
                                            "ms":     elapsed_ms,
                                        })
                                        passed.add(spos)
                                entry["_passedSplits"] = passed
                            entry["spLine"] = sp
                    except Exception:
                        pass
                if size >= 45:
                    try:
                        entry["lapTimeMs"] = struct.unpack_from("<I", data, 33)[0]
                        entry["lastLapMs"] = struct.unpack_from("<I", data, 37)[0]
                        entry["bestLapMs"] = struct.unpack_from("<I", data, 41)[0]
                    except Exception:
                        pass
                if size >= 47:
                    try:
                        entry["lapCount"] = struct.unpack_from("<H", data, 45)[0]
                    except Exception:
                        pass
                with _udp_lock:
                    _car_data[cid] = entry
            elif pkt == 4 and size >= 6:
                cid = data[1]
                lap_ms = struct.unpack_from("<I", data, 2)[0]
                with _udp_lock:
                    entry = dict(_car_data.get(cid, {}))
                if 10000 < lap_ms < 7200000:
                    entry["lastLapMs"] = lap_ms
                    if lap_ms < entry.get("bestLapMs", 99999999):
                        entry["bestLapMs"] = lap_ms
                entry["lapCount"] = entry.get("lapCount", 0) + 1
                with _udp_lock:
                    _car_data[cid] = entry
        except _sock.timeout:
            continue
        except Exception as e:
            _udp_err[0] = str(e)
            continue


def ensure_udp():
    global _udp_ready
    with _udp_start_lock:
        if not _udp_ready:
            _udp_ready = True
            threading.Thread(target=_udp_listener, daemon=True).start()


def get_car_data(car_id: int) -> dict:
    with _udp_lock:
        return dict(_car_data.get(car_id, {}))


def run_systemctl(action: str) -> tuple[bool, str]:
    if docker_api.available():
        return docker_api.run_action(action)
    try:
        r = subprocess.run(
            ["systemctl", action, SERVICE_NAME],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)

def _ac_reachable() -> bool:
    import socket
    try:
        s = socket.create_connection((AC_HOST, 8081), timeout=2)
        s.close()
        return True
    except Exception:
        return False

def server_status() -> str:
    if _ac_reachable():
        return "active"
    try:
        r = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True,
        )
        return r.stdout.strip()
    except Exception:
        return "inactive"


def maybe_restart(data: dict):
    if data.get("restart"):
        run_systemctl("restart")


def server_info():
    import urllib.request
    import urllib.error
    import json
    for url in [f"http://{AC_HOST}:8081/api/details", f"http://{AC_HOST}:8081/INFO"]:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                pass
        except Exception:
            pass
    return None


def server_json():
    import urllib.request, json
    try:
        with urllib.request.urlopen(f"http://{AC_HOST}:8081/JSON|", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


_prev_net: dict = {"sent": 0, "recv": 0, "t": 0.0}

def get_system_stats() -> dict:
    if not HAS_PSUTIL:
        return {"cpu": 0, "mem_percent": 0, "mem_used_mb": 0, "mem_total_mb": 0,
                "net_tx_kbps": 0, "net_rx_kbps": 0}
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    net = psutil.net_io_counters()
    now = time.time()
    dt  = max(now - _prev_net["t"], 0.1)
    tx  = (net.bytes_sent - _prev_net["sent"]) / dt / 1024
    rx  = (net.bytes_recv - _prev_net["recv"]) / dt / 1024
    _prev_net.update({"sent": net.bytes_sent, "recv": net.bytes_recv, "t": now})
    return {
        "cpu":          round(cpu, 1),
        "mem_percent":  round(mem.percent, 1),
        "mem_used_mb":  mem.used // (1024 * 1024),
        "mem_total_mb": mem.total // (1024 * 1024),
        "net_tx_kbps":  round(max(tx, 0), 1),
        "net_rx_kbps":  round(max(rx, 0), 1),
    }


def get_local_ip() -> str:
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ips = r.stdout.strip().split()
        return ips[0] if ips else "unknown"
    except Exception:
        return "unknown"


def get_uptime_string() -> str:
    if docker_api.available():
        return docker_api.uptime()
    try:
        r = subprocess.run(
            ["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5,
        )
        line = r.stdout.strip()
        m = re.search(r"=(.+)", line)
        if not m or not m.group(1).strip() or m.group(1).strip() == "n/a":
            return "unknown"
        ts_str = m.group(1).strip()
        for fmt in ["%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"]:
            try:
                dt = datetime.strptime(ts_str, fmt)
                now = datetime.now()
                total_seconds = max(0, int((now - dt).total_seconds()))
                days    = total_seconds // 86400
                hours   = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60
                parts = []
                if days:    parts.append(f"{days}d")
                if hours:   parts.append(f"{hours}h")
                if minutes: parts.append(f"{minutes}m")
                return " ".join(parts) if parts else "< 1m"
            except Exception:
                continue
        return "unknown"
    except Exception:
        return "unknown"


@functools.lru_cache(maxsize=8)
def load_spline_points(track: str, layout: str) -> tuple:
    ai_path = (
        TRACKS_DIR / track / layout / "ai" / "fast_lane.ai"
        if layout
        else TRACKS_DIR / track / "ai" / "fast_lane.ai"
    )
    if not ai_path.exists():
        return ()
    try:
        with open(ai_path, "rb") as f:
            data = f.read()
        if len(data) < 36:
            return ()
        count = struct.unpack_from("<i", data, 4)[0]
        if not (0 < count < 1_000_000):
            return ()
        DATA_START = 16
        STRIDE     = 20
        pts = []
        for i in range(count):
            off = DATA_START + i * STRIDE
            if off + 12 > len(data):
                break
            x, _y, z = struct.unpack_from("<fff", data, off)
            pts.append((x, z))
        if len(pts) < 10:
            return ()
        mn_x = min(p[0] for p in pts); mx_x = max(p[0] for p in pts)
        mn_z = min(p[1] for p in pts); mx_z = max(p[1] for p in pts)
        w = mx_x - mn_x or 1
        h = mx_z - mn_z or 1
        step = max(1, len(pts) // 2000)
        return tuple(
            (round((p[0] - mn_x) / w, 4), round((p[1] - mn_z) / h, 4))
            for p in pts[::step]
        )
    except Exception:
        return ()


def rcon_send(cmd: str) -> tuple[bool, str]:
    admin_pw = read_server_cfg().get("ADMIN_PASSWORD", "")
    try:
        port = int(read_extra_cfg().get("RconPort", RCON_PORT))
    except (ValueError, TypeError):
        port = RCON_PORT
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(3)
        s.connect((AC_HOST, port))
        def _pack(rid, rtype, body):
            b = body.encode("utf-8") + b"\x00\x00"
            return struct.pack("<iii", 4 + 4 + len(b), rid, rtype) + b
        def _recv():
            raw = s.recv(4)
            if len(raw) < 4:
                return ""
            sz = struct.unpack("<i", raw)[0]
            d = b""
            while len(d) < sz:
                chunk = s.recv(sz - len(d))
                if not chunk:
                    break
                d += chunk
            return d[8:].rstrip(b"\x00").decode("utf-8", errors="replace")
        s.sendall(_pack(1, 3, admin_pw)); _recv()
        s.sendall(_pack(2, 2, cmd)); resp = _recv()
        s.close()
        return True, resp or "OK"
    except Exception as e:
        return False, str(e)


def get_recent_chat(n: int = 40) -> list:
    try:
        r = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", "3000", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=5,
        )
        msgs = []
        for line in r.stdout.split("\n"):
            ts = line[1:9] if line.startswith("[") else ""
            if "CHAT:" in line:
                if re.search(r'CHAT:.*\[0-9A-Z]', line):
                    continue
                try:
                    text = line.split("CHAT: ", 1)[1].strip()
                    msgs.append({"time": ts, "text": text, "source": "player"})
                except Exception:
                    pass
            elif "RCON" in line and re.search(r'/?say (.+)', line, re.IGNORECASE):
                try:
                    m = re.search(r'/?say (.+)', line, re.IGNORECASE)
                    if m:
                        text = m.group(1).strip()
                        msgs.append({"time": ts, "text": f"(Server): {text}", "source": "server"})
                except Exception:
                    pass
        msgs.sort(key=lambda x: x.get("time", ""))
        return msgs[-n:]
    except Exception:
        return []

def read_server_logs(n: int = 300) -> str:
    logs_dir = Path('/opt/assettoserver/logs')
    try:
        files = sorted(logs_dir.glob('log-*.txt'))
        if not files:
            import os
            # Fallback debug directory contents
            dir_contents = os.listdir('/opt/assettoserver/') if os.path.exists('/opt/assettoserver/') else 'Not found'
            log_contents = os.listdir('/opt/assettoserver/logs') if os.path.exists('/opt/assettoserver/logs') else 'No logs dir'
            return f"Error: No log files found. /opt/assettoserver: {dir_contents} | /opt/assettoserver/logs: {log_contents}"
        with open(files[-1], 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
            return '\n'.join(lines[-n:])
    except Exception as e:
        import traceback
        return f"Error reading logs: {e}\n{traceback.format_exc()}"

def stream_server_logs():
    import time
    logs_dir = Path('/opt/assettoserver/logs')
    
    def get_latest():
        try:
            files = sorted(logs_dir.glob('log-*.txt'))
            return files[-1] if files else None
        except Exception:
            return None

    current = get_latest()
    f = None
    if current:
        try:
            f = open(current, 'r', encoding='utf-8', errors='replace')
            f.seek(0, 2)
        except Exception:
            pass

    while True:
        if not f:
            time.sleep(1)
            new_current = get_latest()
            if new_current:
                current = new_current
                try:
                    f = open(current, 'r', encoding='utf-8', errors='replace')
                    f.seek(0, 2)
                except Exception:
                    pass
            continue
            
        line = f.readline()
        if not line:
            new_current = get_latest()
            if new_current and new_current != current:
                f.close()
                f = None
                continue
            time.sleep(0.5)
            continue
            
        yield line.rstrip('\n')
