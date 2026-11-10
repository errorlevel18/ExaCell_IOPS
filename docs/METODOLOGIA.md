# Metodología

## 1. Importación y normalización

- Verificación SHA-256 de todos los archivos; la captura original no se modifica (los
  `.tar.gz`/`.zip` se extraen de forma segura a otro directorio).
- Validación de manifiesto, celdas (manifiesto = `cell_group.txt`), plan de bloques (contiguo,
  sin huecos ni solapes, cubre el periodo), bloques esperados (`celdas × métricas seleccionadas ×
  bloques`) y su estado. Cada problema queda en la tabla `issue` con severidad, archivo y línea.
- Fechas a UTC (se exige zona horaria; nunca se adivina). Valores como `Decimal` exacto,
  guardados como texto en SQLite junto al valor bruto. Separador de miles `,` y decimal `.`.
- Unidades y tipo (`Rate`, `Instantaneous`, `Cumulative`) desde la definición de **cada celda**.
  Métricas sin definición, valores no numéricos y unidades distintas o desconocidas se conservan
  marcadas y se advierten; nunca se convierten en cero.
- Duplicados idénticos se descartan (INFO); duplicados con valor distinto se advierten y se
  conserva el primero. Muestras fuera de su bloque horario se advierten.
- Captura `INCOMPLETE` si hay cualquier ERROR (bloque fallido, no ejecutado, integridad...).

## 2. Medidas

| Medida | Componentes (por celda) | Objetos | Sumable entre celdas | Recurso |
|---|---|---|---|---|
| flash_read_iops_small | CD_IO_RQ_R_SM_SEC | celldisks FlashDisk | sí | flash_read_iops |
| flash_write_iops_small | CD_IO_RQ_W_SM_SEC | FlashDisk | sí | flash_write_iops |
| flash_read_mbps | CD_IO_BY_R_SM_SEC + CD_IO_BY_R_LG_SEC | FlashDisk | sí | flash_read_mbps |
| hdd_iops_small | CD_IO_RQ_R_SM_SEC + CD_IO_RQ_W_SM_SEC | HardDisk | sí | hdd_iops |
| hdd_mbps | CD_IO_BY_{R,W}_{SM,LG}_SEC | HardDisk | sí | hdd_mbps |
| network_mbps | máx(N_HCA_MB_RCV_SEC, N_HCA_MB_TRANS_SEC) | celda | sí | network_mbps |
| *_util_pct, cell_cpu_pct | CD_IO_UTIL, CL_CPUT | | **no** | — |
| *_latency_us | CD_IO_TM_*_RQ ponderada por peticiones | | **no** | — |
| flashcache_read_iops | FC_IO_RQ_R_SEC | | sí (informativa) | — (misma operación que flash celldisk) |

El tipo de disco sale del inventario `LIST CELLDISK` (`diskType`), no del nombre.

**Minuto completo**: todos los objetos esperados de la celda tienen muestra válida de todos los
componentes en ese minuto UTC. **Sistema**: suma en minutos completos de todas las celdas
aplicables. **Cobertura** = minutos completos / minutos del periodo.

**Contadores** (`Cumulative`, solo si falta la métrica de tasa): tasa = Δ/Δt entre muestras
consecutivas con `0 < Δt ≤ 120 s` y `Δ ≥ 0`; los reinicios (Δ<0), huecos y Δt inválidos se
registran en `counter_event` y no generan tasa.

## 3. Demanda

- Estadística: `max` (por defecto), `p99` o `p95` sobre minutos completos.
- Con destino actual: **envolvente** (por defecto) = estadística(origen) + estadística(destino),
  conservadora; **coincidente** = estadística de la suma minuto a minuto en el mismo instante UTC
  (o por hora del día, como aproximación explícita).
- Demanda de diseño = demanda × (1 + crecimiento).

## 4. Motor (por candidato y recurso)

Con `C` = capacidad por celda (publicada, derivada o **probada** si se aporta a la latencia
objetivo), `D` = demanda, `Dd` = demanda de diseño, `r` = reserva, `o` = sobrecarga de
recuperación, `rf` = reserva durante fallo:

```
teóricas     = ceil(D / C)
recomendadas = ceil(Dd / (C · (1 − r)))
N-1          = ceil(Dd · (1 + o) / (C · (1 − rf))) + 1
utilización  = Dd / (N · C)          margen = 1 − utilización
```

- **IOPS flash mixtas**: por minuto `u = R/Rmax + W/Wmax` (celdas-equivalentes); se aplica la
  estadística a `u`. Hipótesis de compensación lineal entre los máximos publicados (no
  simultáneos).
- Resultado del candidato: teórico = máx. por recurso; recomendado = máx(máx. por recurso,
  mínimo de configuración); N-1 = máx(N-1 por recurso, recomendado). Recurso limitante = mayor
  utilización con las celdas evaluadas.
- **Red**: techo nominal; puede producir «No compatible», nunca aprueba; si falta su demanda no bloquea.
- **Servidores de BD** (si se indican o vienen del `rack_config`): lecturas (flash+HDD) y
  escrituras frente a `n_bd × cifra por servidor`.
- **Límites**: mínimo de celdas, máximo por rack (multirack solo si se autoriza), tipo de celda
  publicado en el `rack_config`, homogeneidad (un tipo de celda por candidato).

## 5. Dictamen

1. **Datos insuficientes**: modelo/tipo desconocido; captura < `min_capture_hours` (24 h; menos
   solo en modo prueba explícito) o incompleta (salvo aceptación explícita); cobertura < mínimo;
   demanda no disponible; referencia no publicada o no comparable sin aproximación autorizada;
   origen X8M+ sin aceptar las lecturas PMEM/XRMEM no observadas; carga HDD hacia EF sin
   aproximación. No se muestra margen; las celdas son cota inferior parcial.
2. **No compatible**: celdas fijadas < recomendadas o < N-1, < mínimo de configuración, > máximo por
   rack, servidores de BD o techo de red superados.
3. **Compatible bajo las referencias y supuestos indicados**: en otro caso.

**Aprobación operativa**: «aprobada» solo si todos los recursos evaluados usan capacidad probada
a la latencia objetivo, la captura es ≥ 24 h y los periodos críticos están confirmados; si no,
«pendiente» con la lista de lo que falta.
