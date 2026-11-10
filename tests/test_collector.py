"""Pruebas del recolector Bash con dcli/ssh/cellcli SIMULADOS (nunca contra Exadata real)."""
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from conftest import COLLECTOR, FAKES, FAST_METRICS



def hist_calls(run):
    return [e for e in run.log if "METRICHISTORY" in e.get("remote", "")]


@pytest.mark.parametrize("ncells", [1, 3, 7])
def test_any_number_of_cells_complete(run_collector, ncells):
    cells = [f"cel{i:02d}" for i in range(1, ncells + 1)]
    r = run_collector(cells)
    assert r.rc == 0, r.stderr
    m = r.manifest()
    assert m["capture_status"] == "COMPLETE"
    assert m["cell_count"] == str(ncells)
    assert int(m["expected_blocks"]) == ncells * 3 * 2  # 3 métricas x 2 bloques horarios
    assert int(m["recorded_blocks"]) == int(m["expected_blocks"])
    assert {b["status"] for b in r.blocks()} == {"OK"}
    # cada celda se prevalida con dcli antes del primer histórico
    first_hist = min(e["ts"] for e in hist_calls(r))
    dcli_calls = [e for e in r.log if e["tool"] == "dcli"]
    assert {e["host"] for e in dcli_calls} == set(cells)
    assert max(e["ts"] for e in dcli_calls) < first_hist


def test_only_readonly_cellcli_and_no_files_on_cells(run_collector):
    r = run_collector(["cel01", "cel02"])
    assert r.rc == 0
    assert r.log, "el simulador debe registrar llamadas"
    for e in r.log:
        assert not e["violation"], e
        cmd = re.match(r'^cellcli -e "(.*)"$', e["remote"]).group(1)
        assert cmd.upper().startswith("LIST "), cmd
        for forbidden in (">", "mkdir", "tee", "scp", "ALTER", "CREATE", "DROP", "calibrate"):
            assert forbidden not in cmd.replace("NOT <", "").replace("collectionTime <", ""), cmd


def test_history_syntax_matches_tested_environment(run_collector):
    r = run_collector(["cel01"])
    calls = hist_calls(r)
    assert calls
    for e in calls:
        cmd = e["remote"]
        assert ">=" not in cmd and "<=" not in cmd
        assert re.search(r"WHERE name = '[A-Z0-9_]+' AND collectionTime NOT < '[^']+' AND collectionTime < '[^']+' "
                         r"ATTRIBUTES name, metricObjectName, collectionTime, metricValue", cmd), cmd


def test_metrics_selected_from_catalog_by_scope_and_name(run_collector):
    # Lista por defecto (sin -m). Una celda carece de CL_MEMUT en su catálogo.
    r = run_collector(["cel01", "cel02"], sim={"catalog_remove": {"cel02": ["CL_MEMUT"]}}, metrics=None,
                      end="2026-11-01T01:00:00+01:00")
    assert r.rc == 0, r.stdout + r.stderr
    sel1 = (r.capture / "cells/cel01/metrics_selected.tsv").read_text()
    sel2 = (r.capture / "cells/cel02/metrics_selected.tsv").read_text()
    miss2 = (r.capture / "cells/cel02/metrics_missing.tsv").read_text()
    assert "CL_MEMUT" in sel1 and "CL_MEMUT" not in sel2
    assert "CL_MEMUT\tno_en_catalogo" in miss2
    names = {e["remote"].split("name = '")[1].split("'")[0] for e in hist_calls(r)}
    assert "CD_IO_RQ_R_SM_SEC_EXTRA" not in names  # no se filtra por prefijo
    assert not any(n.startswith(("DB_", "PDB_", "CT_")) for n in names)
    assert int(r.manifest()["expected_blocks"]) == len(sel1.splitlines()) + len(sel2.splitlines())


def test_prevalidation_failure_collects_nothing(run_collector):
    r = run_collector(["cel01", "cel02", "cel03"], sim={"failures": {"prevalidate": [{"cell": "cel02"}]}})
    assert r.rc == 2
    m = r.manifest()
    assert m["capture_status"] == "PREVALIDATION_FAILED"
    assert not hist_calls(r)
    assert not any((r.capture / "data").iterdir())
    pv = (r.capture / "prevalidation/prevalidation.tsv").read_text()
    assert "cel02\tdcli_list_cell\tFAIL" in pv


def test_prevalidation_ssh_failure(run_collector):
    # dcli responde pero ssh directo falla en la prevalidación (red)
    r = run_collector(["cel01", "cel02"], sim={"failures": {"network": [{"cell": "cel01", "metric": None}]}})
    assert r.rc == 2
    assert not hist_calls(r)


@pytest.mark.parametrize("kind,status,rc", [
    ("cellcli_error", "CELLCLI_ERROR", "0"),
    ("network", "NETWORK_ERROR", "255"),
    ("interrupted", "INTERRUPTED", "255"),
    ("empty", "EMPTY", "0"),
])
def test_block_failures_mark_incomplete(run_collector, kind, status, rc):
    f = {"cell": "cel02", "metric": "CD_IO_RQ_R_SM_SEC", "block_start": "2026-11-01T01:00:00+01:00"}
    r = run_collector(["cel01", "cel02"], sim={"failures": {kind: [f]}})
    assert r.rc == 3, r.stdout
    m = r.manifest()
    assert m["capture_status"] == "INCOMPLETE"
    bad = [b for b in r.blocks() if b["status"] != "OK"]
    assert len(bad) == 1
    assert bad[0]["status"] == status and bad[0]["rc"] == rc
    assert bad[0]["cell"] == "cel02" and bad[0]["block_index"] == "1"
    assert status in (r.capture / "summary.txt").read_text()


def test_cell_lost_mid_capture(run_collector):
    f = {"cell": "cel01", "from_block_start": "2026-11-01T01:00:00+01:00"}
    r = run_collector(["cel01", "cel02"], sim={"failures": {"network": [f]}},
                      end="2026-11-01T03:00:00+01:00")
    assert r.rc == 3
    bad = [b for b in r.blocks() if b["status"] != "OK"]
    assert {b["cell"] for b in bad} == {"cel01"}
    assert {b["block_index"] for b in bad} == {"1", "2"}
    assert all(b["status"] == "NETWORK_ERROR" for b in bad)


def test_24h_hourly_blocks_and_boundaries(run_collector):
    r = run_collector(["cel01"], sim={"sec_offset": 0}, metrics="CELL CL_CPUT\n",
                      start="2026-11-01T00:00:00Z", end="2026-11-02T00:00:00Z")
    assert r.rc == 0
    blocks = r.blocks()
    assert len(blocks) == 24
    for a, b in zip(blocks, blocks[1:]):
        assert a["block_end"] == b["block_start"]
    # muestras exactamente en el límite horario: cada minuto aparece una sola vez
    stamps = []
    for b in blocks:
        for line in (r.capture / b["out_file"]).read_text().splitlines():
            stamps.append(line.split()[2])
    assert len(stamps) == 1440 == len(set(stamps))
    assert "AVISO" not in (r.capture / "summary.txt").read_text()


def test_short_capture_warns(run_collector):
    r = run_collector(["cel01"])
    assert "no apto para aprobar" in r.stdout


def test_root_user_accepted(run_collector):
    r = run_collector(["cel01"], user="root")
    assert r.rc == 0
    assert all(e["user"] == "root" for e in r.log if "user" in e)


@pytest.mark.parametrize("kwargs,msg", [
    ({"user": "oracle"}, "usuario remoto no permitido"),
    ({"start": "2026-11-01T00:00:00"}, "zona horaria"),
    ({"end": "2026-10-31T00:00:00+01:00"}, "posterior"),
    ({"metrics": "IORM_DATABASE DB_IO_RQ_SM_SEC\n"}, "no permitido"),
    ({"metrics": "CELLDISK DB_IO_RQ_SM_SEC\n"}, "DB/PDB/IORM"),
    ({"group_lines": ["cel01", "cel01"]}, "duplicada"),
    ({"group_lines": ["root@cel01"]}, "inválido"),
    ({"group_lines": []}, "vacío"),
])
def test_usage_errors_contact_nothing(run_collector, kwargs, msg):
    r = run_collector(["cel01"], **kwargs)
    assert r.rc == 1
    assert msg in r.stderr
    assert r.log == []
    assert r.capture is None


def test_dry_run_creates_nothing(run_collector):
    r = run_collector(["cel01", "cel02"], extra_args=["-n"])
    assert r.rc == 0
    assert "NOT <" in r.stdout and r.capture is None and r.log == []


def test_checksums_verify(run_collector):
    r = run_collector(["cel01"])
    p = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=r.capture, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_sigterm_marks_interrupted(tmp_path):
    cfg = tmp_path / "sim.json"
    cfg.write_text(json.dumps({"cells": ["cel01", "cel02"], "hdd_per_cell": 1, "flash_per_cell": 1}))
    group = tmp_path / "g"
    group.write_text("cel01\ncel02\n")
    out = tmp_path / "out"
    out.mkdir()
    mf = tmp_path / "m"
    mf.write_text(FAST_METRICS)
    env = dict(os.environ, FAKE_EXA_CONFIG=str(cfg), PATH=f"{FAKES}:{os.environ['PATH']}")
    p = subprocess.Popen(["bash", str(COLLECTOR), "-d", str(out), "-g", str(group), "-u", "root", "-m", str(mf),
                          "-s", "2026-11-01T00:00:00Z", "-e", "2026-11-03T00:00:00Z", "-p", "1"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        if list(out.glob("*/data/cel01/*.out")):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=60)
    assert p.returncode == 130
    cap = next(out.glob("exacell_capture_*"))
    man = (cap / "manifest.txt").read_text()
    assert "capture_status=INTERRUPTED" in man
    m = dict(l.split("=", 1) for l in man.splitlines())
    assert int(m["not_run_blocks"]) > 0
