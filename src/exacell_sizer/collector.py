"""Acceso al script recolector incluido en el paquete."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

SCRIPT_NAME = "exacell_collect.sh"


def script_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / SCRIPT_NAME


def script_text() -> str:
    return script_path().read_text(encoding="utf-8")


def script_version() -> str:
    m = re.search(r'^COLLECTOR_VERSION="([^"]+)"', script_text(), re.M)
    return m.group(1) if m else "desconocida"


def script_sha256() -> str:
    return hashlib.sha256(script_path().read_bytes()).hexdigest()


def export_script(dest: str) -> Path:
    """Copia el script a ``dest`` (archivo o directorio). Nunca sobrescribe."""
    d = Path(dest)
    if d.is_dir():
        d = d / SCRIPT_NAME
    if d.exists():
        raise FileExistsError(f"ya existe {d}; no se sobrescribe")
    d.write_bytes(script_path().read_bytes())
    os.chmod(d, 0o755)
    return d
