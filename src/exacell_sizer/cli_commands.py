"""Comandos de la CLI (simulación, importación, análisis, dimensionamiento, informes, catálogo)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _capture_id(db: str, ref: str) -> int:
    con = sqlite3.connect(db)
    try:
        if str(ref).isdigit():
            row = con.execute("SELECT id FROM capture WHERE id=?", (int(ref),)).fetchone()
        else:
            row = con.execute("SELECT id FROM capture WHERE label=?", (ref,)).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"captura {ref!r} no encontrada en {db}")
    return row[0]


# ----------------------------------------------------------------------------- simulate

def cmd_simulate(a) -> int:
    from .simulate import SimConfig, write_capture
    if a.config:
        cfg = SimConfig.from_json_file(a.config)
    else:
        cfg = SimConfig(cells=[f"{a.prefix}{i:02d}" for i in range(1, a.cells + 1)], hdd_per_cell=a.hdd,
                        flash_per_cell=a.flash, load_factor=a.load_factor, seed=a.seed)
    p = write_capture(cfg, a.out, a.start, a.end, name=a.name)
    print(f"Captura SIMULADA generada en {p} (no procede de ningún Exadata real)")
    return 0


# ----------------------------------------------------------------------------- import

def cmd_import(a) -> int:
    from .importer import ImportError_, import_capture
    try:
        r = import_capture(a.capture, a.db, label=a.label, role=a.role, workdir=a.workdir)
    except ImportError_ as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Captura importada: id={r.capture_id} estado={r.import_status} muestras={r.samples}")
    if r.issues:
        print("Incidencias:", ", ".join(f"{k}={v}" for k, v in sorted(r.issues.items())))
    return 0 if r.import_status == "COMPLETE" else 3


def cmd_issues(a) -> int:
    con = sqlite3.connect(a.db)
    cid = _capture_id(a.db, a.capture)
    for row in con.execute("SELECT severity, code, host, metric, message, source_file, source_line FROM issue "
                           "WHERE capture_id=? ORDER BY id LIMIT ?", (cid, a.limit)):
        print(" | ".join("" if x is None else str(x) for x in row))
    return 0


# ----------------------------------------------------------------------------- report (una captura)

def cmd_report(a) -> int:
    from . import catalog, reports
    from .analysis import analyze_capture
    from .sizing import current_load_vs_reference
    cid = _capture_id(a.db, a.capture)
    an = analyze_capture(a.db, cid, max_gap=a.max_gap)
    cat = catalog.load()
    current = None
    if a.model and a.storage_type:
        current = {an.role: current_load_vs_reference(cat, an, a.model, a.storage_type, a.approximation or [])}
    model = reports.build_model(a.db, {an.role: an}, cat, current=current)
    files = reports.write_all(a.db, model, a.out, a.locale)
    for k, v in files.items():
        for f in v:
            print(f"{k}: {f}")
    return 0


# ----------------------------------------------------------------------------- size (comparación)

def cmd_size(a) -> int:
    from . import catalog, reports
    from .analysis import analyze_capture
    from .sizing import ScenarioError, current_load_vs_reference, evaluate
    scenario = json.loads(Path(a.scenario).read_text(encoding="utf-8"))
    cat = catalog.load()
    src = analyze_capture(a.db, _capture_id(a.db, a.source), max_gap=a.max_gap)
    src.role = "origen"
    analyses = {"origen": src}
    dst = None
    if a.dest:
        dst = analyze_capture(a.db, _capture_id(a.db, a.dest), max_gap=a.max_gap)
        dst.role = "destino"
        analyses["destino"] = dst
    try:
        results = evaluate(cat, src, dst, scenario)
    except ScenarioError as e:
        print(f"ERROR en el escenario: {e}", file=sys.stderr)
        return 2
    current = {}
    for role, key in (("origen", "source_system"), ("destino", "destination_current")):
        info = scenario.get(key)
        if info and role in analyses:
            current[role] = current_load_vs_reference(cat, analyses[role], info.get("model", ""),
                                                      info.get("storage_type", ""),
                                                      scenario.get("approximations", []))
    model = reports.build_model(a.db, analyses, cat, scenario, results, current or None)
    files = reports.write_all(a.db, model, a.out, a.locale)
    for r in results:
        print(f"[{r.candidate['id']}] {r.model} {r.storage_type}: {r.verdict} | teórico={r.n_theoretical} "
              f"recomendado={r.n_recommended} N-1={r.n_failure} limitante={r.limiting_resource} "
              f"aprobación={r.operational_approval}")
    for k, v in files.items():
        for f in v:
            print(f"{k}: {f}")
    return 0


# ----------------------------------------------------------------------------- catalog

def cmd_catalog(a) -> int:
    from . import catalog
    cat = catalog.load()
    if a.action == "list":
        print(f"Catálogo {cat.version} ({cat.path})")
        for name, m in cat.models.items():
            src = cat.source(m)
            print(f"  {name:7s} lectura 8K sobre {m['read_iops_tier']:5s} celdas: {', '.join(cat.storage_ids(m)):24s} "
                  f"fuente: {src['file']} [{src['edition']}]")
        print("Aproximaciones de modelo (requieren autorización explícita):")
        for ap in cat.data["approximations"]:
            print(f"  {ap['id']}: {ap['warning']}")
        print("Aproximaciones de métrica:")
        for k, v in cat.data["metric_approximations"].items():
            print(f"  {k}: {v}")
        return 0
    if a.action == "show":
        if not a.model:
            print("show requiere --model", file=sys.stderr)
            return 1
        m, ap = cat.resolve(a.model, a.approximation or [])
        if ap:
            print(f"AVISO: aproximación {ap['id']}: {ap['warning']}")
        for sid in cat.storage_ids(m):
            refs, unav = cat.cell_references(m, sid, a.approximation or [])
            print(f"\n{m['model']} {sid}")
            for r in refs.values():
                print(f"  {r.resource:17s} {r.value:>14} {r.unit:5s} [{r.evidence}] {r.source_file} p.{r.page}"
                      + (f" | {r.derivation}" if r.derivation else ""))
            for u in unav.values():
                print(f"  {u.resource:17s} {'no disponible':>14}       {u.reason[:110]}")
        return 0
    if a.action == "verify":
        if not a.pdf_dir:
            print("verify requiere --pdf-dir", file=sys.stderr)
            return 1
        bad = 0
        for h in cat.verify_pdf_hashes(a.pdf_dir):
            print(f"  {h['file']}: {h['status']}")
            bad += h["status"] != "OK"
        res = cat.verify_pdfs(a.pdf_dir)
        for r in res:
            if r["status"] != "OK":
                bad += 1
                print(f"  {r['model']} {r['figure']}={r['value']} p.{r['page']}: {r['status']}")
        print(f"Cifras verificadas en su página: {sum(r['status'] == 'OK' for r in res)}/{len(res)}")
        return 0 if bad == 0 else 3
    return 1


def register(sub) -> None:
    s = sub.add_parser("simulate", help="Generar una captura SIMULADA (formato del recolector)")
    s.add_argument("--out", required=True)
    s.add_argument("--start", required=True, help="ISO-8601 con zona, p. ej. 2026-11-03T00:00:00+01:00")
    s.add_argument("--end", required=True)
    s.add_argument("--config", help="JSON de SimConfig (celdas, fallos, picos...)")
    s.add_argument("--cells", type=int, default=3)
    s.add_argument("--prefix", default="simcel")
    s.add_argument("--hdd", type=int, default=12)
    s.add_argument("--flash", type=int, default=4)
    s.add_argument("--load-factor", type=float, default=1.0)
    s.add_argument("--seed", default="exacell")
    s.add_argument("--name")
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("import", help="Validar e importar una captura (directorio, .tar.gz o .zip) a SQLite")
    s.add_argument("capture")
    s.add_argument("--db", required=True)
    s.add_argument("--label")
    s.add_argument("--role", default="origen", choices=["origen", "destino", "otro"])
    s.add_argument("--workdir", help="Directorio de extracción para archivos comprimidos")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("issues", help="Listar incidencias de importación")
    s.add_argument("--db", required=True)
    s.add_argument("--capture", required=True)
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_issues)

    s = sub.add_parser("report", help="Informe de una captura (MD, CSV, HTML, JSON)")
    s.add_argument("--db", required=True)
    s.add_argument("--capture", required=True, help="id o etiqueta")
    s.add_argument("--out", required=True)
    s.add_argument("--locale", default="es", choices=["es", "c"])
    s.add_argument("--model", help="Modelo del sistema medido (para comparar con su referencia)")
    s.add_argument("--storage-type")
    s.add_argument("--approximation", action="append")
    s.add_argument("--max-gap", type=int, default=120)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("size", help="Dimensionar candidatos y comparar origen/destino")
    s.add_argument("--db", required=True)
    s.add_argument("--source", required=True, help="captura origen (id o etiqueta)")
    s.add_argument("--dest", help="captura del destino actual (opcional)")
    s.add_argument("--scenario", required=True, help="JSON del escenario (ver examples/)")
    s.add_argument("--out", required=True)
    s.add_argument("--locale", default="es", choices=["es", "c"])
    s.add_argument("--max-gap", type=int, default=120)
    s.set_defaults(func=cmd_size)

    s = sub.add_parser("catalog", help="Consultar o verificar el catálogo de modelos")
    s.add_argument("action", choices=["list", "show", "verify"])
    s.add_argument("--model")
    s.add_argument("--approximation", action="append")
    s.add_argument("--pdf-dir")
    s.set_defaults(func=cmd_catalog)
