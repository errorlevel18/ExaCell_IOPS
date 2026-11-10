"""Pruebas del motor de dimensionamiento con análisis sintéticos exactos."""
from decimal import Decimal as D

import pytest

from exacell_sizer import catalog
from exacell_sizer.analysis import CaptureAnalysis, MeasureResult, Series, calendar_coverage
from exacell_sizer.metrics import MEASURES
from exacell_sizer.sizing import (VERDICT_INSUFFICIENT, VERDICT_NO, VERDICT_OK, ScenarioError, evaluate,
                                  load_scenario)

T0 = 1793750400  # 2026-11-04T00:00:00Z (miércoles)
CAT = catalog.load()

BASE = {  # pico por medida (sistema)
    "flash_read_iops_small": 300000, "flash_write_iops_small": 100000, "flash_read_mbps": 10000,
    "hdd_iops_small": 3000, "hdd_read_iops_small": 2000, "hdd_write_iops_small": 1000, "hdd_mbps": 2000,
    "hdd_read_mbps": 1500, "network_mbps": 5000,
}


def fa(peaks=None, cells=3, hours=24, scale=1, start=T0, peak_minute=600, unavailable=(), import_status="COMPLETE",
       coverage_drop=0, label="src"):
    """CaptureAnalysis sintético: valor = 50 % del pico salvo un minuto con el pico."""
    peaks = dict(BASE if peaks is None else peaks)
    n = int(hours * 60)
    secs = int(hours * 3600)
    minutes = [start + 60 * i for i in range(n)]
    hosts = [f"c{i}" for i in range(cells)]
    measures = {}
    for ms in MEASURES:
        if ms.id in unavailable:
            per = {h: Series({}, n, status="UNAVAILABLE", reason="métrica ausente") for h in hosts}
            measures[ms.id] = MeasureResult(ms, per, Series({}, n, status="UNAVAILABLE", reason="métrica ausente")
                                            if ms.summable_across_cells else None)
            continue
        if ms.id not in peaks:
            st = "NOT_APPLICABLE" if ms.group == "hdd" else "OK"
            per = {h: Series({}, n, status=st) for h in hosts}
            measures[ms.id] = MeasureResult(ms, per, Series({}, n, status=st) if ms.summable_across_cells else None)
            continue
        pk = D(peaks[ms.id]) * D(scale)
        keep = minutes[coverage_drop:]
        sysv = {m: (pk if i == peak_minute else pk / 2) for i, m in enumerate(minutes) if m in set(keep)}
        per = {h: Series({m: v / cells for m, v in sysv.items()}, n) for h in hosts}
        measures[ms.id] = MeasureResult(ms, per, Series(sysv, n))
    return CaptureAnalysis(1, label, "origen", import_status, start, start + secs,
                           "2026-11-04T00:00:00Z", hosts, n, measures,
                           calendar_coverage(start, start + secs, "2026-11-04T00:00:00Z"), [], {})


def run(src, cands, dst=None, **sc):
    sc.setdefault("reserve_pct", 20)
    return evaluate(CAT, src, dst, {"candidates": cands, **sc})


def res(c, r):
    return next(x for x in c.resources if x.resource == r)


# ----------------------------------------------------------------------------- sí / no / insuficiente

def test_yes_compatible_with_exact_margins_and_bottleneck():
    [c] = run(fa(), [{"id": "a", "model": "X7-2", "storage_type": "HC", "cells": 3}])
    assert c.verdict == VERDICT_OK
    mixed = res(c, "flash_iops_mixed")
    u = D(300000) / D(475000) + D(100000) / D(420000)
    assert mixed.demand == u.quantize(D("0.000001"))
    assert mixed.n_theoretical == 1 and mixed.n_recommended == 2
    assert c.n_recommended == 3            # mínimo de configuración
    hm = res(c, "hdd_mbps")
    assert hm.capacity_per_cell == D("1702.990") and hm.capacity_evidence == "derivado"
    assert hm.utilization == (D(2000) / (3 * D("1702.990"))).quantize(D("0.0001"))
    utils = {x.resource: x.utilization for x in c.resources if x.utilization is not None}
    assert c.limiting_resource == max(utils, key=utils.get) == "hdd_mbps"
    assert c.limiting_margin == 1 - utils["hdd_mbps"]
    assert "1 - 2000" in hm.margin_formula and "3 x 1702.990 MB/s" in hm.margin_formula
    assert hm.per_cell_demand == (D(2000) / 3).quantize(D("0.001"))
    assert c.operational_approval == "pendiente"


def test_no_when_fixed_cells_insufficient():
    [c] = run(fa(scale=3), [{"id": "a", "model": "X7-2", "storage_type": "HC", "cells": 3}])
    assert c.verdict == VERDICT_NO
    assert c.n_recommended == 5  # hdd_mbps: 6000 / (1702.99 * 0.8) = 4.4 -> 5 ; hdd_iops 9000/(2571.43*0.8)=4.4->5
    assert any("3 celdas <" in r for r in c.verdict_reasons)
    assert res(c, "hdd_mbps").margin < 0


def test_insufficient_short_capture():
    [c] = run(fa(hours=0.04), [{"id": "a", "model": "X7-2", "storage_type": "HC", "cells": 3}])
    assert c.verdict == VERDICT_INSUFFICIENT
    assert any("no apta" in b for b in c.blocking)
    assert all(x.margin is None for x in c.resources) and c.limiting_margin is None and c.partial


def test_insufficient_unavailable_demand_and_low_coverage():
    [c] = run(fa(unavailable=("flash_write_iops_small",)), [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert c.verdict == VERDICT_INSUFFICIENT
    assert res(c, "flash_write_iops").status == "no_evaluable"
    [c] = run(fa(coverage_drop=200), [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert c.verdict == VERDICT_INSUFFICIENT and any("cobertura" in b for b in c.blocking)
    [c] = run(fa(coverage_drop=200), [{"id": "a", "model": "X7-2", "storage_type": "HC"}], min_coverage_pct=80)
    assert c.verdict == VERDICT_OK


def test_incomplete_capture_blocks_unless_accepted():
    a = fa(import_status="INCOMPLETE")
    [c] = run(a, [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert c.verdict == VERDICT_INSUFFICIENT
    [c] = run(a, [{"id": "a", "model": "X7-2", "storage_type": "HC"}], accept_incomplete_capture=True)
    assert c.verdict == VERDICT_OK and any("INCOMPLETA" in w for w in c.warnings)


# ----------------------------------------------------------------------------- modelos y aproximaciones

def test_unknown_model_and_missing_storage_type():
    c1, c2, c3 = run(fa(), [{"id": "a", "model": "X5-2", "storage_type": "HC"},
                            {"id": "b", "model": "X7-2"},
                            {"id": "c", "model": "X7-2", "storage_type": "HC-Z"}])
    assert c1.verdict == c2.verdict == c3.verdict == VERDICT_INSUFFICIENT
    assert "tipo de celda" in c2.verdict_reasons[0]


def test_hardware_approximation_visible():
    [c] = run(fa(), [{"id": "a", "model": "X8-2", "storage_type": "HC"}])
    assert c.verdict == VERDICT_INSUFFICIENT and "autorización explícita" in c.verdict_reasons[0]
    [c] = run(fa(), [{"id": "a", "model": "X8-2", "storage_type": "HC",
                      "approximations": ["X8-2_as_X8M-2", "read_iops_memory_tier_as_flash"]}])
    assert c.model == "X8M-2" and c.approximation["id"] == "X8-2_as_X8M-2"
    assert any("APROXIMACIÓN DE HARDWARE" in w for w in c.warnings)
    assert c.verdict == VERDICT_OK
    assert "aproximación autorizada" in res(c, "flash_read_iops").capacity_evidence


def test_memory_tier_reads_need_authorisation():
    [c] = run(fa(), [{"id": "a", "model": "X10M", "storage_type": "HC"}])
    assert c.verdict == VERDICT_INSUFFICIENT
    assert res(c, "flash_read_iops").status == "no_evaluable"
    [c] = run(fa(), [{"id": "a", "model": "X10M", "storage_type": "HC"}],
              approximations=["read_iops_memory_tier_as_flash"])
    assert c.verdict == VERDICT_OK


def test_ef_with_hdd_load():
    cand = [{"id": "ef", "model": "X7-2", "storage_type": "EF"}]
    [c] = run(fa(), cand)
    assert c.verdict == VERDICT_INSUFFICIENT and any("hdd_load_to_flash" in b for b in c.blocking)
    [c] = run(fa(), cand, approximations=["hdd_load_to_flash"])
    assert c.verdict == VERDICT_OK
    assert res(c, "flash_read_iops").demand == D(300000) + D(2000)  # el pico coincide en el mismo minuto
    assert res(c, "hdd_iops").status == "no_aplica"


def test_source_without_hdd_is_not_applicable_not_zero():
    peaks = {k: v for k, v in BASE.items() if not k.startswith("hdd")}
    [c] = run(fa(peaks), [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert res(c, "hdd_iops").status == "no_aplica" and "inexistencia" in res(c, "hdd_iops").reason
    assert c.verdict == VERDICT_OK


# ----------------------------------------------------------------------------- N-1, rack, red, BD

def test_n_minus_1():
    [c] = run(fa(scale=4), [{"id": "a", "model": "X7-2", "storage_type": "HC"}],
              n_minus_1={"enabled": True, "recovery_overhead_pct": 15})
    hm = res(c, "hdd_mbps")
    exp = int(((D(8000) * D("1.15")) / D("1702.990")).to_integral_value(rounding="ROUND_CEILING")) + 1
    assert hm.n_failure == exp
    assert c.n_failure >= c.n_recommended and c.cells_evaluated == c.n_failure
    [c2] = run(fa(scale=4), [{"id": "a", "model": "X7-2", "storage_type": "HC", "cells": c.n_recommended}],
               n_minus_1={"enabled": True, "recovery_overhead_pct": 15})
    if c.n_failure > c.n_recommended:
        assert c2.verdict == VERDICT_NO and any("N-1" in r for r in c2.verdict_reasons)


def test_multirack_limit():
    [c] = run(fa(scale=20), [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert c.n_recommended > 18 and c.verdict == VERDICT_NO
    assert any("multirack" in r for r in c.verdict_reasons)
    [c] = run(fa(scale=20), [{"id": "a", "model": "X7-2", "storage_type": "HC"}], allow_multirack=True)
    assert c.verdict == VERDICT_OK and any("multirack" in w for w in c.warnings)


def test_network_nominal_ceiling_only_rejects():
    peaks = dict(BASE, network_mbps=40000)
    [c] = run(fa(peaks), [{"id": "a", "model": "X7-2", "storage_type": "HC", "cells": 3}])
    net = res(c, "network_mbps")
    assert net.status == "techo_nominal" and net.utilization > 1
    assert c.verdict == VERDICT_NO
    assert "NO es capacidad útil" in net.margin_formula


def test_db_server_limit_outside_cells():
    [c] = run(fa(scale=2), [{"id": "q", "model": "X7-2", "storage_type": "HC", "rack_config": "Eighth Rack",
                             "cells": 6}])
    # rack Eighth -> 2 servidores BD de 597,500 IOPS de lectura
    assert c.db_check["db_servers"] == 2
    [c] = run(fa(scale=4), [{"id": "q", "model": "X7-2", "storage_type": "HC", "cells": 8, "db_servers": 1}])
    assert c.db_check["status"] == "excedido" and c.verdict == VERDICT_NO


# ----------------------------------------------------------------------------- combinación origen/destino

def test_combined_envelope_vs_coincident():
    src = fa(peak_minute=100)
    dst = fa(peak_minute=900, label="dst")
    [env] = run(src, [{"id": "a", "model": "X7-2", "storage_type": "HC"}], dst=dst)
    assert res(env, "flash_write_iops").demand == D(200000)
    [coi] = run(src, [{"id": "a", "model": "X7-2", "storage_type": "HC"}], dst=dst, combination="coincident")
    assert res(coi, "flash_write_iops").demand == D(150000)  # pico + 50 % del otro
    other_day = fa(start=T0 + 3 * 86400, label="dst")
    [c] = run(src, [{"id": "a", "model": "X7-2", "storage_type": "HC"}], dst=other_day, combination="coincident")
    assert c.verdict == VERDICT_INSUFFICIENT and any("coincidentes" in b for b in c.blocking)
    [c] = run(src, [{"id": "a", "model": "X7-2", "storage_type": "HC"}], dst=other_day, combination="coincident",
              align="time_of_day")
    assert c.verdict == VERDICT_OK


def test_growth_and_statistic():
    [c] = run(fa(), [{"id": "a", "model": "X7-2", "storage_type": "HC"}], growth_pct=50, statistic="p99")
    w = res(c, "flash_write_iops")
    assert w.demand == D(50000) and w.design_demand == D(75000)


# ----------------------------------------------------------------------------- aprobación operativa

def test_operational_approval_only_with_tested_capacity_at_latency():
    tested = [{"model": "X7-2", "storage_type": "HC", "resource": r, "per_cell": v, "latency_us": 800,
               "evidence": "prueba PoC 2026-10"} for r, v in (("flash_read_iops", 400000), ("flash_write_iops", 300000),
                                                               ("flash_read_mbps", 15000), ("hdd_iops", 2000),
                                                               ("hdd_mbps", 1200))]
    base = dict(latency_target_us=1000, critical_periods_confirmed=True)
    [c] = run(fa(), [{"id": "a", "model": "X7-2", "storage_type": "HC"}], tested_capacities=tested, **base)
    assert c.verdict == VERDICT_OK and c.operational_approval.startswith("aprobada")
    assert res(c, "hdd_iops").capacity_evidence.startswith("probado")
    [c] = run(fa(), [{"id": "a", "model": "X7-2", "storage_type": "HC"}], tested_capacities=tested,
              latency_target_us=500, critical_periods_confirmed=True)
    assert c.operational_approval == "pendiente"   # probado a 800 us > objetivo 500 us
    [c] = run(fa(), [{"id": "a", "model": "X7-2", "storage_type": "HC"}])
    assert c.operational_approval == "pendiente" and any("latencia objetivo" in p for p in c.approval_pending)


def test_scenario_validation():
    with pytest.raises(ScenarioError):
        load_scenario({"candidates": []})
    with pytest.raises(ScenarioError):
        load_scenario({"candidates": [{"id": "a"}], "reserve_pct": 100})
    with pytest.raises(ScenarioError):
        load_scenario({"candidates": [{"id": "a"}, {"id": "a"}]})
    with pytest.raises(ScenarioError):
        load_scenario({"candidates": [{"id": "a"}], "statistic": "avg"})


@pytest.mark.parametrize("ncells", [2, 5, 9])
def test_per_cell_distribution_any_cell_count(ncells):
    [c] = run(fa(cells=ncells), [{"id": "a", "model": "X9M-2", "storage_type": "HC", "cells": 4}],
              approximations=["read_iops_memory_tier_as_flash"])
    for x in c.resources:
        if x.per_cell_demand is not None:
            assert x.per_cell_demand == (x.design_demand / 4).quantize(D("0.001"))
