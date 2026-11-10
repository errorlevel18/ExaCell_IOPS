"""Extremo a extremo: recolector Bash REAL + dcli/ssh simulados -> CLI import -> size -> informes.

Nunca se contacta un Exadata real: los ejecutables ``dcli``/``ssh`` de tests/fakes emulan CellCLI.
"""
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

E2E_METRICS = """CELL CL_CPUT
CELL N_HCA_MB_RCV_SEC
CELL N_HCA_MB_TRANS_SEC
CELLDISK CD_IO_RQ_R_SM_SEC
CELLDISK CD_IO_RQ_W_SM_SEC
CELLDISK CD_IO_BY_R_SM_SEC
CELLDISK CD_IO_BY_R_LG_SEC
CELLDISK CD_IO_BY_W_SM_SEC
CELLDISK CD_IO_BY_W_LG_SEC
"""
START, END = "2026-11-03T00:00:00+01:00", "2026-11-04T00:00:00+01:00"
SCENARIO = {
    "growth_pct": 20, "reserve_pct": 20, "n_minus_1": {"enabled": True, "recovery_overhead_pct": 15},
    "source_system": {"model": "X7-2", "storage_type": "HC"},
    "candidates": [
        {"id": "si", "model": "X7-2", "storage_type": "HC", "cells": 12},
        {"id": "no", "model": "X7-2", "storage_type": "HC", "rack_config": "Quarter Rack"},
        {"id": "insuficiente", "model": "X11M", "storage_type": "HC"},
    ],
}


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "exacell_sizer", *args], capture_output=True, text=True, env=env,
                          timeout=900)


def read_csv(p):
    with open(p, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


@pytest.mark.parametrize("fail", [False, True], ids=["captura_completa", "bloque_con_error_de_red"])
def test_bash_collector_to_reports(run_collector, tmp_path, fail):
    sim = {"hdd_per_cell": 12, "flash_per_cell": 1, "load_factor": 2.5}
    if fail:
        sim["failures"] = {"network": [{"cell": "cel02", "metric": "CD_IO_RQ_R_SM_SEC",
                                        "block_start": "2026-11-03T13:00:00+01:00"}]}
    r = run_collector(["cel01", "cel02"], sim=sim, start=START, end=END, metrics=E2E_METRICS,
                      extra_args=["-p", "2"])
    assert r.rc == (3 if fail else 0), r.stdout + r.stderr
    db = str(tmp_path / "e2e.db")
    imp = cli("import", str(r.capture), "--db", db, "--label", "origen")
    assert imp.returncode == (3 if fail else 0), imp.stdout + imp.stderr
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps(SCENARIO))
    out = tmp_path / "informe"
    sz = cli("size", "--db", db, "--source", "origen", "--scenario", str(sc), "--out", str(out))
    assert sz.returncode == 0, sz.stderr
    for f in ("informe.json", "informe.md", "informe.html", "series.csv", "agregados.csv",
              "dimensionamiento_candidatos.csv", "dimensionamiento_recursos.csv", "carga_combinada.csv",
              "LEEME_CSV.txt", "GUIA_INTERPRETACION.md"):
        assert (out / f).exists(), f
    cands = {c["candidato"]: c for c in read_csv(out / "dimensionamiento_candidatos.csv")}
    if fail:
        assert {c["dictamen"] for c in cands.values()} == {"Datos insuficientes para concluir"}
        md = (out / "informe.md").read_text(encoding="utf-8")
        assert "BLOCK_NETWORK_ERROR" in md and "INCOMPLETA" in md
    else:
        assert cands["si"]["dictamen"] == "Compatible bajo las referencias y supuestos indicados"
        assert cands["no"]["dictamen"] == "No compatible con los límites evaluados"
        assert cands["insuficiente"]["dictamen"] == "Datos insuficientes para concluir"
        assert cands["si"]["aprobacion_operativa"] == "pendiente"
        js = json.loads((out / "informe.json").read_text(encoding="utf-8"))
        assert js["captures"]["origen"]["import_status"] == "COMPLETE"
        assert js["current_vs_reference"]["origen"]
    # la captura original sigue verificando sus sumas tras todo el proceso
    chk = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=r.capture, capture_output=True)
    assert chk.returncode == 0


@pytest.mark.parametrize("src_cells,dst_cells", [(1, 4), (5, 2)])
def test_python_capture_cell_counts_and_combination(tmp_path, src_cells, dst_cells):
    caps = tmp_path / "caps"
    db = str(tmp_path / "x.db")
    for role, n in (("origen", src_cells), ("destino", dst_cells)):
        cfg = tmp_path / f"{role}.json"
        cfg.write_text(json.dumps({"cells": [f"{role[:3]}{i}" for i in range(n)], "hdd_per_cell": 2,
                                   "flash_per_cell": 1, "seed": role}))
        assert cli("simulate", "--out", str(caps), "--name", role, "--config", str(cfg),
                   "--start", START, "--end", END).returncode == 0
        assert cli("import", str(caps / role), "--db", db, "--label", role, "--role", role).returncode == 0
    sc = tmp_path / "sc.json"
    sc.write_text(json.dumps({"candidates": [{"id": "x9", "model": "X9M-2", "storage_type": "HC", "cells": 3,
                                              "approximations": ["read_iops_memory_tier_as_flash"]}]}))
    out = tmp_path / "out"
    r = cli("size", "--db", db, "--source", "origen", "--dest", "destino", "--scenario", str(sc), "--out", str(out))
    assert r.returncode == 0, r.stderr
    js = json.loads((out / "informe.json").read_text(encoding="utf-8"))
    assert len(js["captures"]["origen"]["cells"]) == src_cells
    assert len(js["captures"]["destino"]["cells"]) == dst_cells
    comb = {c["measure"]: c for c in js["combined"]}
    c = comb["flash_write_iops_small"]
    from decimal import Decimal
    assert Decimal(c["envelope"]) == Decimal(c["source_max"]) + Decimal(c["dest_max"])
    assert Decimal(c["coincident_max"]) <= Decimal(c["envelope"])
    assert c["per_destination_cell"]["x9"]["cells"] == 3
    assert js["candidates"][0]["verdict"] == "Compatible bajo las referencias y supuestos indicados"


def test_cli_catalog_and_collector(tmp_path):
    r = cli("catalog", "list")
    assert r.returncode == 0 and "X11M" in r.stdout and "X8-2_as_X8M-2" in r.stdout
    r = cli("catalog", "show", "--model", "X10M")
    assert "requiere aproximación autorizada" in r.stdout
    r = cli("collector", "show")
    assert r.stdout.startswith("#!/bin/bash") and "NOT <" in r.stdout
    r = cli("collector", "export", "--dest", str(tmp_path))
    assert r.returncode == 0 and os.access(tmp_path / "exacell_collect.sh", os.X_OK)
    assert cli("collector", "export", "--dest", str(tmp_path)).returncode == 1  # no sobrescribe
    pdfs = os.environ.get("EXACELL_DATASHEETS")
    if pdfs:
        r = cli("catalog", "verify", "--pdf-dir", pdfs)
        assert r.returncode == 0 and "265/265" in r.stdout
