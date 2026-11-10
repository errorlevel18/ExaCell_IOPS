"""Coherencia entre SQLite, JSON, CSV, Markdown y HTML."""
import csv
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from exacell_sizer import catalog, reports
from exacell_sizer.analysis import analyze_capture
from exacell_sizer.importer import import_capture
from exacell_sizer.simulate import SimConfig, write_capture
from exacell_sizer.sizing import VERDICT_INSUFFICIENT, VERDICT_NO, VERDICT_OK, evaluate

S, E = "2026-11-03T00:00:00+01:00", "2026-11-04T00:00:00+01:00"
SCENARIO = {
    "growth_pct": 10, "reserve_pct": 20, "n_minus_1": {"enabled": True},
    "candidates": [
        {"id": "si", "model": "X7-2", "storage_type": "HC", "cells": 14},
        {"id": "no", "model": "X7-2", "storage_type": "HC", "cells": 3},
        {"id": "insuf", "model": "X10M", "storage_type": "HC"},
    ],
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rep")
    db = str(tmp / "r.db")
    caps = {}
    for role, seed, lf in (("origen", "a", 3.0), ("destino", "b", 1.0)):
        p = write_capture(SimConfig(cells=[f"{role[:3]}{i}" for i in range(3)], hdd_per_cell=12, flash_per_cell=1,
                                    seed=seed, load_factor=lf), str(tmp / "caps"), S, E, name=role)
        r = import_capture(p, db, label=role, role=role)
        caps[role] = analyze_capture(db, r.capture_id)
    cat = catalog.load()
    res = evaluate(cat, caps["origen"], caps["destino"], SCENARIO)
    model = reports.build_model(db, caps, cat, SCENARIO, res)
    out_es = tmp / "es"
    out_c = tmp / "c"
    reports.write_all(db, model, str(out_es), "es")
    reports.write_all(db, model, str(out_c), "c")
    return db, model, res, out_es, out_c


def read_csv(path, sep):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=sep))


def dec(s, comma):
    return None if s == "" else Decimal(s.replace(",", ".") if comma else s)


def test_verdicts_cover_yes_no_insufficient(built):
    _, _, res, _, _ = built
    assert [r.verdict for r in res] == [VERDICT_OK, VERDICT_NO, VERDICT_INSUFFICIENT]


def test_sqlite_vs_series_csv(built):
    db, model, _, out_es, out_c = built
    con = sqlite3.connect(db)
    rows = con.execute("SELECT c.label, m.measure, m.host, m.minute_epoch, m.value FROM measure_minute m "
                       "JOIN capture c ON c.id=m.capture_id").fetchall()
    from exacell_sizer.cellcli_parse import epoch_to_utc
    expected = {(l, ms, "SISTEMA" if h == "*" else h, epoch_to_utc(t)): Decimal(v) for l, ms, h, t, v in rows}
    for out, sep, comma in ((out_es, ";", True), (out_c, ",", False)):
        got = {(r["captura"], r["medida"], r["celda"], r["minuto_utc"]): dec(r["valor"], comma)
               for r in read_csv(out / "series.csv", sep)}
        assert got == expected


def test_json_vs_aggregates_csv_vs_md_html(built):
    db, model, _, out_es, _ = built
    js = json.loads((out_es / "informe.json").read_text(encoding="utf-8"))
    agg = read_csv(out_es / "agregados.csv", ";")
    md = (out_es / "informe.md").read_text(encoding="utf-8")
    html = (out_es / "informe.html").read_text(encoding="utf-8")
    checked = 0
    for role, cb in js["captures"].items():
        for m in cb["measures"]:
            if not m["summable"] or m["system"]["status"] != "OK":
                continue
            row = next(r for r in agg if r["captura"] == cb["label"] and r["medida"] == m["id"] and r["celda"] == "SISTEMA")
            assert dec(row["maximo"], True) == Decimal(m["system"]["stats"]["max"])
            assert row["minutos_completos"] == str(m["system"]["complete_minutes"])
            # el mismo máximo, formateado, aparece en MD y HTML
            f = reports.fmt_es(m["system"]["stats"]["max"])
            assert f in md and f in html
            # y coincide con el máximo de la serie en SQLite
            con = sqlite3.connect(db)
            mx = max(Decimal(v) for (v,) in con.execute(
                "SELECT value FROM measure_minute WHERE capture_id=? AND measure=? AND host='*'", (cb["capture_id"], m["id"])))
            assert mx == Decimal(m["system"]["stats"]["max"])
            checked += 1
    assert checked >= 8


def test_sizing_csv_json_md_html(built):
    _, model, res, out_es, _ = built
    js = json.loads((out_es / "informe.json").read_text(encoding="utf-8"))
    cands = read_csv(out_es / "dimensionamiento_candidatos.csv", ";")
    recs = read_csv(out_es / "dimensionamiento_recursos.csv", ";")
    md = (out_es / "informe.md").read_text(encoding="utf-8")
    html = (out_es / "informe.html").read_text(encoding="utf-8")
    for r, jc, cc in zip(res, js["candidates"], cands):
        assert r.verdict == jc["verdict"] == cc["dictamen"]
        assert str(r.n_recommended) == str(jc["n_recommended"]) == cc["recomendado"]
        assert (cc["parcial"] == "sí") == r.partial
        assert r.verdict in md and r.verdict in html
        for rr in r.resources:
            row = next(x for x in recs if x["candidato"] == r.candidate["id"] and x["recurso"] == rr.resource)
            assert row["estado"] == rr.status
            if rr.margin is not None:
                assert dec(row["margen_pct"], True) == rr.margin * 100
            else:
                assert row["margen_pct"] == ""
    comb = read_csv(out_es / "carga_combinada.csv", ";")
    for c in js["combined"]:
        row = next(x for x in comb if x["medida"] == c["measure"])
        assert dec(row["envolvente_max_separados"], True) == (Decimal(c["envelope"]) if c["envelope"] else None)


def test_html_self_contained_and_reference_labels(built):
    _, _, _, out_es, _ = built
    html = (out_es / "informe.html").read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)
    assert "@import" not in html and "<link" not in html
    assert html.count("<svg") >= 6 and 'class="ref"' in html
    labels = re.findall(r'<text class="reflabel"[^>]*>([^<]*)</text>', html)
    assert any("máx. publicado/teórico" in l for l in labels)
    assert not any("garantizad" in l.lower() for l in labels)
    assert "prefers-color-scheme: dark" in html


def test_guide_and_csv_readme(built):
    _, _, _, out_es, out_c = built
    assert "Datos insuficientes para concluir" in (out_es / "GUIA_INTERPRETACION.md").read_text(encoding="utf-8")
    assert "';'" in (out_es / "LEEME_CSV.txt").read_text(encoding="utf-8")
    assert "','" in (out_c / "LEEME_CSV.txt").read_text(encoding="utf-8")
    first = (out_c / "agregados.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert first.startswith("captura,rol,medida")


def test_single_capture_report(built, tmp_path):
    db, model, _, _, _ = built
    from exacell_sizer.sizing import current_load_vs_reference
    a = analyze_capture(db, 1)
    cat = catalog.load()
    cur = {"origen": current_load_vs_reference(cat, a, "X7-2", "HC")}
    m = reports.build_model(db, {"origen": a}, cat, current=cur)
    files = reports.write_all(db, m, str(tmp_path / "single"))
    assert "candidates" not in json.loads(Path(files["json"][0]).read_text())
    md = Path(files["md"][0]).read_text(encoding="utf-8")
    assert "Carga actual frente a su referencia publicada" in md
