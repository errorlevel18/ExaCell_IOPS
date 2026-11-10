# Recolector `exacell_collect.sh`

Script Bash para un **dbnode Linux** del Exadata origen. Se visualiza con
`python -m exacell_sizer collector show` y se exporta con `collector export --dest DIR`.

## Parámetros

| Opción | Obligatoria | Descripción |
|---|---|---|
| `-d DIR` | sí | Directorio **existente** del dbnode donde se crea `exacell_capture_<host>_<UTC>_<pid>` |
| `-g CELL_GROUP` | sí | Una celda por línea; admite comentarios `#`; cualquier número de celdas; sin duplicados |
| `-u USUARIO` | sí | `root` o `celladmin` (cualquier otro se rechaza) |
| `-s INICIO` | sí | ISO-8601 con zona: `2026-11-03T00:00:00+01:00` o `...Z` (inclusivo) |
| `-e FIN` | sí | ISO-8601 con zona (exclusivo) |
| `-p N` | no | Celdas en paralelo (4 por defecto) |
| `-m ARCHIVO` | no | Lista alternativa `objectType nombre` (solo ámbitos CELL, CELLDISK, FLASHCACHE, FLASHLOG, PMEMCACHE) |
| `-n` | no | Simulación: muestra plan y comandos sin contactar celdas ni crear nada |

## Qué hace, en orden

1. Valida parámetros (sin contactar ninguna celda; código 1 si algo falla).
2. Crea la carpeta de captura **en el dbnode**.
3. **Prevalidación de todas las celdas** antes de recoger nada:
   - `dcli -g GRUPO -l USUARIO 'cellcli -e "LIST CELL ATTRIBUTES name, status"'` → cada celda `online`;
   - por SSH directo (la vía de recogida): `LIST CELL ATTRIBUTES name, status`, `LIST CELL DETAIL`,
     `LIST CELLDISK ATTRIBUTES name, diskType, size`,
     `LIST METRICDEFINITION ATTRIBUTES name, objectType, metricType, unit`;
   - **selección de métricas** cruzando la lista explícita `(objectType, nombre)` con el catálogo de
     **cada** celda (coincidencia exacta de ambos; nunca prefijos ni filtros amplios por objectType).
     Las ausentes se registran en `cells/<celda>/metrics_missing.tsv`.
   - Si cualquier celda falla → `PREVALIDATION_FAILED`, código 2, **no se recoge histórico**.
4. Recogida en **bloques horarios** (el último puede ser más corto). Por celda, bloque y métrica:
   ```
   ssh -n -T -o BatchMode=yes -o StrictHostKeyChecking=yes ... USUARIO@CELDA \
     "cellcli -e \"LIST METRICHISTORY WHERE name = 'CD_IO_RQ_R_SM_SEC' AND collectionTime NOT < '2026-11-03T13:00:00+01:00' AND collectionTime < '2026-11-03T14:00:00+01:00' ATTRIBUTES name, metricObjectName, collectionTime, metricValue\"" \
     > data/CELDA/b0013_CD_IO_RQ_R_SM_SEC.out 2> ...err      # redirección EN EL DBNODE
   ```
5. Clasifica cada bloque: `OK`, `EMPTY`, `CELLCLI_ERROR` (patrón `CELL-nnnnn`), `NETWORK_ERROR`
   (rc 255 sin salida), `INTERRUPTED` (rc 255 o >128 con salida parcial), `COMMAND_ERROR`,
   `MALFORMED` (líneas de otra métrica). Solo `OK` cuenta como dato.
6. Consolida `blocks.tsv`, escribe `manifest.txt`, `summary.txt` y `SHA256SUMS`, e imprime el resumen.

## Garantías

- En las celdas solo se ejecuta `cellcli -e "LIST ..."`: **no** se crean carpetas, archivos,
  scripts ni temporales; **no** se copian scripts; **no** se cambia configuración, equivalencia SSH,
  claves (`BatchMode=yes`, `StrictHostKeyChecking=yes`: nunca añade claves a `known_hosts`), IORM,
  retención ni servicios; **no** se ejecutan pruebas de carga.
- Solo métricas generales de celda, discos, flash y red. Se rechazan ámbitos `IORM_*` y nombres
  `DB_*`, `PDB_*`, `CT_*`, `CG_*`.
- Un bloque vacío, error CellCLI o conexión interrumpida deja la captura `INCOMPLETE` (código 3),
  nunca como carga cero.
- Se interrumpe limpiamente con `SIGINT`/`SIGTERM` (estado `INTERRUPTED`, código 130).

## Sintaxis de CellCLI (evidencia y límites)

En el entorno de referencia del usuario `>=` no se acepta y el inicio inclusivo debe escribirse
`collectionTime NOT < 'inicio' AND collectionTime < 'fin'`; además hubo que filtrar por
`name = '...'` explícito para obtener histórico antiguo. El script usa exactamente esa forma.
**Validación realizada:** solo con el emulador de `tests/fakes` (que reproduce esas reglas). No se
ha probado contra ninguna versión real de CellCLI desde este proyecto; ver
[LIMITACIONES.md](LIMITACIONES.md). Recomendación: ejecutar primero con `-n` y después una
captura corta de prueba (sin usarla para decidir) para confirmar la sintaxis en la versión del cliente.

## Formato de la captura

```
exacell_capture_<host>_<UTC>_<pid>/
  manifest.txt            clave=valor: versión, celdas, periodo (con zona y epoch), bloques, estado, exit_code
  cell_group.txt          copia del grupo usado
  metrics_requested.txt   lista solicitada
  blocks_plan.tsv         índice, inicio, fin de cada bloque horario
  blocks.tsv              una fila por (celda, métrica, bloque): estado, rc, archivos, líneas, bytes, horas
  prevalidation/          salida dcli y prevalidation.tsv
  cells/<celda>/          list_cell, cell_detail, celldisks, metricdefinition (.out/.err/.rc),
                          metrics_selected.tsv, metrics_missing.tsv, blocks.tsv
  data/<celda>/bNNNN_<METRICA>.out|.err
  summary.txt             resumen verificable
  SHA256SUMS              sumas de todos los archivos (verificar: sha256sum -c SHA256SUMS)
```

## Códigos de salida

| Código | Estado | Significado |
|---|---|---|
| 0 | COMPLETE | Todos los bloques OK |
| 1 | USAGE_ERROR | Parámetros inválidos; no se contactó ninguna celda |
| 2 | PREVALIDATION_FAILED | Alguna celda no superó la prevalidación; sin histórico |
| 3 | INCOMPLETE | Bloques con error, vacíos, interrumpidos o sin ejecutar |
| 4 | LOCAL_ERROR | Problema en el dbnode (directorio, herramientas) |
| 130 | INTERRUPTED | Señal recibida |

## Volumen orientativo

Por celda y día con la lista por defecto (30 métricas, 12 HDD + 4 flash): unas 450.000 líneas
(≈ 30-40 MB) y 720 invocaciones SSH (30 métricas × 24 bloques). Arranque de CellCLI ≈ 1-3 s por
invocación → ajuste `-p` según el número de celdas.
