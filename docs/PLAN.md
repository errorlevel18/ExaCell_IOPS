# Plan del proyecto ExaCell Sizer

Objetivo: estimar, **antes de comprar o dimensionar** un Exadata destino, cuántas celdas de
almacenamiento podrían ser necesarias para soportar la carga real medida en un Exadata origen,
con una respuesta comprensible, trazable y prudente.

## Principios no negociables

1. **Separación de evidencias.** Cada cifra se etiqueta como una de:
   `publicado` (datasheet Oracle, con fuente y página), `derivado` (calculado a partir de una cifra
   publicada con una hipótesis explícita, p. ej. rack/nº celdas), `medido_origen` (captura CellCLI),
   `hipotesis` (crecimiento, reserva, escalado lineal, reparto uniforme) y `probado` (capacidad
   confirmada mediante pruebas aportadas por el usuario).
2. **Nunca convertir lo desconocido en cero.** Bloques vacíos, errores CellCLI, conexiones cortadas,
   métricas sin definición, valores no numéricos o unidades desconocidas se conservan y se advierten.
3. **No equiparar automáticamente** lecturas 8K de flash/PMEM/XRMEM, escrituras flash tras mirroring,
   IOPS HDD, throughput de escaneo vs mixto, servidor individual vs rack, máximo comercial vs
   capacidad segura bajo latencia.
4. **Dictamen de tres estados**: «Compatible bajo las referencias y supuestos indicados»,
   «No compatible con los límites evaluados», «Datos insuficientes para concluir».
   La aprobación operativa es independiente y queda «pendiente» salvo capacidad probada.
5. **Capturas originales inmutables**: la importación solo lee; verifica sumas SHA-256.

## Fases

| Fase | Contenido | Validación |
|------|-----------|-----------|
| 0 | Esqueleto, CLI, plan | `python -m exacell_sizer --help` |
| 1 | Recolector Bash (`exacell_collect.sh`) + simulador `dcli`/`ssh`/`cellcli` con inyección de fallos | pytest: N celdas, prevalidación, error CellCLI, red, bloque interrumpido, sintaxis `NOT <`, ninguna escritura en celdas |
| 2 | Importación: manifiesto, bloques, estados, huecos, duplicados, límites horarios, UTC, `Decimal`, definiciones por celda, SQLite | pytest unitarias de parser e importador |
| 3 | Catálogo versionado de datasheets X6-2…X11M (fuente, edición, página, condiciones) + aproximaciones autorizadas | pytest de integridad del catálogo |
| 4 | Análisis: medidas por minuto/celda/sistema, minutos completos, cobertura, tasas de contadores con reinicios y huecos, combinación origen/destino | pytest |
| 5 | Motor de dimensionamiento: mínimo, recomendado, N-1, recurso limitante, margen con fórmula, límites de rack/red/BD, dictamen | pytest «sí», «no», «datos insuficientes» |
| 6 | Informes Markdown, CSV (Excel), HTML autocontenido con SVG, JSON; guía de interpretación | pytest de coherencia SQLite/JSON/CSV/MD/HTML |
| 7 | E2E (recolector real + simulador → importación → dimensionamiento → informes), limitaciones | pytest e2e |

El estado de cada fase y las limitaciones conocidas están en `docs/LIMITACIONES.md` y en el README.

## Estado (2026-11-10)

| Fase | Estado | Pruebas |
|------|--------|---------|
| 0 Esqueleto y CLI | Completada | `--help`, `collector show/info/export` |
| 1 Recolector + simulador | Completada | `tests/test_collector.py` (27): 1/3/7 celdas, prevalidación dcli y SSH, CellCLI, red, interrumpido, vacío, celda perdida a mitad, 24 bloques y límites horarios, sintaxis `NOT <`, solo `LIST`, sin escrituras en celdas, root/celladmin, errores de uso, `-n`, SHA-256, SIGTERM |
| 2 Importación | Completada | `tests/test_importer.py` (25): Decimal exacto, UTC, procedencia archivo/línea, inmutabilidad, reimportación, bloques fallidos ≠ cero, manipulación, huecos de plan, duplicados y límite horario, unidades/definiciones/no numéricos, `.tar.gz` y path traversal, equivalencia con la salida del script real |
| 3 Catálogo | Completada | `tests/test_catalog.py` (12): integridad, PMEM/XRMEM ≠ flash, mirroring, HDD derivado, X6 Eighth, EF/XT, conversión, modelos desconocidos y aproximaciones, 265/265 cifras en su página (con `EXACELL_DATASHEETS`) |
| 4 Análisis | Completada | `tests/test_analysis.py` (12): contadores con reinicio/hueco, minutos incompletos, suma = muestras, no aplicable/no disponible, no sumables, latencia ponderada, KB→MB, persistencia, combinación coincidente/envolvente/sin solape/hora del día, calendario |
| 5 Motor | Completada | `tests/test_sizing.py` (23): sí/no/insuficiente con cifras exactas, margen y cuello de botella, cobertura, captura incompleta y corta, aproximaciones, EF con HDD, N-1, multirack, red nominal, servidores de BD, combinación, crecimiento/percentil, aprobación operativa con capacidad probada, validación de escenario, reparto por celda, red ausente no bloquea, lecturas PMEM/XRMEM del origen |
| 6 Informes | Completada | `tests/test_reports.py` (7): SQLite = series.csv (es y c), JSON = agregados.csv = MD = HTML, dimensionamiento CSV/JSON/MD/HTML, HTML autocontenido y etiquetas de referencia, guía y LEEME |
| 7 E2E y documentación | Completada | `tests/test_e2e.py` (5): recolector Bash real → CLI import → size → informes (completa y con bloque fallido), distintos números de celdas origen/destino, CLI de catálogo y recolector |

Limitaciones: ver `docs/LIMITACIONES.md`. No se ha usado ninguna captura real (no se proporcionó).
