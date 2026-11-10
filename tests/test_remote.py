"""Recogida remota por SSH contra un dbnode SIMULADO (tests/fakes_dbnode/ssh_dbnode).

El dbnode falso ejecuta en local el recolector real, que a su vez usa los dcli/ssh falsos de las
celdas. Nunca se contacta ningún sistema real.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from conftest import ROOT
from exacell_sizer import collector as coll
from exacell_sizer import remote as rmt
from exacell_sizer.web.workspace import Workspace

DBNODE_SSH = ROOT / "tests" / "fakes_dbnode" / "ssh_dbnode"


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbnode_dir = tmp_path / "dbnode" / "capturas"
    dbnode_dir.mkdir(parents=True)
    group = tmp_path / "dbnode" / "cell_group"
    group.write_text("# celdas\ncel01\ncel02\n")
    sim = tmp_path / "sim.json"
    sim.write_text(json.dumps({"cells": ["cel01", "cel02"], "hdd_per_cell": 2, "flash_per_cell": 1}))
    monkeypatch.setenv("EXACELL_SSH_BIN", str(DBNODE_SSH))
    monkeypatch.setenv("FAKE_EXA_CONFIG", str(sim))
    monkeypatch.setenv("FAKE_EXA_LOG", str(tmp_path / "cells.jsonl"))
    monkeypatch.setenv("FAKE_DBNODE_LOG", str(tmp_path / "dbnode.jsonl"))
    ws = Workspace(str(tmp_path / "ws"))
    conn = {"host": "dbnode01", "user": "oracle", "port": 22, "auth": "agent"}
    params = {"dir": str(dbnode_dir), "group": str(group), "cell_user": "celladmin",
              "start": "2026-11-03T00:00:00+01:00", "end": "2026-11-03T02:00:00+01:00", "parallel": 2,
              "label": "remota", "role": "origen"}
    return ws, conn, params, tmp_path


def wait_finished(ws, rid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rec = ws.ssh_status(rid)
        if rec["status"]["state"] == "finished":
            return rec
        time.sleep(1)
    raise AssertionError("la recolección remota no terminó")


def logs(tmp, name):
    p = tmp / name
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ----------------------------------------------------------------------------- validación

@pytest.mark.parametrize("bad", [
    {"host": "db; rm -rf /", "user": "oracle"}, {"host": "db", "user": "or acle"},
    {"host": "db", "user": "oracle", "port": 70000}, {"host": "db", "user": "$(id)"},
    {"host": "db", "user": "oracle", "auth": "password"}, {"host": "db", "user": "oracle", "auth": "key", "key_path": "/no/existe"},
])
def test_conn_validation(bad):
    with pytest.raises(rmt.RemoteError):
        rmt.Conn.from_dict(bad)


@pytest.mark.parametrize("p", ["relativo/dir", "/tmp/../etc", "/tmp/a b", "/tmp/a;rm", "/tmp/$(id)", ""])
def test_dir_validation(p):
    with pytest.raises(rmt.RemoteError):
        rmt.validate_dir(p, "dir")


def test_collector_args_validation(env):
    _, _, params, _ = env
    assert rmt.collector_args(params)[:2] == ["-d", params["dir"]]
    for k, v in (("cell_user", "oracle"), ("start", "2026-11-03 00:00"), ("end", "mañana"), ("parallel", 99)):
        with pytest.raises(rmt.RemoteError):
            rmt.collector_args(dict(params, **{k: v}))


# ----------------------------------------------------------------------------- flujo completo

def test_probe_dryrun_start_status_import(env):
    ws, conn, params, tmp = env
    pr = ws.ssh_test(conn, params["dir"], params["group"])
    assert pr["ready"] and pr["cells"] == ["cel01", "cel02"]
    dr = ws.ssh_dryrun(conn, params)["output"]
    assert "NOT <" in dr and "no se contacta ninguna celda" in dr
    script = Path(rmt.script_remote_path(params["dir"]))
    assert script.read_bytes() == coll.script_path().read_bytes()
    rec = ws.ssh_start(conn, params)
    assert rec["pid"] > 0 and rec["status"]["state"] == "running"
    rec = wait_finished(ws, rec["id"])
    st = rec["status"]
    assert st["capture_status"] == "COMPLETE" and st["exit_code"] == "0"
    assert st["expected_blocks"] == st["done_blocks"] > 0 and st["progress"] == 1
    assert "Estado:             COMPLETE" in st["log_tail"]
    res = ws.ssh_import_run(rec["id"], None)
    assert res["import_status"] == "COMPLETE"
    caps = {c["label"]: c for c in ws.list_captures()}
    assert caps["remota"]["cells"] == 2
    # la captura en el dbnode sigue íntegra y el tar descargado se guardó en solo lectura
    ups = [p for p in (ws.root / "uploads").iterdir() if p.name.endswith(".tar.gz")]
    assert len(ups) == 1 and (ups[0].stat().st_mode & 0o222) == 0
    meta = json.loads((ups[0].parent / (ups[0].name + ".meta.json")).read_text())
    assert meta["source"].startswith("ssh://oracle@dbnode01:22/")
    # segunda ejecución: el script se reutiliza (mismo SHA-256), no se recopia
    rmt.install_script(ws._ssh(conn), params["dir"])
    cmds = [e["remote"] for e in logs(tmp, "dbnode.jsonl") if e.get("result") == "exec"]
    assert all("cellcli" not in c for c in cmds)       # la app nunca habla con celdas
    cells = logs(tmp, "cells.jsonl")
    assert cells and not any(e["violation"] for e in cells)


def test_cancel_marks_interrupted(env):
    ws, conn, params, _ = env
    rec = ws.ssh_start(conn, dict(params, end="2026-11-06T00:00:00+01:00", parallel=1, label="larga"))
    time.sleep(3)
    rec = ws.ssh_cancel(rec["id"])
    rec = wait_finished(ws, rec["id"])
    assert rec["status"]["capture_status"] == "INTERRUPTED" and rec["status"]["exit_code"] == "130"
    res = ws.ssh_import_run(rec["id"], None)
    assert res["import_status"] == "INCOMPLETE"


def test_list_and_fetch_existing_capture(env):
    ws, conn, params, _ = env
    rec = ws.ssh_start(conn, dict(params, label=None))
    wait_finished(ws, rec["id"])
    caps = ws.ssh_list_captures(conn, params["dir"])
    assert len(caps) == 1 and caps[0]["capture_status"] == "COMPLETE" and caps[0]["cell_count"] == "2"
    res = ws.ssh_fetch(conn, params["dir"], caps[0]["name"], "traida", "destino")
    assert res["import_status"] == "COMPLETE"
    with pytest.raises(rmt.RemoteError):
        ws.ssh_fetch(conn, params["dir"], "../../etc", None, "origen")
    with pytest.raises(rmt.RemoteError):
        ws.ssh_fetch(conn, params["dir"], "exacell_capture_noexiste", None, "origen")


def test_host_key_and_errors(env, monkeypatch):
    ws, conn, params, _ = env
    monkeypatch.setenv("FAKE_DBNODE_UNKNOWN", "1")
    with pytest.raises(rmt.RemoteError, match="known_hosts"):
        ws.ssh_test(conn, params["dir"], params["group"])
    assert ws.ssh_test(dict(conn, accept_new=True), params["dir"], params["group"])["ready"]
    monkeypatch.delenv("FAKE_DBNODE_UNKNOWN")
    with pytest.raises(rmt.RemoteError, match="resuelve"):
        ws.ssh_test(dict(conn, host="noexiste"), params["dir"], params["group"])
    pr = ws.ssh_test(conn, params["dir"] + "_no", params["group"])
    assert not pr["ready"] and any(not c["ok"] and "Directorio" in c["check"] for c in pr["checks"])


def test_password_auth_never_persisted(env, monkeypatch):
    ws, conn, params, tmp = env
    monkeypatch.setenv("FAKE_DBNODE_PASSWORD", "S3creta!x")
    with pytest.raises(rmt.RemoteError, match="Autenticación"):
        ws.ssh_test(conn, params["dir"], params["group"])
    with pytest.raises(rmt.RemoteError, match="Autenticación"):
        ws.ssh_test(dict(conn, auth="password", password="mala"), params["dir"], params["group"])
    pc = dict(conn, auth="password", password="S3creta!x")
    assert ws.ssh_test(pc, params["dir"], params["group"])["ready"]
    rec = ws.ssh_start(pc, params)
    rec = wait_finished(ws, rec["id"])          # usa la contraseña de la sesión en memoria
    assert rec["status"]["capture_status"] == "COMPLETE"
    for f in list(ws.root.rglob("*")) + [tmp / "dbnode.jsonl"]:
        if f.is_file() and f.stat().st_size < 50_000_000:
            assert b"S3creta!x" not in f.read_bytes(), f
    ws2 = Workspace(str(ws.root))                 # tras reiniciar la aplicación hay que volver a pedirla
    with pytest.raises(rmt.RemoteError, match="contraseña"):
        ws2.ssh_status(rec["id"])
    assert ws2.ssh_status(rec["id"], "S3creta!x")["status"]["state"] == "finished"
