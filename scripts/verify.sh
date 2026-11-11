#!/usr/bin/env bash
# =============================================================================
# verify.sh — run every check in the suite locally, with one command.
#
#   scripts/verify.sh               lint + all tests + scenario runs
#   scripts/verify.sh --install     first install each project's requirements-dev.txt
#   scripts/verify.sh --no-neo4j    skip the live Neo4j stage
#   scripts/verify.sh --keep-neo4j  leave the Neo4j container running afterwards
#
# The live-graph stage starts Neo4j with `docker compose` when Docker is
# available, and is skipped (and reported as skipped) when it isn't.
# Exit code is 0 only if every stage that ran passed.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INSTALL=0; NEO4J=auto; KEEP=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --no-neo4j) NEO4J=off ;;
    --keep-neo4j) KEEP=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

declare -a RESULTS=()
FAILED=0
run() {  # run "<label>" <command...>
  local label="$1"; shift
  printf '\n\033[1m== %s\033[0m\n' "$label"
  if "$@"; then RESULTS+=("PASS  $label"); else RESULTS+=("FAIL  $label"); FAILED=1; fi
}
skip() { RESULTS+=("SKIP  $1 ($2)"); printf '\n== %s: skipped (%s)\n' "$1" "$2"; }

# --- dependencies ---------------------------------------------------------------
if [[ $INSTALL -eq 1 ]]; then
  run "Install ruff" python -m pip install --quiet ruff
  for req in projects/*/requirements-dev.txt; do
    [[ -f "$req" ]] && run "Install $(dirname "$req")" python -m pip install --quiet -r "$req"
  done
fi

# --- lint -------------------------------------------------------------------------
if command -v ruff >/dev/null 2>&1; then
  run "Lint (ruff check)" ruff check .
  run "Format (ruff format --check)" ruff format --check .
else
  skip "Lint" "ruff not installed; use --install"
fi

# --- unit and scenario tests, per project ------------------------------------------
for proj in projects/*/; do
  name="$(basename "$proj")"
  if compgen -G "${proj}tests/test_*.py" >/dev/null; then
    run "Tests: $name" bash -c "cd '$proj' && NEO4J_URI= python -m pytest -q"
  fi
done

PID=projects/02_bioprint_pid
if [[ -f "$PID/src/app.py" ]]; then
  run "BioPrint-PID: 30 s scenario + PRD acceptance checks" bash -c "cd $PID && python src/app.py"
  for example in "$PID"/config/examples/*.toml; do
    [[ -f "$example" ]] || continue
    run "BioPrint-PID: example config $(basename "$example")" \
      bash -c "cd $PID && python src/app.py --config config/examples/$(basename "$example") --report-only"
  done
fi

GUARD=projects/03_bioprint_guard
if [[ -f "$GUARD/src/app.py" ]]; then
  run "BioPrint-Guard: OPC UA scenario + PRD acceptance checks (alerts in memory)" \
    bash -c "cd $GUARD && NEO4J_URI= python src/app.py --speed 10 --port 0"
  run "BioPrint-Guard: scenario with the frozen baseline" \
    bash -c "cd $GUARD && NEO4J_URI= python src/app.py --speed 10 --port 0 --baseline frozen"
  if python -c "import sklearn" >/dev/null 2>&1; then
    run "BioPrint-Guard: scenario with the Isolation Forest detector" \
      bash -c "cd $GUARD && NEO4J_URI= python src/app.py --speed 10 --port 0 --detector iforest"
  else
    skip "BioPrint-Guard: Isolation Forest detector" "scikit-learn not installed; use --install"
  fi
  # Leave the default detector's chart and data in output/.
  run "BioPrint-Guard: default scenario outputs" \
    bash -c "cd $GUARD && NEO4J_URI= python src/app.py --speed 10 --port 0 >/dev/null 2>&1"
fi

# --- live Neo4j stage ----------------------------------------------------------------
TWIN=projects/01_graphprint_twin
guard_two_process() {  # server first, wait for its port, then the gateway with a timeout
  local port=4841 status=0 server_pid
  ( cd "$GUARD" && exec python src/opcua_server.py --endpoint "opc.tcp://0.0.0.0:$port/bioprint/" \
      --duration 30 --speed 10 --start-delay 4 ) &
  server_pid=$!
  for _ in $(seq 1 50); do
    python -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(s.connect_ex(('127.0.0.1',$port)))" && break
    sleep 0.2
  done
  ( cd "$GUARD" && python src/gateway.py --endpoint "opc.tcp://localhost:$port/bioprint/" \
      --samples 300 --duration 60 --require-alert ) || status=1
  wait "$server_pid" || status=1
  return $status
}
if [[ -f "$TWIN/src/main.py" || -f "$GUARD/src/app.py" ]]; then
  if [[ $NEO4J == off ]]; then
    skip "Live graph (GraphPrint-Twin, BioPrint-Guard)" "--no-neo4j"
  elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    skip "Live graph (GraphPrint-Twin, BioPrint-Guard)" "Docker not available"
  else
    run "Start Neo4j (docker compose)" docker compose up -d neo4j
    export NEO4J_URI=bolt://localhost:7687 NEO4J_WAIT_SECONDS=120
    if [[ -f "$TWIN/src/main.py" ]]; then
      run "GraphPrint-Twin: pipeline run" bash -c "cd $TWIN && python src/main.py --interval 0"
      run "GraphPrint-Twin: live-graph integration tests" \
        bash -c "cd $TWIN && python -m pytest -q tests/test_integration.py"
    fi
    if [[ -f "$GUARD/src/app.py" ]]; then
      run "BioPrint-Guard: server -> gateway -> Neo4j (two processes)" guard_two_process
      run "BioPrint-Guard: live-graph integration tests" \
        bash -c "cd $GUARD && python -m pytest -q tests/test_integration.py"
    fi
    [[ $KEEP -eq 1 ]] || docker compose stop neo4j >/dev/null
  fi
fi

# --- summary ----------------------------------------------------------------------------
printf '\n\033[1m== Summary\033[0m\n'
printf '%s\n' "${RESULTS[@]}"
exit $FAILED
