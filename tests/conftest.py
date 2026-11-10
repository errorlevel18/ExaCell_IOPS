import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "src" / "exacell_sizer" / "resources" / "exacell_collect.sh"
FAKES = ROOT / "tests" / "fakes"

# Métricas reducidas para que las pruebas sean rápidas (una invocación ssh simulada por bloque).
FAST_METRICS = """CELL CL_CPUT
CELLDISK CD_IO_RQ_R_SM_SEC
CELLDISK CD_IO_RQ_W_SM_SEC
"""


class CollectorRun:
    def __init__(self, proc, outdir, log):
        self.proc = proc
        self.rc = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        caps = sorted(Path(outdir).glob("exacell_capture_*"))
        self.capture = caps[0] if caps else None
        self.log = [json.loads(l) for l in Path(log).read_text().splitlines()] if Path(log).exists() else []

    def manifest(self):
        out = {}
        for line in (self.capture / "manifest.txt").read_text().splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out

    def blocks(self):
        lines = (self.capture / "blocks.tsv").read_text().splitlines()
        hdr = lines[0].split("\t")
        return [dict(zip(hdr, l.split("\t"))) for l in lines[1:]]


@pytest.fixture
def run_collector(tmp_path):
    def _run(cells, sim=None, start="2026-11-01T00:00:00+01:00", end="2026-11-01T02:00:00+01:00",
             user="celladmin", metrics=FAST_METRICS, extra_args=(), group_lines=None):
        sim = dict(sim or {})
        sim.setdefault("cells", cells)
        sim.setdefault("hdd_per_cell", 2)
        sim.setdefault("flash_per_cell", 1)
        cfg = tmp_path / "sim.json"
        cfg.write_text(json.dumps(sim))
        group = tmp_path / "cell_group"
        group.write_text("\n".join(group_lines if group_lines is not None else cells) + "\n")
        outdir = tmp_path / "out"
        outdir.mkdir(exist_ok=True)
        args = ["bash", str(COLLECTOR), "-d", str(outdir), "-g", str(group), "-u", user,
                "-s", start, "-e", end]
        if metrics is not None:
            mf = tmp_path / "metrics.txt"
            mf.write_text(metrics)
            args += ["-m", str(mf)]
        args += list(extra_args)
        log = tmp_path / "fake_log.jsonl"
        env = dict(os.environ, FAKE_EXA_CONFIG=str(cfg), FAKE_EXA_LOG=str(log),
                   PATH=f"{FAKES}:{os.environ['PATH']}")
        proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=600)
        return CollectorRun(proc, outdir, log)

    return _run
