"""Espacio de trabajo de la aplicación web: capturas subidas, base de datos y ejecuciones.

Estructura (todo local en el PC del analista):
  <root>/exacell.db          SQLite con muestras, análisis e incidencias
  <root>/uploads/            archivos subidos tal cual (solo lectura) + .meta.json (SHA-256)
  <root>/extracted/          extracción segura de cada archivo (la original no se toca)
  <root>/simulated/          capturas SIMULADAS de demostración
  <root>/runs/<run_id>/      escenario, informe.json/html/md, CSV y guía de cada dimensionamiento
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .. import catalog as catmod
from .. import reports
from ..analysis import CaptureAnalysis, analyze_capture
from ..db import connect
from ..importer import import_capture
from ..metrics import MEASURE_BY_ID
from ..simulate import SimConfig, write_capture
from ..sizing import RESOURCE_LABELS, RESOURCE_MEASURES, evaluate
from ..svgchart import downsample_max

ALLOWED_UPLOAD = (".tar.gz", ".tgz", ".tar", ".zip")
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonable(o):
    return json.loads(json.dumps(o, default=lambda x: format(x, "f") if isinstance(x, Decimal) else str(x)))


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        for d in ("uploads", "extracted", "simulated", "runs"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.db = str(self.root / "exacell.db")
        connect(self.db).close()
        self._lock = threading.Lock()          # serializa escrituras pesadas en SQLite
        self._analyses: Dict[int, CaptureAnalysis] = {}
        self.catalog = catmod.load()

    def warm_up(self) -> None:
        """Precalcula en segundo plano el análisis de las capturas existentes."""
        def run():
            for c in self.list_captures():
                try:
                    self.analysis(c["id"])
                except Exception:  # se informará al abrir la captura
                    pass
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ capturas
    def save_upload(self, filename: str, stream, length: int) -> Path:
        name = SAFE.sub("_", os.path.basename(filename or "captura"))
        if not name.lower().endswith(ALLOWED_UPLOAD):
            raise ValueError(f"formato no admitido: {filename!r} (use .tar.gz, .tgz, .tar o .zip)")
        dest = self.root / "uploads" / f"{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}_{name}"
        h = hashlib.sha256()
        remaining = length
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
        if remaining:
            dest.unlink(missing_ok=True)
            raise ValueError("subida interrumpida: archivo incompleto")
        os.chmod(dest, 0o444)  # el original subido no se modifica
        (dest.parent / (dest.name + ".meta.json")).write_text(json.dumps(
            {"original_name": filename, "bytes": length, "sha256": h.hexdigest(), "uploaded_utc": _now()}))
        return dest

    def import_file(self, path: Path, label: Optional[str], role: str, log=lambda m: None) -> dict:
        label = label or Path(path).name
        if self.label_exists(label):
            raise ValueError(f"ya existe una captura con la etiqueta {label!r}")
        work = None
        if Path(path).is_file():
            work = self.root / "extracted" / (Path(path).name + "_" + uuid.uuid4().hex[:6])
            log("Extrayendo archivo de forma segura…")
        with self._lock:
            log("Validando e importando (integridad, bloques, unidades, duplicados)…")
            r = import_capture(str(path), self.db, label=label, role=role, workdir=str(work) if work else None)
            log("Analizando minutos completos, cobertura y contadores…")
            self._analyses[r.capture_id] = analyze_capture(self.db, r.capture_id)
        log("Hecho.")
        return {"capture_id": r.capture_id, "import_status": r.import_status, "samples": r.samples,
                "issues": dict(r.issues)}

    def simulate(self, p: dict, log=lambda m: None) -> dict:
        cells = int(p.get("cells", 3))
        hours = int(p.get("hours", 24))
        if not (1 <= cells <= 18) or not (1 <= hours <= 72):
            raise ValueError("celdas 1-18 y horas 1-72")
        prefix = SAFE.sub("", p.get("prefix") or "simcel")[:12] or "simcel"
        name = SAFE.sub("_", p.get("label") or f"simulada_{time.strftime('%Y%m%dT%H%M%S')}")
        cfg = SimConfig(cells=[f"{prefix}{i:02d}" for i in range(1, cells + 1)],
                        hdd_per_cell=int(p.get("hdd", 12)), flash_per_cell=int(p.get("flash", 4)),
                        load_factor=float(p.get("load_factor", 1.0)), seed=str(p.get("seed", name)),
                        spikes=[{"start_minute": 600, "minutes": 20, "factor": 1.6}] if p.get("spike", True) else [])
        start = p.get("start") or "2026-11-03T00:00:00+01:00"
        from ..cellcli_parse import epoch_to_utc, parse_timestamp
        s_ep, err = parse_timestamp(start)
        if err:
            raise ValueError(err)
        off = start[-6:] if not start.endswith("Z") else "+00:00"
        from ..simulate import fmt_time
        end = fmt_time(s_ep + hours * 3600, off)
        log(f"Generando captura SIMULADA ({cells} celdas, {hours} h)…")
        path = write_capture(cfg, str(self.root / "simulated"), start, end, name=name + "_" + uuid.uuid4().hex[:4])
        return self.import_file(Path(path), name, p.get("role", "origen"), log)

    def label_exists(self, label: str) -> bool:
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT 1 FROM capture WHERE label=?", (label,)).fetchone() is not None
        finally:
            con.close()

    def list_captures(self) -> List[dict]:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        out = []
        for c in con.execute("SELECT * FROM capture ORDER BY id DESC"):
            cnt = {r[0]: r[1] for r in con.execute(
                "SELECT severity, COUNT(*) FROM issue WHERE capture_id=? GROUP BY severity", (c["id"],))}
            cells = con.execute("SELECT COUNT(*) FROM cell WHERE capture_id=?", (c["id"],)).fetchone()[0]
            model = con.execute("SELECT make_model FROM cell WHERE capture_id=? AND make_model IS NOT NULL LIMIT 1",
                                (c["id"],)).fetchone()
            hours = ((c["period_end_epoch"] or 0) - (c["period_start_epoch"] or 0)) / 3600
            out.append({"id": c["id"], "label": c["label"], "role": c["role"], "import_status": c["import_status"],
                        "reported_status": c["reported_status"], "checksum_status": c["checksum_status"],
                        "period_start": c["period_start_raw"], "period_end": c["period_end_raw"],
                        "hours": round(hours, 2), "cells": cells, "samples": c["sample_count"],
                        "errors": cnt.get("ERROR", 0), "warnings": cnt.get("WARNING", 0),
                        "make_model": model[0] if model else None,
                        "simulated": "-sim" in (c["collector_version"] or ""), "imported_utc": c["imported_utc"]})
        con.close()
        return out

    def analysis(self, cid: int) -> CaptureAnalysis:
        if cid not in self._analyses:
            with self._lock:
                self._analyses[cid] = analyze_capture(self.db, cid)
        return self._analyses[cid]

    def capture_detail(self, cid: int) -> dict:
        a = self.analysis(cid)
        block = reports.capture_block(self.db, a)
        con = sqlite3.connect(self.db)
        block["issues_all"] = [dict(zip(("severity", "code", "host", "metric", "message", "source_file", "source_line"), r))
                               for r in con.execute(
            "SELECT severity, code, host, metric, message, source_file, source_line FROM issue WHERE capture_id=? "
            "ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, id LIMIT 500", (cid,))]
        block["blocks"] = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM block WHERE capture_id=? GROUP BY status", (cid,))}
        con.close()
        return block

    def series(self, cid: int, measure: str, host: str = "*", max_points: int = 600) -> dict:
        if measure not in MEASURE_BY_ID:
            raise ValueError(f"medida desconocida {measure!r}")
        a = self.analysis(cid)
        mr = a.measures[measure]
        if host == "*":
            vals = mr.system.values if mr.system is not None else mr.cells_max
        else:
            if host not in mr.per_cell:
                raise ValueError(f"celda desconocida {host!r}")
            vals = mr.per_cell[host].values
        pts, step = downsample_max(vals, max_points)
        return {"measure": measure, "host": host, "unit": mr.measure.unit, "step_minutes": step,
                "points": [[t, v] for t, v in pts]}

    # ------------------------------------------------------------------ dimensionamiento
    def run_sizing(self, scenario: dict, source: int, dest: Optional[int], log=lambda m: None) -> dict:
        log("Analizando capturas…")
        src = self.analysis(int(source))
        src.role = "origen"
        analyses = {"origen": src}
        dst = None
        if dest not in (None, "", 0):
            if int(dest) == int(source):
                raise ValueError("origen y destino deben ser capturas distintas")
            dst = self.analysis(int(dest))
            dst.role = "destino"
            analyses["destino"] = dst
        log("Evaluando candidatos…")
        results = evaluate(self.catalog, src, dst, scenario)
        current = {}
        for role, key in (("origen", "source_system"), ("destino", "destination_current")):
            info = scenario.get(key) or {}
            if info.get("model") and role in analyses:
                from ..sizing import current_load_vs_reference
                current[role] = current_load_vs_reference(self.catalog, analyses[role], info["model"],
                                                          info.get("storage_type", ""), scenario.get("approximations", []))
        model = reports.build_model(self.db, analyses, self.catalog, scenario, results, current or None)
        run_id = f"run_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:4]}"
        out = self.root / "runs" / run_id
        log("Generando informes (HTML, Markdown, CSV, JSON)…")
        files = reports.write_all(self.db, model, str(out), scenario.get("csv_locale", "es"))
        (out / "escenario.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=1), encoding="utf-8")
        charts = self._charts(model, results)
        pub = _jsonable({k: v for k, v in model.items() if not k.startswith("_")})
        pub["charts"] = charts
        pub["run_id"] = run_id
        pub["source_capture"] = int(source)
        pub["dest_capture"] = int(dest) if dst is not None else None
        pub["files"] = sorted(Path(f).name for v in files.values() for f in v)
        (out / "web.json").write_text(json.dumps(pub, ensure_ascii=False), encoding="utf-8")
        log("Hecho.")
        return {"run_id": run_id}

    def _charts(self, model, results) -> List[dict]:
        comb = model.get("_combined_obj", {})
        charts = []
        for res, mid in RESOURCE_MEASURES.items():
            c = comb.get(mid)
            if c is None or not c.source_series:
                continue
            series = [{"name": "Origen", "slot": 1, "points": downsample_max(c.source_series, 600)[0]}]
            if c.dest_series:
                series.append({"name": "Destino actual", "slot": 2, "points": downsample_max(c.dest_series, 600)[0]})
                if c.align == "utc" and c.coincident_series:
                    series.append({"name": "Suma coincidente", "slot": 3,
                                   "points": downsample_max(c.coincident_series, 600)[0]})
            refs = []
            for r in results:
                rr = next((x for x in r.resources if x.resource == res), None)
                if rr and rr.capacity_per_cell is not None and r.cells_evaluated:
                    kind = "capacidad probada" if rr.capacity_evidence.startswith("probado") else "máx. publicado/teórico"
                    refs.append({"label": f"{kind} · {r.candidate['id']} ({r.cells_evaluated} celdas)",
                                 "value": float(rr.capacity_per_cell * r.cells_evaluated), "candidate": r.candidate["id"]})
            charts.append({"resource": res, "title": RESOURCE_LABELS[res], "unit": c.measure.unit,
                           "series": series, "refs": refs})
        return charts

    def list_runs(self) -> List[dict]:
        out = []
        for d in sorted((self.root / "runs").iterdir(), reverse=True):
            f = d / "web.json"
            if not f.exists():
                continue
            j = json.loads(f.read_text(encoding="utf-8"))
            out.append({"run_id": d.name, "generated_utc": j["generated_utc"],
                        "scenario_name": (j.get("scenario") or {}).get("scenario_name", ""),
                        "source": j["captures"]["origen"]["label"],
                        "dest": j["captures"].get("destino", {}).get("label"),
                        "verdicts": [{"id": c["candidate"]["id"], "verdict": c["verdict"],
                                      "n_recommended": c["n_recommended"]} for c in j["candidates"]]})
        return out

    def get_run(self, run_id: str) -> dict:
        if not re.fullmatch(r"run_[0-9T]+_[0-9a-f]{4}", run_id):
            raise FileNotFoundError(run_id)
        f = self.root / "runs" / run_id / "web.json"
        if not f.exists():
            raise FileNotFoundError(run_id)
        return json.loads(f.read_text(encoding="utf-8"))

    def run_file(self, run_id: str, name: str) -> Path:
        self.get_run(run_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise FileNotFoundError(name)
        p = (self.root / "runs" / run_id / name).resolve()
        if p.parent != (self.root / "runs" / run_id).resolve() or not p.is_file():
            raise FileNotFoundError(name)
        return p

    # ------------------------------------------------------------------ catálogo
    def catalog_view(self) -> dict:
        cat = self.catalog
        models = []
        for name, m in cat.models.items():
            storage = []
            for sid in cat.storage_ids(m):
                s = cat.server(m, sid)
                refs, unav = cat.cell_references(m, sid, list(cat.data["metric_approximations"]))
                _, unav_strict = cat.cell_references(m, sid, [])
                storage.append({
                    "id": sid, "label": s["label"], "hardware": s.get("hardware", {}), "eighth_only": s.get("eighth_only", False),
                    "refs": [dict(_jsonable(r.to_dict()), resource_label=RESOURCE_LABELS.get(r.resource, r.resource),
                                  needs_approval=r.resource in unav_strict and unav_strict[r.resource].reference is not None)
                             for r in refs.values()],
                    "unavailable": [{"resource": u.resource, "resource_label": RESOURCE_LABELS.get(u.resource, u.resource),
                                     "reason": u.reason} for u in unav.values()]})
            dbs = [{"id": s["id"], "label": s["label"], "figures": s.get("figures", {})}
                   for s in m["servers"] if s["role"] == "db"]
            models.append({"model": name, "generation": m["generation"], "read_iops_tier": m["read_iops_tier"],
                           "read_iops_tier_basis": m["read_iops_tier_basis"], "limits": m["limits"],
                           "network": m["network"], "latency_claims": m["latency_claims"], "source": cat.source(m),
                           "storage": storage, "db_servers": dbs,
                           "racks": [{"name": r["name"], "db_servers": r["db_servers"], "storage_servers": r["storage_servers"],
                                      "storage_ids": r["storage_ids"], "flash": r.get("flash", {}), "disk": r.get("disk", {}),
                                      "page": r.get("page_perf")} for r in m["racks"]]})
        return {"version": cat.version, "notes": cat.data["notes"], "models": models,
                "approximations": cat.data["approximations"], "metric_approximations": cat.data["metric_approximations"],
                "metric_kinds": cat.data["metric_kinds"], "unit_conversion": cat.data["unit_conversion"]}


# ----------------------------------------------------------------------------- trabajos

class Jobs:
    """Trabajos largos (importación, simulación, dimensionamiento) en hilos con estado consultable."""

    def __init__(self):
        self._jobs: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, fn, *args) -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "status": "running", "log": [], "result": None, "error": None,
               "started_utc": _now(), "finished_utc": None}
        with self._lock:
            self._jobs[jid] = job

        def log(msg):
            job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")

        def run():
            try:
                job["result"] = fn(*args, log=log)
                job["status"] = "done"
            except Exception as e:  # el error se muestra en la interfaz
                job["status"] = "error"
                job["error"] = str(e)
                log(f"ERROR: {e}")
            finally:
                job["finished_utc"] = _now()

        threading.Thread(target=run, daemon=True).start()
        return jid

    def get(self, jid: str) -> Optional[dict]:
        return self._jobs.get(jid)
