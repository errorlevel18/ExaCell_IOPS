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

## Arrancar la aplicación web

```bash
pip install -e .
```
```bash
python -m exacell_sizer web
```

Se abre el navegador en `http://localhost:8765/` (solo accesible desde este PC; funciona sin
Internet). Los datos se guardan en la carpeta `exacell_workspace` (cámbiela con `--workspace`).
Opciones: `--port`, `--host`, `--no-browser`.

| Sección | Qué permite |
|---|---|
| **Panel** | Estado general, pasos guiados, últimas capturas y dictámenes |
| **Recolector** | Generar el comando para el dbnode (fechas, zona, usuario), ver y descargar el script |
| **Capturas** | Subir el `.tar.gz`/`.zip` del cliente (arrastrar y soltar) o generar una captura **simulada** de demostración; ver carga por medida y por celda, mapa de cobertura e incidencias con procedencia |
| **Dimensionamiento** | Elegir origen y destino actual, crecimiento, reserva, N-1, latencia objetivo, aproximaciones y candidatos; ver dictamen, utilización por recurso, demanda frente a referencia y descargar los informes |
| **Catálogo Exadata** | Cifras por modelo y tipo de celda con evidencia, fuente y página |
| **Guía** | Interpretación de informes, metodología, limitaciones y recolector |

### Recogida por SSH desde la aplicación

Si el PC tiene acceso SSH al dbnode, la sección **Recogida SSH** evita el ida y vuelta de archivos:

1. **Conexión** (host, usuario, agente/clave/contraseña) y **Probar conexión**: comprueba `dcli`,
   `sha256sum`, el directorio y el `cell_group` en el dbnode, y lista las celdas.
2. **Simulación (-n)** y **Iniciar recolección**: copia el recolector al directorio indicado del
   dbnode (verificado por SHA-256) y lo ejecuta en segundo plano con `nohup`; sigue aunque se
   cierre la aplicación.
3. **Progreso** por bloques, log, **Cancelar** (la captura queda INTERRUPTED) y **Descargar e
   importar** al terminar (tar por SSH, sin temporales en el dbnode).
4. **Traer una captura ya ejecutada** por el cliente: lista `exacell_capture_*` y la descarga.

La aplicación se conecta **solo al dbnode**; es el recolector quien consulta las celdas en solo
lectura. Usa el cliente OpenSSH del sistema (Windows 10/11, Linux, macOS) con su
`~/.ssh/config`, agente y `known_hosts`; una clave de host nueva solo se acepta si se marca
expresamente. Las contraseñas solo se guardan en memoria (requiere OpenSSH ≥ 8.4 para
`SSH_ASKPASS_REQUIRE`); se recomienda autenticación por clave.

Para probar sin datos reales: **Capturas → Captura simulada de demostración** (origen y otra con
rol «Destino actual»), y después **Dimensionamiento → Cargar ejemplo → Ejecutar**.

## Flujo de trabajo (línea de comandos)

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
