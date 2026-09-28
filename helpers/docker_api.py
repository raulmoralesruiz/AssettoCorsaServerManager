"""Integración con Docker: control y logs del contenedor AssettoServer.

Se usa cuando el panel corre en Docker (en vez de systemd/journalctl).
Requiere montar /var/run/docker.sock dentro del contenedor del panel.

El nombre del contenedor AC Server se toma de la variable AC_CONTAINER
(por defecto "assettoserver", que es el nombre del servicio en el compose).
"""
import os
from datetime import datetime, timezone

AC_CONTAINER = os.environ.get("AC_CONTAINER", "assettoserver")

try:
    import docker
    _client = docker.from_env()
except Exception as e:
    import logging
    logging.error('Error init docker: %s', e)
    _client = None
    _client = None


def available() -> bool:
    """True si el SDK de Docker está disponible y el socket responde."""
    if _client is None:
        return False
    try:
        _client.ping()
        return True
    except Exception as e:
        import logging
        logging.error('Error getting container %s: %s', AC_CONTAINER, e)
        return False


def _container():
    if _client is None:
        return None
    try:
        return _client.containers.get(AC_CONTAINER)
    except Exception as e:
        import logging
        logging.error('Error getting container %s: %s', AC_CONTAINER, e)
        return None


def run_action(action: str) -> tuple[bool, str]:
    """start / stop / restart del contenedor AssettoServer."""
    if action not in ("start", "stop", "restart"):
        return False, f"Acción no soportada: {action}"
    c = _container()
    if c is None:
        return False, f"Contenedor '{AC_CONTAINER}' no encontrado"
    try:
        before = c.status
        getattr(c, action)()
        if action == "start":
            return True, "Ya estaba en marcha" if before == "running" else "Arrancado"
        if action == "stop":
            return True, "Ya estaba detenido" if before != "running" else "Detenido"
        return True, "Reiniciado"
    except Exception as e:
        return False, str(e)


def status() -> str:
    """'active' / 'inactive' según el estado del contenedor."""
    c = _container()
    if c is None:
        return "unknown"
    return "active" if c.status == "running" else "inactive"


def uptime() -> str:
    """Uptime formateado a partir del StartedAt del contenedor."""
    c = _container()
    if c is None:
        return "unknown"
    try:
        started = c.attrs.get("State", {}).get("StartedAt", "")
        if not started:
            return "unknown"
        # 2026-09-25T23:05:54.123456789Z -> 2026-09-25T23:05:54
        clean = started.rstrip("Z")
        if "." in clean:
            clean = clean.split(".")[0]
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        total = max(0, int((now - dt).total_seconds()))
        d, r = divmod(total, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        parts = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        return " ".join(parts) if parts else "< 1m"
    except Exception as e:
        import logging
        logging.error('Error getting container %s: %s', AC_CONTAINER, e)
        return "unknown"


def read_logs(n: int = 300) -> str:
    """Últimas n líneas de logs del contenedor AssettoServer."""
    c = _container()
    if c is None:
        return ""
    try:
        return c.logs(tail=n).decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error: {e}"


def stream_logs():
    """Yield líneas de logs en vivo (follow)."""
    c = _container()
    if c is None:
        return
    try:
        for chunk in c.logs(stream=True, follow=True, tail=0):
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    yield line
    except Exception as e:
        import logging
        logging.error('Error getting container %s: %s', AC_CONTAINER, e)
        return
