"""Análisis de capturas importadas: series por minuto, cobertura, contadores y combinación.

Reglas principales:
  * Un minuto de una celda es COMPLETO para una medida solo si todos los objetos esperados
    (según el inventario de la celda) tienen muestra numérica válida de todas las métricas
    componentes. Los minutos incompletos no se rellenan ni se cuentan como cero.
  * El total de sistema solo se calcula en minutos completos de TODAS las celdas aplicables.
  * Porcentajes y latencias nunca se suman entre celdas.
  * Las tasas a partir de contadores (Cumulative) exigen delta >= 0, intervalo conocido y
    0 < dt <= max_gap; los reinicios y huecos se registran como eventos y no generan tasa.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .cellcli_parse import epoch_to_utc, parse_timestamp
from .metrics import MEASURES, UNIT_CONVERSIONS, Measure

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS measure_minute (
  capture_id INTEGER, measure TEXT, host TEXT, minute_epoch INTEGER, value TEXT,
  PRIMARY KEY (capture_id, measure, host, minute_epoch)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS measure_coverage (
  capture_id INTEGER, measure TEXT, host TEXT, status TEXT, reason TEXT, source TEXT,
  expected_minutes INTEGER, complete_minutes INTEGER, incomplete_minutes INTEGER,
  PRIMARY KEY (capture_id, measure, host)
);
CREATE TABLE IF NOT EXISTS counter_event (
  capture_id INTEGER, host TEXT, metric TEXT, object_name TEXT, ts_epoch INTEGER, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS derived_rate (
  capture_id INTEGER, host TEXT, metric TEXT, object_name TEXT, ts_epoch INTEGER, rate TEXT, dt INTEGER,
  PRIMARY KEY (capture_id, host, metric, object_name, ts_epoch)
) WITHOUT ROWID;
"""
SYSTEM = "*"
Q6 = Decimal("0.000001")
MAX_GAP_DEFAULT = 120  # segundos; muestras CellCLI cada 60 s


# ----------------------------------------------------------------------------- estructuras

@dataclass
class Series:
    values: Dict[int, Decimal]
    expected_minutes: int
    incomplete_minutes: int = 0
    status: str = "OK"                 # OK | UNAVAILABLE | NOT_APPLICABLE
    reason: str = ""
    source: str = "direct"             # direct | counter | mixed

    @property
    def complete_minutes(self) -> int:
        return len(self.values)

    @property
    def coverage(self) -> Decimal:
        if not self.expected_minutes:
            return Decimal(0)
        return (Decimal(len(self.values)) / Decimal(self.expected_minutes)).quantize(Decimal("0.0001"))

    def stats(self) -> dict:
        return series_stats(self.values)


def percentile(sorted_vals: List[Decimal], p: Decimal) -> Optional[Decimal]:
    if not sorted_vals:
        return None
    k = int((p / 100 * len(sorted_vals)).to_integral_value(rounding="ROUND_CEILING"))
    k = min(max(k, 1), len(sorted_vals))
    return sorted_vals[k - 1]


def series_stats(values: Dict[int, Decimal]) -> dict:
    if not values:
        return {"count": 0, "max": None, "max_minute": None, "p99": None, "p95": None, "p50": None, "mean": None}
    items = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    sv = sorted(values.values())
    mean = sum(sv, Decimal(0)) / len(sv)
    return {"count": len(sv), "max": items[0][1], "max_minute": items[0][0],
            "p99": percentile(sv, Decimal(99)), "p95": percentile(sv, Decimal(95)),
            "p50": percentile(sv, Decimal(50)), "mean": mean.quantize(Decimal("0.001"))}


@dataclass
class MeasureResult:
    measure: Measure
    per_cell: Dict[str, Series]
    system: Optional[Series]           # None si no sumable
    cells_max: Dict[int, Decimal] = field(default_factory=dict)  # máx. entre celdas por minuto (no sumable)


@dataclass
class CaptureAnalysis:
    capture_id: int
    label: str
    role: str
    import_status: str
    period_start: int
    period_end: int
    period_start_raw: str
    cells: List[str]
    expected_minutes: int
    measures: Dict[str, MeasureResult]
    calendar: dict
    counter_events: List[tuple]
    cell_models: Dict[str, str]


# ----------------------------------------------------------------------------- contadores

def derive_counter_rates(points: List[Tuple[int, Decimal]], max_gap: int = MAX_GAP_DEFAULT):
    """Tasas por segundo desde un contador acumulado.

    Devuelve (tasas[(ts, rate, dt)], eventos[(ts, tipo, detalle)]). Tipos de evento:
    ``reset`` (delta negativo), ``gap`` (dt > max_gap), ``bad_dt`` (dt <= 0).
    Nunca se emite una tasa para un intervalo con evento.
    """
    rates, events = [], []
    pts = sorted(points)
    for (t1, v1), (t2, v2) in zip(pts, pts[1:]):
        dt = t2 - t1
        if dt <= 0:
            events.append((t2, "bad_dt", f"dt={dt}"))
            continue
        if dt > max_gap:
            events.append((t2, "gap", f"dt={dt}s > {max_gap}s"))
            continue
        delta = v2 - v1
        if delta < 0:
            events.append((t2, "reset", f"{v1} -> {v2}"))
            continue
        rates.append((t2, (delta / Decimal(dt)).quantize(Q6), dt))
    return rates, events


# ----------------------------------------------------------------------------- análisis

def _definition(con, cid, host, metric):
    return con.execute("SELECT object_type, metric_type, unit FROM metric_definition WHERE capture_id=? AND host=? AND name=?",
                       (cid, host, metric)).fetchone()


def _has_samples(con, cid, host, metric) -> bool:
    return con.execute("SELECT 1 FROM sample WHERE capture_id=? AND host=? AND metric=? AND value IS NOT NULL LIMIT 1",
                       (cid, host, metric)).fetchone() is not None


def _load_points(con, cid, host, metric):
    """{objeto: {minuto: (ts, Decimal)}} con solo muestras válidas (numéricas y unidad aceptable)."""
    out: Dict[str, Dict[int, Tuple[int, Decimal]]] = defaultdict(dict)
    for obj, ts, val, ust in con.execute(
            "SELECT object_name, ts_epoch, value, unit_status FROM sample WHERE capture_id=? AND host=? AND metric=?"
            " ORDER BY ts_epoch", (cid, host, metric)):
        if val is None or ust not in ("OK", "MISSING"):
            continue
        m = ts // 60 * 60
        if m not in out[obj]:
            out[obj][m] = (ts, Decimal(val))
    return out


def _component_source(con, cid, host, metric, fallback, ms: Measure, max_gap, events_out):
    """Obtiene {objeto: {minuto: valor}} de una métrica componente (directa o derivada de contador)."""
    d = _definition(con, cid, host, metric)
    reason = None
    if d is None:
        reason = f"{metric}: sin definición en el catálogo de la celda"
    else:
        ot, mt, unit = d
        if ot != ms.object_type:
            reason = f"{metric}: objectType {ot} != {ms.object_type}"
        elif mt != ms.expected_type:
            reason = f"{metric}: metricType {mt} != {ms.expected_type}"
        elif unit != ms.expected_unit and (unit, ms.expected_unit) not in UNIT_CONVERSIONS:
            reason = f"{metric}: unidad {unit!r} no convertible a {ms.expected_unit!r}"
        elif not _has_samples(con, cid, host, metric):
            reason = f"{metric}: sin muestras válidas"
    if reason is None:
        factor = Decimal(str(UNIT_CONVERSIONS.get((d[2], ms.expected_unit), 1)))
        pts = _load_points(con, cid, host, metric)
        if factor != 1:
            return {o: {m: (v * factor).quantize(Q6) for m, (_, v) in mm.items()} for o, mm in pts.items()}, "direct", None
        return {o: {m: v for m, (_, v) in mm.items()} for o, mm in pts.items()}, "direct", None
    if fallback:
        fd = _definition(con, cid, host, fallback)
        if fd and fd[1] == "Cumulative" and fd[0] == ms.object_type and _has_samples(con, cid, host, fallback):
            raw = _load_points(con, cid, host, fallback)
            out = {}
            for obj, mm in raw.items():
                rates, evs = derive_counter_rates([(ts, v) for ts, v in mm.values()], max_gap)
                for ts, kind, det in evs:
                    events_out.append((host, fallback, obj, ts, kind, det))
                out[obj] = {ts // 60 * 60: r for ts, r, _ in rates}
                for ts, r, dt in rates:
                    events_out.append(("__rate__", host, fallback, obj, ts, r, dt))
            return out, "counter", None
        reason += f"; contador {fallback} tampoco utilizable"
    return None, None, reason


def analyze_capture(db_path: str, capture_id: int, max_gap: int = MAX_GAP_DEFAULT,
                    measures: Optional[List[Measure]] = None, persist: bool = True) -> CaptureAnalysis:
    con = sqlite3.connect(db_path)
    con.executescript(ANALYSIS_SCHEMA)
    cap = con.execute("SELECT label, role, import_status, period_start_epoch, period_end_epoch, period_start_raw"
                      " FROM capture WHERE id=?", (capture_id,)).fetchone()
    if cap is None:
        raise ValueError(f"captura {capture_id} no existe")
    label, role, istatus, ps, pe, ps_raw = cap
    cells = [r[0] for r in con.execute("SELECT host FROM cell WHERE capture_id=? ORDER BY rowid", (capture_id,))]
    models = {r[0]: r[1] or "" for r in con.execute("SELECT host, make_model FROM cell WHERE capture_id=?", (capture_id,))}
    minutes = list(range(ps // 60 * 60 + (60 if ps % 60 else 0), pe, 60))
    expected = len(minutes)
    results: Dict[str, MeasureResult] = {}
    all_events: List[tuple] = []
    for ms in (measures or MEASURES):
        per_cell: Dict[str, Series] = {}
        for host in cells:
            per_cell[host] = _cell_series(con, capture_id, host, ms, expected, max_gap, all_events, ps, pe)
        system = None
        cells_max: Dict[int, Decimal] = {}
        applicable = [h for h in cells if per_cell[h].status != "NOT_APPLICABLE"]
        if ms.summable_across_cells:
            if not applicable:
                system = Series({}, expected, status="NOT_APPLICABLE", reason="no aplica a ninguna celda")
            elif any(per_cell[h].status == "UNAVAILABLE" for h in applicable):
                bad = [h for h in applicable if per_cell[h].status == "UNAVAILABLE"]
                system = Series({}, expected, status="UNAVAILABLE",
                                reason=f"medida no disponible en {bad}: el total de sistema no puede calcularse")
            else:
                common = set.intersection(*(set(per_cell[h].values) for h in applicable))
                vals = {m: sum((per_cell[h].values[m] for h in applicable), Decimal(0)) for m in common}
                inc = len(set().union(*(set(per_cell[h].values) for h in applicable)) - common)
                srcs = {per_cell[h].source for h in applicable}
                system = Series(vals, expected, incomplete_minutes=inc,
                                source=srcs.pop() if len(srcs) == 1 else "mixed")
        else:
            okc = [h for h in applicable if per_cell[h].status == "OK"]
            for h in okc:
                for m, v in per_cell[h].values.items():
                    if m not in cells_max or v > cells_max[m]:
                        cells_max[m] = v
        results[ms.id] = MeasureResult(ms, per_cell, system, cells_max)

    counter_events = list(dict.fromkeys(e for e in all_events if e[0] != "__rate__"))
    analysis = CaptureAnalysis(capture_id, label, role, istatus, ps, pe, ps_raw or "", cells, expected, results,
                               calendar_coverage(ps, pe, ps_raw or ""), counter_events, models)
    if persist:
        _persist(con, analysis, all_events)
    con.close()
    return analysis


def _cell_series(con, cid, host, ms: Measure, expected, max_gap, events, ps, pe) -> Series:
    # Objetos esperados según inventario
    if ms.object_type == "CELLDISK":
        objs = [r[0] for r in con.execute("SELECT name FROM celldisk WHERE capture_id=? AND host=? AND disk_type=?",
                                          (cid, host, ms.disk_type))]
        if not objs:
            any_inv = con.execute("SELECT COUNT(*) FROM celldisk WHERE capture_id=? AND host=?", (cid, host)).fetchone()[0]
            if any_inv:
                return Series({}, expected, status="NOT_APPLICABLE", reason=f"la celda no tiene celldisks {ms.disk_type}")
            return Series({}, expected, status="UNAVAILABLE", reason="inventario de celldisks vacío o ausente")
    else:
        objs = None  # se fijan con lo observado
    comp_data, sources = [], set()
    fallbacks = ms.counter_fallback or ()
    for i, metric in enumerate(ms.components):
        fb = fallbacks[i] if i < len(fallbacks) else None
        data, src, reason = _component_source(con, cid, host, metric, fb, ms, max_gap, events)
        if data is None:
            return Series({}, expected, status="UNAVAILABLE", reason=reason)
        comp_data.append(data)
        sources.add(src)
    weights = []
    for metric in ms.weight_components:
        data, _, reason = _component_source(con, cid, host, metric, None, ms_weight(ms), max_gap, events)
        weights.append(data or {})
    if objs is None:
        observed = set()
        for d in comp_data:
            observed |= set(d)
        objs = sorted(observed)
        if not objs:
            return Series({}, expected, status="UNAVAILABLE", reason="sin objetos observados")
    values: Dict[int, Decimal] = {}
    touched = set()
    for d in comp_data:
        for o in objs:
            touched |= {m for m in d.get(o, {}) if ps <= m < pe}
    for m in sorted(touched):
        ok = all(m in d.get(o, {}) for d in comp_data for o in objs)
        if not ok:
            continue
        if ms.object_agg == "sum":
            v = sum((d[o][m] for d in comp_data for o in objs), Decimal(0))
        elif ms.object_agg == "mean":
            v = (sum((comp_data[0][o][m] for o in objs), Decimal(0)) / len(objs)).quantize(Q6)
        elif ms.object_agg == "max_of_components":
            v = sum((max(d[o][m] for d in comp_data) for o in objs), Decimal(0))
        elif ms.object_agg == "weighted_mean":
            w = weights[0] if weights else {}
            ws = [(comp_data[0][o][m], w.get(o, {}).get(m)) for o in objs]
            if all(x[1] is not None for x in ws) and sum((x[1] for x in ws), Decimal(0)) > 0:
                tot = sum((x[1] for x in ws), Decimal(0))
                v = (sum((x[0] * x[1] for x in ws), Decimal(0)) / tot).quantize(Q6)
            else:
                v = (sum((x[0] for x in ws), Decimal(0)) / len(ws)).quantize(Q6)
        else:  # pragma: no cover
            raise ValueError(ms.object_agg)
        values[m] = v
    src = sources.pop() if len(sources) == 1 else "mixed"
    return Series(values, expected, incomplete_minutes=len(touched) - len(values), source=src)


def ms_weight(ms: Measure) -> Measure:
    return Measure(ms.id + "_w", "", "IO/s", ms.object_type, (), "IO/sec", "Rate", ms.disk_type)


def _persist(con, a: CaptureAnalysis, all_events):
    cid = a.capture_id
    with con:
        for t in ("measure_minute", "measure_coverage", "counter_event", "derived_rate"):
            con.execute(f"DELETE FROM {t} WHERE capture_id=?", (cid,))
        for mid, mr in a.measures.items():
            series = list(mr.per_cell.items()) + ([(SYSTEM, mr.system)] if mr.system is not None else [])
            for host, s in series:
                con.execute("INSERT INTO measure_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                            (cid, mid, host, s.status, s.reason, s.source, s.expected_minutes, s.complete_minutes,
                             s.incomplete_minutes))
                con.executemany("INSERT INTO measure_minute VALUES (?,?,?,?,?)",
                                ((cid, mid, host, m, format(v, "f")) for m, v in s.values.items()))
        seen_rates = set()
        for e in all_events:
            if e[0] == "__rate__":
                _, host, metric, obj, ts, r, dt = e
                if (host, metric, obj, ts) in seen_rates:
                    continue
                seen_rates.add((host, metric, obj, ts))
                con.execute("INSERT INTO derived_rate VALUES (?,?,?,?,?,?,?)", (cid, host, metric, obj, ts, format(r, "f"), dt))
        for host, metric, obj, ts, kind, det in set(a.counter_events):
            con.execute("INSERT INTO counter_event VALUES (?,?,?,?,?,?,?)", (cid, host, metric, obj, ts, kind, det))


# ----------------------------------------------------------------------------- calendario

def calendar_coverage(ps: int, pe: int, start_raw: str) -> dict:
    """Qué parte del calendario cubre la captura y qué actividades críticas podrían faltar."""
    off = timedelta(0)
    if start_raw and not start_raw.endswith("Z") and len(start_raw) >= 6 and start_raw[-6] in "+-":
        sign = 1 if start_raw[-6] == "+" else -1
        off = sign * timedelta(hours=int(start_raw[-5:-3]), minutes=int(start_raw[-2:]))
    tz = timezone(off)
    hours = (pe - ps) / 3600
    days = set()
    t = ps
    while t < pe:
        days.add(datetime.fromtimestamp(t, tz).date())
        t += 3600
    names = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    weekdays = sorted({d.weekday() for d in days})
    month_end = any((d + timedelta(days=1)).day == 1 for d in days)
    month_start = any(d.day == 1 for d in days)
    warnings = []
    if hours < 24:
        warnings.append(f"Captura de {hours:.2f} h (< 24 h): NO apta como base para aprobar una migración.")
    if hours < 7 * 24:
        warnings.append("No cubre una semana completa: pueden faltar picos semanales.")
    if not (5 in weekdays or 6 in weekdays):
        warnings.append("No incluye fin de semana (procesos batch/backups de fin de semana no observados).")
    if not (month_end or month_start):
        warnings.append("No incluye cierre/inicio de mes: pueden faltar picos de cierre mensual.")
    warnings.append("Backups, cargas masivas, estadísticas y otras ventanas críticas: confirmar con el cliente "
                    "si ocurrieron durante la captura (la captura no lo indica).")
    return {"hours": round(hours, 3), "days": [d.isoformat() for d in sorted(days)],
            "weekdays": [names[w] for w in weekdays], "includes_weekend": bool({5, 6} & set(weekdays)),
            "includes_month_end": month_end or month_start, "warnings": warnings,
            "start_utc": epoch_to_utc(ps), "end_utc": epoch_to_utc(pe)}


# ----------------------------------------------------------------------------- combinación

@dataclass
class Combined:
    measure: Measure
    source_max: Optional[Decimal]
    source_max_minute: Optional[int]
    dest_max: Optional[Decimal]
    dest_max_minute: Optional[int]
    coincident_max: Optional[Decimal]
    coincident_minute: Optional[int]
    coincident_overlap_minutes: int
    envelope: Optional[Decimal]
    align: str
    notes: List[str]
    source_series: Dict[int, Decimal] = field(default_factory=dict)
    dest_series: Dict[int, Decimal] = field(default_factory=dict)
    coincident_series: Dict[int, Decimal] = field(default_factory=dict)

    def value(self, method: str) -> Optional[Decimal]:
        return {"envelope": self.envelope, "coincident": self.coincident_max}[method]


def combine(src: CaptureAnalysis, dst: Optional[CaptureAnalysis], align: str = "utc") -> Dict[str, Combined]:
    """Carga combinada por medida sumable: máximos separados, suma coincidente y envolvente.

    align='utc': mismo instante UTC (requiere solape real de periodos).
    align='time_of_day': mismo minuto del día UTC (aproximación explícita para capturas de días
    distintos; si hay varios días se toma el máximo de cada minuto del día).
    """
    out = {}
    for mid, mr in src.measures.items():
        ms = mr.measure
        if not ms.summable_across_cells:
            continue
        notes = []
        ss = mr.system.values if mr.system and mr.system.status == "OK" else {}
        if not ss:
            notes.append(f"Origen sin total de sistema: {mr.system.reason if mr.system else 'n/d'}")
        s_stats = series_stats(ss)
        ds, d_stats = {}, series_stats({})
        if dst is not None:
            dmr = dst.measures.get(mid)
            ds = dmr.system.values if dmr and dmr.system and dmr.system.status == "OK" else {}
            d_stats = series_stats(ds)
            if not ds:
                if dmr and dmr.system and dmr.system.status == "NOT_APPLICABLE":
                    notes.append("No aplica al destino actual (sin ese tipo de dispositivo).")
                else:
                    notes.append(f"Destino sin total de sistema: {dmr.system.reason if dmr and dmr.system else 'n/d'}")
        coinc = {}
        if dst is None:
            coinc = dict(ss)
        elif ss and ds:
            if align == "utc":
                for m in set(ss) & set(ds):
                    coinc[m] = ss[m] + ds[m]
                if not coinc:
                    notes.append("Sin minutos coincidentes en UTC: la suma coincidente no puede calcularse.")
            elif align == "time_of_day":
                notes.append("Alineación por hora del día (aproximación): los periodos no son simultáneos.")
                def tod(series):
                    agg = {}
                    for m, v in series.items():
                        k = m % 86400
                        agg[k] = max(agg.get(k, v), v)
                    return agg
                a, b = tod(ss), tod(ds)
                for k in set(a) & set(b):
                    coinc[k] = a[k] + b[k]
            else:
                raise ValueError(align)
        elif dst is not None and ss and not ds and "No aplica" in " ".join(notes):
            coinc = dict(ss)
        c_stats = series_stats(coinc)
        if dst is None:
            env = s_stats["max"]
        elif s_stats["max"] is not None and (d_stats["max"] is not None or "No aplica" in " ".join(notes)):
            env = s_stats["max"] + (d_stats["max"] or Decimal(0))
        else:
            env = None
        out[mid] = Combined(ms, s_stats["max"], s_stats["max_minute"], d_stats["max"], d_stats["max_minute"],
                            c_stats["max"], c_stats["max_minute"], len(coinc), env, align, notes, ss, ds, coinc)
    return out


def load_analysis_tables(db_path: str, capture_id: int) -> dict:
    """Lectura directa de las tablas persistidas (para pruebas de coherencia)."""
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT measure, host, minute_epoch, value FROM measure_minute WHERE capture_id=?",
                       (capture_id,)).fetchall()
    cov = con.execute("SELECT measure, host, status, complete_minutes, expected_minutes FROM measure_coverage"
                      " WHERE capture_id=?", (capture_id,)).fetchall()
    con.close()
    return {"minutes": rows, "coverage": cov}
