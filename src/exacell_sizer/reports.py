"""Informes: modelo único -> JSON, CSV (Excel), Markdown y HTML autocontenido.

Todos los formatos se generan a partir del MISMO modelo (``build_model``), de modo que las
cifras coinciden por construcción. Las series temporales completas van en ``series.csv``
(y en SQLite, tabla ``measure_minute``); el JSON y los informes contienen agregados.
"""
from __future__ import annotations

import csv
import html
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .analysis import CaptureAnalysis, Combined, combine, series_stats
from .cellcli_parse import epoch_to_utc
from .collector import script_version
from .sizing import (MIXED, RESOURCE_LABELS, RESOURCE_MEASURES, VERDICT_INSUFFICIENT, VERDICT_NO, VERDICT_OK,
                     CandidateResult)
from .svgchart import CHART_CSS, CHART_JS, bar_chart, line_chart

GUIDE = Path(__file__).resolve().parent / "resources" / "GUIA_INTERPRETACION.md"
LOCALES = {"es": {"sep": ";", "dec": ",", "desc": "separador ';', decimal ',' (Excel en español)"},
           "c": {"sep": ",", "dec": ".", "desc": "separador ',', decimal '.' (Excel en inglés / herramientas)"}}


# ----------------------------------------------------------------------------- formato

def d2s(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def fmt_es(v, dec: int = 2) -> str:
    """Número con formato español (miles '.', decimal ',') para MD/HTML."""
    if v is None or v == "":
        return "—"
    d = Decimal(str(v))
    q = d.quantize(Decimal(1).scaleb(-dec)) if dec > 0 else d.quantize(Decimal(1))
    s = f"{q:,.{dec}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def pct_es(v, dec: int = 1) -> str:
    if v is None:
        return "—"
    return fmt_es(Decimal(str(v)) * 100, dec) + " %"


def ts_utc(m) -> Optional[str]:
    return None if m is None else epoch_to_utc(int(m))


VERDICT_ICON = {VERDICT_OK: ("✔", "good"), VERDICT_NO: ("✖", "critical"), VERDICT_INSUFFICIENT: ("?", "warning")}


# ----------------------------------------------------------------------------- modelo

def _stats_dict(st: dict) -> dict:
    return {"count": st["count"], "max": d2s(st["max"]), "max_minute_utc": ts_utc(st["max_minute"]),
            "p99": d2s(st["p99"]), "p95": d2s(st["p95"]), "p50": d2s(st["p50"]), "mean": d2s(st["mean"])}


def _series_dict(s) -> dict:
    return {"status": s.status, "reason": s.reason, "source": s.source, "expected_minutes": s.expected_minutes,
            "complete_minutes": s.complete_minutes, "incomplete_minutes": s.incomplete_minutes,
            "coverage": d2s(s.coverage), "stats": _stats_dict(s.stats())}


def capture_block(db_path: str, a: CaptureAnalysis) -> dict:
    con = sqlite3.connect(db_path)
    cap = con.execute("SELECT source_path, collector_version, dbnode_host, remote_user, reported_status, "
                      "checksum_status, sample_count, period_start_raw, period_end_raw FROM capture WHERE id=?",
                      (a.capture_id,)).fetchone()
    issues = [{"severity": s, "code": c, "count": n} for s, c, n in con.execute(
        "SELECT severity, code, COUNT(*) FROM issue WHERE capture_id=? GROUP BY severity, code "
        "ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, code", (a.capture_id,))]
    sample = [dict(zip(("severity", "code", "host", "metric", "message", "source_file", "source_line"), r))
              for r in con.execute("SELECT severity, code, host, metric, message, source_file, source_line FROM issue "
                                   "WHERE capture_id=? ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 "
                                   "ELSE 2 END, id LIMIT 60", (a.capture_id,))]
    con.close()
    measures = []
    for mid, mr in a.measures.items():
        ms = mr.measure
        measures.append({
            "id": mid, "label": ms.label, "unit": ms.unit, "group": ms.group, "summable": ms.summable_across_cells,
            "resource": ms.resource, "components": list(ms.components), "disk_type": ms.disk_type, "note": ms.note,
            "system": _series_dict(mr.system) if mr.system is not None else None,
            "cells_max_stats": None if ms.summable_across_cells else _stats_dict(series_stats(mr.cells_max)),
            "per_cell": {h: _series_dict(s) for h, s in mr.per_cell.items()},
        })
    return {
        "capture_id": a.capture_id, "label": a.label, "role": a.role, "import_status": a.import_status,
        "source_path": cap[0], "collector_version": cap[1], "dbnode_host": cap[2], "remote_user": cap[3],
        "reported_status": cap[4], "checksum_status": cap[5], "sample_count": cap[6],
        "period": {"start_raw": cap[7], "end_raw": cap[8], "start_utc": epoch_to_utc(a.period_start),
                   "end_utc": epoch_to_utc(a.period_end), "expected_minutes": a.expected_minutes},
        "cells": a.cells, "cell_models": a.cell_models, "calendar": a.calendar,
        "issues_summary": issues, "issues_sample": sample,
        "counter_events": {"count": len(a.counter_events),
                           "sample": [{"host": e[0], "metric": e[1], "object": e[2], "ts_utc": epoch_to_utc(e[3]),
                                       "kind": e[4], "detail": e[5]} for e in a.counter_events[:20]]},
        "measures": measures,
    }


def _combined_dict(c: Combined, cand_cells: Dict[str, Optional[int]]) -> dict:
    per_cell = {}
    for cid, n in cand_cells.items():
        if n:
            per_cell[cid] = {"cells": n,
                             "envelope_per_cell": d2s((c.envelope / n).quantize(Decimal("0.001"))) if c.envelope is not None else None,
                             "coincident_per_cell": d2s((c.coincident_max / n).quantize(Decimal("0.001")))
                             if c.coincident_max is not None else None}
    return {"measure": c.measure.id, "label": c.measure.label, "unit": c.measure.unit,
            "source_max": d2s(c.source_max), "source_max_utc": ts_utc(c.source_max_minute),
            "dest_max": d2s(c.dest_max), "dest_max_utc": ts_utc(c.dest_max_minute),
            "coincident_max": d2s(c.coincident_max), "coincident_utc": ts_utc(c.coincident_minute) if c.align == "utc" else
            (f"minuto del día {c.coincident_minute // 60 % 1440 // 60:02d}:{c.coincident_minute // 60 % 60:02d} UTC"
             if c.coincident_minute is not None else None),
            "coincident_overlap_minutes": c.coincident_overlap_minutes, "envelope": d2s(c.envelope),
            "align": c.align, "notes": c.notes, "per_destination_cell": per_cell}


def build_model(db_path: str, analyses: Dict[str, CaptureAnalysis], cat=None, scenario: Optional[dict] = None,
                results: Optional[List[CandidateResult]] = None, current: Optional[Dict[str, list]] = None) -> dict:
    model = {
        "tool": {"name": "exacell-sizer", "version": __version__, "collector_version": script_version(),
                 "catalog_version": cat.version if cat else None},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "database": str(db_path),
        "captures": {role: capture_block(db_path, a) for role, a in analyses.items()},
        "evidence_legend": {
            "medido_origen": "Medición CellCLI de la captura (minutos completos).",
            "publicado": "Cifra máxima del datasheet Oracle (fuente y página indicadas). No es capacidad garantizada.",
            "derivado": "Calculado a partir de una cifra publicada con la fórmula indicada (p. ej. rack / nº de celdas).",
            "hipotesis": "Supuesto del escenario (crecimiento, reserva, escalado lineal, reparto uniforme).",
            "probado": "Capacidad confirmada mediante pruebas aportadas por el usuario, con su latencia.",
        },
    }
    if current:
        model["current_vs_reference"] = current
    if results is not None:
        src = analyses.get("origen")
        dst = analyses.get("destino")
        sc = scenario or {}
        cand_cells = {r.candidate["id"]: r.cells_evaluated for r in results}
        comb = combine(src, dst, sc.get("align", "utc")) if src else {}
        model["scenario"] = json.loads(json.dumps(scenario, default=str))
        model["combined"] = [_combined_dict(c, cand_cells) for c in comb.values()]
        model["candidates"] = [json.loads(json.dumps(r.to_dict(), default=str)) for r in results]
        model["_combined_obj"] = comb  # solo en memoria (gráficos)
    return model


def _public(model: dict) -> dict:
    return {k: v for k, v in model.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------- JSON

def write_json(model: dict, path: Path) -> Path:
    path.write_text(json.dumps(_public(model), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------- CSV

def _csv_writer(path: Path, locale: str):
    fh = open(path, "w", encoding="utf-8-sig", newline="")
    return fh, csv.writer(fh, delimiter=LOCALES[locale]["sep"])


def _num(v, locale: str) -> str:
    if v is None:
        return ""
    s = d2s(v)
    return s.replace(".", LOCALES[locale]["dec"]) if LOCALES[locale]["dec"] != "." else s


def write_csvs(db_path: str, model: dict, outdir: Path, locale: str = "es") -> List[Path]:
    files = []
    con = sqlite3.connect(db_path)
    # series temporales (desde SQLite: misma fuente que los agregados)
    p = outdir / "series.csv"
    fh, w = _csv_writer(p, locale)
    w.writerow(["captura", "rol", "medida", "unidad", "celda", "minuto_utc", "valor"])
    units = {}
    for role, cb in model["captures"].items():
        for m in cb["measures"]:
            units[m["id"]] = m["unit"]
        for mid, host, minute, val in con.execute(
                "SELECT measure, host, minute_epoch, value FROM measure_minute WHERE capture_id=? "
                "ORDER BY measure, host, minute_epoch", (cb["capture_id"],)):
            w.writerow([cb["label"], role, mid, units.get(mid, ""), "SISTEMA" if host == "*" else host,
                        epoch_to_utc(minute), _num(val, locale)])
    fh.close()
    files.append(p)
    con.close()
    # agregados y cobertura
    p = outdir / "agregados.csv"
    fh, w = _csv_writer(p, locale)
    w.writerow(["captura", "rol", "medida", "unidad", "sumable", "celda", "estado", "motivo", "origen_dato",
                "minutos_esperados", "minutos_completos", "minutos_incompletos", "cobertura_pct", "maximo",
                "maximo_minuto_utc", "p99", "p95", "p50", "media"])
    for role, cb in model["captures"].items():
        for m in cb["measures"]:
            rows = list(m["per_cell"].items())
            if m["system"] is not None:
                rows.append(("SISTEMA", m["system"]))
            for host, s in rows:
                st = s["stats"]
                cov = Decimal(s["coverage"]) * 100 if s["coverage"] is not None else None
                w.writerow([cb["label"], role, m["id"], m["unit"], "sí" if m["summable"] else "no", host, s["status"],
                            s["reason"], s["source"], s["expected_minutes"], s["complete_minutes"],
                            s["incomplete_minutes"], _num(cov, locale), _num(st["max"], locale), st["max_minute_utc"] or "",
                            _num(st["p99"], locale), _num(st["p95"], locale), _num(st["p50"], locale),
                            _num(st["mean"], locale)])
    fh.close()
    files.append(p)
    p = outdir / "incidencias.csv"
    fh, w = _csv_writer(p, locale)
    w.writerow(["captura", "severidad", "codigo", "numero"])
    for role, cb in model["captures"].items():
        for i in cb["issues_summary"]:
            w.writerow([cb["label"], i["severity"], i["code"], i["count"]])
    fh.close()
    files.append(p)
    if "candidates" in model:
        p = outdir / "carga_combinada.csv"
        fh, w = _csv_writer(p, locale)
        w.writerow(["medida", "unidad", "max_origen", "max_origen_utc", "max_destino_actual", "max_destino_utc",
                    "suma_coincidente_max", "suma_coincidente_instante", "minutos_coincidentes",
                    "envolvente_max_separados", "alineacion", "notas"])
        for c in model["combined"]:
            w.writerow([c["measure"], c["unit"], _num(c["source_max"], locale), c["source_max_utc"] or "",
                        _num(c["dest_max"], locale), c["dest_max_utc"] or "", _num(c["coincident_max"], locale),
                        c["coincident_utc"] or "", c["coincident_overlap_minutes"], _num(c["envelope"], locale),
                        c["align"], " | ".join(c["notes"])])
        fh.close()
        files.append(p)
        p = outdir / "dimensionamiento_candidatos.csv"
        fh, w = _csv_writer(p, locale)
        w.writerow(["candidato", "modelo", "tipo_celda", "aproximacion", "dictamen", "celdas_evaluadas",
                    "minimo_teorico", "minimo_configuracion", "recomendado", "n_menos_1", "recurso_limitante",
                    "margen_limitante_pct", "aprobacion_operativa", "parcial"])
        for c in model["candidates"]:
            lm = Decimal(c["limiting_margin"]) * 100 if c["limiting_margin"] is not None else None
            w.writerow([c["candidate"]["id"], c["model"] or "", c["storage_type"] or "",
                        (c["approximation"] or {}).get("id", ""), c["verdict"], c["cells_evaluated"] or "",
                        c["n_theoretical"] if c["n_theoretical"] is not None else "", c["n_config_minimum"] or "",
                        c["n_recommended"] or "", c["n_failure"] or "", c["limiting_resource"] or "",
                        _num(lm, locale), c["operational_approval"], "sí" if c["partial"] else "no"])
        fh.close()
        files.append(p)
        p = outdir / "dimensionamiento_recursos.csv"
        fh, w = _csv_writer(p, locale)
        w.writerow(["candidato", "recurso", "estado", "unidad", "demanda", "metodo_demanda", "demanda_diseno",
                    "capacidad_por_celda", "evidencia", "fuente", "pagina", "derivacion", "celdas_teoricas",
                    "celdas_recomendadas", "celdas_n_menos_1", "utilizacion_pct", "margen_pct", "demanda_por_celda",
                    "formula_margen", "motivo"])
        for c in model["candidates"]:
            for r in c["resources"]:
                ref = r.get("reference") or {}
                u = Decimal(r["utilization"]) * 100 if r["utilization"] not in (None, "None") else None
                mg = Decimal(r["margin"]) * 100 if r["margin"] not in (None, "None") else None
                w.writerow([c["candidate"]["id"], r["resource"], r["status"], r["unit"], _num(_nn(r["demand"]), locale),
                            r["demand_method"], _num(_nn(r["design_demand"]), locale),
                            _num(_nn(r["capacity_per_cell"]), locale), r["capacity_evidence"],
                            ref.get("source_file", ""), ref.get("page", ""), ref.get("derivation", "") or "",
                            _v(r["n_theoretical"]), _v(r["n_recommended"]), _v(r["n_failure"]), _num(u, locale),
                            _num(mg, locale), _num(_nn(r["per_cell_demand"]), locale), r["margin_formula"], r["reason"]])
        fh.close()
        files.append(p)
    readme = outdir / "LEEME_CSV.txt"
    readme.write_text(
        f"Configuración regional de los CSV: '{locale}' -> {LOCALES[locale]['desc']}.\n"
        "Codificación UTF-8 con BOM (Excel la detecta). Sin separador de miles.\n"
        "Fechas: minuto_utc en ISO-8601 UTC (sufijo Z). 'SISTEMA' = suma de celdas en minutos completos de todas.\n"
        "Unidades: columna 'unidad'. IO/s = operaciones por segundo; MB/s = MB de CellCLI (2^20 bytes) por segundo;\n"
        "% y us (microsegundos) NO se suman entre celdas.\n"
        "Celdas vacías = dato no disponible (nunca cero implícito).\n"
        "Archivos: series.csv (series por minuto), agregados.csv (estadísticas y cobertura por medida y celda),\n"
        "incidencias.csv, carga_combinada.csv, dimensionamiento_candidatos.csv, dimensionamiento_recursos.csv.\n",
        encoding="utf-8")
    files.append(readme)
    return files


def _nn(v):
    return None if v in (None, "None") else v


def _v(v):
    return "" if v is None else v


# ----------------------------------------------------------------------------- Markdown

def _md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


def _capture_md(cb: dict) -> List[str]:
    L = [f"### Captura «{cb['label']}» ({cb['role']})", ""]
    L.append(_md_table(["Campo", "Valor"], [
        ["Periodo", f"{cb['period']['start_raw']} → {cb['period']['end_raw']} ({cb['period']['start_utc']} → {cb['period']['end_utc']})"],
        ["Duración", f"{fmt_es(cb['calendar']['hours'], 2)} h; días: {', '.join(cb['calendar']['weekdays'])}"],
        ["Celdas", f"{len(cb['cells'])}: {', '.join(cb['cells'])}"],
        ["Modelos (makeModel)", "; ".join(sorted({v for v in cb['cell_models'].values() if v})) or "desconocido"],
        ["Estado del recolector", cb["reported_status"]],
        ["Integridad SHA-256", cb["checksum_status"]],
        ["Estado de importación", cb["import_status"]],
        ["Muestras", fmt_es(cb["sample_count"], 0)],
    ]))
    L += ["", "**Avisos de cobertura temporal:**", ""] + [f"- {w}" for w in cb["calendar"]["warnings"]] + [""]
    if cb["issues_summary"]:
        L += ["**Incidencias de importación:**", "",
              _md_table(["Severidad", "Código", "Nº"], [[i["severity"], i["code"], i["count"]] for i in cb["issues_summary"]]), ""]
    if cb["counter_events"]["count"]:
        L += [f"**Eventos de contadores (reinicios/huecos):** {cb['counter_events']['count']}", ""]
    return L


def _measures_md(cb: dict) -> List[str]:
    L = [f"#### Carga medida — «{cb['label']}»", "",
         "Totales de sistema = suma de celdas en minutos completos de todas las celdas. "
         "Porcentajes y latencias no se suman (se muestra el máximo entre celdas).", ""]
    rows = []
    for m in cb["measures"]:
        if m["summable"]:
            s = m["system"]
            st = s["stats"]
            rows.append([m["label"], m["unit"], s["status"], fmt_es(st["max"]), st["max_minute_utc"] or "—",
                         fmt_es(st["p99"]), fmt_es(st["p95"]), fmt_es(st["mean"]),
                         f"{s['complete_minutes']}/{s['expected_minutes']} ({pct_es(s['coverage'])})"])
        else:
            st = m["cells_max_stats"]
            rows.append([m["label"] + " (máx. entre celdas)", m["unit"], "no sumable", fmt_es(st["max"]),
                         st["max_minute_utc"] or "—", fmt_es(st["p99"]), fmt_es(st["p95"]), fmt_es(st["mean"]), "ver por celda"])
    L.append(_md_table(["Medida", "Unidad", "Estado", "Máximo observado", "Instante (UTC)", "p99", "p95", "Media",
                        "Minutos completos (cobertura)"], rows))
    L += ["", f"#### Por celda y cobertura — «{cb['label']}»", ""]
    rows = []
    for m in cb["measures"]:
        for h, s in m["per_cell"].items():
            rows.append([m["id"], h, s["status"], s["source"], fmt_es(s["stats"]["max"]),
                         f"{s['complete_minutes']}/{s['expected_minutes']}", pct_es(s["coverage"]), s["reason"]])
    L.append(_md_table(["Medida", "Celda", "Estado", "Origen dato", "Máximo", "Minutos completos", "Cobertura", "Motivo"], rows))
    return L + [""]


def _candidate_md(c: dict) -> List[str]:
    icon = VERDICT_ICON.get(c["verdict"], ("", ""))[0]
    L = [f"### Candidato «{c['candidate']['id']}»: {c['model'] or '?'} {c['storage_type'] or '?'}", "",
         f"**Dictamen: {icon} {c['verdict']}**", ""]
    L += [f"- {r}" for r in c["verdict_reasons"]] + [""]
    if c["approximation"]:
        L += [f"> ⚠ **Aproximación de hardware:** {c['approximation']['warning']}", ""]
    part = " (cota inferior parcial: faltan recursos esenciales)" if c["partial"] else ""
    L.append(_md_table(["Resultado", "Valor"], [
        ["Mínimo teórico por carga (sin crecimiento ni reserva)", f"{_v(c['n_theoretical'])}{part}"],
        ["Mínimo de configuración", _v(c["n_config_minimum"])],
        ["Recomendado (crecimiento + reserva)", f"{_v(c['n_recommended'])}{part}"],
        ["Con fallo de una celda (N-1 + recuperación)", _v(c["n_failure"]) or "no solicitado"],
        ["Celdas evaluadas", _v(c["cells_evaluated"])],
        ["Recurso limitante", RESOURCE_LABELS.get(c["limiting_resource"], "—") if c["limiting_resource"] else "—"],
        ["Margen del recurso limitante", pct_es(c["limiting_margin"]) if c["limiting_margin"] else "—"],
        ["Aprobación operativa", c["operational_approval"]],
    ]))
    L += ["", "**Recursos evaluados** (capacidad por celda; evidencia: publicado / derivado / probado):", ""]
    rows = []
    for r in c["resources"]:
        ref = r.get("reference") or {}
        src = f"{ref.get('source_file', '')} p.{ref.get('page', '')}" if ref.get("source_file") else \
            ("; ".join(f"{x['source_file']} p.{x['page']}" for x in r.get("references", [])) or "")
        rows.append([RESOURCE_LABELS.get(r["resource"], r["resource"]), r["status"], fmt_es(_nn(r["demand"])),
                     fmt_es(_nn(r["design_demand"])), fmt_es(_nn(r["capacity_per_cell"]), 3),
                     r["capacity_evidence"], src, _v(r["n_theoretical"]), _v(r["n_recommended"]), _v(r["n_failure"]),
                     pct_es(_nn(r["utilization"])), pct_es(_nn(r["margin"])), r["unit"]])
    L.append(_md_table(["Recurso", "Estado", "Demanda", "Demanda diseño", "Capacidad/celda", "Evidencia", "Fuente",
                        "Teóricas", "Recomendadas", "N-1", "Utilización", "Margen", "Unidad"], rows))
    L += ["", "**Fórmulas de margen y motivos:**", ""]
    for r in c["resources"]:
        txt = r["margin_formula"] or r["reason"]
        if txt:
            L.append(f"- *{RESOURCE_LABELS.get(r['resource'], r['resource'])}*: {txt}")
        if r.get("demand_method"):
            L.append(f"  - Demanda: {r['demand_method']}")
        ref = r.get("reference") or {}
        if ref.get("derivation"):
            L.append(f"  - Derivación: {ref['derivation']}")
        for wn in r.get("warnings", []):
            L.append(f"  - ⚠ {wn}")
    db = c.get("db_check") or {}
    L += ["", f"**Servidores de BD:** {db.get('status', 'n/d')}. {db.get('reason', '')}"]
    for ch in db.get("checks", []):
        L.append(f"- {ch['resource']}: demanda de diseño {fmt_es(ch.get('design_demand'))} / capacidad "
                 f"{fmt_es(ch.get('capacity'))} → {ch.get('utilization', ch.get('reason'))}")
    L += ["", f"**Límites de configuración:** mínimo {c['rack_limits'].get('min_storage_servers')} celdas, máximo "
               f"{c['rack_limits'].get('max_storage_servers_per_rack')} por rack ({c['rack_limits'].get('source')} "
               f"p.{c['rack_limits'].get('page')}).", ""]
    if c["approval_pending"]:
        L += ["**Pendiente para la aprobación operativa:**", ""] + [f"- {p}" for p in c["approval_pending"]] + [""]
    L += ["**Supuestos:**", ""] + [f"- {a}" for a in c["assumptions"]] + [""]
    if c["warnings"]:
        L += ["**Advertencias:**", ""] + [f"- {w}" for w in c["warnings"]] + [""]
    return L


def render_markdown(model: dict) -> str:
    L = ["# Informe ExaCell Sizer", "",
         f"Generado {model['generated_utc']} · exacell-sizer {model['tool']['version']} · recolector "
         f"{model['tool']['collector_version']} · catálogo {model['tool']['catalog_version'] or 'n/a'}", "",
         "> Las cifras de datasheet son **máximos publicados/teóricos**, no capacidad garantizada. "
         "Ver la guía de interpretación (`GUIA_INTERPRETACION.md`).", ""]
    if "candidates" in model:
        L += ["## 1. Dictamen", ""]
        L.append(_md_table(["Candidato", "Modelo", "Celda", "Dictamen", "Teórico", "Recomendado", "N-1",
                            "Evaluadas", "Limitante", "Margen", "Aprobación operativa"],
                           [[c["candidate"]["id"], c["model"] or "?", c["storage_type"] or "?",
                             f"{VERDICT_ICON.get(c['verdict'], ('', ''))[0]} {c['verdict']}",
                             f"{_v(c['n_theoretical'])}{' (parcial)' if c['partial'] else ''}", _v(c["n_recommended"]),
                             _v(c["n_failure"]), _v(c["cells_evaluated"]),
                             RESOURCE_LABELS.get(c["limiting_resource"], "—") if c["limiting_resource"] else "—",
                             pct_es(c["limiting_margin"]) if c["limiting_margin"] else "—", c["operational_approval"]]
                            for c in model["candidates"]]))
        L += ["", "## 2. Escenario", "", "```json", json.dumps(model["scenario"], ensure_ascii=False, indent=1), "```", ""]
    L += ["## 3. Capturas", ""]
    for cb in model["captures"].values():
        L += _capture_md(cb)
    if model.get("current_vs_reference"):
        L += ["## 4. Carga actual frente a su referencia publicada", ""]
        for role, rows in model["current_vs_reference"].items():
            L += [f"**{role}**", "", _md_table(["Recurso", "Estado", "Máx. observado", "Referencia total", "Por celda",
                                                "Evidencia", "Utilización"],
                                               [[r.get("label", r["resource"]), r["status"], fmt_es(r.get("observed_max")),
                                                 fmt_es(r.get("reference_total")), fmt_es(r.get("reference_per_cell")),
                                                 r.get("evidence", r.get("reason", "")), r.get("utilization", "—")]
                                                for r in rows]), ""]
    L += ["## 5. Carga observada global y por celda", ""]
    for cb in model["captures"].values():
        L += _measures_md(cb)
    if "combined" in model:
        L += ["## 6. Carga combinada estimada (destino)", "",
              "- **Máximo observado**: pico de cada sistema por separado.",
              "- **Suma coincidente**: máximo de la suma minuto a minuto en instantes simultáneos.",
              "- **Envolvente conservadora**: suma de máximos separados (aunque no coincidan en el tiempo).", ""]
        L.append(_md_table(["Medida", "Unidad", "Máx. origen", "Máx. destino actual", "Suma coincidente (máx.)",
                            "Instante", "Envolvente", "Notas"],
                           [[c["label"], c["unit"], fmt_es(c["source_max"]), fmt_es(c["dest_max"]),
                             fmt_es(c["coincident_max"]), c["coincident_utc"] or "—", fmt_es(c["envelope"]),
                             "; ".join(c["notes"])] for c in model["combined"]]))
        L += ["", "**Por celda del destino** (envolvente / nº de celdas evaluadas, suponiendo reparto uniforme):", ""]
        rows = []
        for c in model["combined"]:
            for cid, pc in c["per_destination_cell"].items():
                rows.append([c["label"], cid, pc["cells"], fmt_es(pc["envelope_per_cell"]),
                             fmt_es(pc["coincident_per_cell"]), c["unit"]])
        L += [_md_table(["Medida", "Candidato", "Celdas", "Envolvente/celda", "Coincidente/celda", "Unidad"], rows), ""]
        L += ["## 7. Detalle por candidato", ""]
        for c in model["candidates"]:
            L += _candidate_md(c)
    L += ["## 8. Leyenda de evidencias", ""] + [f"- **{k}**: {v}" for k, v in model["evidence_legend"].items()] + [""]
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------- HTML

CSS = """
:root{--surface-1:#fcfcfb;--surface-2:#ffffff;--text-primary:#0b0b0b;--text-secondary:#52514e;--grid:#e4e3df;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--surface-1:#1a1a19;--surface-2:#242423;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;color-scheme:dark}}
:root[data-theme="dark"]{--surface-1:#1a1a19;--surface-2:#242423;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--grid:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;color-scheme:dark}
body{background:var(--surface-1);color:var(--text-primary);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
margin:0;padding:16px}main{max-width:1100px;margin:0 auto}
h1{font-size:24px}h2{font-size:19px;margin-top:32px;border-bottom:1px solid var(--grid);padding-bottom:4px}h3{font-size:16px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:12.5px;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--grid);padding:4px 8px;text-align:left;vertical-align:top}
th{color:var(--text-secondary);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.muted{color:var(--text-secondary)}.small{font-size:12px}
.verdict{display:inline-flex;gap:6px;align-items:center;font-weight:600;padding:2px 10px;border-radius:12px;border:1px solid var(--grid)}
.verdict .ic{display:inline-block;width:18px;height:18px;border-radius:9px;color:#fff;text-align:center;line-height:18px;font-size:12px}
.verdict.good .ic{background:var(--good)}.verdict.critical .ic{background:var(--critical)}.verdict.warning .ic{background:var(--warning);color:#000}
.note{border-left:3px solid var(--text-secondary);padding:4px 10px;margin:8px 0;background:var(--surface-2)}
details{margin:8px 0}summary{cursor:pointer;color:var(--text-secondary)}
code,pre{font-size:12px}pre{overflow-x:auto;background:var(--surface-2);padding:8px;border:1px solid var(--grid)}
"""


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def _h_table(headers, rows, num_cols=()) -> str:
    out = ["<table><thead><tr>" + "".join(f"<th>{_e(h)}</th>" for h in headers) + "</tr></thead><tbody>"]
    for r in rows:
        out.append("<tr>" + "".join(f'<td class="n">{_e(x)}</td>' if i in num_cols else f"<td>{_e(x)}</td>"
                                    for i, x in enumerate(r)) + "</tr>")
    return "".join(out) + "</tbody></table>"


def _verdict_html(v: str) -> str:
    ic, cls = VERDICT_ICON.get(v, ("", "warning"))
    return f'<span class="verdict {cls}"><span class="ic" aria-hidden="true">{ic}</span>{_e(v)}</span>'


def _series_from_db(db_path, capture_id, measure, host="*") -> Dict[int, Decimal]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT minute_epoch, value FROM measure_minute WHERE capture_id=? AND measure=? AND host=?",
                       (capture_id, measure, host)).fetchall()
    con.close()
    return {m: Decimal(v) for m, v in rows}


def render_html(db_path: str, model: dict) -> str:
    B = [f"<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Informe ExaCell Sizer</title><style>{CSS}{CHART_CSS}</style></head><body><main>",
         "<h1>Informe ExaCell Sizer</h1>",
         f"<p class='muted'>Generado {model['generated_utc']} · exacell-sizer {model['tool']['version']} · recolector "
         f"{_e(model['tool']['collector_version'])} · catálogo {_e(model['tool']['catalog_version'] or 'n/a')}. "
         "Informe autocontenido: funciona sin Internet.</p>",
         "<p class='note'>Las líneas discontinuas son <b>máximos publicados/teóricos</b> (datasheet o derivados) "
         "o capacidades probadas, nunca «capacidad garantizada». Consulte <code>GUIA_INTERPRETACION.md</code>.</p>"]
    comb_obj = model.get("_combined_obj", {})
    if "candidates" in model:
        B.append("<h2>1. Dictamen</h2>")
        rows = []
        for c in model["candidates"]:
            rows.append(f"<tr><td>{_e(c['candidate']['id'])}</td><td>{_e(c['model'])} {_e(c['storage_type'])}</td>"
                        f"<td>{_verdict_html(c['verdict'])}</td><td class='n'>{_e(_v(c['n_theoretical']))}{' (parcial)' if c['partial'] else ''}</td>"
                        f"<td class='n'>{_e(_v(c['n_recommended']))}</td><td class='n'>{_e(_v(c['n_failure']))}</td>"
                        f"<td class='n'>{_e(_v(c['cells_evaluated']))}</td>"
                        f"<td>{_e(RESOURCE_LABELS.get(c['limiting_resource'], '—') if c['limiting_resource'] else '—')}</td>"
                        f"<td class='n'>{_e(pct_es(c['limiting_margin']) if c['limiting_margin'] else '—')}</td>"
                        f"<td>{_e(c['operational_approval'])}</td></tr>")
        B.append("<table><thead><tr><th>Candidato</th><th>Configuración</th><th>Dictamen</th><th>Teórico</th>"
                 "<th>Recomendado</th><th>N-1</th><th>Evaluadas</th><th>Recurso limitante</th><th>Margen</th>"
                 "<th>Aprobación operativa</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")
        for c in model["candidates"]:
            B.append(f"<h3>{_e(c['candidate']['id'])}: {_e(c['model'])} {_e(c['storage_type'])}</h3>"
                     f"<p>{_verdict_html(c['verdict'])}</p><ul>" + "".join(f"<li>{_e(r)}</li>" for r in c["verdict_reasons"]) + "</ul>")
            if c["approximation"]:
                B.append(f"<p class='note'>⚠ Aproximación de hardware: {_e(c['approximation']['warning'])}</p>")
    B.append("<h2>2. Capturas y cobertura</h2>")
    for cb in model["captures"].values():
        B.append(f"<h3>«{_e(cb['label'])}» ({_e(cb['role'])})</h3>")
        B.append(_h_table(["Campo", "Valor"], [
            ["Periodo", f"{cb['period']['start_raw']} → {cb['period']['end_raw']} (UTC {cb['period']['start_utc']} → {cb['period']['end_utc']})"],
            ["Duración", f"{fmt_es(cb['calendar']['hours'], 2)} h ({', '.join(cb['calendar']['weekdays'])})"],
            ["Celdas", f"{len(cb['cells'])}: {', '.join(cb['cells'])}"],
            ["Estado recolector / integridad / importación", f"{cb['reported_status']} / {cb['checksum_status']} / {cb['import_status']}"],
            ["Muestras", fmt_es(cb["sample_count"], 0)]]))
        B.append("<ul>" + "".join(f"<li>{_e(w)}</li>" for w in cb["calendar"]["warnings"]) + "</ul>")
        if cb["issues_summary"]:
            B.append("<details><summary>Incidencias de importación</summary>" + _h_table(
                ["Severidad", "Código", "Nº"], [[i["severity"], i["code"], i["count"]] for i in cb["issues_summary"]], (2,))
                + _h_table(["Severidad", "Código", "Celda", "Métrica", "Mensaje", "Archivo", "Línea"],
                           [[i["severity"], i["code"], i["host"], i["metric"], i["message"], i["source_file"], i["source_line"]]
                            for i in cb["issues_sample"]]) + "</details>")
        cov_rows = []
        for m in cb["measures"]:
            for h, s in m["per_cell"].items():
                cov_rows.append([m["id"], h, s["status"], s["source"], f"{s['complete_minutes']}/{s['expected_minutes']}",
                                 pct_es(s["coverage"]), s["reason"]])
        B.append("<details><summary>Minutos completos y cobertura por medida y celda</summary>"
                 + _h_table(["Medida", "Celda", "Estado", "Origen dato", "Minutos completos", "Cobertura", "Motivo"],
                            cov_rows, (4, 5)) + "</details>")
    B.append("<h2>3. Carga observada global y por celda</h2>")
    for role, cb in model["captures"].items():
        B.append(f"<h3>«{_e(cb['label'])}» ({_e(role)})</h3>")
        rows = []
        for m in cb["measures"]:
            if m["summable"]:
                s = m["system"]
                st = s["stats"]
                rows.append([m["label"], m["unit"], s["status"], fmt_es(st["max"]), st["max_minute_utc"] or "—",
                             fmt_es(st["p99"]), fmt_es(st["p95"]), fmt_es(st["mean"]), pct_es(s["coverage"])])
            else:
                st = m["cells_max_stats"]
                rows.append([m["label"] + " (máx. entre celdas, no sumable)", m["unit"], "—", fmt_es(st["max"]),
                             st["max_minute_utc"] or "—", fmt_es(st["p99"]), fmt_es(st["p95"]), fmt_es(st["mean"]), "—"])
        B.append(_h_table(["Medida", "Unidad", "Estado", "Máximo observado", "Instante UTC", "p99", "p95", "Media",
                           "Cobertura sistema"], rows, (3, 5, 6, 7, 8)))
        a_id = cb["capture_id"]
        for key in ("flash_read_iops_small", "flash_write_iops_small", "flash_read_mbps", "hdd_iops_small", "hdd_mbps",
                    "network_mbps"):
            m = next((x for x in cb["measures"] if x["id"] == key), None)
            if not m or not m["system"] or m["system"]["status"] != "OK":
                continue
            bars = [(h, Decimal(s["stats"]["max"])) for h, s in m["per_cell"].items() if s["stats"]["max"] is not None]
            B.append(bar_chart(f"pc-{role}-{key}", f"{m['label']}: máximo observado por celda", m["unit"], bars,
                               subtitle="Máximo de cada celda en minutos completos (pueden ocurrir en instantes distintos)."))
        for key in ("cell_cpu_pct", "flash_read_latency_us", "hdd_read_latency_us"):
            m = next((x for x in cb["measures"] if x["id"] == key), None)
            if m:
                series = [(h, i + 1, _series_from_db(db_path, a_id, key, h)) for i, h in enumerate(cb["cells"][:3])]
                more = f" (se muestran 3 de {len(cb['cells'])} celdas; resto en series.csv)" if len(cb["cells"]) > 3 else ""
                B.append(line_chart(f"ns-{role}-{key}", f"{m['label']} por celda (no se suma)", m["unit"], series,
                                    subtitle="Métrica no sumable entre celdas" + more))
    if "combined" in model:
        B.append("<h2>4. Carga combinada estimada y referencia por candidato</h2>")
        B.append("<p>Máximo observado de cada sistema, <b>suma en instantes coincidentes</b> y <b>envolvente "
                 "conservadora</b> (suma de máximos separados). Las líneas discontinuas muestran el máximo "
                 "publicado/teórico (o probado) del número de celdas evaluado de cada candidato.</p>")
        B.append(_h_table(["Medida", "Unidad", "Máx. origen", "Máx. destino actual", "Suma coincidente máx.", "Instante",
                           "Envolvente", "Notas"],
                          [[c["label"], c["unit"], fmt_es(c["source_max"]), fmt_es(c["dest_max"]),
                            fmt_es(c["coincident_max"]), c["coincident_utc"] or "—", fmt_es(c["envelope"]),
                            "; ".join(c["notes"])] for c in model["combined"]], (2, 3, 4, 6)))
        per_rows = []
        for c in model["combined"]:
            for cid, pc in c["per_destination_cell"].items():
                per_rows.append([c["label"], cid, pc["cells"], fmt_es(pc["envelope_per_cell"]),
                                 fmt_es(pc["coincident_per_cell"]), c["unit"]])
        B.append("<h3>Por celda del destino (reparto uniforme supuesto)</h3>"
                 + _h_table(["Medida", "Candidato", "Celdas", "Envolvente/celda", "Coincidente/celda", "Unidad"], per_rows, (2, 3, 4)))
        for res, mid in RESOURCE_MEASURES.items():
            cobj = comb_obj.get(mid)
            if cobj is None:
                continue
            series = [("Origen", 1, cobj.source_series)]
            if model["captures"].get("destino"):
                series.append(("Destino actual", 2, cobj.dest_series))
                if cobj.align == "utc" and cobj.coincident_series:
                    series.append(("Suma coincidente", 3, cobj.coincident_series))
            refs = []
            for c in model["candidates"][:3]:
                r = next((x for x in c["resources"] if x["resource"] == res), None)
                if r and _nn(r["capacity_per_cell"]) and c["cells_evaluated"]:
                    total = Decimal(r["capacity_per_cell"]) * c["cells_evaluated"]
                    kind = "capacidad probada" if r["capacity_evidence"].startswith("probado") else "máximo publicado/teórico"
                    refs.append((f"{kind} {c['candidate']['id']} ({c['cells_evaluated']} celdas)", total))
            if not any(s[2] for s in series):
                continue
            B.append(line_chart(f"cmb-{res}", RESOURCE_LABELS[res], cobj.measure.unit, series, refs,
                                subtitle="Demanda medida sin crecimiento; la evaluación aplica crecimiento y reserva del escenario."))
        B.append("<h2>5. Detalle por candidato</h2>")
        for c in model["candidates"]:
            B.append(f"<h3>{_e(c['candidate']['id'])}: {_e(c['model'])} {_e(c['storage_type'])} — {_verdict_html(c['verdict'])}</h3>")
            part = " (cota inferior parcial)" if c["partial"] else ""
            B.append(_h_table(["Resultado", "Valor"], [
                ["Mínimo teórico por carga", f"{_v(c['n_theoretical'])}{part}"],
                ["Mínimo de configuración", _v(c["n_config_minimum"])],
                ["Recomendado (crecimiento + reserva)", f"{_v(c['n_recommended'])}{part}"],
                ["N-1 (fallo de una celda + recuperación)", _v(c["n_failure"]) or "no solicitado"],
                ["Celdas evaluadas", _v(c["cells_evaluated"])],
                ["Recurso limitante", RESOURCE_LABELS.get(c["limiting_resource"], "—") if c["limiting_resource"] else "—"],
                ["Margen del recurso limitante", pct_es(c["limiting_margin"]) if c["limiting_margin"] else "—"],
                ["Aprobación operativa", c["operational_approval"]]]))
            rows = []
            for r in c["resources"]:
                ref = r.get("reference") or {}
                src = f"{ref.get('source_file', '')} p.{ref.get('page', '')}" if ref.get("source_file") else \
                    "; ".join(f"{x['source_file']} p.{x['page']}" for x in r.get("references", []))
                rows.append([RESOURCE_LABELS.get(r["resource"], r["resource"]), r["status"], fmt_es(_nn(r["demand"])),
                             fmt_es(_nn(r["design_demand"])), fmt_es(_nn(r["capacity_per_cell"]), 3), r["capacity_evidence"],
                             src, _v(r["n_theoretical"]), _v(r["n_recommended"]), _v(r["n_failure"]),
                             pct_es(_nn(r["utilization"])), pct_es(_nn(r["margin"])), r["unit"]])
            B.append(_h_table(["Recurso", "Estado", "Demanda", "Demanda diseño", "Capacidad/celda", "Evidencia", "Fuente",
                               "Teóricas", "Recom.", "N-1", "Utilización", "Margen", "Unidad"], rows, (2, 3, 4, 7, 8, 9, 10, 11)))
            B.append("<details><summary>Fórmulas, derivaciones y avisos</summary><ul>")
            for r in c["resources"]:
                ref = r.get("reference") or {}
                B.append(f"<li><b>{_e(RESOURCE_LABELS.get(r['resource'], r['resource']))}</b>: {_e(r['margin_formula'] or r['reason'])}"
                         + (f"<br>Demanda: {_e(r['demand_method'])}" if r.get("demand_method") else "")
                         + (f"<br>Derivación: {_e(ref['derivation'])}" if ref.get("derivation") else "")
                         + "".join(f"<br>⚠ {_e(w)}" for w in r.get("warnings", [])) + "</li>")
            B.append("</ul></details>")
            db = c.get("db_check") or {}
            B.append(f"<p><b>Servidores de BD:</b> {_e(db.get('status'))}. {_e(db.get('reason', ''))}</p>")
            if c["approval_pending"]:
                B.append("<p><b>Pendiente para la aprobación operativa:</b></p><ul>"
                         + "".join(f"<li>{_e(p)}</li>" for p in c["approval_pending"]) + "</ul>")
            B.append("<details><summary>Supuestos y advertencias</summary><ul>"
                     + "".join(f"<li>{_e(a)}</li>" for a in c["assumptions"] + c["warnings"]) + "</ul></details>")
        B.append("<h2>6. Escenario</h2><pre>" + _e(json.dumps(model["scenario"], ensure_ascii=False, indent=1)) + "</pre>")
    else:
        B.append("<h2>4. Series del sistema</h2>")
        for role, cb in model["captures"].items():
            for m in cb["measures"]:
                if m["summable"] and m["system"] and m["system"]["status"] == "OK" and m["resource"]:
                    s = _series_from_db(db_path, cb["capture_id"], m["id"])
                    B.append(line_chart(f"sys-{role}-{m['id']}", m["label"] + " (sistema)", m["unit"],
                                        [("Sistema", 1, s)]))
    if model.get("current_vs_reference"):
        B.append("<h2>Carga actual frente a su referencia publicada</h2>")
        for role, rows in model["current_vs_reference"].items():
            B.append(f"<h3>{_e(role)}</h3>" + _h_table(
                ["Recurso", "Estado", "Máx. observado", "Referencia total", "Por celda", "Evidencia", "Utilización"],
                [[r.get("label", r["resource"]), r["status"], fmt_es(r.get("observed_max")), fmt_es(r.get("reference_total")),
                  fmt_es(r.get("reference_per_cell")), r.get("evidence", r.get("reason", "")), r.get("utilization", "—")]
                 for r in rows], (2, 3, 4)))
    B.append("<h2>Leyenda de evidencias</h2><ul>" + "".join(
        f"<li><b>{_e(k)}</b>: {_e(v)}</li>" for k, v in model["evidence_legend"].items()) + "</ul>")
    B.append(f"<script>{CHART_JS}</script></main></body></html>")
    return "".join(B)


# ----------------------------------------------------------------------------- escritura

def write_all(db_path: str, model: dict, outdir: str, locale: str = "es") -> Dict[str, List[str]]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    files = {"json": [str(write_json(model, out / "informe.json"))]}
    files["csv"] = [str(p) for p in write_csvs(db_path, model, out, locale)]
    (out / "informe.md").write_text(render_markdown(model), encoding="utf-8")
    files["md"] = [str(out / "informe.md")]
    (out / "informe.html").write_text(render_html(db_path, model), encoding="utf-8")
    files["html"] = [str(out / "informe.html")]
    if GUIDE.exists():
        shutil.copyfile(GUIDE, out / "GUIA_INTERPRETACION.md")
        files["guide"] = [str(out / "GUIA_INTERPRETACION.md")]
    return files
