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
