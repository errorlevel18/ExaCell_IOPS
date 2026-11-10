"""Pruebas de la aplicación web local: API, seguridad básica y flujo en navegador real."""
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT
from exacell_sizer.simulate import SimConfig, write_capture
from exacell_sizer.web.server import make_server, markdown_to_html


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    httpd, workspace, jobs = make_server(str(ws), port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", workspace
    httpd.shutdown()


def call(base, path, data=None, raw=None, headers=None, method=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            ct = r.headers.get("Content-Type", "")
            b = r.read()
            return r.status, (json.loads(b) if "json" in ct else b)
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b


def wait_job(base, jid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s, j = call(base, f"/api/jobs/{jid}")
        if j["status"] != "running":
            return j
        time.sleep(0.3)
    raise AssertionError("trabajo sin terminar")


@pytest.fixture(scope="module")
def loaded(app, tmp_path_factory):
    base, ws = app
    for label, role, prefix in (("web_origen", "origen", "o"), ("web_destino", "destino", "d")):
        s, b = call(base, "/api/captures/simulate", {"label": label, "role": role, "cells": 2, "hours": 24,
                                                     "hdd": 3, "flash": 1, "prefix": prefix, "seed": label})
        assert s == 202
        j = wait_job(base, b["job"])
        assert j["status"] == "done", j
    # subida de un .tar.gz con una captura incompleta (bloque con error de red)
    tmp = tmp_path_factory.mktemp("up")
    cap = write_capture(SimConfig(cells=["u1", "u2"], hdd_per_cell=2, flash_per_cell=1,
                                  failures={"network": [{"cell": "u2", "metric": "CD_IO_RQ_R_SM_SEC",
                                                         "block_start": "2026-11-03T05:00:00+01:00"}]}),
                        str(tmp), "2026-11-03T00:00:00+01:00", "2026-11-04T00:00:00+01:00", name="exacell_capture_x")
    arc = tmp / "cap.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(cap, arcname="exacell_capture_x")
    data = arc.read_bytes()
    s, b = call(base, "/api/captures/upload", raw=data, headers={
        "Content-Type": "application/octet-stream", "X-Filename": "cap.tar.gz", "X-Label": "web_subida", "X-Role": "otro"})
    assert s == 202, b
    j = wait_job(base, b["job"])
    assert j["status"] == "done" and j["result"]["import_status"] == "INCOMPLETE"
    return base, ws, arc


def test_static_and_info(app):
    base, _ = app
    s, html = call(base, "/")
    assert s == 200 and b"ExaCell Sizer" in html and b"/static/app.js" in html
    clean = html.replace(b"http://www.w3.org/2000/svg", b"")  # espacio de nombres SVG, no es un recurso
    assert b"https://" not in clean and b"http://" not in clean  # sin recursos externos
    for f in ("app.js", "charts.js", "app.css"):
        s, body = call(base, f"/static/{f}")
        assert s == 200 and b"http://" not in body.replace(b"http://www.w3.org/2000/svg", b"")
    s, info = call(base, "/api/info")
    assert s == 200 and info["catalog_version"]


def test_captures_list_detail_series(loaded):
    base, ws, _ = loaded
    s, caps = call(base, "/api/captures")
    labels = {c["label"]: c for c in caps}
    assert {"web_origen", "web_destino", "web_subida"} <= set(labels)
    assert labels["web_subida"]["import_status"] == "INCOMPLETE" and labels["web_subida"]["errors"] > 0
    cid = labels["web_origen"]["id"]
    s, d = call(base, f"/api/captures/{cid}")
    assert s == 200 and d["import_status"] == "COMPLETE" and len(d["measures"]) >= 15
    s, se = call(base, f"/api/captures/{cid}/series?measure=flash_write_iops_small&host=*")
    assert s == 200 and len(se["points"]) > 100
    # la serie coincide con el máximo de sistema del detalle
    m = next(x for x in d["measures"] if x["id"] == "flash_write_iops_small")
    assert max(p[1] for p in se["points"]) == pytest.approx(float(m["system"]["stats"]["max"]))
    s, se = call(base, f"/api/captures/{cid}/series?measure=cell_cpu_pct&host={d['cells'][0]}")
    assert s == 200
    assert call(base, f"/api/captures/{cid}/series?measure=nope")[0] == 400
    s, d2 = call(base, f"/api/captures/{labels['web_subida']['id']}")
    assert any(i["code"] == "BLOCK_NETWORK_ERROR" for i in d2["issues_all"])


def test_upload_kept_readonly_and_validation(loaded):
    base, ws, arc = loaded
    ups = [p for p in (ws.root / "uploads").iterdir() if not p.name.endswith(".meta.json")]
    assert len(ups) == 1 and ups[0].read_bytes() == arc.read_bytes()
    assert not os.access(ups[0], os.W_OK) or os.geteuid() == 0
    assert (ups[0].stat().st_mode & 0o222) == 0
    meta = json.loads((ups[0].parent / (ups[0].name + ".meta.json")).read_text())
    import hashlib
    assert meta["sha256"] == hashlib.sha256(arc.read_bytes()).hexdigest()
    s, b = call(base, "/api/captures/upload", raw=b"xx", headers={"X-Filename": "evil.exe"})
    assert s == 400
    s, b = call(base, "/api/captures/upload", raw=b"xx", headers={"X-Filename": "a.tar.gz", "X-Label": "web_origen"})
    assert s == 409


def test_sizing_run_and_downloads(loaded):
    base, ws, _ = loaded
    s, caps = call(base, "/api/captures")
    ids = {c["label"]: c["id"] for c in caps}
    s, ex = call(base, "/api/scenario/example")
    assert s == 200 and ex["candidates"]
    sc = dict(ex, candidates=[{"id": "si", "model": "X7-2", "storage_type": "HC", "cells": 6},
                              {"id": "no", "model": "X7-2", "storage_type": "HC", "cells": 3, "db_servers": 2},
                              {"id": "insuf", "model": "X10M", "storage_type": "HC"}], growth_pct=0)
    s, b = call(base, "/api/sizing", {"scenario": sc, "source": ids["web_origen"], "dest": ids["web_destino"]})
    assert s == 202
    j = wait_job(base, b["job"])
    assert j["status"] == "done", j
    run_id = j["result"]["run_id"]
    s, r = call(base, f"/api/runs/{run_id}")
    assert s == 200
    verdicts = {c["candidate"]["id"]: c["verdict"] for c in r["candidates"]}
    assert verdicts["insuf"] == "Datos insuficientes para concluir"
    assert r["charts"] and all(ch["refs"] for ch in r["charts"] if ch["resource"] != "flash_read_iops")
    assert {"informe.html", "informe.json", "informe.md", "series.csv"} <= set(r["files"])
    # el JSON web y el informe JSON coinciden en dictámenes
    s, js = call(base, f"/runs/{run_id}/informe.json")
    jsd = json.loads(js) if isinstance(js, bytes) else js
    assert [c["verdict"] for c in jsd["candidates"]] == [c["verdict"] for c in r["candidates"]]
    s, html = call(base, f"/runs/{run_id}/informe.html?view=1")
    assert s == 200 and b"Informe ExaCell Sizer" in html
    s, runs = call(base, "/api/runs")
    assert runs[0]["run_id"] == run_id


def test_sizing_errors_and_traversal(loaded):
    base, ws, _ = loaded
    s, b = call(base, "/api/sizing", {"scenario": {"candidates": []}, "source": 1})
    assert s == 400 and "Escenario no válido" in b["error"]
    s, b = call(base, "/api/sizing", {"scenario": {"candidates": [{"id": "a"}], "min_capture_hours": 1}, "source": 1})
    assert s == 400
    for p in ("/runs/../../exacell.db", "/runs/run_1_abcd/..%2F..%2Fexacell.db", "/static/../server.py",
              "/api/runs/..", "/runs/run_20260101T000000_abcd/informe.html"):
        assert call(base, p)[0] == 404, p


def test_catalog_collector_docs(app):
    base, _ = app
    s, cat = call(base, "/api/catalog")
    assert s == 200 and {m["model"] for m in cat["models"]} == {"X6-2", "X7-2", "X8M-2", "X9M-2", "X10M", "X11M"}
    x10 = next(m for m in cat["models"] if m["model"] == "X10M")
    hc = next(s for s in x10["storage"] if s["id"] == "HC")
    rd = next(r for r in hc["refs"] if r["resource"] == "flash_read_iops")
    assert rd["needs_approval"] is True
    s, col = call(base, "/api/collector")
    assert col["script"].startswith("#!/bin/bash") and len(col["sha256"]) == 64
    s, raw = call(base, "/api/collector/download")
    assert s == 200 and raw.startswith(b"#!/bin/bash")
    s, d = call(base, "/api/docs/guia")
    assert s == 200 and "<h1>" in d["html"] and "<table>" in d["html"]
    assert call(base, "/api/docs/nada")[0] == 404


def test_markdown_renderer_escapes():
    h = markdown_to_html("# T\n\n<script>alert(1)</script> **b** `c`\n\n- x\n- y\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert "<strong>b</strong>" in h and "<code>c</code>" in h and "<ul>" in h and "<td>1</td>" in h


def _node_playwright():
    if not shutil.which("node"):
        return False
    r = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="requiere node + playwright")
def test_browser_user_flow(tmp_path):
    cap = write_capture(SimConfig(cells=["b1", "b2"], hdd_per_cell=2, flash_per_cell=1), str(tmp_path / "c"),
                        "2026-11-03T00:00:00+01:00", "2026-11-04T00:00:00+01:00", name="exacell_capture_b")
    arc = tmp_path / "b.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(cap, arcname="exacell_capture_b")
    httpd, ws, jobs = make_server(str(tmp_path / "ws"), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r = subprocess.run(["node", str(ROOT / "tests" / "web_ui_flow.js"), f"http://127.0.0.1:{httpd.server_address[1]}",
                            str(arc), ""], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.startswith("OK")
    finally:
        httpd.shutdown()


@pytest.mark.skipif(not _node_playwright(), reason="requiere node + playwright")
def test_browser_ssh_flow(tmp_path, monkeypatch):
    rdir = tmp_path / "dbnode" / "capturas"
    rdir.mkdir(parents=True)
    group = tmp_path / "dbnode" / "cell_group"
    group.write_text("cel01\ncel02\n")
    sim = tmp_path / "sim.json"
    sim.write_text(json.dumps({"cells": ["cel01", "cel02"], "hdd_per_cell": 2, "flash_per_cell": 1}))
    monkeypatch.setenv("EXACELL_SSH_BIN", str(ROOT / "tests" / "fakes_dbnode" / "ssh_dbnode"))
    monkeypatch.setenv("FAKE_EXA_CONFIG", str(sim))
    httpd, ws, jobs = make_server(str(tmp_path / "ws"), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r = subprocess.run(["node", str(ROOT / "tests" / "web_ssh_flow.js"), f"http://127.0.0.1:{httpd.server_address[1]}",
                            str(rdir), str(group), ""], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        httpd.shutdown()
