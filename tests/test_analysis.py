"""Pruebas del análisis: minutos completos, contadores, aplicabilidad, combinación y calendario."""
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from exacell_sizer.analysis import (analyze_capture, calendar_coverage, combine, derive_counter_rates,
                                    load_analysis_tables)
from exacell_sizer.cellcli_parse import parse_timestamp
from exacell_sizer.importer import import_capture
from exacell_sizer.metrics import MEASURE_BY_ID
from exacell_sizer.simulate import SimConfig, write_capture

S, E = "2026-11-01T00:00:00+01:00", "2026-11-01T02:00:00+01:00"
T0 = parse_timestamp(S)[0]


def build(tmp_path, name="c", start=S, end=E, mutate=None, **kw):
    kw.setdefault("cells", ["cel01", "cel02"])
    kw.setdefault("hdd_per_cell", 2)
    kw.setdefault("flash_per_cell", 2)
    kw.setdefault("sec_offset", 30)
    cap = Path(write_capture(SimConfig(**kw), str(tmp_path / name), start, end))
    if mutate:
        mutate(cap)
    db = str(tmp_path / f"{name}.db")
    r = import_capture(str(cap), db)
    return db, r, analyze_capture(db, r.capture_id)


# ----------------------------------------------------------------------------- contadores

def test_counter_rates_reset_gap_and_bad_dt():
    D = Decimal
    pts = [(0, D(100)), (60, D(160)), (120, D(10)), (180, D(70)), (400, D(500)), (400, D(510)), (460, D(560))]
    rates, events = derive_counter_rates(pts, max_gap=120)
    assert [(t, r) for t, r, _ in rates] == [(60, D(1)), (180, D(1)), (460, D("0.833333"))]
    kinds = [(t, k) for t, k, _ in events]
    assert (120, "reset") in kinds and (400, "gap") in kinds and (400, "bad_dt") in kinds


def test_counter_fallback_and_reset_not_zero(tmp_path):
    reset_minute = (T0 + 3600) // 60
    db, r, a = build(tmp_path, catalog_remove={"cel02": ["CD_IO_RQ_R_SM_SEC"]},
                     counter_resets={"cel02": [reset_minute]})
    s = a.measures["flash_read_iops_small"].per_cell["cel02"]
    assert s.source == "counter"
    assert s.complete_minutes == 120 - 2      # primer minuto sin predecesor + intervalo con reinicio
    reset_bucket = (T0 + 3600 + 30) // 60 * 60
    assert reset_bucket not in s.values
    assert all(v > 0 for v in s.values.values())
    assert any(e[4] == "reset" for e in a.counter_events)
    con = sqlite3.connect(db)
    # un reinicio por celldisk (2 flash + 2 HDD), sin duplicados aunque dos medidas usen el contador
    assert con.execute("SELECT COUNT(*), COUNT(DISTINCT object_name) FROM counter_event WHERE kind='reset'").fetchone() == (4, 4)
    assert con.execute("SELECT COUNT(*) FROM derived_rate").fetchone()[0] > 0
    # el total de sistema solo usa minutos completos en ambas celdas
    assert a.measures["flash_read_iops_small"].system.complete_minutes == 118


# ----------------------------------------------------------------------------- completitud

def test_missing_object_makes_minute_incomplete(tmp_path):
    def drop(cap):
        f = cap / "data/cel01/b0000_CD_IO_RQ_W_SM_SEC.out"
        lines = [l for l in f.read_text().splitlines() if not ("FD_01_cel01" in l and "T00:10:30" in l)]
        f.write_text("\n".join(lines) + "\n")
        from tests_helpers import resign
        resign(cap)
    db, r, a = build(tmp_path, mutate=drop)
    s = a.measures["flash_write_iops_small"].per_cell["cel01"]
    assert s.complete_minutes == 119 and s.incomplete_minutes == 1
    sysm = a.measures["flash_write_iops_small"].system
    m = (T0 + 600) // 60 * 60
    assert m not in sysm.values and sysm.complete_minutes == 119
    assert sysm.coverage == Decimal("0.9917")


def test_sum_matches_raw_samples(tmp_path):
    db, r, a = build(tmp_path)
    con = sqlite3.connect(db)
    m = T0 + 1800
    raw = con.execute(
        "SELECT s.value FROM sample s JOIN celldisk c ON c.capture_id=s.capture_id AND c.host=s.host AND c.name=s.object_name"
        " WHERE s.metric='CD_IO_RQ_R_SM_SEC' AND c.disk_type='FlashDisk' AND s.ts_epoch BETWEEN ? AND ?",
        (m, m + 59)).fetchall()
    assert len(raw) == 4
    assert a.measures["flash_read_iops_small"].system.values[m] == sum(Decimal(x[0]) for x in raw)


def test_ef_cells_hdd_not_applicable_and_unavailable_metric(tmp_path):
    db, r, a = build(tmp_path, hdd_per_cell=0, catalog_remove={"cel01": ["CD_IO_BY_R_LG_SEC"]})
    assert a.measures["hdd_iops_small"].per_cell["cel01"].status == "NOT_APPLICABLE"
    assert a.measures["hdd_iops_small"].system.status == "NOT_APPLICABLE"
    fr = a.measures["flash_read_mbps"]
    assert fr.per_cell["cel01"].status == "UNAVAILABLE" and "CD_IO_BY_R_LG_SEC" in fr.per_cell["cel01"].reason
    assert fr.system.status == "UNAVAILABLE" and fr.system.values == {}


def test_non_summable_never_summed(tmp_path):
    db, r, a = build(tmp_path)
    for mid in ("cell_cpu_pct", "hdd_util_pct", "flash_read_latency_us"):
        mr = a.measures[mid]
        assert mr.system is None
        m = next(iter(mr.cells_max))
        assert mr.cells_max[m] == max(mr.per_cell[h].values[m] for h in mr.per_cell)
    assert "cell_cpu_pct" not in combine(a, a)


def test_weighted_latency(tmp_path):
    db, r, a = build(tmp_path, cells=["cel01"])
    con = sqlite3.connect(db)
    m = T0 + 600
    q = ("SELECT s.object_name, s.value FROM sample s JOIN celldisk c ON c.capture_id=s.capture_id AND c.host=s.host"
         " AND c.name=s.object_name WHERE s.metric=? AND c.disk_type='FlashDisk' AND s.ts_epoch BETWEEN ? AND ?")
    lat = dict(con.execute(q, ("CD_IO_TM_R_SM_RQ", m, m + 59)).fetchall())
    w = dict(con.execute(q, ("CD_IO_RQ_R_SM_SEC", m, m + 59)).fetchall())
    exp = sum(Decimal(lat[o]) * Decimal(w[o]) for o in lat) / sum(Decimal(w[o]) for o in lat)
    assert a.measures["flash_read_latency_us"].per_cell["cel01"].values[m] == exp.quantize(Decimal("0.000001"))


def test_unit_conversion_kb_to_mb(tmp_path):
    db, r, a = build(tmp_path, catalog_unit_override={"cel01": {"N_HCA_MB_RCV_SEC": "KB/sec", "N_HCA_MB_TRANS_SEC": "KB/sec"}})
    con = sqlite3.connect(db)
    m = T0 + 600
    raw = con.execute("SELECT metric, value FROM sample WHERE host='cel01' AND metric LIKE 'N_HCA_MB_%' AND ts_epoch BETWEEN ? AND ?",
                      (m, m + 59)).fetchall()
    exp = max(Decimal(v) for _, v in raw) / 1024
    assert a.measures["network_mbps"].per_cell["cel01"].values[m] == exp.quantize(Decimal("0.000001"))


def test_persisted_tables_match_memory(tmp_path):
    db, r, a = build(tmp_path)
    t = load_analysis_tables(db, r.capture_id)
    mem = {(mid, h, m): v for mid, mr in a.measures.items()
           for h, s in list(mr.per_cell.items()) + ([("*", mr.system)] if mr.system else []) for m, v in s.values.items()}
    assert len(t["minutes"]) == len(mem)
    for mid, h, m, v in t["minutes"]:
        assert Decimal(v) == mem[(mid, h, m)]


# ----------------------------------------------------------------------------- combinación

def test_combine_coincident_envelope_and_no_overlap(tmp_path):
    _, _, a = build(tmp_path, name="src", spikes=[{"start_minute": 0, "minutes": 10, "factor": 3}])
    _, _, b = build(tmp_path, name="dst", spikes=[{"start_minute": 60, "minutes": 10, "factor": 3}], seed="other")
    c = combine(a, b)["flash_read_iops_small"]
    assert c.envelope == c.source_max + c.dest_max
    assert c.coincident_max <= c.envelope            # picos en instantes distintos
    assert c.coincident_overlap_minutes == 120
    m = c.coincident_minute
    assert c.coincident_max == c.source_series[m] + c.dest_series[m]
    # Destino capturado otro día: sin solape UTC
    _, _, d = build(tmp_path, name="other_day", start="2026-11-02T00:00:00+01:00", end="2026-11-02T02:00:00+01:00")
    c2 = combine(a, d)["flash_read_iops_small"]
    assert c2.coincident_max is None and any("Sin minutos coincidentes" in n for n in c2.notes)
    assert c2.envelope is not None
    c3 = combine(a, d, align="time_of_day")["flash_read_iops_small"]
    assert c3.coincident_max is not None and any("aproximación" in n for n in c3.notes)


def test_combine_without_destination(tmp_path):
    _, _, a = build(tmp_path)
    c = combine(a, None)["hdd_mbps"]
    assert c.envelope == c.source_max == c.coincident_max


# ----------------------------------------------------------------------------- calendario

def test_calendar_24h_and_week():
    ps = parse_timestamp("2026-11-03T00:00:00+01:00")[0]  # martes
    cal = calendar_coverage(ps, ps + 86400, "2026-11-03T00:00:00+01:00")
    assert cal["hours"] == 24 and cal["weekdays"] == ["martes"]
    assert not any("< 24 h" in w for w in cal["warnings"])
    assert any("semana" in w for w in cal["warnings"]) and any("fin de semana" in w for w in cal["warnings"])
    assert any("cierre" in w for w in cal["warnings"])
    two_min = calendar_coverage(ps, ps + 120, "2026-11-03T00:00:00+01:00")
    assert any("NO apta" in w for w in two_min["warnings"])
    ps2 = parse_timestamp("2026-10-26T00:00:00+01:00")[0]
    week = calendar_coverage(ps2, ps2 + 7 * 86400, "2026-10-26T00:00:00+01:00")
    assert week["includes_weekend"] and week["includes_month_end"]
    assert not any("semana completa" in w for w in week["warnings"])
