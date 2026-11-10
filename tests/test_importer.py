"""Pruebas de importación, validación y normalización."""
import hashlib
import io
import os
import shutil
import sqlite3
import tarfile
from decimal import Decimal
from pathlib import Path

import pytest

from exacell_sizer import cellcli_parse as cp
from exacell_sizer.importer import ImportError_, import_capture
from exacell_sizer.simulate import SimConfig, write_capture

FAST = [("CELL", "CL_CPUT"), ("CELLDISK", "CD_IO_RQ_R_SM_SEC"), ("CELLDISK", "CD_IO_RQ_W_SM_SEC")]
S, E = "2026-11-01T00:00:00+01:00", "2026-11-01T02:00:00+01:00"


def sim(tmp_path, metrics=FAST, start=S, end=E, name=None, **kw):
    kw.setdefault("cells", ["cel01", "cel02"])
    kw.setdefault("hdd_per_cell", 2)
    kw.setdefault("flash_per_cell", 1)
    return Path(write_capture(SimConfig(**kw), str(tmp_path / "caps"), start, end, metrics=metrics, name=name))


def resign(cap: Path):
    sums = []
    for f in sorted(p for p in cap.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        sums.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  ./{f.relative_to(cap)}")
    (cap / "SHA256SUMS").write_text("\n".join(sums) + "\n")


def tree_hash(p: Path):
    h = hashlib.sha256()
    for f in sorted(x for x in p.rglob("*") if x.is_file()):
        h.update(str(f.relative_to(p)).encode())
        h.update(f.read_bytes())
        h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()


def codes(db, cid=1):
    con = sqlite3.connect(db)
    return {r[0] for r in con.execute("SELECT code FROM issue WHERE capture_id=?", (cid,))}


# ----------------------------------------------------------------------------- parser

@pytest.mark.parametrize("raw,val,unit", [
    ("7,406.5 IO/sec", "7406.5", "IO/sec"),
    ("1,234,567.000123456789 MB/sec", "1234567.000123456789", "MB/sec"),
    ("0.000 %", "0", "%"),
    ("12 IO requests", "12", "IO requests"),
    ("3.5", "3.5", ""),
    ("-1.25 C", "-1.25", "C"),
])
def test_parse_value_exact(raw, val, unit):
    d, u, err = cp.parse_value(raw)
    assert err is None and cp.canonical(d) == val and u == unit
    assert isinstance(d, Decimal)


@pytest.mark.parametrize("raw", ["N/A IO/sec", "", "1,23 IO/sec", "nan %", "12abc"])
def test_parse_value_non_numeric_is_none_not_zero(raw):
    d, _, err = cp.parse_value(raw)
    assert d is None and err


def test_timestamp_utc_and_no_guessing():
    assert cp.parse_timestamp("2026-11-01T01:00:17+01:00")[0] == cp.parse_timestamp("2026-11-01T00:00:17Z")[0]
    assert cp.parse_timestamp("2026-11-01T00:00:17-05:00")[0] == cp.parse_timestamp("2026-11-01T05:00:17Z")[0]
    assert cp.parse_timestamp("2026-11-01T00:00:17")[0] is None


# ----------------------------------------------------------------------------- importación

def test_import_complete_and_utc(tmp_path):
    cap = sim(tmp_path, tz_offset="-05:00", sec_offset=17)
    db = str(tmp_path / "a.db")
    r = import_capture(str(cap), db, role="origen")
    assert r.import_status == "COMPLETE"
    con = sqlite3.connect(db)
    # 2 celdas x (1 objeto CELL + 2 métricas x 3 discos) x 120 minutos
    assert r.samples == 2 * (1 + 2 * 3) * 120
    row = con.execute("SELECT ts_raw, ts_utc FROM sample ORDER BY ts_epoch LIMIT 1").fetchone()
    assert row[0].endswith("-05:00") and row[1] == "2026-10-31T23:00:17Z"
    # procedencia: archivo y línea
    src = con.execute("SELECT b.out_file, s.source_line, s.raw_value, s.value FROM sample s JOIN block b ON b.id=s.block_id"
                      " LIMIT 1").fetchone()
    line = (cap / src[0]).read_text().splitlines()[src[1] - 1]
    assert src[2] in line and cp.canonical(cp.parse_value(src[2])[0]) == src[3]


def test_import_does_not_modify_original(tmp_path):
    cap = sim(tmp_path)
    before = tree_hash(cap)
    import_capture(str(cap), str(tmp_path / "a.db"))
    assert tree_hash(cap) == before
    with pytest.raises(ImportError_):
        import_capture(str(cap), str(cap / "dentro.db"))
    assert tree_hash(cap) == before


def test_reimport_rejected(tmp_path):
    cap = sim(tmp_path)
    db = str(tmp_path / "a.db")
    import_capture(str(cap), db)
    with pytest.raises(ImportError_):
        import_capture(str(cap), db)


@pytest.mark.parametrize("kind,code", [
    ("cellcli_error", "BLOCK_CELLCLI_ERROR"),
    ("network", "BLOCK_NETWORK_ERROR"),
    ("interrupted", "BLOCK_INTERRUPTED"),
    ("empty", "BLOCK_EMPTY"),
])
def test_failed_blocks_incomplete_not_zero(tmp_path, kind, code):
    f = {"cell": "cel01", "metric": "CD_IO_RQ_R_SM_SEC", "block_start": "2026-11-01T01:00:00+01:00"}
    cap = sim(tmp_path, failures={kind: [f]})
    db = str(tmp_path / "a.db")
    r = import_capture(str(cap), db)
    assert r.import_status == "INCOMPLETE"
    assert code in codes(db) and "CAPTURE_REPORTED_INCOMPLETE" in codes(db)
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM sample WHERE host='cel01' AND metric='CD_IO_RQ_R_SM_SEC' AND ts_epoch>=?",
                    (cp.parse_timestamp("2026-11-01T01:00:00+01:00")[0],)).fetchone()[0]
    assert n < 60 * 3  # faltan muestras: no se rellenan con ceros
    assert con.execute("SELECT COUNT(*) FROM sample WHERE value='0' AND metric='CD_IO_RQ_R_SM_SEC'").fetchone()[0] == 0


def test_tampered_capture_detected(tmp_path):
    cap = sim(tmp_path)
    f = next((cap / "data" / "cel01").glob("b0000_CL_CPUT.out"))
    os.chmod(f, 0o644)
    f.write_text(f.read_text().replace("\t", " ", 1))
    db = str(tmp_path / "a.db")
    r = import_capture(str(cap), db)
    assert r.import_status == "INCOMPLETE" and "CHECKSUM_MISMATCH" in codes(db)


def test_missing_block_record_and_plan_gap(tmp_path):
    cap = sim(tmp_path)
    lines = (cap / "blocks.tsv").read_text().splitlines()
    (cap / "blocks.tsv").write_text("\n".join(lines[:-1]) + "\n")
    plan = (cap / "blocks_plan.tsv").read_text().splitlines()
    plan[1] = plan[1].replace("01:00:00+01:00\t", "01:05:00+01:00\t", 1)
    (cap / "blocks_plan.tsv").write_text("\n".join(plan) + "\n")
    resign(cap)
    db = str(tmp_path / "a.db")
    r = import_capture(str(cap), db)
    c = codes(db)
    assert r.import_status == "INCOMPLETE"
    assert {"BLOCK_NOT_RUN", "PLAN_GAP"} <= c


def test_duplicates_and_hour_boundary(tmp_path):
    cap = sim(tmp_path, sec_offset=0)
    b0 = cap / "data/cel01/b0000_CL_CPUT.out"
    b1 = cap / "data/cel01/b0001_CL_CPUT.out"
    first_b1 = b1.read_text().splitlines()[0]          # muestra exactamente en el límite 01:00:00
    assert "T01:00:00+01:00" in first_b1
    b0.write_text(b0.read_text() + first_b1 + "\n")    # aparece también en el bloque anterior (fuera de rango)
    second = b1.read_text().splitlines()[1]
    conflict = second.rsplit("\t", 1)[0] + "\t99.999 %"
    b1.write_text(b1.read_text() + second + "\n" + conflict + "\n")
    resign(cap)
    db = str(tmp_path / "a.db")
    import_capture(str(cap), db)
    c = codes(db)
    assert {"SAMPLE_OUT_OF_BLOCK", "DUPLICATE_SAMPLE", "DUPLICATE_CONFLICT"} <= c
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM sample WHERE host='cel01' AND metric='CL_CPUT'").fetchone()[0] == 120
    kept = con.execute("SELECT raw_value FROM sample WHERE host='cel01' AND metric='CL_CPUT' AND ts_raw=?",
                       (second.split("\t")[3],)).fetchone()[0]
    assert kept != "99.999 %"


def test_units_definitions_non_numeric(tmp_path):
    cap = sim(tmp_path, catalog_unit_override={"cel02": {"CL_CPUT": "furlongs"}})
    f = cap / "data/cel01/b0000_CD_IO_RQ_R_SM_SEC.out"
    lines = f.read_text().splitlines()
    lines[0] = lines[0].rsplit("\t", 1)[0] + "\tN/A IO/sec"
    lines[1] = lines[1].rsplit("\t", 1)[0] + "\t12.5 MB/sec"
    f.write_text("\n".join(lines) + "\n")
    # cel01 pierde la definición de CL_CPUT en su catálogo
    md = cap / "cells/cel01/metricdefinition.out"
    md.write_text("\n".join(l for l in md.read_text().splitlines() if "CL_CPUT" not in l) + "\n")
    resign(cap)
    db = str(tmp_path / "a.db")
    import_capture(str(cap), db)
    c = codes(db)
    assert {"NON_NUMERIC_VALUE", "UNIT_MISMATCH", "UNIT_UNRECOGNIZED", "METRIC_WITHOUT_DEFINITION"} <= c
    con = sqlite3.connect(db)
    row = con.execute("SELECT value, unit_status FROM sample WHERE raw_value='N/A IO/sec'").fetchone()
    assert row[0] is None  # nunca cero
    assert con.execute("SELECT unit_status FROM sample WHERE raw_value='12.5 MB/sec'").fetchone()[0] == "MISMATCH"
    assert con.execute("SELECT DISTINCT unit_status FROM sample WHERE host='cel01' AND metric='CL_CPUT'").fetchone()[0] \
        == "NO_DEFINITION"
    assert con.execute("SELECT DISTINCT unit_status FROM sample WHERE host='cel02' AND metric='CL_CPUT'").fetchone()[0] \
        == "UNRECOGNIZED"


def test_archive_import_and_traversal_rejected(tmp_path):
    cap = sim(tmp_path)
    arc = tmp_path / "cap.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        t.add(cap, arcname=cap.name)
    r = import_capture(str(arc), str(tmp_path / "a.db"), workdir=str(tmp_path / "wd"))
    assert r.import_status == "COMPLETE"
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        data = b"x"
        ti = tarfile.TarInfo("../../escape.txt")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    with pytest.raises(ImportError_):
        import_capture(str(evil), str(tmp_path / "b.db"), workdir=str(tmp_path / "wd2"))
    assert not (tmp_path / "escape.txt").exists()


def test_bash_collector_output_imports_and_matches_python_writer(run_collector, tmp_path):
    r = run_collector(["cel01", "cel02"], sim={"sec_offset": 11})
    assert r.rc == 0
    db = str(tmp_path / "bash.db")
    res = import_capture(str(r.capture), db)
    assert res.import_status == "COMPLETE"
    py = sim(tmp_path, sec_offset=11, metrics=FAST)
    # Mismos archivos de datos con idéntico contenido
    bash_files = sorted(p.relative_to(r.capture) for p in (r.capture / "data").rglob("*.out"))
    py_files = sorted(p.relative_to(py) for p in (py / "data").rglob("*.out"))
    assert bash_files == py_files
    for rel in bash_files:
        assert (r.capture / rel).read_text() == (py / rel).read_text(), rel
    for f in ("blocks_plan.tsv", "cell_group.txt"):
        assert (r.capture / f).read_text() == (py / f).read_text()
    hb = (r.capture / "blocks.tsv").read_text().splitlines()[0]
    assert hb == (py / "blocks.tsv").read_text().splitlines()[0]
    kb = set(dict(l.split("=", 1) for l in (r.capture / "manifest.txt").read_text().splitlines()))
    kp = set(dict(l.split("=", 1) for l in (py / "manifest.txt").read_text().splitlines()))
    assert kb == kp
