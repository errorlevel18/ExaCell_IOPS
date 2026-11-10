"""Motor de dimensionamiento de celdas de almacenamiento.

Separa SIEMPRE tres cosas:
  1. Cálculo teórico (cifras de datasheet o capacidad probada vs demanda medida).
  2. Margen estimado bajo esas referencias (con fórmula, unidades, celda y perfil limitante).
  3. Aprobación operativa: «pendiente» salvo capacidad probada a la latencia objetivo,
     captura >= 24 h y periodos críticos confirmados.

Dictámenes posibles (literales):
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Dict, List, Optional, Tuple

from . import catalog as catmod
from .analysis import CaptureAnalysis, series_stats

VERDICT_OK = "Compatible bajo las referencias y supuestos indicados"
VERDICT_NO = "No compatible con los límites evaluados"
VERDICT_INSUFFICIENT = "Datos insuficientes para concluir"

# recurso -> (medida(s) de demanda)
RESOURCE_MEASURES = {
    "flash_read_iops": "flash_read_iops_small",
    "flash_write_iops": "flash_write_iops_small",
    "flash_read_mbps": "flash_read_mbps",
    "hdd_iops": "hdd_iops_small",
    "hdd_mbps": "hdd_mbps",
    "network_mbps": "network_mbps",
}
HDD_TO_FLASH = {"flash_read_iops": "hdd_read_iops_small", "flash_write_iops": "hdd_write_iops_small",
                "flash_read_mbps": "hdd_read_mbps"}
MIXED = "flash_iops_mixed"
RESOURCE_LABELS = {
    "flash_read_iops": "Flash: IOPS de lectura pequeña",
    "flash_write_iops": "Flash: IOPS de escritura pequeña (tras mirroring)",
    MIXED: "Flash: IOPS mixtas lectura+escritura (R/Rmax + W/Wmax)",
    "flash_read_mbps": "Flash: MB/s de lectura (escaneo)",
    "hdd_iops": "HDD: IOPS pequeñas",
    "hdd_mbps": "HDD: MB/s",
    "network_mbps": "Red de almacenamiento: MB/s por sentido (techo nominal)",
    "db_servers": "Servidores de BD: IOPS SQL",
}
ONE = Decimal(1)
HUNDRED = Decimal(100)


class ScenarioError(ValueError):
    pass


DEFAULTS = {
    "statistic": "max", "combination": "envelope", "align": "utc", "growth_pct": 0, "reserve_pct": 20,
    "latency_target_us": None, "n_minus_1": {"enabled": False, "recovery_overhead_pct": 15,
                                              "reserve_pct_during_failure": 0},
    "min_coverage_pct": 95, "min_capture_hours": 24, "accept_incomplete_capture": False,
    "allow_multirack": False, "apply_observed_skew": False, "approximations": [], "tested_capacities": [],
    "critical_periods_confirmed": False, "candidates": [], "allow_short_capture_for_testing": False,
}


def load_scenario(d: dict) -> dict:
    sc = {**DEFAULTS, **d}
    sc["n_minus_1"] = {**DEFAULTS["n_minus_1"], **(d.get("n_minus_1") or {})}
    if sc["statistic"] not in ("max", "p99", "p95"):
        raise ScenarioError("statistic debe ser max, p99 o p95")
    if sc["combination"] not in ("envelope", "coincident"):
        raise ScenarioError("combination debe ser envelope o coincident")
    if sc["align"] not in ("utc", "time_of_day"):
        raise ScenarioError("align debe ser utc o time_of_day")
    for k in ("growth_pct", "reserve_pct", "min_coverage_pct", "min_capture_hours"):
        v = Decimal(str(sc[k]))
        if v < 0:
            raise ScenarioError(f"{k} no puede ser negativo")
        sc[k] = v
    if sc["min_capture_hours"] < 24 and not sc["allow_short_capture_for_testing"]:
        raise ScenarioError("min_capture_hours < 24 solo se admite con allow_short_capture_for_testing=true "
                            "(una captura corta nunca sirve para aprobar una migración)")
    if sc["reserve_pct"] >= HUNDRED:
        raise ScenarioError("reserve_pct debe ser < 100")
    for k in ("recovery_overhead_pct", "reserve_pct_during_failure"):
        sc["n_minus_1"][k] = Decimal(str(sc["n_minus_1"][k]))
    if not sc["candidates"]:
        raise ScenarioError("el escenario no define candidatos")
    ids = [c.get("id") for c in sc["candidates"]]
    if None in ids or len(set(ids)) != len(ids):
        raise ScenarioError("cada candidato necesita un 'id' único")
    return sc


def _ceil(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_CEILING))


def _stat(values: Dict[int, Decimal], statistic: str) -> Optional[Decimal]:
    st = series_stats(values)
    return st["max" if statistic == "max" else statistic]


def _pct(x: Optional[Decimal]) -> Optional[str]:
    return None if x is None else f"{(x * HUNDRED).quantize(Decimal('0.1'))} %"


# ----------------------------------------------------------------------------- demanda

@dataclass
class DemandSeries:
    """Series de sistema por minuto de origen y destino actual para una medida."""
    measure: str
    src: Dict[int, Decimal]
    dst: Optional[Dict[int, Decimal]]
    src_status: str
    dst_status: Optional[str]
    src_reason: str = ""
    dst_reason: str = ""
    src_coverage: Decimal = Decimal(0)
    dst_coverage: Optional[Decimal] = None


def _series_of(a: CaptureAnalysis, mid: str):
    mr = a.measures.get(mid)
    if mr is None or mr.system is None:
        return {}, "UNAVAILABLE", "medida no calculada", Decimal(0)
    s = mr.system
    return dict(s.values), s.status, s.reason, s.coverage


def demand_series(src: CaptureAnalysis, dst: Optional[CaptureAnalysis], mid: str) -> DemandSeries:
    sv, ss, sr, sc = _series_of(src, mid)
    if dst is None:
        return DemandSeries(mid, sv, None, ss, None, sr, "", sc, None)
    dv, ds, dr, dc = _series_of(dst, mid)
    return DemandSeries(mid, sv, dv, ss, ds, sr, dr, sc, dc)


def _align(a: Dict[int, Decimal], b: Dict[int, Decimal], align: str) -> Dict[int, Decimal]:
    if align == "utc":
        return {m: a[m] + b[m] for m in set(a) & set(b)}
    def tod(x):
        agg = {}
        for m, v in x.items():
            k = m % 86400
            agg[k] = max(agg.get(k, v), v)
        return agg
    ta, tb = tod(a), tod(b)
    return {k: ta[k] + tb[k] for k in set(ta) & set(tb)}


def reduce_demand(src: Dict[int, Decimal], dst: Optional[Dict[int, Decimal]], sc: dict) -> Tuple[Optional[Decimal], str]:
    """Aplica estadística y método de combinación. Devuelve (valor, descripción)."""
    st = sc["statistic"]
    if dst is None:
        return _stat(src, st), f"{st} de minutos completos del origen"
    if sc["combination"] == "envelope":
        a, b = _stat(src, st), _stat(dst, st)
        if a is None or b is None:
            return None, "envolvente no calculable (falta origen o destino)"
        return a + b, f"envolvente conservadora: {st}(origen) + {st}(destino actual), aunque ocurran en instantes distintos"
    comb = _align(src, dst, sc["align"])
    if not comb:
        return None, "suma coincidente no calculable: sin minutos coincidentes"
    return _stat(comb, st), f"{st} de la suma en instantes coincidentes ({sc['align']})"


# ----------------------------------------------------------------------------- resultados

@dataclass
class ResourceResult:
    resource: str
    label: str
    status: str                   # evaluado | no_aplica | no_evaluable | techo_nominal
    reason: str = ""
    unit: str = ""
    demand: Optional[Decimal] = None
    demand_method: str = ""
    demand_coverage: Dict[str, str] = field(default_factory=dict)
    design_demand: Optional[Decimal] = None
    capacity_per_cell: Optional[Decimal] = None
    capacity_evidence: str = ""
    reference: Optional[dict] = None
    references: List[dict] = field(default_factory=list)
    n_theoretical: Optional[int] = None
    n_recommended: Optional[int] = None
    n_failure: Optional[int] = None
    utilization: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    margin_formula: str = ""
    per_cell_demand: Optional[Decimal] = None
    warnings: List[str] = field(default_factory=list)
    blocking: bool = False

    def to_dict(self):
        d = {}
        for k, v in self.__dict__.items():
            d[k] = str(v) if isinstance(v, Decimal) else v
        return d


@dataclass
class CandidateResult:
    candidate: dict
    model: Optional[str]
    storage_type: Optional[str]
    approximation: Optional[dict]
    verdict: str
    verdict_reasons: List[str]
    cells_evaluated: Optional[int]
    n_theoretical: Optional[int]
    n_config_minimum: Optional[int]
    n_recommended: Optional[int]
    n_failure: Optional[int]
    limiting_resource: Optional[str]
    limiting_margin: Optional[Decimal]
    resources: List[ResourceResult]
    db_check: Optional[dict]
    rack_limits: dict
    operational_approval: str
    approval_pending: List[str]
    assumptions: List[str]
    warnings: List[str]
    blocking: List[str]
    partial: bool = False          # cifras de celdas = cota inferior parcial (faltan recursos)

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "resources"}
        d["limiting_margin"] = str(self.limiting_margin) if self.limiting_margin is not None else None
        d["resources"] = [r.to_dict() for r in self.resources]
        return d


# ----------------------------------------------------------------------------- motor

def _quality_gates(src: CaptureAnalysis, dst: Optional[CaptureAnalysis], sc: dict) -> Tuple[List[str], List[str], dict]:
    blocking, warnings = [], []
    flags = {"short_capture": False}
    for a, name in ((src, "origen"), (dst, "destino actual")):
        if a is None:
            continue
        hours = Decimal(a.period_end - a.period_start) / 3600
        if hours < 24:
            flags["short_capture"] = True
        if hours < sc["min_capture_hours"]:
            blocking.append(f"Captura {name} de {hours.quantize(Decimal('0.01'))} h < {sc['min_capture_hours']} h: "
                            "no apta para aprobar una migración.")
        if a.import_status != "COMPLETE":
            msg = (f"Captura {name} INCOMPLETA (bloques con error, vacíos, interrumpidos o integridad no verificada).")
            if sc["accept_incomplete_capture"]:
                warnings.append(msg + " Aceptada explícitamente en el escenario: el dictamen depende de la cobertura.")
            else:
                blocking.append(msg)
        warnings.extend(f"[{name}] {w}" for w in a.calendar["warnings"])
    if not sc["critical_periods_confirmed"]:
        warnings.append("Periodos críticos (picos semanales, cierres, backups) no confirmados por el cliente.")
    if sc["allow_short_capture_for_testing"]:
        warnings.insert(0, "MODO PRUEBA: se admiten capturas cortas; el resultado no sirve para decidir una compra "
                           "ni una migración.")
    return blocking, warnings, flags


def _tested(sc, model, stype, resource, latency_target):
    for t in sc["tested_capacities"]:
        if t.get("model") == model and t.get("storage_type") == stype and t.get("resource") == resource:
            lat = t.get("latency_us")
            if latency_target is not None and (lat is None or Decimal(str(lat)) > Decimal(str(latency_target))):
                continue
            return t
    return None


def _skew(a: CaptureAnalysis, mid: str) -> Optional[Decimal]:
    """Desequilibrio observado: máx por celda / media por celda en el minuto pico del sistema."""
    mr = a.measures.get(mid)
    if not mr or not mr.system or not mr.system.values:
        return None
    m = series_stats(mr.system.values)["max_minute"]
    vals = [s.values[m] for s in mr.per_cell.values() if s.status == "OK" and m in s.values]
    if not vals or sum(vals) == 0:
        return None
    return (max(vals) / (sum(vals) / len(vals))).quantize(Decimal("0.001"))


def evaluate(cat: catmod.Catalog, src: CaptureAnalysis, dst: Optional[CaptureAnalysis], scenario: dict
             ) -> List[CandidateResult]:
    sc = load_scenario(scenario)
    gates_block, gates_warn, flags = _quality_gates(src, dst, sc)
    out = []
    for cand in sc["candidates"]:
        out.append(_evaluate_candidate(cat, src, dst, sc, dict(cand), gates_block, gates_warn, flags))
    return out


def _evaluate_candidate(cat, src, dst, sc, cand, gates_block, gates_warn, flags) -> CandidateResult:
    approx_list = list(sc["approximations"]) + list(cand.get("approximations", []))
    blocking = list(gates_block)
    warnings = list(gates_warn)
    assumptions = [
        "Las cifras de datasheet son máximos comerciales, no capacidad garantizada ni segura bajo latencia objetivo.",
        "Escalado lineal con el número de celdas y reparto uniforme de la carga entre celdas (ASM).",
        f"Demanda: estadística '{sc['statistic']}' sobre minutos completos; crecimiento {sc['growth_pct']} %; "
        f"reserva {sc['reserve_pct']} % (utilización máxima admitida {HUNDRED - sc['reserve_pct']} %).",
        "IOPS flash mixtas: compensación lineal entre máximos publicados de lectura y escritura "
        "(R/Rmax + W/Wmax <= 1); los máximos no son simultáneos.",
        "E/S 'pequeñas' de CellCLI (<=128 KB) comparadas con cifras publicadas para 8 KB "
        "(si el tamaño medio es mayor, la capacidad real en IOPS es menor).",
    ]
    model = stype = None
    ap = None
    try:
        if not cand.get("model"):
            raise catmod.UnknownModel("el candidato no indica modelo")
        model_d, ap = cat.resolve(cand["model"], approx_list)
        model = model_d["model"]
        if ap:
            warnings.append(f"APROXIMACIÓN DE HARDWARE '{ap['id']}': se usa {ap['use_model']} para {ap['requested']}. {ap['warning']}")
        if not cand.get("storage_type"):
            raise catmod.UnknownModel("el candidato no indica el tipo de celda (HC, EF, HC-Z, ...)")
        server = cat.server(model_d, cand["storage_type"])
        if server["role"] != "storage":
            raise catmod.UnknownModel(f"{cand['storage_type']} no es un tipo de celda")
        stype = server["id"]
    except catmod.UnknownModel as e:
        return CandidateResult(cand, model, stype, ap, VERDICT_INSUFFICIENT, [str(e)] + blocking, cand.get("cells"),
                               None, None, None, None, None, None, [], None, {}, "pendiente",
                               ["Modelo/configuración no determinados"], assumptions, warnings, [str(e)] + blocking)

    refs, unav = cat.cell_references(model_d, stype, approx_list)
    limits = model_d["limits"]
    min_cells = int(limits.get("min_storage_servers", 3))
    max_cells = int(limits.get("max_hc_storage_servers_per_rack", limits["max_storage_servers_per_rack"])
                    if stype == "HC" else limits["max_storage_servers_per_rack"])
    rack_limits = {"min_storage_servers": min_cells, "max_storage_servers_per_rack": max_cells,
                   "page": limits.get("page"), "source": cat.source(model_d)["file"]}
    if stype in ("HC", "HC-EIGHTH", "HC-Z"):
        assumptions.append("En HC la flash actúa como caché: la proporción de aciertos observada en el origen "
                           "puede no mantenerse en el destino (otra capacidad de flash/memoria).")
    n_fixed = cand.get("cells")
    rack_cfg = None
    if cand.get("rack_config"):
        try:
            rack_cfg = cat.rack_config(model_d, cand["rack_config"])
            n_cfg = rack_cfg.get("storage_servers_by_type", {}).get(stype, rack_cfg["storage_servers"])
            if n_fixed is None:
                n_fixed = n_cfg
            elif n_fixed != n_cfg:
                warnings.append(f"cells={n_fixed} difiere de las {n_cfg} celdas de '{rack_cfg['name']}'.")
            if stype not in rack_cfg["storage_ids"]:
                blocking.append(f"'{rack_cfg['name']}' no se publica con celdas {stype}.")
            cand.setdefault("db_servers", rack_cfg["db_servers"])
            cand.setdefault("db_server_type", rack_cfg.get("db_server_id", "DB"))
        except catmod.UnknownModel as e:
            blocking.append(str(e))

    growth = ONE + sc["growth_pct"] / HUNDRED
    max_util = ONE - sc["reserve_pct"] / HUNDRED
    n1 = sc["n_minus_1"]
    rec_f = ONE + n1["recovery_overhead_pct"] / HUNDRED
    fail_util = ONE - n1["reserve_pct_during_failure"] / HUNDRED

    srv_has_hdd = server.get("has_hdd", False)
    srv_has_flash = server.get("has_flash", False)

    # ---------- series de demanda por recurso
    series = {r: demand_series(src, dst, m) for r, m in RESOURCE_MEASURES.items()}
    hdd_moved = False
    hdd_demand_present = any(series[r].src_status == "OK" and series[r].src for r in ("hdd_iops", "hdd_mbps")) or \
        any(series[r].dst_status == "OK" and series[r].dst for r in ("hdd_iops", "hdd_mbps"))
    if not srv_has_hdd and hdd_demand_present:
        if "hdd_load_to_flash" in approx_list:
            hdd_moved = True
            warnings.append("APROXIMACIÓN 'hdd_load_to_flash': " + cat.data["metric_approximations"]["hdd_load_to_flash"])
            for r, hm in HDD_TO_FLASH.items():
                h = demand_series(src, dst, hm)
                ds = series[r]
                ds.src = _add_series(ds.src, h.src) if h.src_status == "OK" else ds.src
                if ds.dst is not None and h.dst is not None and h.dst_status == "OK":
                    ds.dst = _add_series(ds.dst, h.dst)
        else:
            blocking.append(f"El origen tiene carga HDD pero {stype} no tiene discos HDD: trasladarla a flash requiere "
                            "la aproximación autorizada 'hdd_load_to_flash'.")

    results: List[ResourceResult] = []
    for r in ("flash_read_iops", "flash_write_iops", "flash_read_mbps", "hdd_iops", "hdd_mbps", "network_mbps"):
        results.append(_resource(r, series[r], refs, unav, sc, cand, model, stype, growth, max_util, rec_f, fail_util,
                                 srv_has_hdd, srv_has_flash, hdd_moved, src))
    mixed = _mixed(series, refs, unav, sc, cand, model, stype, growth, max_util, rec_f, fail_util, srv_has_flash)
    results.insert(2, mixed)

    # ---------- agregación
    evaluated = [x for x in results if x.status in ("evaluado", "techo_nominal")]
    for x in results:
        if x.blocking:
            blocking.append(f"{x.label}: {x.reason}")
    n_theo = max([x.n_theoretical for x in evaluated if x.n_theoretical is not None], default=None)
    n_rec_load = max([x.n_recommended for x in evaluated if x.n_recommended is not None], default=None)
    n_rec = max(n_rec_load, min_cells) if n_rec_load is not None else None
    n_fail = None
    if n1["enabled"]:
        n_fail = max([x.n_failure for x in evaluated if x.n_failure is not None], default=None)
        if n_fail is not None:
            n_fail = max(n_fail, n_rec)

    n_eval = n_fixed if n_fixed is not None else (n_fail if n1["enabled"] and n_fail else n_rec)
    reasons: List[str] = []
    # utilización y margen con n_eval
    for x in evaluated:
        _fill_margin(x, n_eval, max_util)
    limiting = None
    if evaluated and n_eval:
        cand_lim = [x for x in evaluated if x.utilization is not None]
        if cand_lim:
            limiting = max(cand_lim, key=lambda x: x.utilization)

    # ---------- servidores de BD
    db_check = _db_check(cat, model_d, cand, src, dst, sc, growth, max_util, ap)
    if db_check and db_check.get("status") == "no_evaluable":
        warnings.append(db_check["reason"])

    # ---------- dictamen
    if blocking:
        verdict = VERDICT_INSUFFICIENT
        reasons = blocking
    elif n_rec is None:
        verdict = VERDICT_INSUFFICIENT
        reasons = ["Ningún recurso evaluable con las referencias disponibles."]
    else:
        violations = []
        if n_fixed is not None:
            if n_fixed < min_cells:
                violations.append(f"{n_fixed} celdas < mínimo de configuración {min_cells}.")
            if n_fixed < n_rec:
                violations.append(f"{n_fixed} celdas < {n_rec} recomendadas (recurso limitante: "
                                  f"{limiting.label if limiting else 'n/d'}).")
            if n1["enabled"] and n_fail is not None and n_fixed < n_fail:
                violations.append(f"{n_fixed} celdas < {n_fail} necesarias con N-1 (fallo de una celda + "
                                  f"{n1['recovery_overhead_pct']} % de recuperación).")
        target = n_eval or n_rec
        if target > max_cells:
            msg = (f"Se necesitan {target} celdas y el máximo por rack de {model} {stype} es {max_cells} "
                   f"(p.{limits.get('page')}).")
            if sc["allow_multirack"]:
                warnings.append(msg + " Configuración multirack aceptada en el escenario: verificar red y homogeneidad.")
            else:
                violations.append(msg + " Requiere multirack (no autorizado en el escenario).")
        if db_check and db_check.get("status") == "excedido":
            violations.append(db_check["reason"])
        nominal = [x for x in results if x.status == "techo_nominal" and x.utilization is not None and x.utilization > ONE]
        for x in nominal:
            violations.append(f"{x.label}: la demanda supera el techo físico nominal.")
        if violations:
            verdict, reasons = VERDICT_NO, violations
        else:
            verdict = VERDICT_OK
            reasons = [f"{n_eval} celdas {model} {stype}: todas las utilizaciones de diseño <= {_pct(max_util)} "
                       f"y dentro de los límites de configuración evaluados."]
    if verdict == VERDICT_INSUFFICIENT:
        for x in results:  # no mostrar márgenes positivos sin información esencial
            x.margin = None
            x.margin_formula = "no se muestra margen: faltan datos esenciales"

    # ---------- aprobación operativa
    pending = []
    if sc["latency_target_us"] is None:
        pending.append("No se ha definido latencia objetivo: la capacidad segura bajo latencia no está evaluada.")
    for x in evaluated:
        if x.resource == "network_mbps":
            continue
        if not x.capacity_evidence.startswith("probado"):
            pending.append(f"{x.label}: capacidad no probada en destino"
                           + (f" a {sc['latency_target_us']} µs" if sc["latency_target_us"] is not None else "") + ".")
    if flags["short_capture"]:
        pending.append("Captura inferior a 24 h.")
    if not sc["critical_periods_confirmed"]:
        pending.append("Periodos críticos no confirmados.")
    if verdict != VERDICT_OK:
        approval = "no aplicable (el dictamen teórico no es compatible)" if verdict == VERDICT_NO else "pendiente"
    else:
        approval = "aprobada (capacidad probada bajo latencia objetivo)" if not pending else "pendiente"

    partial = verdict == VERDICT_INSUFFICIENT
    lim_margin = limiting.margin if (limiting and not partial) else None
    lim_res = limiting.resource if (limiting and not partial) else None
    return CandidateResult(cand, model, stype, ap, verdict, reasons, n_eval, n_theo, min_cells, n_rec, n_fail,
                           lim_res, lim_margin, results, db_check, rack_limits,
                           approval, pending, assumptions, warnings, blocking, partial)


def _add_series(a: Dict[int, Decimal], b: Dict[int, Decimal]) -> Dict[int, Decimal]:
    return {m: a[m] + b[m] for m in set(a) & set(b)}


def _coverage(ds: DemandSeries) -> Dict[str, str]:
    d = {"origen": _pct(ds.src_coverage)}
    if ds.dst is not None:
        d["destino"] = _pct(ds.dst_coverage)
    return d


def _resource(r, ds: DemandSeries, refs, unav, sc, cand, model, stype, growth, max_util, rec_f, fail_util,
              has_hdd, has_flash, hdd_moved, src) -> ResourceResult:
    rr = ResourceResult(r, RESOURCE_LABELS[r], "evaluado", unit="MB/s" if r.endswith("mbps") else "IO/s")
    rr.demand_coverage = _coverage(ds)
    is_hdd = r.startswith("hdd")
    if is_hdd and not has_hdd:
        rr.status = "no_aplica"
        rr.reason = (f"{stype} sin HDD; carga trasladada a flash (aproximación)" if hdd_moved
                     else f"{stype} sin HDD")
        return rr
    if r.startswith("flash") and not has_flash:
        rr.status = "no_aplica"
        rr.reason = f"{stype} sin flash"
        src_has = ds.src_status == "OK" and ds.src and max(ds.src.values()) > 0
        if src_has:
            rr.blocking = True
            rr.reason = f"{stype} sin flash pero el origen tiene carga flash"
        return rr
    # estado de la demanda
    if ds.src_status == "NOT_APPLICABLE" and (ds.dst is None or ds.dst_status == "NOT_APPLICABLE") and not hdd_moved:
        rr.status = "no_aplica"
        rr.reason = "sin dispositivos de este tipo en el origen (demanda nula por inexistencia, no por omisión)"
        return rr
    if ds.src_status not in ("OK", "NOT_APPLICABLE"):
        rr.status, rr.blocking = "no_evaluable", True
        rr.reason = f"demanda del origen no disponible: {ds.src_reason}"
        return rr
    if ds.dst is not None and ds.dst_status not in ("OK", "NOT_APPLICABLE"):
        rr.status, rr.blocking = "no_evaluable", True
        rr.reason = f"demanda del destino actual no disponible: {ds.dst_reason}"
        return rr
    min_cov = sc["min_coverage_pct"] / HUNDRED
    for name, cov, stt in (("origen", ds.src_coverage, ds.src_status), ("destino actual", ds.dst_coverage, ds.dst_status)):
        if cov is not None and stt == "OK" and cov < min_cov:
            rr.status, rr.blocking = "no_evaluable", True
            rr.reason = f"cobertura {name} {_pct(cov)} < mínimo {_pct(min_cov)}"
            return rr
    src_vals = ds.src if ds.src_status == "OK" else {}
    dst_vals = None if ds.dst is None else (ds.dst if ds.dst_status == "OK" else {})
    if dst_vals is not None and not dst_vals and ds.dst_status == "NOT_APPLICABLE":
        dst_vals = None
    demand, method = reduce_demand(src_vals, dst_vals, sc)
    if demand is None:
        rr.status, rr.blocking = "no_evaluable", True
        rr.reason = method
        return rr
    if sc["apply_observed_skew"]:
        sk = _skew(src, RESOURCE_MEASURES[r])
        if sk and sk > 1:
            demand = demand * sk
            method += f"; x{sk} por desequilibrio observado entre celdas"
    rr.demand, rr.demand_method = demand, method
    rr.design_demand = (demand * growth).quantize(Decimal("0.001"))
    # capacidad
    tested = _tested(sc, model, stype, r, sc["latency_target_us"])
    ref = refs.get(r)
    if tested:
        cap = Decimal(str(tested["per_cell"]))
        rr.capacity_evidence = f"probado ({tested.get('evidence', 'sin referencia')}; latencia {tested.get('latency_us')} µs)"
        rr.reference = {"tested": {k: str(v) for k, v in tested.items()}}
    elif ref:
        cap = ref.value
        rr.capacity_evidence = ref.evidence
        rr.reference = ref.to_dict()
        rr.warnings.extend(w for w in ref.warnings if w)
    else:
        u = unav.get(r)
        rr.status = "no_evaluable"
        rr.reason = f"sin referencia utilizable: {u.reason if u else 'no publicada'}"
        if demand > 0:
            rr.blocking = True
        return rr
    rr.capacity_per_cell = cap
    if ref and ref.kind == "nominal_link" and not tested:
        rr.status = "techo_nominal"
    rr.n_theoretical = _ceil(demand / cap) if demand > 0 else 0
    rr.n_recommended = _ceil(rr.design_demand / (cap * max_util)) if demand > 0 else 0
    rr.n_failure = (_ceil(rr.design_demand * rec_f / (cap * fail_util)) + 1) if demand > 0 else 0
    return rr


def _mixed(series, refs, unav, sc, cand, model, stype, growth, max_util, rec_f, fail_util, has_flash) -> ResourceResult:
    rr = ResourceResult(MIXED, RESOURCE_LABELS[MIXED], "evaluado", unit="fracción de capacidad de 1 celda")
    if not has_flash:
        rr.status, rr.reason = "no_aplica", f"{stype} sin flash"
        return rr
    R, W = series["flash_read_iops"], series["flash_write_iops"]
    rr.demand_coverage = {"lectura": _coverage(R), "escritura": _coverage(W)}
    caps = {}
    for key, res in (("R", "flash_read_iops"), ("W", "flash_write_iops")):
        t = _tested(sc, model, stype, res, sc["latency_target_us"])
        if t:
            caps[key] = (Decimal(str(t["per_cell"])), "probado")
        elif refs.get(res):
            caps[key] = (refs[res].value, refs[res].evidence)
            rr.references.append(refs[res].to_dict())
        else:
            u = unav.get(res)
            rr.status, rr.blocking = "no_evaluable", True
            rr.reason = f"falta la referencia de {res}: {u.reason if u else 'no publicada'}"
            return rr
    if R.src_status != "OK" or W.src_status != "OK" or (R.dst is not None and (R.dst_status != "OK" or W.dst_status != "OK")):
        rr.status, rr.blocking = "no_evaluable", True
        rr.reason = "demanda de lectura o escritura flash no disponible"
        return rr
    min_cov = sc["min_coverage_pct"] / HUNDRED
    for cov in (R.src_coverage, W.src_coverage, R.dst_coverage, W.dst_coverage):
        if cov is not None and cov < min_cov:
            rr.status, rr.blocking = "no_evaluable", True
            rr.reason = f"cobertura {_pct(cov)} < mínimo {_pct(min_cov)}"
            return rr
    rmax, wmax = caps["R"][0], caps["W"][0]

    def u_series(rs, ws):
        return {m: (rs[m] / rmax + ws[m] / wmax).quantize(Decimal("0.000001")) for m in set(rs) & set(ws)}

    us = u_series(R.src, W.src)
    ud = u_series(R.dst, W.dst) if R.dst is not None else None
    demand, method = reduce_demand(us, ud, sc)
    if demand is None:
        rr.status, rr.blocking = "no_evaluable", True
        rr.reason = method
        return rr
    rr.demand = demand
    rr.demand_method = (f"{method}; por minuto u = R/{rmax} + W/{wmax} (celdas-equivalentes de "
                        "capacidad de 1 celda)")
    rr.design_demand = (demand * growth).quantize(Decimal("0.000001"))
    rr.capacity_per_cell = ONE
    rr.capacity_evidence = "probado" if caps["R"][1] == caps["W"][1] == "probado" else \
        f"lectura: {caps['R'][1]}; escritura: {caps['W'][1]}"
    rr.n_theoretical = _ceil(demand) if demand > 0 else 0
    rr.n_recommended = _ceil(rr.design_demand / max_util) if demand > 0 else 0
    rr.n_failure = (_ceil(rr.design_demand * rec_f / fail_util) + 1) if demand > 0 else 0
    rr.warnings.append("Hipótesis de compensación lineal entre los máximos de lectura y escritura publicados.")
    return rr


def _fill_margin(x: ResourceResult, n: Optional[int], max_util: Decimal):
    if x.design_demand is None or x.capacity_per_cell is None or not n:
        return
    cap_total = x.capacity_per_cell * n
    x.utilization = (x.design_demand / cap_total).quantize(Decimal("0.0001"))
    x.margin = (ONE - x.utilization).quantize(Decimal("0.0001"))
    x.per_cell_demand = (x.design_demand / n).quantize(Decimal("0.001"))
    unit = x.unit
    x.margin_formula = (f"margen = 1 - demanda_diseño / (celdas x capacidad_por_celda) = 1 - {x.design_demand} {unit} / "
                        f"({n} x {x.capacity_per_cell} {unit}) = {_pct(x.margin)}; utilización {_pct(x.utilization)} "
                        f"(máx. admitida {_pct(max_util)}); demanda por celda {x.per_cell_demand} {unit} "
                        f"suponiendo reparto uniforme")
    if x.status == "techo_nominal":
        x.margin_formula += ". Techo físico nominal: este margen NO es capacidad útil."


def _db_check(cat, model_d, cand, src, dst, sc, growth, max_util, ap) -> Optional[dict]:
    n_db = cand.get("db_servers")
    if not n_db:
        return {"status": "no_evaluado", "reason": "Servidores de BD no indicados: límite de rack por BD no evaluado."}
    if ap:
        return {"status": "no_evaluable", "reason": f"Límite de servidores de BD no evaluable con la aproximación {ap['id']}."}
    try:
        refs = cat.db_references(model_d, cand.get("db_server_type", "DB"))
    except catmod.UnknownModel as e:
        return {"status": "no_evaluable", "reason": str(e)}
    out = {"status": "dentro", "db_servers": n_db, "db_server_type": cand.get("db_server_type", "DB"), "checks": []}
    for res, measures in (("db_read_iops", ("flash_read_iops_small", "hdd_read_iops_small")),
                          ("db_write_iops", ("flash_write_iops_small", "hdd_write_iops_small"))):
        ref = refs.get(res)
        if not ref:
            continue
        vals, dvals = None, None
        for mid in measures:
            ds = demand_series(src, dst, mid)
            if ds.src_status == "OK":
                vals = ds.src if vals is None else _add_series(vals, ds.src)
            if ds.dst is not None and ds.dst_status == "OK":
                dvals = ds.dst if dvals is None else _add_series(dvals, ds.dst)
        if dst is not None and dvals is None:
            dvals = {}
        demand, method = reduce_demand(vals or {}, dvals, sc)
        if demand is None:
            out["checks"].append({"resource": res, "status": "no_evaluable", "reason": method})
            continue
        dd = demand * growth
        cap = ref.value * n_db
        util = (dd / cap).quantize(Decimal("0.0001"))
        out["checks"].append({"resource": res, "design_demand": str(dd), "capacity": str(cap),
                              "utilization": _pct(util), "reference": ref.to_dict()})
        if util > max_util:
            out["status"] = "excedido"
            out["reason"] = (f"Servidores de BD ({n_db} x {ref.value} IOPS, {res}) con utilización de diseño "
                             f"{_pct(util)} > {_pct(max_util)}: el límite está fuera de las celdas.")
    return out


# ----------------------------------------------------------------------------- carga actual vs referencia

def current_load_vs_reference(cat: catmod.Catalog, a: CaptureAnalysis, model: str, storage_type: str,
                              approximations: Optional[List[str]] = None) -> List[dict]:
    """Utilización observada del sistema medido frente a su propia referencia publicada (sin crecimiento)."""
    try:
        model_d, ap = cat.resolve(model, approximations)
        refs, unav = cat.cell_references(model_d, storage_type, approximations)
    except catmod.UnknownModel as e:
        return [{"resource": "*", "status": "no_evaluable", "reason": str(e)}]
    n = len(a.cells)
    out = []
    for r, mid in RESOURCE_MEASURES.items():
        mr = a.measures.get(mid)
        ref = refs.get(r)
        row = {"resource": r, "label": RESOURCE_LABELS[r], "cells": n}
        if not mr or not mr.system or mr.system.status != "OK":
            row.update(status="sin_demanda", reason=mr.system.reason if mr and mr.system else "n/d")
        elif not ref:
            row.update(status="sin_referencia", reason=unav[r].reason if r in unav else "n/d")
        else:
            st = series_stats(mr.system.values)
            cap = ref.value * n
            row.update(status="ok", observed_max=str(st["max"]), reference_total=str(cap),
                       reference_per_cell=str(ref.value), evidence=ref.evidence,
                       utilization=_pct((st["max"] / cap).quantize(Decimal("0.0001"))))
        out.append(row)
    return out
