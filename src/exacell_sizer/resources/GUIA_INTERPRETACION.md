# Guía de interpretación de los informes ExaCell Sizer

Esta guía explica cada sección del informe, qué significa y **qué decisión permite tomar**
(y cuál no). Se copia junto a cada informe generado.

## Qué responde el informe y qué no

Responde: *con la carga medida en el origen (y, si existe, la del destino actual), y bajo las
referencias publicadas por Oracle y los supuestos del escenario, ¿cuántas celdas de cada
configuración candidata harían falta y cuál sería el recurso limitante?*

No responde: *¿funcionará la migración?* Eso solo lo confirma una prueba con la carga real en
el destino a la latencia objetivo. Por eso el informe separa **dictamen teórico** y
**aprobación operativa**.

## Tipos de evidencia

| Etiqueta | Significado | Cómo usarla |
|---|---|---|
| medido_origen | Valores CellCLI de la captura, solo minutos completos | Es la demanda; su calidad depende de la cobertura |
| publicado | Máximo comercial del datasheet (fuente y página) | Techo optimista; nunca capacidad garantizada |
| derivado | Cálculo a partir de una cifra publicada (fórmula indicada) | Igual que «publicado», más la hipótesis de la fórmula (p. ej. escalado lineal) |
| hipotesis | Crecimiento, reserva, reparto uniforme, compensación lineal lectura/escritura | Revisar con el cliente |
| probado | Capacidad medida en pruebas aportadas por el usuario, con su latencia | Única evidencia que permite la aprobación operativa |

## Secciones

### 1. Dictamen
Una fila por configuración candidata.
- **Compatible bajo las referencias y supuestos indicados**: la demanda de diseño cabe en
  todos los recursos evaluados sin superar la utilización admitida (100 % − reserva), y se
  respetan los límites de configuración (mínimo de celdas, máximo por rack, N-1 si se pidió,
  servidores de BD si se indicaron). **Decisión que permite:** seguir con esa configuración
  como candidata a validar con pruebas. **No permite:** comprar sin pruebas si la aprobación
  operativa está «pendiente».
- **No compatible con los límites evaluados**: al menos un recurso o límite se supera.
  **Decisión:** descartar esa configuración o aumentar celdas/cambiar modelo; el recurso
  limitante indica qué falta.
- **Datos insuficientes para concluir**: falta información esencial (captura corta o
  incompleta, cobertura baja, modelo/tipo desconocido, referencia no comparable sin
  aproximación autorizada...). Los números de celdas, si aparecen, son **cota inferior
  parcial** y no se muestra margen. **Decisión:** obtener el dato que falta (ver motivos).

Columnas de celdas:
- **Teórico**: `ceil(demanda / capacidad_por_celda)` del recurso más exigente, sin crecimiento
  ni reserva. Es el mínimo absoluto bajo las referencias.
- **Recomendado**: `ceil(demanda × (1 + crecimiento) / (capacidad × (1 − reserva)))`, y nunca
  menos que el mínimo de configuración.
- **N-1**: `ceil(demanda_diseño × (1 + recuperación) / (capacidad × (1 − reserva_en_fallo))) + 1`:
  con una celda caída, las restantes absorben la carga y la reconstrucción.
- **Recurso limitante y margen**: el recurso con mayor utilización en las celdas evaluadas;
  `margen = 1 − demanda_diseño / (celdas × capacidad_por_celda)`. Un margen menor que la
  reserva significa que no se respeta la reserva.
- **Aprobación operativa**: «pendiente» salvo que todos los recursos usen capacidad
  **probada** a la latencia objetivo, la captura dure ≥ 24 h y los periodos críticos estén
  confirmados.

### 2. Escenario
Parámetros usados (crecimiento, reserva, estadística de demanda, combinación, N-1,
aproximaciones autorizadas, capacidades probadas). Cualquier cambio aquí cambia el
resultado: revíselo antes de leer los números.

### 3. Capturas y cobertura
Periodo, celdas, estado del recolector, integridad SHA-256 y estado de importación.
- **Minutos completos**: minutos en los que todos los objetos esperados de la celda (p. ej.
  todos sus discos flash) tienen dato válido. Los incompletos no se cuentan como cero.
- **Cobertura** = minutos completos / minutos del periodo. Por debajo del mínimo del
  escenario (95 % por defecto) el recurso es «no evaluable».
- **Avisos de calendario**: una captura de 24 h no incluye picos semanales, cierres de mes ni
  backups salvo que coincidan; confirme con el cliente. Una captura de minutos nunca sirve
  para aprobar una migración.
- **Incidencias**: errores (bloques fallidos, vacíos, interrumpidos, sumas distintas) y avisos
  (unidades, valores no numéricos, duplicados) con archivo y línea de procedencia.

### 4/5. Carga observada global y por celda
- Totales de **sistema**: suma de celdas en minutos completos de todas las celdas.
- **Máximo observado**: pico real del sistema; p99/p95 ayudan a ver si el pico es aislado.
- **Por celda**: detecta desequilibrios (una celda mucho más cargada que el resto). El
  dimensionamiento supone reparto uniforme; si el desequilibrio es grande, use
  `apply_observed_skew` en el escenario.
- **Porcentajes (CPU, utilización) y latencias** se muestran por celda y como máximo entre
  celdas; **nunca se suman**. La CPU de celda no es trasladable entre generaciones.
- **Flash Cache** es informativa: representa en parte las mismas lecturas que los discos
  flash; no se suma con ellas.

### Carga combinada estimada (destino)
- **Máximo observado** de cada sistema por separado.
- **Suma coincidente**: máximo de la suma minuto a minuto en instantes simultáneos (requiere
  periodos que se solapen en UTC; con «hora del día» es una aproximación).
- **Envolvente conservadora**: suma de máximos aunque ocurran en momentos distintos. Es el
  método por defecto del dimensionamiento.
- **Por celda del destino**: carga combinada / número de celdas evaluadas (reparto uniforme).

### Gráficos
- Línea continua: demanda medida (sin crecimiento). Si la serie es larga, cada punto es el
  **máximo** de su intervalo (se indica), para no ocultar picos.
- Línea **discontinua**: «máximo publicado/teórico» (o «capacidad probada») del número de
  celdas evaluado de cada candidato. **No es capacidad garantizada.** La evaluación aplica
  además crecimiento y reserva.

### Detalle por candidato
Para cada recurso: demanda, demanda de diseño, capacidad por celda con evidencia, fuente y
página, número de celdas por recurso, utilización, margen con su fórmula y demanda por celda.
- **IOPS flash mixtas**: lectura y escritura se combinan como `R/Rmax + W/Wmax` minuto a
  minuto: los máximos publicados de lectura y escritura no son simultáneos.
- **Red**: velocidad nominal de los puertos; solo sirve para descartar.
- **Servidores de BD**: límite fuera de las celdas; si se supera, añadir celdas no lo resuelve.
- **Pendiente para la aprobación operativa**: lista exacta de lo que falta medir o confirmar.

## Comparabilidad: lo que el informe nunca mezcla
- Lecturas 8K publicadas sobre flash (X6/X7), PMEM (X8M/X9M) o XRMEM (X10M/X11M): las dos
  últimas solo se usan como referencia de lectura flash con la aproximación autorizada
  `read_iops_memory_tier_as_flash`.
- Escrituras flash publicadas tras mirroring ASM (comparables con las escrituras observadas en
  las celdas).
- IOPS HDD (publicadas solo por rack: valor por celda derivado).
- Escaneo SQL (lectura sin compresión) frente a throughput mixto.
- Rendimiento por servidor frente a agregado de rack (a menudo limitado por los servidores de BD).
- Máximo comercial frente a capacidad segura bajo latencia objetivo.
