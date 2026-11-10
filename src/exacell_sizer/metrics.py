"""Definición de medidas lógicas a partir de métricas CellCLI.

Cada medida declara explícitamente:
  * qué métricas CellCLI la componen (nombres exactos, nunca prefijos),
  * el tipo de objeto y, para CELLDISK, el tipo de disco (FlashDisk/HardDisk) según el
    inventario ``LIST CELLDISK`` de cada celda (no el prefijo del nombre),
  * cómo se combinan los objetos de una celda (suma, media ponderada, máximo),
  * si la medida puede sumarse entre celdas (IOPS y MB/s sí; porcentajes y latencias NO),
  * el recurso del motor de dimensionamiento al que alimenta (o ninguno).

Reglas para no sumar la misma operación en capas distintas: las medidas de flashcache (FC_*)
son informativas y nunca se suman con las de celldisk flash; las medidas de red son
independientes de las de disco.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Measure:
    id: str
    label: str
    unit: str                          # unidad de presentación
    object_type: str                   # CELLDISK | CELL | FLASHCACHE
    components: tuple                  # métricas Rate/Instantaneous que se suman por objeto
    expected_unit: str                 # unidad CellCLI esperada en la definición
    expected_type: str                 # Rate | Instantaneous
    disk_type: Optional[str] = None    # FlashDisk | HardDisk
    object_agg: str = "sum"            # sum | weighted_mean | mean | max_of_components
    weight_components: tuple = ()      # para weighted_mean (peticiones por segundo)
    summable_across_cells: bool = True
    counter_fallback: tuple = ()       # métricas Cumulative equivalentes (mismo orden que components)
    resource: Optional[str] = None     # recurso del motor de dimensionamiento
    group: str = ""
    note: str = ""


MEASURES: List[Measure] = [
    # ---------------- flash (celldisks FlashDisk)
    Measure("flash_read_iops_small", "Flash: lecturas pequeñas (<=128 KB)", "IO/s", "CELLDISK",
            ("CD_IO_RQ_R_SM_SEC",), "IO/sec", "Rate", "FlashDisk", counter_fallback=("CD_IO_RQ_R_SM",),
            resource="flash_read_iops", group="flash"),
    Measure("flash_write_iops_small", "Flash: escrituras pequeñas (<=128 KB), tras mirroring", "IO/s", "CELLDISK",
            ("CD_IO_RQ_W_SM_SEC",), "IO/sec", "Rate", "FlashDisk", counter_fallback=("CD_IO_RQ_W_SM",),
            resource="flash_write_iops", group="flash"),
    Measure("flash_read_iops_large", "Flash: lecturas grandes (>128 KB)", "IO/s", "CELLDISK",
            ("CD_IO_RQ_R_LG_SEC",), "IO/sec", "Rate", "FlashDisk", counter_fallback=("CD_IO_RQ_R_LG",), group="flash"),
    Measure("flash_write_iops_large", "Flash: escrituras grandes (>128 KB)", "IO/s", "CELLDISK",
            ("CD_IO_RQ_W_LG_SEC",), "IO/sec", "Rate", "FlashDisk", counter_fallback=("CD_IO_RQ_W_LG",), group="flash"),
    Measure("flash_read_mbps", "Flash: MB/s leídos", "MB/s", "CELLDISK",
            ("CD_IO_BY_R_SM_SEC", "CD_IO_BY_R_LG_SEC"), "MB/sec", "Rate", "FlashDisk",
            resource="flash_read_mbps", group="flash"),
    Measure("flash_write_mbps", "Flash: MB/s escritos", "MB/s", "CELLDISK",
            ("CD_IO_BY_W_SM_SEC", "CD_IO_BY_W_LG_SEC"), "MB/sec", "Rate", "FlashDisk", group="flash",
            note="Sin cifra publicada comparable: no se usa para dimensionar."),
    # ---------------- HDD (celldisks HardDisk)
    Measure("hdd_read_iops_small", "HDD: lecturas pequeñas", "IO/s", "CELLDISK",
            ("CD_IO_RQ_R_SM_SEC",), "IO/sec", "Rate", "HardDisk", counter_fallback=("CD_IO_RQ_R_SM",), group="hdd"),
    Measure("hdd_write_iops_small", "HDD: escrituras pequeñas", "IO/s", "CELLDISK",
            ("CD_IO_RQ_W_SM_SEC",), "IO/sec", "Rate", "HardDisk", counter_fallback=("CD_IO_RQ_W_SM",), group="hdd"),
    Measure("hdd_iops_small", "HDD: E/S pequeñas (lectura+escritura)", "IO/s", "CELLDISK",
            ("CD_IO_RQ_R_SM_SEC", "CD_IO_RQ_W_SM_SEC"), "IO/sec", "Rate", "HardDisk",
            counter_fallback=("CD_IO_RQ_R_SM", "CD_IO_RQ_W_SM"), resource="hdd_iops", group="hdd"),
    Measure("hdd_read_mbps", "HDD: MB/s leídos", "MB/s", "CELLDISK",
            ("CD_IO_BY_R_SM_SEC", "CD_IO_BY_R_LG_SEC"), "MB/sec", "Rate", "HardDisk", group="hdd"),
    Measure("hdd_write_mbps", "HDD: MB/s escritos", "MB/s", "CELLDISK",
            ("CD_IO_BY_W_SM_SEC", "CD_IO_BY_W_LG_SEC"), "MB/sec", "Rate", "HardDisk", group="hdd"),
    Measure("hdd_mbps", "HDD: MB/s (lectura+escritura)", "MB/s", "CELLDISK",
            ("CD_IO_BY_R_SM_SEC", "CD_IO_BY_R_LG_SEC", "CD_IO_BY_W_SM_SEC", "CD_IO_BY_W_LG_SEC"), "MB/sec", "Rate",
            "HardDisk", resource="hdd_mbps", group="hdd"),
    # ---------------- utilización y latencias (NO sumables)
    Measure("hdd_util_pct", "HDD: utilización media de discos", "%", "CELLDISK", ("CD_IO_UTIL",), "%", "Rate",
            "HardDisk", object_agg="mean", summable_across_cells=False, group="util"),
    Measure("flash_util_pct", "Flash: utilización media de discos", "%", "CELLDISK", ("CD_IO_UTIL",), "%", "Rate",
            "FlashDisk", object_agg="mean", summable_across_cells=False, group="util"),
    Measure("hdd_read_latency_us", "HDD: latencia lectura pequeña (ponderada)", "us", "CELLDISK",
            ("CD_IO_TM_R_SM_RQ",), "us/request", "Rate", "HardDisk", object_agg="weighted_mean",
            weight_components=("CD_IO_RQ_R_SM_SEC",), summable_across_cells=False, group="latency"),
    Measure("flash_read_latency_us", "Flash: latencia lectura pequeña (ponderada)", "us", "CELLDISK",
            ("CD_IO_TM_R_SM_RQ",), "us/request", "Rate", "FlashDisk", object_agg="weighted_mean",
            weight_components=("CD_IO_RQ_R_SM_SEC",), summable_across_cells=False, group="latency"),
    Measure("flash_write_latency_us", "Flash: latencia escritura pequeña (ponderada)", "us", "CELLDISK",
            ("CD_IO_TM_W_SM_RQ",), "us/request", "Rate", "FlashDisk", object_agg="weighted_mean",
            weight_components=("CD_IO_RQ_W_SM_SEC",), summable_across_cells=False, group="latency"),
    Measure("cell_cpu_pct", "CPU de celda", "%", "CELL", ("CL_CPUT",), "%", "Instantaneous",
            summable_across_cells=False, group="cpu",
            note="No escalable entre generaciones: informativo."),
    # ---------------- red
    Measure("network_mbps", "Red de almacenamiento: MB/s (máx. de recepción y transmisión)", "MB/s", "CELL",
            ("N_HCA_MB_RCV_SEC", "N_HCA_MB_TRANS_SEC"), "MB/sec", "Rate", object_agg="max_of_components",
            resource="network_mbps", group="network",
            note="Por celda y minuto se toma el mayor sentido; la suma entre celdas es conservadora."),
    # ---------------- informativas en otra capa (no sumar con celldisk flash)
    Measure("flashcache_read_iops", "Flash Cache: lecturas (capa caché, informativa)", "IO/s", "FLASHCACHE",
            ("FC_IO_RQ_R_SEC",), "IO/sec", "Rate", counter_fallback=("FC_IO_RQ_R",), group="info",
            note="Misma operación que parte de las lecturas flash de celldisk: no sumar."),
]
MEASURE_BY_ID: Dict[str, Measure] = {m.id: m for m in MEASURES}

# Conversiones de unidad explícitas (de -> a): factor multiplicativo
UNIT_CONVERSIONS = {("KB/sec", "MB/sec"): 1 / 1024}
