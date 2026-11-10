"""Analizadores de la salida de CellCLI (formato tabular de ``LIST ... ATTRIBUTES``).

Reglas:
  * Las columnas se separan por espacios en blanco/tabuladores; la última columna puede
    contener espacios (unidad, makeModel...), por eso se piden los atributos en un orden
    en el que solo el último puede tenerlos.
  * ``metricValue`` llega como "<número> <unidad>", con ',' como separador de miles y '.'
    como separador decimal. El número se conserva como ``Decimal`` exacto.
  * Nunca se convierte un valor desconocido en cero: se devuelve ``None`` y un motivo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

KNOWN_UNITS = {
    "%", "IO/sec", "MB/sec", "KB/sec", "us/request", "ms/request", "number", "IO requests",
    "MB", "KB", "GB", "us", "ms", "C", "F",
}

_NUM_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
_NUM_PLAIN = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


@dataclass
class HistoryRow:
    metric: str
    object_name: str
    ts_raw: str
    value_raw: str


def split_fixed(line: str, nfixed: int) -> Optional[List[str]]:
    """Divide en ``nfixed`` columnas sin espacios + resto (puede contener espacios)."""
    s = line.strip()
    if not s:
        return None
    parts = s.split(None, nfixed)
    if len(parts) < nfixed + 1:
        return None
    return parts


def parse_history_line(line: str) -> Optional[HistoryRow]:
    parts = split_fixed(line, 3)
    if parts is None:
        return None
    return HistoryRow(parts[0], parts[1], parts[2], parts[3].strip())


def parse_value(raw: str) -> Tuple[Optional[Decimal], str, Optional[str]]:
    """Devuelve (valor, unidad, motivo_error). valor None si no es numérico."""
    s = raw.strip()
    if not s:
        return None, "", "valor vacío"
    parts = s.split(None, 1)
    num = parts[0]
    unit = parts[1].strip() if len(parts) > 1 else ""
    if _NUM_THOUSANDS.match(num):
        num = num.replace(",", "")
    elif not _NUM_PLAIN.match(num):
        return None, unit, f"valor no numérico: {parts[0]!r}"
    try:
        d = Decimal(num)
    except InvalidOperation:
        return None, unit, f"valor no numérico: {parts[0]!r}"
    if not d.is_finite():
        return None, unit, f"valor no finito: {parts[0]!r}"
    return d, unit, None


def canonical(d: Decimal) -> str:
    """Representación textual exacta y estable (sin notación científica)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


@lru_cache(maxsize=65536)
def parse_timestamp(raw: str) -> Tuple[Optional[int], Optional[str]]:
    """ISO-8601 con zona -> epoch UTC (segundos). Sin zona => error (no se adivina)."""
    if not _TS.match(raw):
        return None, f"marca temporal sin zona horaria o con formato no reconocido: {raw!r}"
    s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, f"marca temporal inválida: {raw!r}"
    return int(dt.timestamp()), None


@lru_cache(maxsize=65536)
def epoch_to_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_metric_definitions(text: str) -> Tuple[Dict[str, Tuple[str, str, str]], List[Tuple[int, str]]]:
    """``name objectType metricType unit`` -> {name: (objectType, metricType, unit)}."""
    defs, bad = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = split_fixed(line, 3)
        if parts is None:
            # Definiciones sin unidad: se admiten con unidad vacía.
            p = line.split()
            if len(p) == 3:
                parts = p + [""]
            else:
                bad.append((n, line))
                continue
        defs[parts[0]] = (parts[1], parts[2], parts[3].strip())
    return defs, bad


def parse_celldisks(text: str) -> Dict[str, Tuple[str, str]]:
    out = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 2:
            out[p[0]] = (p[1], p[2] if len(p) > 2 else "")
    return out


def parse_detail(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out
