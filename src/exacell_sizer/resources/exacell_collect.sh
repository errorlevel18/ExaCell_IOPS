#!/bin/bash
# =============================================================================
# exacell_collect.sh - Recolector de histórico de métricas GLOBALES de celdas Exadata
#
# Se ejecuta en un dbnode Linux. Lee el histórico (LIST METRICHISTORY) de cada celda del
# archivo cell_group, en bloques horarios, y guarda TODO en el dbnode.
#
# Garantías de diseño (verificadas en pruebas simuladas, no en todas las versiones de CellCLI):
#   * En las celdas solo se ejecuta:  cellcli -e "LIST ..."   (solo lectura).
#   * No se crean carpetas, archivos, scripts ni temporales en las celdas: cada salida viaja
#     por SSH y se redirige en el dbnode.
#   * No se modifica configuración, equivalencia SSH, claves, known_hosts (BatchMode=yes,
#     StrictHostKeyChecking=yes), IORM, retención ni servicios. No se ejecutan pruebas de carga.
#   * Solo métricas generales de celda, discos, flash y red. Nunca DB/PDB/IORM.
#   * Los nombres de métrica salen del catálogo (LIST METRICDEFINITION) de CADA celda y se
#     filtran por pareja exacta (objectType, name). No se usan prefijos ni filtros amplios.
#   * Un bloque vacío, error CellCLI o conexión interrumpida marca la captura INCOMPLETA.
#
# Sintaxis de filtro temporal usada (validada en el entorno de referencia, donde '>=' no se
# acepta):   collectionTime NOT < 'inicio' AND collectionTime < 'fin'
#
# Códigos de salida:
#   0  COMPLETE              todos los bloques OK
#   1  USAGE_ERROR           parámetros inválidos (no se ha contactado ninguna celda)
#   2  PREVALIDATION_FAILED  alguna celda no supera la prevalidación (no se recoge histórico)
#   3  INCOMPLETE            hay bloques con error, vacíos, interrumpidos o sin ejecutar
#   4  LOCAL_ERROR           error local en el dbnode (directorio, herramientas)
#   130 INTERRUPTED          interrumpido por señal (captura incompleta)
# =============================================================================
set -u
set -o pipefail
umask 077

COLLECTOR_VERSION="1.0.0"
FORMAT_VERSION="1"
BLOCK_SECONDS=3600
PROG=$(basename "$0")

# Pareja explícita "objectType nombre". Solo ámbito global (celda, discos, flash, red).
DEFAULT_METRICS='
CELL CL_CPUT
CELL CL_MEMUT
CELL N_HCA_MB_RCV_SEC
CELL N_HCA_MB_TRANS_SEC
CELL N_NIC_KB_RCV_SEC
CELL N_NIC_KB_TRANS_SEC
CELLDISK CD_IO_RQ_R_SM_SEC
CELLDISK CD_IO_RQ_R_LG_SEC
CELLDISK CD_IO_RQ_W_SM_SEC
CELLDISK CD_IO_RQ_W_LG_SEC
CELLDISK CD_IO_BY_R_SM_SEC
CELLDISK CD_IO_BY_R_LG_SEC
CELLDISK CD_IO_BY_W_SM_SEC
CELLDISK CD_IO_BY_W_LG_SEC
CELLDISK CD_IO_TM_R_SM_RQ
CELLDISK CD_IO_TM_R_LG_RQ
CELLDISK CD_IO_TM_W_SM_RQ
CELLDISK CD_IO_TM_W_LG_RQ
CELLDISK CD_IO_UTIL
CELLDISK CD_IO_LOAD
CELLDISK CD_IO_RQ_R_SM
CELLDISK CD_IO_RQ_R_LG
CELLDISK CD_IO_RQ_W_SM
CELLDISK CD_IO_RQ_W_LG
FLASHCACHE FC_IO_RQ_R_SEC
FLASHCACHE FC_IO_RQ_W_SEC
FLASHCACHE FC_IO_BY_R_SEC
FLASHCACHE FC_IO_BY_W_SEC
FLASHCACHE FC_IO_RQ_R_MISS_SEC
FLASHCACHE FC_IO_RQ_R
'
ALLOWED_OBJECT_TYPES=" CELL CELLDISK FLASHCACHE FLASHLOG PMEMCACHE "

SSH_OPTS=(-n -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20
          -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o LogLevel=ERROR)

usage() {
  cat <<EOF
Uso: $PROG -d DIR_LOCAL -g CELL_GROUP -u root|celladmin -s INICIO -e FIN [opciones]

  -d DIR_LOCAL   Directorio EXISTENTE en el dbnode donde se creará la carpeta de captura.
  -g CELL_GROUP  Archivo con una celda por línea (cualquier número de celdas).
  -u USUARIO     Usuario remoto: root o celladmin.
  -s INICIO      Inicio inclusivo ISO-8601 con zona: 2026-11-01T00:00:00+01:00 (o ...Z)
  -e FIN         Fin exclusivo ISO-8601 con zona.
Opciones:
  -p N           Celdas en paralelo (por defecto 4).
  -m ARCHIVO     Lista alternativa "objectType nombre" (solo ámbitos: ${ALLOWED_OBJECT_TYPES}).
  -n             Simulación: muestra el plan y los comandos sin contactar celdas ni crear nada.
  -h             Ayuda.

Ejemplo (24 h):
  $PROG -d /home/oracle/capturas -g ~/cell_group -u celladmin \\
        -s 2026-11-01T00:00:00+01:00 -e 2026-11-02T00:00:00+01:00
EOF
}

die() { local code=$1; shift; echo "$PROG: ERROR: $*" >&2; exit "$code"; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ----------------------------------------------------------------------------- parámetros
DIR="" GROUP="" RUSER="" START="" END="" PARALLEL=4 METRICS_FILE="" DRYRUN=0
while getopts ":d:g:u:s:e:p:m:nh" opt; do
  case $opt in
    d) DIR=$OPTARG ;; g) GROUP=$OPTARG ;; u) RUSER=$OPTARG ;;
    s) START=$OPTARG ;; e) END=$OPTARG ;; p) PARALLEL=$OPTARG ;;
    m) METRICS_FILE=$OPTARG ;; n) DRYRUN=1 ;;
    h) usage; exit 0 ;;
    :) usage >&2; die 1 "falta el valor de -$OPTARG" ;;
    *) usage >&2; die 1 "opción desconocida -$OPTARG" ;;
  esac
done
shift $((OPTIND - 1))
[ $# -eq 0 ] || die 1 "argumentos sobrantes: $*"
[ -n "$DIR" ] && [ -n "$GROUP" ] && [ -n "$RUSER" ] && [ -n "$START" ] && [ -n "$END" ] \
  || { usage >&2; die 1 "faltan parámetros obligatorios (-d -g -u -s -e)"; }
case $RUSER in root|celladmin) ;; *) die 1 "usuario remoto no permitido: '$RUSER' (root o celladmin)";; esac
[[ $PARALLEL =~ ^[1-9][0-9]*$ ]] || die 1 "-p debe ser un entero positivo"

ISO_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([+-][0-9]{2}:[0-9]{2}|Z)$'
[[ $START =~ $ISO_RE ]] || die 1 "INICIO sin formato ISO-8601 con zona horaria: '$START'"
[[ $END =~ $ISO_RE ]] || die 1 "FIN sin formato ISO-8601 con zona horaria: '$END'"
START_EPOCH=$(date -u -d "$START" +%s 2>/dev/null) || die 1 "INICIO no interpretable: $START"
END_EPOCH=$(date -u -d "$END" +%s 2>/dev/null) || die 1 "FIN no interpretable: $END"
[ "$END_EPOCH" -gt "$START_EPOCH" ] || die 1 "FIN debe ser posterior a INICIO"

# Desfase de la zona de INICIO: todos los límites de bloque se expresan con él.
if [[ $START == *Z ]]; then OFF_STR="+00:00"; OFF_SEC=0
else
  OFF_STR=${START: -6}
  _h=${OFF_STR:1:2}; _m=${OFF_STR:4:2}
  OFF_SEC=$(( (10#$_h * 3600 + 10#$_m * 60) ))
  [ "${OFF_STR:0:1}" = "-" ] && OFF_SEC=$(( -OFF_SEC ))
fi
fmt_ts() { echo "$(date -u -d "@$(( $1 + OFF_SEC ))" +%Y-%m-%dT%H:%M:%S)${OFF_STR}"; }

# ----------------------------------------------------------------------------- celdas
[ -r "$GROUP" ] || die 1 "no se puede leer cell_group: $GROUP"
CELLS=()
while IFS= read -r line || [ -n "$line" ]; do
  line=${line%%#*}; line=$(echo "$line" | tr -d '[:space:]')
  [ -z "$line" ] && continue
  [[ $line =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 1 "nombre de celda inválido en $GROUP: '$line'"
  for c in "${CELLS[@]:-}"; do [ "$c" = "$line" ] && die 1 "celda duplicada en $GROUP: $line"; done
  CELLS+=("$line")
done < "$GROUP"
[ ${#CELLS[@]} -ge 1 ] || die 1 "cell_group vacío: $GROUP"

# ----------------------------------------------------------------------------- métricas
if [ -n "$METRICS_FILE" ]; then
  [ -r "$METRICS_FILE" ] || die 1 "no se puede leer $METRICS_FILE"
  METRICS_TEXT=$(cat "$METRICS_FILE")
else
  METRICS_TEXT=$DEFAULT_METRICS
fi
WANTED=()
while read -r ot name extra; do
  [ -z "${ot:-}" ] && continue
  [[ $ot == \#* ]] && continue
  [ -z "${extra:-}" ] || die 1 "línea de métrica inválida: '$ot $name $extra'"
  [[ $ALLOWED_OBJECT_TYPES == *" $ot "* ]] || die 1 "ámbito no permitido en modo global: $ot ($name)"
  [[ $name =~ ^[A-Z][A-Z0-9_]*$ ]] || die 1 "nombre de métrica inválido: '$name'"
  case $name in DB_*|PDB_*|CT_*|CG_*|IORM*) die 1 "métrica por DB/PDB/IORM no permitida: $name";; esac
  WANTED+=("$ot $name")
done <<< "$METRICS_TEXT"
[ ${#WANTED[@]} -ge 1 ] || die 1 "lista de métricas vacía"

# ----------------------------------------------------------------------------- bloques
B_START=() B_END=() B_SEPOCH=()
t=$START_EPOCH
while [ "$t" -lt "$END_EPOCH" ]; do
  e=$(( t + BLOCK_SECONDS )); [ "$e" -gt "$END_EPOCH" ] && e=$END_EPOCH
  B_SEPOCH+=("$t"); B_START+=("$(fmt_ts "$t")"); B_END+=("$(fmt_ts "$e")")
  t=$e
done
NBLOCKS=${#B_START[@]}
DURATION=$(( END_EPOCH - START_EPOCH ))

hist_cmd() { # metric start end
  echo "LIST METRICHISTORY WHERE name = '$1' AND collectionTime NOT < '$2' AND collectionTime < '$3' ATTRIBUTES name, metricObjectName, collectionTime, metricValue"
}

if [ "$DRYRUN" -eq 1 ]; then
  echo "== exacell_collect.sh $COLLECTOR_VERSION (simulación: no se contacta ninguna celda) =="
  echo "Celdas (${#CELLS[@]}): ${CELLS[*]}"
  echo "Usuario remoto: $RUSER   Periodo: $START -> $END   Bloques horarios: $NBLOCKS"
  echo "Métricas solicitadas (${#WANTED[@]}), se cruzarán con el catálogo de cada celda:"
  printf '  %s\n' "${WANTED[@]}"
  echo "Prevalidación: dcli -g $GROUP -l $RUSER 'cellcli -e \"LIST CELL ATTRIBUTES name, status\"'"
  echo "Ejemplo de comando de bloque:"
  echo "  ssh ${SSH_OPTS[*]} $RUSER@${CELLS[0]} \"cellcli -e \\\"$(hist_cmd CL_CPUT "${B_START[0]}" "${B_END[0]}")\\\"\" > (dbnode)"
  [ "$DURATION" -lt 86400 ] && echo "AVISO: periodo < 24 h; no apto para aprobar una migración."
  exit 0
fi

# ----------------------------------------------------------------------------- entorno local
for tool in dcli ssh date sha256sum awk grep sort; do
  command -v "$tool" >/dev/null 2>&1 || die 4 "herramienta no encontrada en el dbnode: $tool"
done
[ -d "$DIR" ] && [ -w "$DIR" ] || die 4 "el directorio local no existe o no es escribible: $DIR"
HOST_SHORT=$(hostname -s 2>/dev/null || hostname)
CAP="$DIR/exacell_capture_${HOST_SHORT}_$(date -u +%Y%m%dT%H%M%SZ)_$$"
mkdir "$CAP" 2>/dev/null || die 4 "no se pudo crear $CAP (¿ya existe?)"
mkdir "$CAP/prevalidation" "$CAP/cells" "$CAP/data" || die 4 "no se pudo crear la estructura en $CAP"
cp "$GROUP" "$CAP/cell_group.txt" || die 4 "no se pudo copiar cell_group"
printf '%s\n' "${WANTED[@]}" > "$CAP/metrics_requested.txt"
CREATED_UTC=$(now_utc)
: > "$CAP/blocks_plan.tsv"
for i in $(seq 0 $((NBLOCKS - 1))); do
  printf '%d\t%s\t%s\n' "$i" "${B_START[$i]}" "${B_END[$i]}" >> "$CAP/blocks_plan.tsv"
done

CAPTURE_STATUS="RUNNING"
EXIT_CODE=0
PREVAL_STATUS="NOT_RUN"
EXPECTED_BLOCKS=0

write_manifest() {
  local f="$CAP/manifest.txt"
  {
    echo "format_version=$FORMAT_VERSION"
    echo "collector_version=$COLLECTOR_VERSION"
    echo "metrics_mode=global"
    echo "dbnode_host=$HOST_SHORT"
    echo "remote_user=$RUSER"
    echo "cell_group_file=$GROUP"
    echo "cell_count=${#CELLS[@]}"
    echo "cells=${CELLS[*]}"
    echo "period_start=$START"
    echo "period_end=$END"
    echo "period_start_epoch=$START_EPOCH"
    echo "period_end_epoch=$END_EPOCH"
    echo "block_seconds=$BLOCK_SECONDS"
    echo "block_count=$NBLOCKS"
    echo "time_filter=collectionTime NOT < 'start' AND collectionTime < 'end'"
    echo "created_utc=$CREATED_UTC"
    echo "finished_utc=$(now_utc)"
    echo "prevalidation_status=$PREVAL_STATUS"
    echo "expected_blocks=$EXPECTED_BLOCKS"
    echo "recorded_blocks=${RECORDED_BLOCKS:-0}"
    echo "not_run_blocks=${NOT_RUN_BLOCKS:-0}"
    echo "status_counts=${STATUS_COUNTS:-}"
    echo "capture_status=$CAPTURE_STATUS"
    echo "exit_code=$EXIT_CODE"
  } > "$f"
}

finalize() {
  # Consolida bloques, calcula estado global, escribe manifiesto, resumen y sumas SHA-256.
  {
    printf 'cell\tobject_type\tmetric\tblock_index\tblock_start\tblock_end\tstatus\trc\tout_file\terr_file\tout_lines\tout_bytes\terr_bytes\tstarted_utc\tended_utc\n'
    for c in "${CELLS[@]}"; do
      [ -f "$CAP/cells/$c/blocks.tsv" ] && cat "$CAP/cells/$c/blocks.tsv"
    done
  } > "$CAP/blocks.tsv"
  RECORDED_BLOCKS=$(( $(wc -l < "$CAP/blocks.tsv") - 1 ))
  NOT_RUN_BLOCKS=$(( EXPECTED_BLOCKS - RECORDED_BLOCKS ))
  [ "$NOT_RUN_BLOCKS" -lt 0 ] && NOT_RUN_BLOCKS=0
  STATUS_COUNTS=$(awk -F'\t' 'NR>1{c[$7]++} END{for(k in c) printf "%s:%d ", k, c[k]}' "$CAP/blocks.tsv" | sed 's/ $//')
  local bad
  bad=$(awk -F'\t' 'NR>1 && $7!="OK"' "$CAP/blocks.tsv" | wc -l)
  if [ "$CAPTURE_STATUS" = "RUNNING" ]; then
    if [ "$bad" -eq 0 ] && [ "$NOT_RUN_BLOCKS" -eq 0 ] && [ "$RECORDED_BLOCKS" -gt 0 ]; then
      CAPTURE_STATUS="COMPLETE"; EXIT_CODE=0
    else
      CAPTURE_STATUS="INCOMPLETE"; EXIT_CODE=3
    fi
  fi
  write_manifest
  {
    echo "== Resumen exacell_collect.sh $COLLECTOR_VERSION =="
    echo "Captura:            $CAP"
    echo "Estado:             $CAPTURE_STATUS (código de salida $EXIT_CODE)"
    echo "Prevalidación:      $PREVAL_STATUS"
    echo "Celdas:             ${#CELLS[@]} (${CELLS[*]})"
    echo "Periodo:            $START -> $END  ($NBLOCKS bloques de ${BLOCK_SECONDS}s)"
    echo "Bloques esperados:  $EXPECTED_BLOCKS   registrados: $RECORDED_BLOCKS   sin ejecutar: $NOT_RUN_BLOCKS"
    echo "Estados:            ${STATUS_COUNTS:-ninguno}"
    if [ "$bad" -gt 0 ]; then
      echo "Bloques no OK (primeros 20):"
      awk -F'\t' 'NR>1 && $7!="OK"{printf "  %s %s bloque %s [%s] rc=%s\n", $1, $3, $4, $7, $8}' "$CAP/blocks.tsv" | head -20
    fi
    [ "$DURATION" -lt 86400 ] && echo "AVISO: periodo < 24 h; no apto para aprobar una migración."
    echo "Verificación: (cd '$CAP' && sha256sum -c SHA256SUMS)"
  } > "$CAP/summary.txt"
  ( cd "$CAP" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | xargs -d '\n' sha256sum > SHA256SUMS )
  cat "$CAP/summary.txt"
}

on_signal() {
  trap - INT TERM
  echo "$PROG: interrumpido; finalizando captura como INTERRUPTED" >&2
  local pids; pids=$(jobs -p); [ -n "$pids" ] && kill $pids 2>/dev/null; wait 2>/dev/null
  CAPTURE_STATUS="INTERRUPTED"; EXIT_CODE=130
  finalize
  exit 130
}
trap on_signal INT TERM

run_ssh() { # host comando_cellcli out err  -> rc
  ssh "${SSH_OPTS[@]}" "$RUSER@$1" "cellcli -e \"$2\"" > "$3" 2> "$4"
}

has_cellcli_error() { grep -q -E '(^|[^A-Za-z0-9])CELL-[0-9]{5}' "$@" 2>/dev/null; }

# ----------------------------------------------------------------------------- prevalidación
echo "[$(now_utc)] Prevalidación de ${#CELLS[@]} celda(s) con dcli..."
PV="$CAP/prevalidation"
dcli -g "$GROUP" -l "$RUSER" "cellcli -e \"LIST CELL ATTRIBUTES name, status\"" \
  > "$PV/dcli_list_cell.out" 2> "$PV/dcli_list_cell.err"
echo $? > "$PV/dcli_list_cell.rc"
PV_FAIL=0
printf 'cell\tcheck\tresult\tdetail\n' > "$PV/prevalidation.tsv"
for c in "${CELLS[@]}"; do
  st=$(awk -v h="$c:" '$1==h {print $3; exit}' "$PV/dcli_list_cell.out")
  if [ "$st" = "online" ]; then
    printf '%s\tdcli_list_cell\tOK\tstatus=online\n' "$c" >> "$PV/prevalidation.tsv"
  else
    printf '%s\tdcli_list_cell\tFAIL\tstatus=%s\n' "$c" "${st:-sin_respuesta}" >> "$PV/prevalidation.tsv"
    PV_FAIL=1
  fi
done
if has_cellcli_error "$PV/dcli_list_cell.out"; then PV_FAIL=1; fi

# SSH directo (vía de recogida) + inventario de solo lectura de cada celda.
for c in "${CELLS[@]}"; do
  d="$CAP/cells/$c"; mkdir -p "$d"
  ok=1
  for item in "list_cell:LIST CELL ATTRIBUTES name, status" \
              "cell_detail:LIST CELL DETAIL" \
              "celldisks:LIST CELLDISK ATTRIBUTES name, diskType, size" \
              "metricdefinition:LIST METRICDEFINITION ATTRIBUTES name, objectType, metricType, unit"; do
    key=${item%%:*}; cmd=${item#*:}
    run_ssh "$c" "$cmd" "$d/$key.out" "$d/$key.err"; rc=$?
    echo "$rc" > "$d/$key.rc"
    if [ "$rc" -ne 0 ] || has_cellcli_error "$d/$key.out" "$d/$key.err" || [ ! -s "$d/$key.out" ]; then
      printf '%s\tssh_%s\tFAIL\trc=%s\n' "$c" "$key" "$rc" >> "$PV/prevalidation.tsv"; ok=0
    else
      printf '%s\tssh_%s\tOK\trc=0\n' "$c" "$key" >> "$PV/prevalidation.tsv"
    fi
  done
  if [ "$ok" -eq 1 ]; then
    # Selección explícita por (objectType, name) contra el catálogo de esta celda.
    printf '%s\n' "${WANTED[@]}" | awk -v sel="$d/metrics_selected.tsv" -v miss="$d/metrics_missing.tsv" '
      NR==FNR { cat[$1]=$2; mt[$1]=$3; u=$4; for(i=5;i<=NF;i++) u=u" "$i; un[$1]=u; next }
      { ot=$1; n=$2
        if (!(n in cat)) { print ot"\t"n"\tno_en_catalogo" > miss; next }
        if (cat[n]!=ot)  { print ot"\t"n"\tobjectType_distinto:"cat[n] > miss; next }
        print ot"\t"n"\t"mt[n]"\t"un[n] > sel }' "$d/metricdefinition.out" -
    touch "$d/metrics_selected.tsv" "$d/metrics_missing.tsv"
    nsel=$(wc -l < "$d/metrics_selected.tsv")
    if [ "$nsel" -eq 0 ]; then
      printf '%s\tmetric_selection\tFAIL\tninguna métrica seleccionada\n' "$c" >> "$PV/prevalidation.tsv"; ok=0
    else
      printf '%s\tmetric_selection\tOK\tseleccionadas=%s ausentes=%s\n' "$c" "$nsel" "$(wc -l < "$d/metrics_missing.tsv")" >> "$PV/prevalidation.tsv"
      EXPECTED_BLOCKS=$(( EXPECTED_BLOCKS + nsel * NBLOCKS ))
    fi
  fi
  [ "$ok" -eq 1 ] || PV_FAIL=1
done

if [ "$PV_FAIL" -ne 0 ]; then
  PREVAL_STATUS="FAILED"; CAPTURE_STATUS="PREVALIDATION_FAILED"; EXIT_CODE=2
  echo "[$(now_utc)] Prevalidación FALLIDA: no se recoge histórico." >&2
  awk -F'\t' 'NR>1 && $3=="FAIL"{print "  " $1 " " $2 " " $4}' "$PV/prevalidation.tsv" >&2
  finalize
  exit 2
fi
PREVAL_STATUS="OK"
echo "[$(now_utc)] Prevalidación OK. Bloques esperados: $EXPECTED_BLOCKS"

# ----------------------------------------------------------------------------- recogida
classify() { # rc out err metric -> estado
  local rc=$1 out=$2 err=$3 metric=$4
  if [ "$rc" -eq 255 ]; then
    if [ -s "$out" ]; then echo INTERRUPTED; else echo NETWORK_ERROR; fi; return
  fi
  if [ "$rc" -gt 128 ]; then echo INTERRUPTED; return; fi
  if has_cellcli_error "$out" "$err"; then echo CELLCLI_ERROR; return; fi
  if [ "$rc" -ne 0 ]; then echo COMMAND_ERROR; return; fi
  if ! grep -q '[^[:space:]]' "$out"; then echo EMPTY; return; fi
  if awk -v m="$metric" 'NF>0 && $1!=m {bad=1} END{exit !bad}' "$out"; then echo MALFORMED; return; fi
  echo OK
}

collect_cell() {
  trap - INT TERM
  local c=$1 d="$CAP/cells/$1" dd="$CAP/data/$1"
  mkdir -p "$dd"
  : > "$d/blocks.tsv"
  local i ot name mt unit cmd out err rc st started
  for i in $(seq 0 $((NBLOCKS - 1))); do
    while IFS=$'\t' read -r ot name mt unit; do
      [ -z "$name" ] && continue
      out="data/$c/b$(printf '%04d' "$i")_${name}.out"; err="data/$c/b$(printf '%04d' "$i")_${name}.err"
      cmd=$(hist_cmd "$name" "${B_START[$i]}" "${B_END[$i]}")
      started=$(now_utc)
      run_ssh "$c" "$cmd" "$CAP/$out" "$CAP/$err"; rc=$?
      st=$(classify "$rc" "$CAP/$out" "$CAP/$err" "$name")
      printf '%s\t%s\t%s\t%d\t%s\t%s\t%s\t%d\t%s\t%s\t%d\t%d\t%d\t%s\t%s\n' \
        "$c" "$ot" "$name" "$i" "${B_START[$i]}" "${B_END[$i]}" "$st" "$rc" "$out" "$err" \
        "$(grep -c '[^[:space:]]' "$CAP/$out")" "$(wc -c < "$CAP/$out")" "$(wc -c < "$CAP/$err")" \
        "$started" "$(now_utc)" >> "$d/blocks.tsv"
    done < "$d/metrics_selected.tsv"
  done
}

echo "[$(now_utc)] Recogiendo $NBLOCKS bloque(s) horarios de ${#CELLS[@]} celda(s), $PARALLEL en paralelo..."
idx=0
while [ "$idx" -lt "${#CELLS[@]}" ]; do
  for c in "${CELLS[@]:$idx:$PARALLEL}"; do collect_cell "$c" & done
  wait
  idx=$(( idx + PARALLEL ))
done
trap - INT TERM
finalize
exit "$EXIT_CODE"
