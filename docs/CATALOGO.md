# Catálogo de modelos Exadata

Archivo versionado: `src/exacell_sizer/catalog/exadata_catalog.json` (versión `1.0.0`).
Consulta: `python -m exacell_sizer catalog list` y `catalog show --model X10M`.

## Fuentes

| Modelo | Archivo | Edición | SHA-256 (prefijo) |
|---|---|---|---|
| X6-2 | exadata-x6-2-ds.pdf | PDF 2017-07-21 (© 2017) | 090ef0fd… |
| X7-2 | exadata-x7-2-ds.pdf | PDF modificado 2019-03-05 (© 2017) | 82ea84ba… |
| X8M-2 | exadata-x8m-2-ds.pdf | PDF modificado 2020-01-30 (© 2020) | 67ce4a43… |
| X9M-2 | exadata-x9m-2-ds.pdf | PDF modificado 2023-07-05 (© 2021) | 4faa4d49… |
| X10M | exadata-x10m-ds.pdf | Version 1.2 (© 2024) | f4ec6c72… |
| X11M | exadata-x11m-ds.pdf | Version 1.1 (© 2025) | 852aa528… |

Los PDF no se versionan en el repositorio (derechos de Oracle). `catalog verify --pdf-dir DIR`
comprueba su SHA-256 y que **cada una de las 265 cifras** aparece en la página citada
(verificado: 265/265).

No existe datasheet de X8-2 (no M), X5 ni anteriores: esos modelos devuelven «datos
insuficientes» salvo aproximación autorizada (X8-2 → X8M-2).

## Contenido por modelo

- Servidores: `DB`, `DB-Z` (X11M), celdas `HC`, `EF`, `XT` (X8M–X10M), `HC-EIGHTH`, `HC-Z` (X11M), con
  hardware (núcleos, discos, flash, memoria PMEM/XRMEM) y página.
- Cifras por servidor: `sql_flash_scan_bandwidth`, `sql_read_iops_8k`, `sql_flash_write_iops_8k`,
  `sql_rdma_memory_bandwidth` (X11M), `db_sql_read/write_iops_8k`.
- Configuraciones de rack publicadas (Eighth, Quarter, Half, Full, Elastic 1/2, Quarter con
  Server-Z) con nº de servidores BD/celdas y cifras flash y HDD de rack.
- Límites: mínimo de celdas, máximo por rack (18 en X6–X9M; 17 en X10M/X11M, 16 HC en X10M),
  red (2×40 Gb QDR InfiniBand en X6/X7; 2×100 Gb RoCE desde X8M), afirmaciones de latencia de
  marketing (no usadas como capacidad).

## Nivel de las lecturas 8K (no equiparable)

| Modelo | `Maximum SQL Read IOPS` se obtiene desde | Base |
|---|---|---|
| X6-2, X7-2 | flash | tabla de rack «Flash Read IOPS» |
| X8M-2, X9M-2 | PMEM | tabla de rack «PMEM Read IOPS» |
| X10M, X11M | XRMEM | tabla de rack «XRMEM Read IOPS» |

Para X8M+ la cifra solo se usa como referencia de lectura flash con la aproximación autorizada
`read_iops_memory_tier_as_flash` (aviso visible en el informe).

## Derivaciones (evidencia «derivado»)

| Qué | Fórmula | Motivo |
|---|---|---|
| IOPS y MB/s HDD por celda | `min(cifra_rack / nº_celdas)` sobre las configuraciones declaradas | HDD solo se publica por rack; supone escalado lineal y reparto uniforme |
| X6-2 Eighth HC: escaneo flash | `32 GB/s / 3` | sin cifra por servidor; IOPS NO derivadas (rack limitado por BD) |
| Red por celda | `puertos × Gb/s / 8` | techo físico nominal; solo descarta |
| GB/s → MB/s CellCLI | `× 10^9 / 2^20` (953,674) | conversión conservadora |

Las cifras de rack de lectura suelen estar limitadas por los **servidores de BD**
(p. ej. X9M Full: 22,4 M = 8 × 2,8 M; X10M Elastic 2: 5,6 M = 2 × 2,8 M): nunca se dividen entre
celdas; el motor comprueba aparte el límite de servidores de BD si se indican.

## Aproximaciones (requieren autorización explícita en el escenario)

| Id | Efecto |
|---|---|
| `X8-2_as_X8M-2` | Usa X8M-2 para un X8-2 (sobreestima: PMEM y RoCE) |
| `X9M-8_as_X9M-2`, `X8M-8_as_X8M-2` | Celdas equivalentes; límites de BD no evaluables |
| `read_iops_memory_tier_as_flash` | Lectura 8K PMEM/XRMEM como referencia de lectura flash |
| `hdd_load_to_flash` | Traslada la carga HDD del origen a flash en un destino EF |
| `source_memory_tier_reads_unobserved` | Acepta que en un origen X8M+ las lecturas desde PMEM/XRMEM no se observan |
| `hdd_iops_from_rack`, `small_io_as_8k` | Hipótesis siempre aplicadas y siempre advertidas (no bloquean) |

## Mantenimiento

Para añadir un datasheet: añadir la fuente (archivo, edición, páginas, SHA-256), el modelo con
página por cifra, ejecutar `catalog verify --pdf-dir` y las pruebas, e incrementar
`catalog_version`.
