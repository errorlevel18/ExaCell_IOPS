# Limitaciones conocidas

## Validación realizada y no realizada

- **No se ha ejecutado nada contra un Exadata real.** El recolector se ha probado únicamente con
  el emulador de `tests/fakes` (`dcli`/`ssh` falsos que interpretan un subconjunto de CellCLI).
- La sintaxis `collectionTime NOT < '...' AND collectionTime < '...'` y el filtro `name = '...'`
  reproducen lo observado por el usuario en su entorno; **no se afirma que funcione en todas las
  versiones de CellCLI**. El emulador rechaza `>=` y `<=` para reproducir esa restricción.
- Formato de salida de CellCLI asumido: columnas separadas por espacios/tabuladores, `metricValue`
  como «número unidad» con `,` de miles y `.` decimal, `collectionTime` ISO-8601 con zona. Si una
  versión real difiere, la importación lo marca como líneas no interpretables (nunca como cero).
- No se ha probado con captura real del cliente: no se ha proporcionado ninguna.

## Métricas

- Los nombres de la lista por defecto son nombres CellCLI conocidos, pero su disponibilidad
  depende de la versión. El recolector solo pide los que existen en el catálogo de cada celda y
  registra los ausentes. En particular, las métricas de red `N_HCA_MB_*` podrían no existir con
  esos nombres en algunas versiones (p. ej. en RoCE); en ese caso la red queda «no evaluada» sin
  bloquear el dictamen.
- No se recogen métricas de PMEM/XRMEM. En un **origen X8M o posterior** las lecturas servidas
  desde PMEM/XRMEM no aparecen en los celldisks flash: la demanda de lectura queda infraestimada.
  El motor lo bloquea si se declara ese origen, salvo aceptación explícita
  (`source_memory_tier_reads_unobserved`).
- CellCLI clasifica como «pequeñas» las E/S de hasta 128 KB; los datasheets publican IOPS a 8 KB.
  Si el tamaño medio es mayor, la capacidad real en IOPS es menor que la referencia.
- La relación de aciertos de Flash Cache del origen no es trasladable a un destino con otra
  capacidad de flash/memoria; los minutos de CPU de celda no se escalan entre generaciones.
- La unidad «MB» de CellCLI se asume 2^20 bytes; los GB/s de datasheet, 10^9 bytes (conversión
  conservadora).

## Referencias y modelo

- Las cifras de datasheet son máximos comerciales; HDD por celda es derivado (escalado lineal).
  Nada de esto es capacidad segura bajo una latencia objetivo: solo la capacidad **probada**
  permite la aprobación operativa.
- Las IOPS flash mixtas se modelan con compensación lineal `R/Rmax + W/Wmax`; el comportamiento
  real puede ser peor (o mejor) que esa recta.
- Se supone reparto uniforme de la carga entre celdas (ASM). `apply_observed_skew` permite aplicar
  el desequilibrio observado del origen, que puede no repetirse en el destino.
- No se modelan: capacidad (TB), redundancia ASM Normal/High en espacio, compresión, Smart Scan
  offload de CPU, límites de RAM/CPU de servidores de BD salvo IOPS, ni multirack más allá del aviso.
- Límites por rack y mínimos se toman de las notas de los datasheets; las configuraciones
  elásticas reales pueden tener restricciones adicionales (espacio, potencia, licencias).

## Cobertura temporal

- Una captura de 24 h no garantiza incluir picos semanales, cierres de mes, backups, cargas
  masivas o estadísticas. El informe lo advierte siempre y la aprobación operativa exige
  confirmar los periodos críticos (`critical_periods_confirmed`).
- El histórico de métricas de las celdas tiene retención limitada (`metricHistoryDays`, que se
  registra en el inventario); periodos más antiguos pueden devolver bloques vacíos.

## Rendimiento de la herramienta

- Importación ≈ 150.000 líneas/s en un PC moderno (≈ 1 min por 14 celdas × 24 h). `series.csv`
  puede ocupar decenas de MB por captura.

## Recogida por SSH desde la aplicación

- Probada únicamente contra un **dbnode simulado** (`tests/fakes_dbnode/ssh_dbnode`, que ejecuta en
  local el recolector real con celdas simuladas). No se ha probado contra un dbnode real.
- La autenticación por contraseña usa `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` (OpenSSH ≥ 8.4).
  En clientes OpenSSH antiguos o en algunas versiones de Windows puede no funcionar: use clave.
- La comprobación de que el PID sigue siendo el recolector usa `/proc/<pid>/cmdline` (Linux, como
  los dbnodes Exadata).
- Si se pierde la conexión durante la descarga, basta con repetir «Descargar e importar»: la
  captura sigue en el dbnode. La aplicación no borra nada en el dbnode (script, log y captura
  quedan en el directorio indicado para su revisión o limpieza manual).
