"""Código común de los ejecutables falsos ``ssh`` y ``dcli`` (solo pruebas)."""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from exacell_sizer.simulate import SimBackend, SimConfig, parse_cellcli_time  # noqa: E402

REMOTE_RE = re.compile(r'^cellcli -e "(?P<cmd>[^"]*)"$')
HIST_T1 = re.compile(r"collectionTime NOT < '([^']+)'")
HIST_NAME = re.compile(r"name = '([A-Za-z0-9_]+)'")


def backend():
    cfg = SimConfig.from_json_file(os.environ["FAKE_EXA_CONFIG"])
    return SimBackend(cfg)


def log(entry):
    path = os.environ.get("FAKE_EXA_LOG")
    if not path:
        return
    entry["ts"] = time.time()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def run_remote(be, tool, user, host, remote):
    """Ejecuta el comando remoto simulado. Devuelve (stdout, stderr, rc)."""
    m = REMOTE_RE.match(remote.strip())
    if not m:
        log({"tool": tool, "user": user, "host": host, "remote": remote, "violation": True})
        return "", f"fake: comando remoto no permitido: {remote!r}\n", 1
    cmd = m.group("cmd")
    log({"tool": tool, "user": user, "host": host, "remote": remote, "violation": False})
    if user not in ("root", "celladmin"):
        return "", f"Permission denied (publickey) for {user}@{host}\n", 255
    t1 = None
    mt = HIST_T1.search(cmd)
    if mt:
        t1 = parse_cellcli_time(mt.group(1)).timestamp()
    mn = HIST_NAME.search(cmd)
    metric = mn.group(1) if mn else None
    if be.failure_for(host, "network", metric, t1):
        return "", f"ssh: connect to host {host} port 22: Connection timed out\n", 255
    return be.run(host, cmd)
