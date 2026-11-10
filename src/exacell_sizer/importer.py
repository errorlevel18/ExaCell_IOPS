"""Importación y validación de capturas del recolector a SQLite.

La captura original NUNCA se modifica: solo se leen archivos. Si se entrega comprimida
(.tar.gz/.tgz/.zip) se extrae de forma segura a un directorio de trabajo independiente.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import cellcli_parse as cp
from .db import connect

SUPPORTED_FORMATS = {"1"}
REQUIRED_MANIFEST = [
    "format_version", "collector_version", "metrics_mode", "cells", "cell_count",
    "period_start", "period_end", "period_start_epoch", "period_end_epoch",
    "block_seconds", "block_count", "capture_status", "exit_code", "prevalidation_status",
    "expected_blocks",
]
ISSUE_DETAIL_CAP = 25  # incidencias individuales por (código, celda, métrica); el resto se resume


class ImportError_(Exception):
    pass


@dataclass
class ImportResult:
    capture_id: int
    import_status: str
    samples: int
    issues: Counter = field(default_factory=Counter)
    capture_dir: str = ""


class IssueSink:
    def __init__(self, con, capture_id):
        self.con, self.capture_id = con, capture_id
        self.counts: Counter = Counter()
        self.by_sev: Counter = Counter()

    def add(self, severity, code, message, host=None, metric=None, source_file=None, source_line=None):
        key = (code, host, metric)
        self.counts[key] += 1
        self.by_sev[severity] += 1
        if self.counts[key] <= ISSUE_DETAIL_CAP:
            self.con.execute(
                "INSERT INTO issue(capture_id, severity, code, host, metric, message, source_file, source_line)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (self.capture_id, severity, code, host, metric, message, source_file, source_line))

    def finish(self):
        for (code, host, metric), n in self.counts.items():
            if n > ISSUE_DETAIL_CAP:
                sev = self.con.execute("SELECT severity FROM issue WHERE capture_id=? AND code=? LIMIT 1",
                                       (self.capture_id, code)).fetchone()[0]
                self.con.execute(
                    "INSERT INTO issue(capture_id, severity, code, host, metric, message) VALUES (?,?,?,?,?,?)",
                    (self.capture_id, sev, code, host, metric,
                     f"{n - ISSUE_DETAIL_CAP} incidencias adicionales {code} no detalladas (total {n})"))


# ----------------------------------------------------------------------------- localización

def _safe_extract(archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                target = (dest / m.filename).resolve()
                if not str(target).startswith(str(dest) + os.sep) and target != dest:
                    raise ImportError_(f"ruta insegura en el archivo: {m.filename}")
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise ImportError_(f"ruta insegura en el archivo: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise ImportError_(f"tipo de entrada no permitido en el archivo: {m.name}")
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
        else:  # pragma: no cover - Python < 3.12 sin filtros
            t.extractall(dest)


def locate_capture(src: str, workdir: Optional[str] = None) -> Path:
    p = Path(src)
    if p.is_file():
        wd = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="exacell_extract_"))
        wd.mkdir(parents=True, exist_ok=True)
        _safe_extract(p, wd)
        p = wd
    if not p.is_dir():
        raise ImportError_(f"no existe la captura: {src}")
    if (p / "manifest.txt").exists():
        return p
    subs = [d for d in p.iterdir() if d.is_dir() and (d / "manifest.txt").exists()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        raise ImportError_(f"no se encuentra manifest.txt en {p}")
    raise ImportError_(f"varias capturas en {p}; indique una: {[s.name for s in subs]}")


def read_kv(path: Path) -> Dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def read_tsv(path: Path, header: bool = True) -> List[Dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return []
    if header:
        hdr = lines[0].split("\t")
        return [dict(zip(hdr, l.split("\t"))) for l in lines[1:] if l.strip()]
    return [dict(enumerate(l.split("\t"))) for l in lines if l.strip()]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- importación

def import_capture(src: str, db_path: str, label: Optional[str] = None, role: str = "origen",
                   workdir: Optional[str] = None) -> ImportResult:
    cap = locate_capture(src, workdir)
    if Path(db_path).resolve().is_relative_to(cap.resolve()):
        raise ImportError_("la base de datos no puede crearse dentro de la captura original")
    man_path = cap / "manifest.txt"
    man = read_kv(man_path)
    label = label or cap.name
    con = connect(db_path)
    try:
        with con:
            res = _import(con, cap, man, label, role)
        return res
    finally:
        con.close()


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _import(con, cap: Path, man: Dict[str, str], label: str, role: str) -> ImportResult:
    exists = con.execute("SELECT id FROM capture WHERE dbnode_host IS ? AND created_utc IS ? AND label=?",
                         (man.get("dbnode_host"), man.get("created_utc"), label)).fetchone()
    if exists:
        raise ImportError_(f"captura ya importada con id {exists[0]} y etiqueta {label!r}")
    cur = con.execute(
        "INSERT INTO capture(label, role, source_path, format_version, collector_version, dbnode_host, remote_user,"
        " created_utc, metrics_mode, period_start_raw, period_end_raw, period_start_epoch, period_end_epoch,"
        " block_seconds, block_count, reported_status, reported_exit_code, prevalidation_status, imported_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, role, str(cap), man.get("format_version"), man.get("collector_version"), man.get("dbnode_host"),
         man.get("remote_user"), man.get("created_utc"), man.get("metrics_mode"), man.get("period_start"),
         man.get("period_end"), _int(man.get("period_start_epoch")), _int(man.get("period_end_epoch")),
         _int(man.get("block_seconds")), _int(man.get("block_count")), man.get("capture_status"),
         man.get("exit_code"), man.get("prevalidation_status"),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    cid = cur.lastrowid
    iss = IssueSink(con, cid)

    # --- manifiesto
    for k in REQUIRED_MANIFEST:
        if k not in man:
            iss.add("ERROR", "MANIFEST_FIELD_MISSING", f"falta '{k}' en manifest.txt", source_file="manifest.txt")
    if man.get("format_version") not in SUPPORTED_FORMATS:
        iss.add("ERROR", "FORMAT_UNSUPPORTED", f"format_version={man.get('format_version')!r} no soportado")
    if man.get("metrics_mode") != "global":
        iss.add("ERROR", "MODE_NOT_GLOBAL", f"metrics_mode={man.get('metrics_mode')!r}; se esperaba 'global'")
    if man.get("capture_status") != "COMPLETE":
        iss.add("ERROR", "CAPTURE_REPORTED_" + str(man.get("capture_status")),
                f"el recolector informó capture_status={man.get('capture_status')} exit_code={man.get('exit_code')}")
    if man.get("prevalidation_status") != "OK":
        iss.add("ERROR", "PREVALIDATION_NOT_OK", f"prevalidation_status={man.get('prevalidation_status')}")
    ps, pe, bs = _int(man.get("period_start_epoch")), _int(man.get("period_end_epoch")), _int(man.get("block_seconds"))
    # Coherencia de las épocas con las fechas con zona
    for k_raw, k_ep, val in (("period_start", "period_start_epoch", ps), ("period_end", "period_end_epoch", pe)):
        if man.get(k_raw):
            ep, err = cp.parse_timestamp(man[k_raw])
            if err or ep != val:
                iss.add("ERROR", "MANIFEST_TIME_INCONSISTENT", f"{k_raw}={man.get(k_raw)} no coincide con {k_ep}={val}")
    if ps is not None and pe is not None and pe - ps < 86400:
        iss.add("WARNING", "SHORT_CAPTURE",
                f"captura de {(pe - ps) / 3600:.2f} h (< 24 h): no apta como base para aprobar una migración")

    # --- sumas de verificación
    checksum_status = _verify_checksums(cap, iss)
    con.execute("UPDATE capture SET checksum_status=? WHERE id=?", (checksum_status, cid))

    # --- celdas
    cells = man.get("cells", "").split()
    if _int(man.get("cell_count")) != len(cells):
        iss.add("ERROR", "CELL_COUNT_MISMATCH", f"cell_count={man.get('cell_count')} pero cells={cells}")
    if len(set(cells)) != len(cells):
        iss.add("ERROR", "CELL_DUPLICATED", f"celdas duplicadas en el manifiesto: {cells}")
    group_file = cap / "cell_group.txt"
    if group_file.exists():
        grp = [l.split("#")[0].strip() for l in group_file.read_text().splitlines()]
        grp = [g for g in grp if g]
        if grp != cells:
            iss.add("ERROR", "CELL_GROUP_MISMATCH", f"cell_group.txt={grp} difiere de manifest cells={cells}")
    else:
        iss.add("WARNING", "CELL_GROUP_MISSING", "no se encuentra cell_group.txt en la captura")

    defs: Dict[str, Dict[str, Tuple[str, str, str]]] = {}
    disks: Dict[str, Dict[str, Tuple[str, str]]] = {}
    selected: Dict[str, Dict[str, str]] = {}
    for c in cells:
        defs[c], disks[c], selected[c] = _import_cell(con, cid, cap, c, iss)

    # --- plan de bloques
    plan = _check_plan(cap, ps, pe, bs, _int(man.get("block_count")), iss)

    # --- bloques
    rows = read_tsv(cap / "blocks.tsv") if (cap / "blocks.tsv").exists() else []
    if not (cap / "blocks.tsv").exists():
        iss.add("ERROR", "BLOCKS_FILE_MISSING", "no existe blocks.tsv")
    seen = {}
    for r in rows:
        key = (r.get("cell"), r.get("metric"), _int(r.get("block_index")))
        if key in seen:
            iss.add("ERROR", "DUPLICATE_BLOCK", f"bloque duplicado {key}", host=key[0], metric=key[1],
                    source_file="blocks.tsv")
            continue
        seen[key] = r
    expected = {(c, m, i) for c in cells for m in selected.get(c, {}) for i in plan}
    if _int(man.get("expected_blocks")) is not None and _int(man.get("expected_blocks")) != len(expected):
        iss.add("ERROR", "EXPECTED_BLOCKS_MISMATCH",
                f"manifest expected_blocks={man.get('expected_blocks')} pero selección x plan = {len(expected)}")
    for key in sorted(expected - set(seen), key=str):
        iss.add("ERROR", "BLOCK_NOT_RUN", f"bloque esperado sin registro: celda={key[0]} métrica={key[1]} bloque={key[2]}",
                host=key[0], metric=key[1])
    for key in sorted(set(seen) - expected, key=str):
        iss.add("WARNING", "BLOCK_UNEXPECTED", f"bloque no previsto en el plan/selección: {key}",
                host=key[0], metric=key[1], source_file="blocks.tsv")

    total = 0
    by_metric = defaultdict(list)
    for key, r in seen.items():
        by_metric[(key[0], key[1])].append(r)
    for (host, metric), brs in sorted(by_metric.items()):
        total += _import_metric(con, cid, cap, host, metric, brs, plan, defs.get(host, {}),
                                disks.get(host, {}), iss)

    iss.finish()
    status = "INCOMPLETE" if iss.by_sev["ERROR"] else "COMPLETE"
    con.execute("UPDATE capture SET import_status=?, sample_count=? WHERE id=?", (status, total, cid))
    return ImportResult(cid, status, total, Counter({k[0]: v for k, v in iss.counts.items()}), str(cap))


def _verify_checksums(cap: Path, iss: IssueSink) -> str:
    sums = cap / "SHA256SUMS"
    if not sums.exists():
        iss.add("ERROR", "CHECKSUM_FILE_MISSING", "no existe SHA256SUMS: no se puede verificar la integridad")
        return "MISSING"
    listed = {}
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        h, _, name = line.partition("  ")
        listed[os.path.normpath(name.strip())] = h.strip()
    status = "OK"
    for name, h in listed.items():
        f = cap / name
        if not f.exists():
            iss.add("ERROR", "CHECKSUM_FILE_ABSENT", f"archivo listado y ausente: {name}", source_file=name)
            status = "MISMATCH"
        elif _sha256(f) != h:
            iss.add("ERROR", "CHECKSUM_MISMATCH", f"suma SHA-256 distinta (archivo alterado): {name}", source_file=name)
            status = "MISMATCH"
    for f in cap.rglob("*"):
        if f.is_file():
            rel = os.path.normpath(str(f.relative_to(cap)))
            if rel != "SHA256SUMS" and rel not in listed:
                iss.add("WARNING", "UNLISTED_FILE", f"archivo no incluido en SHA256SUMS: {rel}", source_file=rel)
    return status


def _import_cell(con, cid, cap: Path, c: str, iss: IssueSink):
    d = cap / "cells" / c
    if not d.is_dir():
        iss.add("ERROR", "CELL_DIR_MISSING", f"falta el inventario de la celda {c}", host=c)
        con.execute("INSERT INTO cell(capture_id, host, inventory_ok) VALUES (?,?,0)", (cid, c))
        return {}, {}, {}
    inv_ok = 1
    texts = {}
    for key in ("cell_detail", "celldisks", "metricdefinition"):
        f, rcf = d / f"{key}.out", d / f"{key}.rc"
        if not f.exists():
            iss.add("ERROR", "INVENTORY_MISSING", f"falta {key}.out", host=c, source_file=str(f.relative_to(cap)))
            inv_ok, texts[key] = 0, ""
            continue
        rc = rcf.read_text().strip() if rcf.exists() else "?"
        if rc != "0":
            iss.add("ERROR", "INVENTORY_RC", f"{key} terminó con rc={rc}", host=c)
            inv_ok = 0
        texts[key] = f.read_text(encoding="utf-8", errors="replace")
    det = cp.parse_detail(texts.get("cell_detail", ""))
    con.execute("INSERT INTO cell(capture_id, host, cell_name, make_model, release_version, metric_history_days,"
                " status, inventory_ok) VALUES (?,?,?,?,?,?,?,?)",
                (cid, c, det.get("name"), det.get("makeModel"), det.get("releaseVersion"),
                 det.get("metricHistoryDays"), det.get("status"), inv_ok))
    if not det.get("makeModel"):
        iss.add("WARNING", "CELL_MODEL_UNKNOWN", "makeModel no disponible: el modelo origen debe indicarse", host=c)
    dk = cp.parse_celldisks(texts.get("celldisks", ""))
    for name, (dtype, size) in dk.items():
        con.execute("INSERT INTO celldisk VALUES (?,?,?,?,?)", (cid, c, name, dtype, size))
        if dtype not in ("HardDisk", "FlashDisk"):
            iss.add("WARNING", "DISK_TYPE_UNKNOWN", f"diskType desconocido {dtype!r} en {name}", host=c)
    defs, bad = cp.parse_metric_definitions(texts.get("metricdefinition", ""))
    for n, line in bad:
        iss.add("WARNING", "DEFINITION_MALFORMED", f"línea de definición no interpretable: {line!r}", host=c,
                source_file=f"cells/{c}/metricdefinition.out", source_line=n)
    sel = {}
    sf = d / "metrics_selected.tsv"
    if sf.exists():
        for line in sf.read_text().splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                sel[p[1]] = p[0]
    else:
        iss.add("ERROR", "SELECTION_MISSING", "falta metrics_selected.tsv", host=c)
    mf = d / "metrics_missing.tsv"
    if mf.exists():
        for line in mf.read_text().splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                iss.add("WARNING", "METRIC_NOT_IN_CATALOG",
                        f"métrica solicitada no disponible en el catálogo de la celda: {p[1]} ({p[2] if len(p) > 2 else ''})",
                        host=c, metric=p[1])
    for name, (ot, mt, unit) in defs.items():
        rec = 1 if unit in cp.KNOWN_UNITS else 0
        con.execute("INSERT INTO metric_definition VALUES (?,?,?,?,?,?,?,?)",
                    (cid, c, name, ot, mt, unit, rec, 1 if name in sel else 0))
        if name in sel and not rec:
            iss.add("WARNING", "UNIT_UNRECOGNIZED", f"unidad no reconocida {unit!r} para {name}", host=c, metric=name)
        if name in sel and mt not in ("Rate", "Instantaneous", "Cumulative"):
            iss.add("WARNING", "METRIC_TYPE_UNKNOWN", f"metricType {mt!r} no reconocido para {name}", host=c, metric=name)
    for name, ot in sel.items():
        if name not in defs:
            iss.add("WARNING", "METRIC_WITHOUT_DEFINITION", f"{name} seleccionada sin definición en el catálogo",
                    host=c, metric=name)
        elif defs[name][0] != ot:
            iss.add("ERROR", "SCOPE_MISMATCH", f"{name}: objectType seleccionado {ot} != catálogo {defs[name][0]}",
                    host=c, metric=name)
    return defs, dk, sel


def _check_plan(cap: Path, ps, pe, bs, bc, iss: IssueSink) -> Dict[int, Tuple[int, int, str, str]]:
    plan = {}
    f = cap / "blocks_plan.tsv"
    if not f.exists():
        iss.add("ERROR", "PLAN_MISSING", "no existe blocks_plan.tsv")
        return plan
    prev_end = ps
    for n, line in enumerate(f.read_text().splitlines(), 1):
        p = line.split("\t")
        if len(p) != 3:
            iss.add("ERROR", "PLAN_MALFORMED", f"línea de plan inválida: {line!r}", source_file="blocks_plan.tsv",
                    source_line=n)
            continue
        i = _int(p[0])
        s, e1 = cp.parse_timestamp(p[1])
        e, e2 = cp.parse_timestamp(p[2])
        if i is None or e1 or e2:
            iss.add("ERROR", "PLAN_MALFORMED", f"línea de plan inválida: {line!r}", source_file="blocks_plan.tsv",
                    source_line=n)
            continue
        if prev_end is not None and s != prev_end:
            code = "PLAN_GAP" if s > prev_end else "PLAN_OVERLAP"
            iss.add("ERROR", code, f"bloque {i} empieza en {p[1]} y el anterior terminaba en "
                    f"{cp.epoch_to_utc(prev_end)}", source_file="blocks_plan.tsv", source_line=n)
        if bs and e - s > bs:
            iss.add("ERROR", "PLAN_BLOCK_TOO_LONG", f"bloque {i} dura {e - s}s > {bs}s", source_file="blocks_plan.tsv",
                    source_line=n)
        if e <= s:
            iss.add("ERROR", "PLAN_BLOCK_EMPTY", f"bloque {i} sin duración", source_file="blocks_plan.tsv", source_line=n)
        plan[i] = (s, e, p[1], p[2])
        prev_end = e
    if pe is not None and prev_end != pe:
        iss.add("ERROR", "PLAN_DOES_NOT_COVER", f"el plan termina en {prev_end} y el periodo en {pe}")
    if bc is not None and bc != len(plan):
        iss.add("ERROR", "PLAN_COUNT_MISMATCH", f"block_count={bc} pero el plan tiene {len(plan)} bloques")
    return plan


def _import_metric(con, cid, cap: Path, host, metric, brs, plan, defs, disks, iss: IssueSink) -> int:
    d = defs.get(metric)
    def_unit = d[2] if d else None
    obj_type = d[0] if d else None
    store: Dict[Tuple[str, int], tuple] = {}
    unknown_objs = set()
    for r in sorted(brs, key=lambda x: _int(x.get("block_index"), -1)):
        bi = _int(r.get("block_index"))
        pl = plan.get(bi)
        s_ep, e1 = cp.parse_timestamp(r.get("block_start", ""))
        e_ep, e2 = cp.parse_timestamp(r.get("block_end", ""))
        if pl is None or e1 or e2 or (s_ep, e_ep) != (pl[0], pl[1]):
            iss.add("ERROR", "BLOCK_PERIOD_MISMATCH",
                    f"bloque {bi} con periodo {r.get('block_start')}..{r.get('block_end')} distinto del plan",
                    host=host, metric=metric, source_file="blocks.tsv")
        status = r.get("status")
        cur = con.execute(
            "INSERT INTO block(capture_id, host, object_type, metric, block_index, start_raw, end_raw, start_epoch,"
            " end_epoch, status, rc, out_file, err_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, host, r.get("object_type"), metric, bi, r.get("block_start"), r.get("block_end"), s_ep, e_ep,
             status, r.get("rc"), r.get("out_file"), r.get("err_file")))
        block_id = cur.lastrowid
        if status != "OK":
            iss.add("ERROR", f"BLOCK_{status}", f"bloque {bi} ({r.get('block_start')}) estado {status} rc={r.get('rc')}:"
                    " la captura queda incompleta; no se interpreta como carga cero",
                    host=host, metric=metric, source_file=r.get("out_file"))
        if status in ("CELLCLI_ERROR", "NETWORK_ERROR", "EMPTY", "COMMAND_ERROR"):
            continue
        out = cap / (r.get("out_file") or "")
        if not r.get("out_file") or not out.is_file():
            iss.add("ERROR", "BLOCK_FILE_MISSING", f"falta el archivo de salida del bloque {bi}", host=host,
                    metric=metric, source_file=r.get("out_file"))
            continue
        lines_read = stored = 0
        with open(out, encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                lines_read += 1
                row = cp.parse_history_line(line)
                if row is None or row.metric != metric:
                    iss.add("WARNING", "MALFORMED_LINE", f"línea no interpretable: {line.rstrip()!r}", host=host,
                            metric=metric, source_file=r.get("out_file"), source_line=ln)
                    continue
                ts, terr = cp.parse_timestamp(row.ts_raw)
                if terr:
                    iss.add("WARNING", "BAD_TIMESTAMP", terr, host=host, metric=metric,
                            source_file=r.get("out_file"), source_line=ln)
                    continue
                if s_ep is not None and not (s_ep <= ts < e_ep):
                    iss.add("WARNING", "SAMPLE_OUT_OF_BLOCK",
                            f"muestra {row.ts_raw} fuera del bloque [{r.get('block_start')}, {r.get('block_end')})",
                            host=host, metric=metric, source_file=r.get("out_file"), source_line=ln)
                if obj_type == "CELLDISK" and row.object_name not in disks and row.object_name not in unknown_objs:
                    unknown_objs.add(row.object_name)
                    iss.add("WARNING", "OBJECT_UNKNOWN", f"celldisk {row.object_name} no está en el inventario",
                            host=host, metric=metric, source_file=r.get("out_file"), source_line=ln)
                val, unit, verr = cp.parse_value(row.value_raw)
                if verr:
                    iss.add("WARNING", "NON_NUMERIC_VALUE", verr + "; se conserva sin valor numérico", host=host,
                            metric=metric, source_file=r.get("out_file"), source_line=ln)
                if d is None:
                    ustatus = "NO_DEFINITION"
                elif unit == "":
                    ustatus = "MISSING"
                elif unit != def_unit:
                    ustatus = "MISMATCH"
                    iss.add("WARNING", "UNIT_MISMATCH", f"unidad {unit!r} != definición {def_unit!r}", host=host,
                            metric=metric, source_file=r.get("out_file"), source_line=ln)
                elif unit not in cp.KNOWN_UNITS:
                    ustatus = "UNRECOGNIZED"
                else:
                    ustatus = "OK"
                vtxt = cp.canonical(val) if val is not None else None
                key = (row.object_name, ts)
                prev = store.get(key)
                if prev is not None:
                    if prev[1] == vtxt:
                        iss.add("INFO", "DUPLICATE_SAMPLE", f"muestra duplicada idéntica {row.object_name} {row.ts_raw}",
                                host=host, metric=metric, source_file=r.get("out_file"), source_line=ln)
                    else:
                        iss.add("WARNING", "DUPLICATE_CONFLICT",
                                f"muestra duplicada con valor distinto {row.object_name} {row.ts_raw}: "
                                f"{prev[1]} (se conserva) vs {vtxt}", host=host, metric=metric,
                                source_file=r.get("out_file"), source_line=ln)
                    continue
                store[key] = (row.ts_raw, vtxt, row.value_raw, unit or def_unit, ustatus, block_id, ln)
                stored += 1
        con.execute("UPDATE block SET lines_read=?, rows_stored=? WHERE id=?", (lines_read, stored, block_id))
        if status == "OK" and lines_read == 0:
            iss.add("ERROR", "BLOCK_EMPTY_DATA", f"bloque {bi} marcado OK pero sin datos", host=host, metric=metric,
                    source_file=r.get("out_file"))
    con.executemany(
        "INSERT INTO sample(capture_id, host, metric, object_name, ts_epoch, ts_utc, ts_raw, value, raw_value, unit,"
        " unit_status, block_id, source_line) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((cid, host, metric, obj, ts, cp.epoch_to_utc(ts), v[0], v[1], v[2], v[3], v[4], v[5], v[6])
         for (obj, ts), v in store.items()))
    return len(store)
