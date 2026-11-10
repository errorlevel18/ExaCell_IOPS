"""Backend CellCLI simulado y generador de capturas sintéticas.

Se usa en tres sitios:
  * los ejecutables falsos ``tests/fakes/dcli`` y ``tests/fakes/ssh`` que permiten ejecutar el
    recolector Bash real sin ningún Exadata;
  * el comando ``exacell-sizer simulate`` para generar capturas de demostración;
  * pruebas unitarias.

NUNCA se conecta a sistemas reales. Los datos son deterministas (hash de la clave), de modo que
dos ejecuciones con la misma configuración producen exactamente la misma salida.

La sintaxis aceptada reproduce las restricciones observadas en el entorno probado por el usuario:
  * ``>=`` y ``<=`` no se aceptan en filtros (error CELL-01504);
  * el inicio inclusivo se expresa como ``collectionTime NOT < 'inicio' AND collectionTime < 'fin'``;
  * sin ``name = '...'`` explícito el histórico antiguo no se devuelve (se emula devolviendo vacío
    para periodos de más de ``history_without_name_hours`` horas de antigüedad).
Estas reglas NO garantizan el comportamiento de todas las versiones de CellCLI.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import Counter
from typing import Dict, List, Optional, Tuple

# (name, objectType, metricType, unit) — catálogo simulado, nombres reales de CellCLI.
SIM_CATALOG: List[Tuple[str, str, str, str]] = [
    ("CL_CPUT", "CELL", "Instantaneous", "%"),
    ("CL_MEMUT", "CELL", "Instantaneous", "%"),
    ("N_HCA_MB_RCV_SEC", "CELL", "Rate", "MB/sec"),
    ("N_HCA_MB_TRANS_SEC", "CELL", "Rate", "MB/sec"),
    ("N_NIC_KB_RCV_SEC", "CELL", "Rate", "KB/sec"),
    ("N_NIC_KB_TRANS_SEC", "CELL", "Rate", "KB/sec"),
    ("CD_IO_RQ_R_SM_SEC", "CELLDISK", "Rate", "IO/sec"),
    ("CD_IO_RQ_R_LG_SEC", "CELLDISK", "Rate", "IO/sec"),
    ("CD_IO_RQ_W_SM_SEC", "CELLDISK", "Rate", "IO/sec"),
    ("CD_IO_RQ_W_LG_SEC", "CELLDISK", "Rate", "IO/sec"),
    ("CD_IO_BY_R_SM_SEC", "CELLDISK", "Rate", "MB/sec"),
    ("CD_IO_BY_R_LG_SEC", "CELLDISK", "Rate", "MB/sec"),
    ("CD_IO_BY_W_SM_SEC", "CELLDISK", "Rate", "MB/sec"),
    ("CD_IO_BY_W_LG_SEC", "CELLDISK", "Rate", "MB/sec"),
    ("CD_IO_TM_R_SM_RQ", "CELLDISK", "Rate", "us/request"),
    ("CD_IO_TM_R_LG_RQ", "CELLDISK", "Rate", "us/request"),
    ("CD_IO_TM_W_SM_RQ", "CELLDISK", "Rate", "us/request"),
    ("CD_IO_TM_W_LG_RQ", "CELLDISK", "Rate", "us/request"),
    ("CD_IO_UTIL", "CELLDISK", "Rate", "%"),
    ("CD_IO_LOAD", "CELLDISK", "Instantaneous", "number"),
    ("CD_IO_RQ_R_SM", "CELLDISK", "Cumulative", "IO requests"),
    ("CD_IO_RQ_R_LG", "CELLDISK", "Cumulative", "IO requests"),
    ("CD_IO_RQ_W_SM", "CELLDISK", "Cumulative", "IO requests"),
    ("CD_IO_RQ_W_LG", "CELLDISK", "Cumulative", "IO requests"),
    ("FC_IO_RQ_R_SEC", "FLASHCACHE", "Rate", "IO/sec"),
    ("FC_IO_RQ_W_SEC", "FLASHCACHE", "Rate", "IO/sec"),
    ("FC_IO_BY_R_SEC", "FLASHCACHE", "Rate", "MB/sec"),
    ("FC_IO_BY_W_SEC", "FLASHCACHE", "Rate", "MB/sec"),
    ("FC_IO_RQ_R_MISS_SEC", "FLASHCACHE", "Rate", "IO/sec"),
    ("FC_IO_RQ_R", "FLASHCACHE", "Cumulative", "IO requests"),
    # Métricas que el recolector NO debe pedir en modo global (ámbito DB/PDB/IORM):
    ("DB_IO_RQ_SM_SEC", "IORM_DATABASE", "Rate", "IO/sec"),
    ("PDB_IO_RQ_SM_SEC", "IORM_PLUGGABLE_DATABASE", "Rate", "IO/sec"),
    ("CT_IO_RQ_SM_SEC", "IORM_CATEGORY", "Rate", "IO/sec"),
    ("CD_IO_RQ_R_SM_SEC_EXTRA", "CELLDISK", "Rate", "IO/sec"),  # trampa para filtros por prefijo
]

# Carga base por objeto (por disco/celda) a la que se aplica patrón diario + ruido.
BASE_RATES: Dict[str, Dict[str, float]] = {
    "HardDisk": {
        "CD_IO_RQ_R_SM_SEC": 70.0, "CD_IO_RQ_R_LG_SEC": 12.0,
        "CD_IO_RQ_W_SM_SEC": 25.0, "CD_IO_RQ_W_LG_SEC": 4.0,
        "CD_IO_TM_R_SM_RQ": 6000.0, "CD_IO_TM_R_LG_RQ": 15000.0,
        "CD_IO_TM_W_SM_RQ": 3000.0, "CD_IO_TM_W_LG_RQ": 12000.0,
        "CD_IO_UTIL": 35.0, "CD_IO_LOAD": 2.0,
    },
    "FlashDisk": {
        "CD_IO_RQ_R_SM_SEC": 9000.0, "CD_IO_RQ_R_LG_SEC": 900.0,
        "CD_IO_RQ_W_SM_SEC": 3000.0, "CD_IO_RQ_W_LG_SEC": 150.0,
        "CD_IO_TM_R_SM_RQ": 350.0, "CD_IO_TM_R_LG_RQ": 1500.0,
        "CD_IO_TM_W_SM_RQ": 250.0, "CD_IO_TM_W_LG_RQ": 1200.0,
        "CD_IO_UTIL": 20.0, "CD_IO_LOAD": 1.0,
    },
    "CELL": {"CL_CPUT": 30.0, "CL_MEMUT": 60.0, "N_NIC_KB_RCV_SEC": 800.0, "N_NIC_KB_TRANS_SEC": 600.0},
}
# Tamaños de E/S (MB) para derivar bytes a partir de peticiones.
IO_SIZE_MB = {"SM": 8.0 / 1024.0, "LG": 1.0}
NOT_SCALED = {"CD_IO_TM_R_SM_RQ", "CD_IO_TM_R_LG_RQ", "CD_IO_TM_W_SM_RQ", "CD_IO_TM_W_LG_RQ", "CL_MEMUT"}


@dataclass
class SimConfig:
    cells: List[str]
    hdd_per_cell: int = 12
    flash_per_cell: int = 4
    tz_offset: str = "+01:00"
    seed: str = "exacell"
    load_factor: float = 1.0
    cell_factors: Dict[str, float] = field(default_factory=dict)
    # picos: [{"start_minute": 120, "minutes": 30, "factor": 3.0}] (minuto del día UTC)
    spikes: List[dict] = field(default_factory=list)
    # reinicios de contadores: {"cel01": [epoch_minute, ...]}
    counter_resets: Dict[str, List[int]] = field(default_factory=dict)
    # métricas a eliminar del catálogo de una celda: {"cel01": ["CL_MEMUT"]}
    catalog_remove: Dict[str, List[str]] = field(default_factory=dict)
    # cambios de unidad en catálogo (prueba de unidades no reconocidas)
    catalog_unit_override: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Inyección de fallos (ver ``failure_for``)
    failures: Dict[str, list] = field(default_factory=dict)
    history_without_name_hours: int = 24
    make_model: str = "Oracle Corporation ORACLE SERVER X7-2L High Capacity"
    release_version: str = "19.3.12.0.0.201006"
    sec_offset: Optional[int] = None  # segundo de recogida dentro del minuto

    @classmethod
    def from_dict(cls, d: dict) -> "SimConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def from_json_file(cls, path: str) -> "SimConfig":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# --------------------------------------------------------------------------- utilidades

def _rand(*key) -> float:
    h = hashlib.sha256("|".join(str(k) for k in key).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0 ** 64


def parse_offset(off: str) -> timedelta:
    if off in ("Z", "+00:00", "-00:00"):
        return timedelta(0)
    m = re.fullmatch(r"([+-])(\d\d):(\d\d)", off)
    if not m:
        raise ValueError(f"desfase horario inválido: {off}")
    sign = 1 if m.group(1) == "+" else -1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))


def parse_cellcli_time(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def fmt_time(epoch: int, off: str) -> str:
    tz = timezone(parse_offset(off))
    t = datetime.fromtimestamp(epoch, tz)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + ("+00:00" if off == "Z" else off)


def fmt_value(v: float, unit: str) -> str:
    """Formatea como CellCLI: separador de miles ',' y punto decimal."""
    if unit in ("IO requests",):
        txt = f"{int(round(v)):,}"
    elif abs(v) >= 1000:
        txt = f"{v:,.1f}"
    else:
        txt = f"{v:.3f}"
    return f"{txt} {unit}"


# --------------------------------------------------------------------------- modelo de celda

class SimCell:
    def __init__(self, cfg: SimConfig, name: str):
        self.cfg = cfg
        self.name = name
        self.disks: List[Tuple[str, str]] = (
            [(f"CD_{i:02d}_{name}", "HardDisk") for i in range(cfg.hdd_per_cell)]
            + [(f"FD_{i:02d}_{name}", "FlashDisk") for i in range(cfg.flash_per_cell)]
        )
        self.disk_type = dict(self.disks)
        sec = cfg.sec_offset if cfg.sec_offset is not None else int(_rand(cfg.seed, name, "sec") * 50) + 5
        self.sec_offset = sec

    # ----- catálogo
    def catalog(self) -> List[Tuple[str, str, str, str]]:
        removed = set(self.cfg.catalog_remove.get(self.name, []))
        units = self.cfg.catalog_unit_override.get(self.name, {})
        out = []
        for name, ot, mt, unit in SIM_CATALOG:
            if name in removed:
                continue
            out.append((name, ot, mt, units.get(name, unit)))
        return out

    def definition(self, metric: str):
        for row in self.catalog():
            if row[0] == metric:
                return row
        return None

    def objects(self, object_type: str) -> List[str]:
        if object_type == "CELLDISK":
            return [d for d, _ in self.disks]
        if object_type == "CELL":
            return [self.name]
        if object_type == "FLASHCACHE":
            return [f"{self.name}_FLASHCACHE"]
        return []

    # ----- valores
    def _shape(self, minute: int) -> float:
        day = 2 * math.pi * (minute % 1440) / 1440.0
        f = 1.0 + 0.5 * math.sin(day)
        mod = minute % 1440
        for sp in self.cfg.spikes:
            if sp["start_minute"] <= mod < sp["start_minute"] + sp["minutes"]:
                f *= float(sp["factor"])
        return f * self.cfg.load_factor * float(self.cfg.cell_factors.get(self.name, 1.0))

    def rate(self, metric: str, obj: str, minute: int) -> float:
        noise = 0.9 + 0.2 * _rand(self.cfg.seed, self.name, metric, obj, minute)
        if metric.startswith("CD_"):
            dt = self.disk_type[obj]
            base = BASE_RATES[dt]
            if metric.startswith("CD_IO_BY_"):
                # bytes coherentes con peticiones
                kind = metric[len("CD_IO_BY_"):-len("_SEC")]  # R_SM
                rq = self.rate(f"CD_IO_RQ_{kind}_SEC", obj, minute)
                return rq * IO_SIZE_MB[kind[-2:]]
            b = base[metric]
            if metric in NOT_SCALED:
                return b * noise
            v = b * self._shape(minute) * noise
            if metric == "CD_IO_UTIL":
                return min(v, 100.0)
            return v
        if metric.startswith("FC_"):
            flash = [d for d, t in self.disks if t == "FlashDisk"]
            if metric == "FC_IO_RQ_R_SEC":
                return sum(self.rate("CD_IO_RQ_R_SM_SEC", d, minute) for d in flash) * 0.95
            if metric == "FC_IO_RQ_W_SEC":
                return sum(self.rate("CD_IO_RQ_W_SM_SEC", d, minute) for d in flash) * 0.5
            if metric == "FC_IO_BY_R_SEC":
                return sum(self.rate("CD_IO_BY_R_SM_SEC", d, minute) for d in flash) * 0.95
            if metric == "FC_IO_BY_W_SEC":
                return sum(self.rate("CD_IO_BY_W_SM_SEC", d, minute) for d in flash) * 0.5
            if metric == "FC_IO_RQ_R_MISS_SEC":
                return self.rate("FC_IO_RQ_R_SEC", obj, minute) * 0.05
        if metric.startswith("N_HCA_MB_"):
            tot = sum(self.rate("CD_IO_BY_R_SM_SEC", d, minute) + self.rate("CD_IO_BY_R_LG_SEC", d, minute)
                      for d, _ in self.disks)
            return tot * (0.25 if metric.endswith("TRANS_SEC") else 0.10)  # Smart Scan filtra en la celda
        if metric in BASE_RATES["CELL"]:
            b = BASE_RATES["CELL"][metric]
            v = b if metric in NOT_SCALED else b * self._shape(minute)
            v *= 0.9 + 0.2 * _rand(self.cfg.seed, self.name, metric, minute)
            return min(v, 100.0) if metric.startswith("CL_") else v
        raise KeyError(metric)

    def counter(self, metric: str, obj: str, minute: int) -> int:
        """Contador acumulado con forma cerrada (monótono) y reinicios configurables."""
        rate_metric = metric + "_SEC"
        base = BASE_RATES[self.disk_type[obj]][rate_metric] if metric.startswith("CD_") else 8000.0
        lf = self.cfg.load_factor * float(self.cfg.cell_factors.get(self.name, 1.0))
        period = 1440.0

        def integral(m: float) -> float:
            return 60.0 * base * lf * (m - (period / (2 * math.pi)) * 0.5 * math.cos(2 * math.pi * m / period))

        value = integral(minute) - integral(0) + 1_000_000
        resets = [r for r in self.cfg.counter_resets.get(self.name, []) if r <= minute]
        if resets:
            r = max(resets)
            value = integral(minute) - integral(r)
        return int(value)

    def history(self, metric: str, t1: int, t2: int) -> List[str]:
        d = self.definition(metric)
        if d is None:
            return []
        _, ot, mt, unit = d
        lines = []
        first_min = (t1 - self.sec_offset + 59) // 60
        m = first_min
        while True:
            ts = m * 60 + self.sec_offset
            if ts >= t2:
                break
            if ts >= t1:
                for obj in self.objects(ot):
                    if mt == "Cumulative":
                        v = float(self.counter(metric, obj, m))
                    else:
                        v = self.rate(metric, obj, m)
                    lines.append("\t" + "\t".join([metric, obj, fmt_time(ts, self.cfg.tz_offset), fmt_value(v, unit)]))
            m += 1
        return lines


# --------------------------------------------------------------------------- intérprete CellCLI

_RE_HIST = re.compile(
    r"^LIST METRICHISTORY WHERE (?P<where>.+?) ATTRIBUTES (?P<attrs>.+)$", re.IGNORECASE)
_RE_COND = re.compile(
    r"^(?:name\s*=\s*'(?P<name>[A-Za-z0-9_]+)'|collectionTime\s+NOT\s*<\s*'(?P<ge>[^']+)'|"
    r"collectionTime\s*<\s*'(?P<lt>[^']+)'|collectionTime\s*>\s*'(?P<gt>[^']+)')$", re.IGNORECASE)


class SimBackend:
    def __init__(self, cfg: SimConfig, now_epoch: Optional[int] = None):
        self.cfg = cfg
        self.cells = {c: SimCell(cfg, c) for c in cfg.cells}
        self.now_epoch = now_epoch

    def failure_for(self, cell: str, kind: str, metric: Optional[str] = None,
                    t1: Optional[int] = None) -> bool:
        for f in self.cfg.failures.get(kind, []):
            if f.get("cell") not in (None, cell):
                continue
            if f.get("metric") is not None and f.get("metric") != metric:
                continue
            if t1 is None and ("block_start" in f or "from_block_start" in f):
                continue
            if t1 is not None and "block_start" in f:
                if parse_cellcli_time(f["block_start"]).timestamp() != t1:
                    continue
            if t1 is not None and "from_block_start" in f:
                if t1 < parse_cellcli_time(f["from_block_start"]).timestamp():
                    continue
            return True
        return False

    def run(self, cell: str, cmd: str) -> Tuple[str, str, int]:
        """Devuelve (stdout, stderr, rc) de ``cellcli -e "<cmd>"`` en ``cell``."""
        if cell not in self.cells:
            return "", f"ssh: Could not resolve hostname {cell}: Name or service not known\n", 255
        c = self.cells[cell]
        cmd = cmd.strip()
        if ">=" in cmd or "<=" in cmd:
            return "CELL-01504: Invalid command syntax.\n", "", 0
        up = cmd.upper()
        if up == "LIST CELL ATTRIBUTES NAME, STATUS":
            return f"\t{cell}\tonline\n", "", 0
        if up == "LIST CELL DETAIL":
            return (f"\t name:              \t{cell}\n"
                    f"\t makeModel:         \t{self.cfg.make_model}\n"
                    f"\t metricHistoryDays: \t7\n"
                    f"\t releaseVersion:    \t{self.cfg.release_version}\n"
                    f"\t status:            \tonline\n"), "", 0
        if up == "LIST CELLDISK ATTRIBUTES NAME, DISKTYPE, SIZE":
            out = "".join(f"\t{d}\t{t}\t{'7.15T' if t == 'HardDisk' else '5.82T'}\n" for d, t in c.disks)
            return out, "", 0
        if up == "LIST METRICDEFINITION ATTRIBUTES NAME, OBJECTTYPE, METRICTYPE, UNIT":
            return "".join(f"\t{n}\t{o}\t{m}\t{u}\n" for n, o, m, u in c.catalog()), "", 0
        m = _RE_HIST.match(cmd)
        if m:
            attrs = [a.strip().lower() for a in m.group("attrs").split(",")]
            if attrs != ["name", "metricobjectname", "collectiontime", "metricvalue"]:
                return "CELL-01504: Invalid command syntax.\n", "", 0
            name = t1 = t2 = None
            for cond in re.split(r"\s+AND\s+", m.group("where"), flags=re.IGNORECASE):
                cm = _RE_COND.match(cond.strip())
                if not cm:
                    return "CELL-01504: Invalid command syntax.\n", "", 0
                if cm.group("name"):
                    name = cm.group("name")
                elif cm.group("ge"):
                    t1 = int(parse_cellcli_time(cm.group("ge")).timestamp())
                elif cm.group("lt"):
                    t2 = int(parse_cellcli_time(cm.group("lt")).timestamp())
                elif cm.group("gt"):
                    t1 = int(parse_cellcli_time(cm.group("gt")).timestamp()) + 1
            if t1 is None or t2 is None:
                return "CELL-01504: Invalid command syntax.\n", "", 0
            if name is None:
                # Sin nombre explícito, el histórico antiguo no se devuelve (comportamiento observado).
                return "", "", 0
            if self.failure_for(cell, "cellcli_error", name, t1):
                return ("CELL-02559: There is a communication error between Management Server "
                        "and Cell Server caused by a mismatch of security keys.\n"), "", 0
            if self.failure_for(cell, "empty", name, t1):
                return "", "", 0
            lines = c.history(name, t1, t2)
            if self.failure_for(cell, "interrupted", name, t1):
                half = lines[: max(1, len(lines) // 2)]
                return "\n".join(half) + "\n", "Connection to %s closed by remote host.\n" % cell, 255
            return ("\n".join(lines) + "\n") if lines else "", "", 0
        return "CELL-01504: Invalid command syntax.\n", "", 0


# --------------------------------------------------------------------------- captura sintética

DEFAULT_METRICS: List[Tuple[str, str]] = []  # se rellena desde el script recolector


def _default_metrics() -> List[Tuple[str, str]]:
    from .collector import script_text
    txt = script_text().split("DEFAULT_METRICS='", 1)[1].split("'", 1)[0]
    return [tuple(l.split()) for l in txt.splitlines() if l.strip()]


def write_capture(cfg: SimConfig, outdir: str, start: str, end: str,
                  metrics: Optional[List[Tuple[str, str]]] = None, user: str = "celladmin",
                  dbnode: str = "simdbnode", name: Optional[str] = None) -> str:
    """Genera una captura con el MISMO formato que exacell_collect.sh (sin bash ni ssh).

    Aplica la inyección de fallos de ``cfg.failures`` igual que los falsos dcli/ssh.
    Devuelve la ruta de la captura. Nunca sobrescribe una existente.
    """
    import hashlib
    import os
    from pathlib import Path

    from .collector import script_version

    be = SimBackend(cfg)
    metrics = metrics or _default_metrics()
    s_ep = int(parse_cellcli_time(start).timestamp())
    e_ep = int(parse_cellcli_time(end).timestamp())
    off = "+00:00" if start.endswith("Z") else start[-6:]
    blocks = []
    t = s_ep
    while t < e_ep:
        e = min(t + 3600, e_ep)
        blocks.append((t, e, fmt_time(t, off), fmt_time(e, off)))
        t = e
    cap = Path(outdir) / (name or f"exacell_capture_{dbnode}_sim")
    cap.mkdir(parents=True, exist_ok=False)
    for sub in ("prevalidation", "cells", "data"):
        (cap / sub).mkdir()
    (cap / "cell_group.txt").write_text("\n".join(cfg.cells) + "\n")
    (cap / "metrics_requested.txt").write_text("".join(f"{o} {n}\n" for o, n in metrics))
    (cap / "blocks_plan.tsv").write_text("".join(f"{i}\t{b[2]}\t{b[3]}\n" for i, b in enumerate(blocks)))

    pv_fail = {f.get("cell") for f in cfg.failures.get("prevalidate", [])}
    pv_lines = ["cell\tcheck\tresult\tdetail"]
    dcli_out = []
    for c in cfg.cells:
        if c in pv_fail:
            pv_lines.append(f"{c}\tdcli_list_cell\tFAIL\tstatus=sin_respuesta")
        else:
            dcli_out.append(f"{c}: \t{c}\tonline")
            pv_lines.append(f"{c}\tdcli_list_cell\tOK\tstatus=online")
    (cap / "prevalidation" / "dcli_list_cell.out").write_text("\n".join(dcli_out) + ("\n" if dcli_out else ""))
    (cap / "prevalidation" / "dcli_list_cell.err").write_text("")
    (cap / "prevalidation" / "dcli_list_cell.rc").write_text("1\n" if pv_fail else "0\n")
    expected = 0
    selected = {}
    for c in cfg.cells:
        d = cap / "cells" / c
        d.mkdir()
        for key, cmd in (("list_cell", "LIST CELL ATTRIBUTES name, status"), ("cell_detail", "LIST CELL DETAIL"),
                         ("celldisks", "LIST CELLDISK ATTRIBUTES name, diskType, size"),
                         ("metricdefinition", "LIST METRICDEFINITION ATTRIBUTES name, objectType, metricType, unit")):
            out, err, rc = be.run(c, cmd)
            (d / f"{key}.out").write_text(out)
            (d / f"{key}.err").write_text(err)
            (d / f"{key}.rc").write_text(f"{rc}\n")
            pv_lines.append(f"{c}\tssh_{key}\tOK\trc=0")
        cat = {n: (o, mt, u) for n, o, mt, u in be.cells[c].catalog()}
        sel, miss = [], []
        for o, n in metrics:
            if n not in cat:
                miss.append(f"{o}\t{n}\tno_en_catalogo")
            elif cat[n][0] != o:
                miss.append(f"{o}\t{n}\tobjectType_distinto:{cat[n][0]}")
            else:
                sel.append((o, n, cat[n][1], cat[n][2]))
        (d / "metrics_selected.tsv").write_text("".join("\t".join(s) + "\n" for s in sel))
        (d / "metrics_missing.tsv").write_text("".join(m + "\n" for m in miss))
        pv_lines.append(f"{c}\tmetric_selection\tOK\tseleccionadas={len(sel)} ausentes={len(miss)}")
        selected[c] = sel
        expected += len(sel) * len(blocks)
    (cap / "prevalidation" / "prevalidation.tsv").write_text("\n".join(pv_lines) + "\n")

    rows = []
    status_all = "COMPLETE"
    if pv_fail:
        status_all = "PREVALIDATION_FAILED"
    else:
        for c in cfg.cells:
            (cap / "data" / c).mkdir()
            for i, (bs, be_, bs_s, be_s) in enumerate(blocks):
                for o, n, mt, u in selected[c]:
                    if be.failure_for(c, "network", n, bs):
                        out, err, rc = "", f"ssh: connect to host {c} port 22: Connection timed out\n", 255
                    else:
                        cmd = (f"LIST METRICHISTORY WHERE name = '{n}' AND collectionTime NOT < '{bs_s}' AND "
                               f"collectionTime < '{be_s}' ATTRIBUTES name, metricObjectName, collectionTime, metricValue")
                        out, err, rc = be.run(c, cmd)
                    of, ef = f"data/{c}/b{i:04d}_{n}.out", f"data/{c}/b{i:04d}_{n}.err"
                    (cap / of).write_text(out)
                    (cap / ef).write_text(err)
                    if rc == 255:
                        st = "INTERRUPTED" if out else "NETWORK_ERROR"
                    elif re.search(r"(^|[^A-Za-z0-9])CELL-\d{5}", out + err):
                        st = "CELLCLI_ERROR"
                    elif rc != 0:
                        st = "COMMAND_ERROR"
                    elif not out.strip():
                        st = "EMPTY"
                    else:
                        st = "OK"
                    nlines = sum(1 for l in out.splitlines() if l.strip())
                    rows.append([c, o, n, str(i), bs_s, be_s, st, str(rc), of, ef, str(nlines),
                                 str(len(out.encode())), str(len(err.encode())),
                                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"])
        if any(r[6] != "OK" for r in rows):
            status_all = "INCOMPLETE"
    hdr = ("cell\tobject_type\tmetric\tblock_index\tblock_start\tblock_end\tstatus\trc\tout_file\terr_file\t"
           "out_lines\tout_bytes\terr_bytes\tstarted_utc\tended_utc")
    (cap / "blocks.tsv").write_text(hdr + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    counts = Counter(r[6] for r in rows)
    exit_code = {"COMPLETE": 0, "INCOMPLETE": 3, "PREVALIDATION_FAILED": 2}[status_all]
    man = {
        "format_version": "1", "collector_version": script_version() + "-sim", "metrics_mode": "global",
        "dbnode_host": dbnode, "remote_user": user, "cell_group_file": "cell_group", "cell_count": len(cfg.cells),
        "cells": " ".join(cfg.cells), "period_start": start, "period_end": end, "period_start_epoch": s_ep,
        "period_end_epoch": e_ep, "block_seconds": 3600, "block_count": len(blocks),
        "time_filter": "collectionTime NOT < 'start' AND collectionTime < 'end'",
        "created_utc": "2026-01-01T00:00:00Z", "finished_utc": "2026-01-01T00:00:00Z",
        "prevalidation_status": "FAILED" if pv_fail else "OK", "expected_blocks": expected if not pv_fail else expected,
        "recorded_blocks": len(rows), "not_run_blocks": (expected - len(rows)),
        "status_counts": " ".join(f"{k}:{v}" for k, v in counts.items()),
        "capture_status": status_all, "exit_code": exit_code,
    }
    (cap / "manifest.txt").write_text("".join(f"{k}={v}\n" for k, v in man.items()))
    (cap / "summary.txt").write_text(f"Captura simulada {cap}\nEstado: {status_all}\n")
    sums = []
    for f in sorted(p for p in cap.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        rel = "./" + str(f.relative_to(cap)).replace(os.sep, "/")
        sums.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {rel}")
    (cap / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return str(cap)
