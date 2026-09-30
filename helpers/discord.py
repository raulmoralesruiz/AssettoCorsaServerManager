"""Discord-Webhook-Integration: Rich Embeds, Crash/Restart, Join/Leave, PB, Record, Summary."""
import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

from constants import DISCORD_FILE, SERVICE_NAME
from helpers.system import server_status

logger = logging.getLogger(__name__)

_discord_last_status = [None]
_discord_status_lock = threading.Lock()

_WEBHOOK_RE = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+/?$")


def is_valid_webhook_url(url: str) -> bool:
    """Lässt nur echte Discord-Webhook-URLs zu, um SSRF über beliebige Ziel-Hosts zu verhindern."""
    return bool(url) and bool(_WEBHOOK_RE.match(url.strip()))

# ── Embed-Farben ──────────────────────────────────────────────────────────────
_COL_GREEN  = 0x27ae60
_COL_RED    = 0xe8150c
_COL_GOLD   = 0xf1c40f
_COL_BLUE   = 0x3498db
_COL_GRAY   = 0x808080
_COL_ORANGE = 0xe67e22


# ── Config ────────────────────────────────────────────────────────────────────

def _load_discord_config() -> dict:
    if DISCORD_FILE.exists():
        try:
            return json.loads(DISCORD_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_discord_url() -> str:
    return _load_discord_config().get("url", "")


# ── Low-level senden ──────────────────────────────────────────────────────────

def _send(webhook_url: str, payload: dict, raise_on_error: bool = False):
    """Sendet beliebiges payload an einen Discord-Webhook."""
    if not is_valid_webhook_url(webhook_url):
        logger.warning("Discord-Send abgelehnt: keine gültige Discord-Webhook-URL")
        if raise_on_error:
            raise ValueError("URL de webhook de Discord no válida")
        return
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req  = urllib.request.Request(
            webhook_url, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "DiscordBot (AC-Server-Dashboard, 1.0)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        if raise_on_error:
            raise


def discord_notify(webhook_url: str, message: str, raise_on_error: bool = False):
    """Plain-text Nachricht (rückwärtskompatibel)."""
    _send(webhook_url, {"content": message}, raise_on_error)


def discord_embed(webhook_url: str, embed: dict, username: str = "Panel de AC Server",
                  raise_on_error: bool = False):
    """Sendet einen Rich Embed."""
    _send(webhook_url, {"username": username, "embeds": [embed]}, raise_on_error)


# ── Embed-Builder ─────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build(title: str, color: int, description: str = "", fields: list = None) -> dict:
    e = {
        "title":     title,
        "color":     color,
        "timestamp": _ts(),
        "footer":    {"text": "Panel de AC Server"},
    }
    if description:
        e["description"] = description
    if fields:
        e["fields"] = fields
    return e


# ── Vorgefertigte Embed-Typen ─────────────────────────────────────────────────

def embed_server_status(online: bool, status_str: str = "") -> dict:
    if online:
        return _build(f"🟢 Servidor en línea", _COL_GREEN,
                      f"`{SERVICE_NAME}` está disponible de nuevo.")
    return _build(f"🔴 Servidor fuera de línea", _COL_RED,
                  f"`{SERVICE_NAME}` no está disponible." +
                  (f"\n**Estado:** {status_str}" if status_str else ""))


def embed_join(driver: str, car: str) -> dict:
    return _build("🟢 Piloto conectado", _COL_GREEN, fields=[
        {"name": "Piloto", "value": f"**{esc(driver)}**", "inline": True},
        {"name": "Auto",   "value": esc(car) or "—",       "inline": True},
    ])


def embed_leave(driver: str) -> dict:
    return _build("🔴 Piloto desconectado", _COL_GRAY,
                  f"**{esc(driver)}** ha salido del servidor.")


def embed_pb(driver: str, car: str, track: str, laptime_ms: int,
             prev_ms: int | None, cuts: int = 0) -> dict:
    fields = [
        {"name": "🏎️ Piloto",  "value": f"**{esc(driver)}**", "inline": True},
        {"name": "⏱️ Nuevo PB", "value": _fmt_ms(laptime_ms),  "inline": True},
    ]
    if prev_ms:
        delta = laptime_ms - prev_ms
        s, ms = abs(delta) // 1000, abs(delta) % 1000
        sign  = "-" if delta < 0 else "+"
        fields.append({"name": "📉 Mejora", "value": f"{sign}{s}.{ms:03d}s", "inline": True})
    fields += [
        {"name": "🚗 Auto",    "value": esc(car) or "—",   "inline": True},
        {"name": "🗺️ Pista", "value": esc(track) or "—", "inline": True},
    ]
    if cuts > 0:
        fields.append({"name": "⚠️ Cortes", "value": str(cuts), "inline": True})
    title = "⏱️ ¡Nuevo récord personal!" if cuts == 0 else f"⏱️ ¡Nuevo récord personal! ({cuts} cuts)"
    return _build(title, _COL_BLUE, fields=fields)


def embed_record(driver: str, car: str, track: str, laptime_ms: int,
                 prev_ms: int | None) -> dict:
    fields = [
        {"name": "🥇 Piloto",       "value": f"**{esc(driver)}**", "inline": True},
        {"name": "🏆 Nueva vuelta rápida", "value": _fmt_ms(laptime_ms), "inline": True},
    ]
    if prev_ms:
        delta = laptime_ms - prev_ms
        s, ms = abs(delta) // 1000, abs(delta) % 1000
        fields.append({"name": "📉 Mejorado en", "value": f"-{s}.{ms:03d}s", "inline": True})
    fields += [
        {"name": "🚗 Auto",    "value": esc(car) or "—",   "inline": True},
        {"name": "🗺️ Pista", "value": esc(track) or "—", "inline": True},
    ]
    return _build("🏆 ¡Nuevo récord de pista!", _COL_GOLD, fields=fields)


def embed_summary(today_entries: list, track: str) -> dict:
    if not today_entries:
        return _build("📊 Resumen del día", _COL_BLUE,
                      "Aún no hay vueltas hoy.")
    drivers     = {e.get("driver", "") for e in today_entries if e.get("driver")}
    best_entry  = min(today_entries, key=lambda e: e.get("laptime", 99999999))
    best_driver = best_entry.get("driver", "?")
    best_time   = _fmt_ms(best_entry.get("laptime", 0))
    clean_laps  = sum(1 for e in today_entries if e.get("cuts", 0) == 0)
    clean_pct   = round(clean_laps / len(today_entries) * 100) if today_entries else 0

    # Top 5 Bestzeiten
    seen: set = set()
    top5 = []
    for e in sorted(today_entries, key=lambda x: x.get("laptime", 99999999)):
        d = e.get("driver", "?")
        if d not in seen:
            seen.add(d)
            top5.append(e)
        if len(top5) == 5:
            break

    top5_str = "\n".join(
        f"**{i+1}.** {esc(e.get('driver','?'))} — {_fmt_ms(e.get('laptime',0))}"
        for i, e in enumerate(top5)
    )

    return _build("📊 Resumen del día", _COL_BLUE,
                  f"**{track}**" if track else "", fields=[
        {"name": "🏎️ Piloto",         "value": str(len(drivers)),           "inline": True},
        {"name": "📋 Vueltas",          "value": str(len(today_entries)),     "inline": True},
        {"name": "🧹 Limpieza",      "value": f"{clean_pct}% limpias",      "inline": True},
        {"name": "🥇 Mejor tiempo del día",  "value": f"{best_driver}: {best_time}", "inline": False},
        {"name": "🏆 Top 5",           "value": top5_str or "—",            "inline": False},
    ])


def _fmt_ms(ms: int) -> str:
    if not ms:
        return "—"
    mins = ms // 60000
    secs = (ms % 60000) / 1000
    return f"{mins}:{secs:06.3f}"


def esc(s: str) -> str:
    return str(s).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


# ── Discord-Monitor (Crash/Restart) ───────────────────────────────────────────

def _discord_monitor():
    while True:
        time.sleep(30)
        try:
            dcfg = _load_discord_config()
            url  = dcfg.get("url", "")
            if not url or dcfg.get("notify_crash") is False:
                with _discord_status_lock:
                    _discord_last_status[0] = server_status()
                continue
            current = server_status()
            with _discord_status_lock:
                prev = _discord_last_status[0]
                _discord_last_status[0] = current
            if prev is not None and prev == "active" and current in ("failed", "inactive"):
                discord_embed(url, embed_server_status(False, current))
            elif prev is not None and prev in ("failed", "inactive") and current == "active":
                discord_embed(url, embed_server_status(True))
        except Exception:
            logger.exception("_discord_monitor: Fehler im Überwachungs-Loop")


def start_discord_monitor():
    from helpers.threads import supervised
    supervised(_discord_monitor, name="discord-monitor", restart_delay=10.0)
