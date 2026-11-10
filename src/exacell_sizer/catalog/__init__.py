"""Catálogo versionado de referencias de rendimiento Exadata (datasheets Oracle).

Conceptos:
  * ``Reference``: límite por celda (o por servidor de BD) para un *recurso* del motor, con
    evidencia ``publicado`` o ``derivado``, fuente, página, condiciones y fórmula de derivación.
  * Los recursos del motor NUNCA mezclan capas: lectura 8K por nivel (flash/PMEM/XRMEM),
    escritura flash tras mirroring, IOPS HDD, escaneo flash, escaneo HDD, red, servidor de BD.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CATALOG_PATH = Path(__file__).resolve().parent / "exadata_catalog.json"

# Recursos del motor -> descripción
RESOURCES = {
    "flash_read_iops": "IOPS de lectura pequeña (<=128 KB) servidas por flash (celldisks FlashDisk)",
    "flash_write_iops": "IOPS de escritura pequeña en flash tras mirroring ASM (celldisks FlashDisk)",
    "flash_read_mbps": "MB/s de lectura en flash (escaneo)",
    "hdd_iops": "IOPS pequeñas (lectura+escritura) en discos HDD",
    "hdd_mbps": "MB/s (lectura+escritura) en discos HDD",
    "network_mbps": "MB/s por sentido en la red de almacenamiento de la celda",
}
GATING_APPROXIMATIONS = {"read_iops_memory_tier_as_flash"}


class UnknownModel(Exception):
    pass


@dataclass
class Reference:
    resource: str
    value: Decimal                 # por celda/servidor, en unidad del motor (IOPS o MB/s CellCLI)
    unit: str
    evidence: str                  # publicado | derivado
    source_file: str
    source_title: str
    source_edition: str
    page: Optional[int]
    label: str
    conditions: str
    published_value: Decimal
    published_unit: str
    derivation: Optional[str] = None
    tier: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    requires_approximation: Optional[str] = None
    kind: str = "datasheet_max"    # datasheet_max | nominal_link

    def to_dict(self):
        d = dict(self.__dict__)
        d["value"] = str(self.value)
        d["published_value"] = str(self.published_value)
        return d


@dataclass
class Unavailable:
    resource: str
    reason: str
    reference: Optional[Reference] = None   # cifra existente pero no utilizable sin aproximación


class Catalog:
    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path
        self.version = data["catalog_version"]
        self.models = {m["model"]: m for m in data["models"]}
        conv = data["unit_conversion"]
        self.mb_per_gb = Decimal(conv["datasheet_GB_bytes"]) / Decimal(conv["cellcli_MB_bytes"])

    # ------------------------------------------------------------------ modelos
    def source(self, model: dict) -> dict:
        return self.data["sources"][model["source"]]

    def resolve(self, requested: str, allowed_approximations: Optional[List[str]] = None) -> Tuple[dict, Optional[dict]]:
        """Devuelve (modelo, aproximación usada o None). Lanza UnknownModel si no procede."""
        req = normalize_model_name(requested)
        if req in self.models:
            return self.models[req], None
        allowed = set(allowed_approximations or [])
        for ap in self.data["approximations"]:
            if ap["requested"] == req:
                if ap["id"] in allowed:
                    return self.models[ap["use_model"]], ap
                raise UnknownModel(
                    f"Modelo {requested!r} no está en el catálogo. Aproximación disponible '{ap['id']}' "
                    f"(requiere autorización explícita): {ap['warning']}")
        raise UnknownModel(f"Modelo {requested!r} no está en el catálogo (disponibles: {sorted(self.models)})")

    def server(self, model: dict, server_id: str) -> dict:
        for s in model["servers"]:
            if s["id"] == server_id:
                return s
        raise UnknownModel(f"Tipo de servidor {server_id!r} no existe en {model['model']} "
                           f"(disponibles: {[s['id'] for s in model['servers']]})")

    def storage_ids(self, model: dict) -> List[str]:
        return [s["id"] for s in model["servers"] if s["role"] == "storage"]

    # ------------------------------------------------------------------ referencias por celda
    def _ref(self, model, resource, kind, published, page, label, evidence="publicado", derivation=None,
             tier=None, warnings=None, approx=None, value=None, unit=None, rkind="datasheet_max"):
        src = self.source(model)
        mk = self.data["metric_kinds"].get(kind, {})
        pub_unit = mk.get("unit", unit or "")
        if value is None:
            value = Decimal(str(published))
            if pub_unit == "GB/s":
                value = (value * self.mb_per_gb).quantize(Decimal("0.001"))
        return Reference(
            resource=resource, value=value, unit=unit or ("MB/s" if pub_unit == "GB/s" else pub_unit),
            evidence=evidence, source_file=src["file"], source_title=src["title"], source_edition=src["edition"],
            page=page, label=label or mk.get("label", kind), conditions=mk.get("conditions", ""),
            published_value=Decimal(str(published)), published_unit=pub_unit, derivation=derivation, tier=tier,
            warnings=list(warnings or []), requires_approximation=approx, kind=rkind)

    def _derive_from_racks(self, model, server, spec, section, metric):
        best = None
        for rack in model["racks"]:
            if rack["name"] not in spec["racks"]:
                continue
            sid = spec.get("storage_id", server["id"])
            if section == "disk":
                if rack.get("disk_storage_id", None) not in (None, sid):
                    continue
                if sid not in rack["storage_ids"]:
                    continue
                val = rack.get("disk", {}).get(metric)
            else:
                val = rack.get("flash", {}).get(sid, {}).get(metric)
            if val is None:
                continue
            n = rack.get("storage_servers_by_type", {}).get(sid, rack["storage_servers"])
            per = Decimal(str(val)) / Decimal(n)
            page = rack.get("page_disk" if section == "disk" else "page_perf", rack.get("page_perf"))
            cand = (per, rack["name"], val, n, page)
            if best is None or per < best[0]:
                best = cand
        return best

    def cell_references(self, model: dict, storage_id: str,
                        allowed_metric_approximations: Optional[List[str]] = None
                        ) -> Tuple[Dict[str, Reference], Dict[str, Unavailable]]:
        allowed = set(allowed_metric_approximations or [])
        s = self.server(model, storage_id)
        if s["role"] != "storage":
            raise UnknownModel(f"{storage_id} no es un servidor de almacenamiento")
        refs: Dict[str, Reference] = {}
        unav: Dict[str, Unavailable] = {}
        fig = s.get("figures", {})
        tier = model["read_iops_tier"]
        pg = s.get("page")
        # --- flash
        if not s.get("has_flash"):
            for r in ("flash_read_iops", "flash_write_iops", "flash_read_mbps"):
                unav[r] = Unavailable(r, f"{s['label']} no tiene flash")
        else:
            if "sql_read_iops_8k" in fig:
                warns = ["Cifra para E/S de 8 KB; CellCLI clasifica como 'pequeñas' E/S de hasta 128 KB."]
                approx = None
                if tier != "flash":
                    approx = "read_iops_memory_tier_as_flash"
                    warns.append(f"La cifra publicada se alcanza leyendo desde {tier.upper()} "
                                 f"({model['read_iops_tier_basis']}), no desde flash.")
                refs["flash_read_iops"] = self._ref(model, "flash_read_iops", "sql_read_iops_8k", fig["sql_read_iops_8k"],
                                                    pg, f"{s['label']}: Maximum SQL Read IOPS", tier=tier,
                                                    warnings=warns, approx=approx)
            if "sql_flash_write_iops_8k" in fig:
                refs["flash_write_iops"] = self._ref(
                    model, "flash_write_iops", "sql_flash_write_iops_8k", fig["sql_flash_write_iops_8k"], pg,
                    f"{s['label']}: Maximum SQL Write IOPS", tier="flash",
                    warnings=["Medida en las celdas tras el mirroring ASM: comparable con escrituras observadas en celldisk."])
            if "sql_flash_scan_bandwidth" in fig:
                refs["flash_read_mbps"] = self._ref(
                    model, "flash_read_mbps", "sql_flash_scan_bandwidth", fig["sql_flash_scan_bandwidth"], pg,
                    f"{s['label']}: Maximum SQL Flash Bandwidth", tier="flash",
                    warnings=["Ancho de banda de escaneo SQL sin compresión (lectura); no es throughput mixto."])
            fd = s.get("derivations", {}).get("flash")
            if fd:
                for metric in fd.get("metrics", []):
                    res = {"sql_flash_scan_bandwidth": "flash_read_mbps"}[metric]
                    if res in refs:
                        continue
                    best = self._derive_from_racks(model, s, fd, "flash", metric)
                    if best:
                        per, rname, val, n, page = best
                        value = (per * self.mb_per_gb).quantize(Decimal("0.001"))
                        refs[res] = self._ref(model, res, metric, val, page, f"{rname}: {metric}", evidence="derivado",
                                              derivation=f"{rname} {val} GB/s / {n} celdas = {per:.3f} GB/s por celda",
                                              tier="flash", value=value,
                                              warnings=["Derivado de una cifra de rack: supone escalado lineal.", fd.get("note", "")])
            for r in ("flash_read_iops", "flash_write_iops", "flash_read_mbps"):
                if r not in refs:
                    note = (fd or {}).get("note") or s.get("unpublished_note") or "no publicado por servidor"
                    unav[r] = Unavailable(r, f"{s['label']}: {note}")
        # --- HDD (solo publicado por rack -> derivado)
        if not s.get("has_hdd"):
            for r in ("hdd_iops", "hdd_mbps"):
                unav[r] = Unavailable(r, f"{s['label']} no tiene discos HDD")
        else:
            spec = s.get("derivations", {}).get("hdd")
            for res, metric in (("hdd_iops", "sql_disk_iops"), ("hdd_mbps", "sql_disk_bandwidth")):
                best = self._derive_from_racks(model, s, spec, "disk", metric) if spec else None
                if not best:
                    unav[res] = Unavailable(res, s.get("unpublished_note") or f"{s['label']}: sin cifra HDD publicada")
                    continue
                per, rname, val, n, page = best
                if metric == "sql_disk_bandwidth":
                    value = (per * self.mb_per_gb).quantize(Decimal("0.001"))
                    deriv = f"min(rack/nº celdas) = {rname} {val} GB/s / {n} = {per:.4f} GB/s por celda"
                else:
                    value = per.quantize(Decimal("0.001"))
                    deriv = f"min(rack/nº celdas) = {rname} {val} IOPS / {n} = {per:.1f} IOPS por celda"
                refs[res] = self._ref(model, res, metric, val, page, f"{rname}: {metric}", evidence="derivado",
                                      derivation=deriv, tier="hdd", value=value,
                                      warnings=["HDD solo se publica por rack: valor por celda derivado suponiendo escalado lineal y reparto uniforme.",
                                                "La mezcla lectura/escritura de la cifra publicada no se especifica." if metric == "sql_disk_iops"
                                                else "Escaneo SQL (lectura) sin compresión; se compara con lectura+escritura HDD."])
        # --- red (techo físico nominal)
        net = model["network"]
        mbps = Decimal(net["ports_per_server"] * net["gbps_per_port"]) * Decimal(10 ** 9) / 8 / Decimal(1048576)
        refs["network_mbps"] = self._network_ref(model, mbps)
        for r, ref in list(refs.items()):
            if ref.requires_approximation and ref.requires_approximation not in allowed:
                unav[r] = Unavailable(r, f"requiere aproximación autorizada '{ref.requires_approximation}': "
                                         + self.data["metric_approximations"][ref.requires_approximation], ref)
                del refs[r]
            elif ref.requires_approximation:
                ref.warnings.append("APROXIMACIÓN AUTORIZADA '" + ref.requires_approximation + "': "
                                    + self.data["metric_approximations"][ref.requires_approximation])
                ref.evidence = "publicado (aproximación autorizada)"
        return refs, unav

    def _network_ref(self, model, mbps: Decimal) -> Reference:
        net = model["network"]
        src = self.source(model)
        return Reference(
            resource="network_mbps", value=mbps.quantize(Decimal("0.1")), unit="MB/s", evidence="derivado",
            source_file=src["file"], source_title=src["title"], source_edition=src["edition"], page=net["page"],
            label=f"{net['ports_per_server']}x {net['gbps_per_port']} Gb/s {net['fabric']} por servidor",
            conditions="Especificación de puertos de la tabla de hardware.",
            published_value=Decimal(net["gbps_per_port"]), published_unit="Gb/s por puerto",
            derivation=f"{net['ports_per_server']} puertos x {net['gbps_per_port']} Gb/s / 8 bits = {mbps:.1f} MB/s por sentido",
            tier="network", kind="nominal_link",
            warnings=["Velocidad nominal de puerto (techo físico), no rendimiento útil: solo sirve para descartar, no para aprobar."])

    def db_references(self, model: dict, db_id: str = "DB") -> Dict[str, Reference]:
        s = self.server(model, db_id)
        fig = s.get("figures", {})
        out = {}
        for res, kind in (("db_read_iops", "db_sql_read_iops_8k"), ("db_write_iops", "db_sql_write_iops_8k")):
            if kind in fig:
                out[res] = self._ref(model, res, kind, fig[kind], s["page"], f"{s['label']}: {kind}",
                                     warnings=["Límite del servidor de base de datos (fuera de las celdas)."])
        return out

    def rack_config(self, model: dict, name: str) -> dict:
        for r in model["racks"]:
            if r["name"].lower() == name.lower():
                return r
        raise UnknownModel(f"Configuración {name!r} no publicada para {model['model']} "
                           f"(disponibles: {[r['name'] for r in model['racks']]})")

    # ------------------------------------------------------------------ validación
    def validate(self) -> List[str]:
        errs = []
        for name, m in self.models.items():
            if m["source"] not in self.data["sources"]:
                errs.append(f"{name}: fuente desconocida {m['source']}")
            if m["read_iops_tier"] not in ("flash", "pmem", "xrmem"):
                errs.append(f"{name}: read_iops_tier inválido")
            ids = {s["id"] for s in m["servers"]}
            pages = self.data["sources"][m["source"]]["pages"]
            for s in m["servers"]:
                if not s.get("page") or not 1 <= s["page"] <= pages:
                    errs.append(f"{name}/{s['id']}: página inválida")
                for k, v in s.get("figures", {}).items():
                    if k not in self.data["metric_kinds"]:
                        errs.append(f"{name}/{s['id']}: métrica desconocida {k}")
                    if not (isinstance(v, (int, float)) and v > 0):
                        errs.append(f"{name}/{s['id']}: valor no positivo {k}={v}")
            for r in m["racks"]:
                for sid in r["storage_ids"]:
                    if sid not in ids:
                        errs.append(f"{name}/{r['name']}: storage id {sid} inexistente")
                for sid, figs in r.get("flash", {}).items():
                    for k, v in figs.items():
                        if k not in self.data["metric_kinds"] or not v > 0:
                            errs.append(f"{name}/{r['name']}/{sid}: {k}={v}")
                for p in ("page_config", "page_perf"):
                    if not 1 <= r.get(p, 0) <= pages:
                        errs.append(f"{name}/{r['name']}: {p} inválida")
        for ap in self.data["approximations"]:
            if ap["use_model"] not in self.models:
                errs.append(f"aproximación {ap['id']} apunta a modelo inexistente")
            if ap["requested"] in self.models:
                errs.append(f"aproximación {ap['id']} solicita un modelo que ya existe")
        return errs

    # ------------------------------------------------------------------ verificación contra PDF
    def figure_locations(self):
        """(modelo, descripción, valor, página) de cada cifra publicada."""
        for name, m in self.models.items():
            for s in m["servers"]:
                for k, v in s.get("figures", {}).items():
                    yield name, m["source"], f"{s['id']}.{k}", v, s["page"]
            for r in m["racks"]:
                for sid, figs in r.get("flash", {}).items():
                    for k, v in figs.items():
                        yield name, m["source"], f"{r['name']}.{sid}.{k}", v, r["page_perf"]
                for k, v in r.get("disk", {}).items():
                    yield name, m["source"], f"{r['name']}.disk.{k}", v, r.get("page_disk", r["page_perf"])

    def verify_pdfs(self, pdf_dir: str) -> List[dict]:
        results = []
        texts: Dict[Tuple[str, int], str] = {}
        for model, src_id, desc, value, page in self.figure_locations():
            src = self.data["sources"][src_id]
            pdf = Path(pdf_dir) / src["file"]
            if not pdf.exists():
                results.append({"model": model, "figure": desc, "value": value, "page": page, "status": "PDF_AUSENTE"})
                continue
            key = (str(pdf), page)
            if key not in texts:
                texts[key] = _page_text(pdf, page)
            norm = re.sub(r"\s+", "", texts[key])
            found = any(v in norm for v in _value_variants(value))
            results.append({"model": model, "figure": desc, "value": value, "page": page,
                            "status": "OK" if found else "NO_ENCONTRADO"})
        return results

    def verify_pdf_hashes(self, pdf_dir: str) -> List[dict]:
        import hashlib
        out = []
        for sid, src in self.data["sources"].items():
            p = Path(pdf_dir) / src["file"]
            if not p.exists():
                out.append({"source": sid, "file": src["file"], "status": "PDF_AUSENTE"})
                continue
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            out.append({"source": sid, "file": src["file"], "status": "OK" if h == src["sha256"] else "SHA256_DISTINTO"})
        return out


def _value_variants(v) -> List[str]:
    out = set()
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        i = int(v)
        out.add(f"{i:,}")
        out.add(str(i))
    else:
        out.add(str(v))
    return [o for o in out]


def _page_text(pdf: Path, page: int) -> str:
    if shutil.which("pdftotext"):
        p = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
    try:
        from pypdf import PdfReader  # opcional
        return PdfReader(str(pdf)).pages[page - 1].extract_text() or ""
    except Exception:  # pragma: no cover
        return ""


def normalize_model_name(s: str) -> str:
    s = s.strip().upper().replace(" ", "")
    s = re.sub(r"^EXADATA", "", s)
    if s in ("X10M-2",):
        return "X10M"
    if s in ("X11M-2",):
        return "X11M"
    return s


def load(path: Optional[str] = None) -> Catalog:
    p = Path(path) if path else CATALOG_PATH
    with open(p, encoding="utf-8") as fh:
        cat = Catalog(json.load(fh), p)
    errs = cat.validate()
    if errs:
        raise ValueError("Catálogo inválido: " + "; ".join(errs))
    return cat


def detect_from_make_model(make_model: Optional[str]) -> dict:
    """Pistas de modelo/tipo a partir de ``makeModel``; nunca decide por sí sola."""
    if not make_model:
        return {"candidates": [], "storage_type": None, "note": "makeModel no disponible"}
    t = make_model.upper()
    stype = None
    if "EXTREME FLASH" in t:
        stype = "EF"
    elif "HIGH CAPACITY" in t:
        stype = "HC"
    elif "EXTENDED" in t:
        stype = "XT"
    m = re.search(r"X(\d+)(M?)-(\d)", t)
    cands = []
    note = ""
    if m:
        gen = int(m.group(1))
        if gen == 8 and not m.group(2):
            cands = ["X8-2", "X8M-2"]
            note = "El hardware de celda X8-2L se usa en X8 y X8M: el modelo debe confirmarse (¿PMEM/RoCE?)."
        elif gen in (6, 7):
            cands = [f"X{gen}-2"]
        elif gen in (9,):
            cands = ["X9M-2"]
        elif gen in (10, 11):
            cands = [f"X{gen}M"]
        else:
            cands = [f"X{gen}{m.group(2)}-{m.group(3)}"]
            note = "Modelo no presente en el catálogo."
    return {"candidates": cands, "storage_type": stype, "note": note}
