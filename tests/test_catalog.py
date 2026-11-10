"""Integridad del catálogo y reglas de no-equiparación."""
import os
from decimal import Decimal
from pathlib import Path

import pytest

from exacell_sizer import catalog

PDF_DIR = os.environ.get("EXACELL_DATASHEETS")


@pytest.fixture(scope="module")
def cat():
    return catalog.load()


def test_catalog_valid_and_covers_x6_to_x11(cat):
    assert cat.validate() == []
    assert set(cat.models) == {"X6-2", "X7-2", "X8M-2", "X9M-2", "X10M", "X11M"}
    for m in cat.models.values():
        src = cat.source(m)
        assert src["file"].endswith(".pdf") and len(src["sha256"]) == 64 and src["edition"]
    assert "HC-Z" in cat.storage_ids(cat.models["X11M"])
    assert "XT" in cat.storage_ids(cat.models["X9M-2"])
    assert "HC-EIGHTH" in cat.storage_ids(cat.models["X7-2"])


def test_every_figure_has_page_and_conditions(cat):
    for m in cat.models.values():
        for sid in cat.storage_ids(m):
            refs, unav = cat.cell_references(m, sid, ["read_iops_memory_tier_as_flash"])
            for r in refs.values():
                assert r.page and r.source_file and r.label and r.evidence
                assert r.value > 0
                if r.evidence == "derivado":
                    assert r.derivation


def test_memory_tier_reads_not_equated_with_flash(cat):
    for name in ("X8M-2", "X9M-2", "X10M", "X11M"):
        refs, unav = cat.cell_references(cat.models[name], "HC")
        assert "flash_read_iops" not in refs
        assert "read_iops_memory_tier_as_flash" in unav["flash_read_iops"].reason
        assert unav["flash_read_iops"].reference.tier in ("pmem", "xrmem")
        refs2, _ = cat.cell_references(cat.models[name], "HC", ["read_iops_memory_tier_as_flash"])
        assert "flash_read_iops" in refs2
        assert any("APROXIMACIÓN AUTORIZADA" in w for w in refs2["flash_read_iops"].warnings)
    refs, _ = cat.cell_references(cat.models["X7-2"], "HC")
    assert refs["flash_read_iops"].tier == "flash" and refs["flash_read_iops"].requires_approximation is None


def test_write_iops_after_mirroring_and_scan_vs_rdma_separate(cat):
    refs, _ = cat.cell_references(cat.models["X11M"], "HC")
    assert "mirroring" in refs["flash_write_iops"].conditions
    # flash scan por servidor = 100 GB/s, no los 500 GB/s de RDMA Memory ni los 1,500 GB/s de rack
    assert refs["flash_read_mbps"].published_value == Decimal("100")


def test_hdd_derived_from_rack_conservatively(cat):
    refs, _ = cat.cell_references(cat.models["X10M"], "HC")
    r = refs["hdd_iops"]
    assert r.evidence == "derivado"
    assert r.value == Decimal("2555.556")  # Elastic Configuration 1: 23,000 / 9 (mínimo)
    assert "23000" in r.derivation and "/ 9" in r.derivation
    assert any("escalado lineal" in w for w in r.warnings)
    refs_z, _ = cat.cell_references(cat.models["X11M"], "HC-Z")
    assert refs_z["hdd_iops"].value == Decimal("1300.000")  # 3,900 / 3 (Quarter Rack Server-Z)


def test_x6_eighth_iops_not_derived_from_db_bound_rack(cat):
    refs, unav = cat.cell_references(cat.models["X6-2"], "HC-EIGHTH")
    assert "flash_read_iops" in unav and "flash_write_iops" in unav
    assert refs["flash_read_mbps"].evidence == "derivado"


def test_ef_and_xt(cat):
    refs, unav = cat.cell_references(cat.models["X9M-2"], "EF")
    assert "hdd_iops" in unav
    refs, unav = cat.cell_references(cat.models["X9M-2"], "XT")
    assert set(refs) == {"network_mbps"}
    assert "no tiene flash" in unav["flash_write_iops"].reason
    assert "n/a" in unav["hdd_iops"].reason


def test_unit_conversion_conservative(cat):
    refs, _ = cat.cell_references(cat.models["X7-2"], "HC")
    # 25 GB/s (10^9 B) en MB de CellCLI (2^20 B) = 23,841.858 < 25,000
    assert refs["flash_read_mbps"].value == Decimal("23841.858")


def test_unknown_model_and_approximation(cat):
    with pytest.raises(catalog.UnknownModel, match="no está en el catálogo"):
        cat.resolve("X5-2")
    with pytest.raises(catalog.UnknownModel, match="autorización explícita"):
        cat.resolve("X8-2")
    m, ap = cat.resolve("X8-2", ["X8-2_as_X8M-2"])
    assert m["model"] == "X8M-2" and "sobreestiman" in ap["warning"]
    m, ap = cat.resolve("exadata x10m")
    assert m["model"] == "X10M" and ap is None
    with pytest.raises(catalog.UnknownModel):
        cat.server(cat.models["X7-2"], "HC-Z")


def test_make_model_detection_never_decides_x8():
    d = catalog.detect_from_make_model("Oracle Corporation ORACLE SERVER X8-2L High Capacity")
    assert d["candidates"] == ["X8-2", "X8M-2"] and d["storage_type"] == "HC" and d["note"]
    assert catalog.detect_from_make_model("ORACLE SERVER X7-2L Extreme Flash")["candidates"] == ["X7-2"]
    assert catalog.detect_from_make_model(None)["candidates"] == []


def test_db_server_references(cat):
    r = cat.db_references(cat.models["X11M"], "DB-Z")
    assert r["db_read_iops"].value == Decimal("1400000")


@pytest.mark.skipif(not PDF_DIR, reason="defina EXACELL_DATASHEETS con la carpeta de PDFs")
def test_figures_found_on_cited_pages(cat):
    assert all(r["status"] == "OK" for r in cat.verify_pdf_hashes(PDF_DIR))
    bad = [r for r in cat.verify_pdfs(PDF_DIR) if r["status"] != "OK"]
    assert bad == []
