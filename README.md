# ExaCell Sizer

Evalúa, **antes de comprar o dimensionar un Exadata destino**, cuántas celdas de almacenamiento
podrían ser necesarias para soportar la carga real medida en un Exadata origen, comparándola con
las referencias publicadas en los datasheets de Oracle (X6-2 … X11M). Da una respuesta
comprensible, trazable y prudente:

- **Compatible bajo las referencias y supuestos indicados**
- **No compatible con los límites evaluados**
- **Datos insuficientes para concluir**

…y separa siempre el **cálculo teórico**, el **margen estimado** y la **aprobación operativa**
(que queda «pendiente» salvo capacidad probada a la latencia objetivo).

> Las cifras de datasheet son máximos comerciales. La herramienta nunca afirma que una
> configuración funcionará solo porque el cálculo sea favorable.

## Flujo de trabajo

```
 dbnode del ORIGEN (cliente)                  PC del analista
 ───────────────────────────                  ─────────────────────────────────────────────
 exacell_collect.sh  ──► carpeta de captura ──► import ──► SQLite ──► size ──► informes
 (dcli + ssh + cellcli,     + SHA256SUMS          (valida)   (análisis)  (motor)   MD/CSV/HTML/JSON
  solo lectura)
```

1. **Mostrar/entregar el recolector al cliente** (no se ejecuta nada contra Exadata desde el PC):
   ```bash
   python -m exacell_sizer collector show
   ```
   ```bash
   python -m exacell_sizer collector export --dest ./para_cliente
   ```
2. **El cliente lo ejecuta en un dbnode** (24 h o más; ver [docs/RECOLECTOR.md](docs/RECOLECTOR.md)):
   ```bash
   ./exacell_collect.sh -d /home/oracle/capturas -g ~/cell_group -u celladmin -s 2026-11-03T00:00:00+01:00 -e 2026-11-04T00:00:00+01:00
   ```
   y entrega la carpeta `exacell_capture_*` (o un `.tar.gz`) sin modificarla.
3. **Importar en el PC** (valida integridad, bloques, huecos, duplicados, unidades…):
   ```bash
   python -m exacell_sizer import exacell_capture_dbnode01_....tar.gz --db proyecto.db --label origen --workdir ./extraido
   ```
   Si existe un destino con carga propia, importe también su captura con `--role destino`.
4. **Informe de una captura**:
   ```bash
   python -m exacell_sizer report --db proyecto.db --capture origen --model X7-2 --storage-type HC --out ./informe_origen
   ```
5. **Dimensionamiento y comparación origen/destino** con un escenario
   ([examples/escenario_ejemplo.json](examples/escenario_ejemplo.json)):
   ```bash
   python -m exacell_sizer size --db proyecto.db --source origen --dest destino --scenario escenario.json --out ./informe_dimensionamiento
   ```
   Genera `informe.md`, `informe.html` (autocontenido, sin Internet), `informe.json`, los CSV
   (`LEEME_CSV.txt` documenta la configuración regional) y `GUIA_INTERPRETACION.md`.

Demostración completa sin ningún Exadata (datos **simulados**):
```bash
python -m exacell_sizer simulate --out ./demo --name origen --cells 3 --load-factor 2 --start 2026-11-03T00:00:00+01:00 --end 2026-11-04T00:00:00+01:00
```

## Instalación

Python ≥ 3.9 y **solo biblioteca estándar** (sqlite3, decimal, csv, json). Opcional: `pytest` para
las pruebas y `pdftotext`/`pypdf` para verificar el catálogo contra los PDF.
```bash
pip install -e .[test]
```
Sin instalar: `PYTHONPATH=src python -m exacell_sizer ...`

## Documentación

| Documento | Contenido |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | Fases, validación y estado |
| [docs/RECOLECTOR.md](docs/RECOLECTOR.md) | Uso, garantías, formato de captura, códigos de salida |
| [docs/CATALOGO.md](docs/CATALOGO.md) | Catálogo de modelos, evidencias, derivaciones, aproximaciones, verificación |
| [docs/METODOLOGIA.md](docs/METODOLOGIA.md) | Medidas, cobertura, combinación, fórmulas del motor, dictamen |
| [src/exacell_sizer/resources/GUIA_INTERPRETACION.md](src/exacell_sizer/resources/GUIA_INTERPRETACION.md) | Cómo leer cada sección del informe y qué decisión permite |
| [docs/LIMITACIONES.md](docs/LIMITACIONES.md) | Lo que NO está validado y riesgos conocidos |

## Pruebas

```bash
python -m pytest -q
```
Con los datasheets en una carpeta local se verifica además que cada cifra del catálogo aparece
en la página citada:
```bash
EXACELL_DATASHEETS=/ruta/a/datasheets python -m pytest -q tests/test_catalog.py tests/test_e2e.py
```
Las pruebas del recolector ejecutan el **script Bash real** con `dcli`/`ssh` simulados
(`tests/fakes`), que emulan CellCLI e inyectan fallos. Nunca se contacta un sistema real.
