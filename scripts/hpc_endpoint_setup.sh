#!/usr/bin/env bash
# Diamond Agent — per-supercomputer endpoint setup (Phase 0, run ON the login node).
#
#   scp scripts/hpc_endpoint_setup.sh <machine>:
#   ssh -t <machine> 'bash hpc_endpoint_setup.sh'
#
# Bootstraps via uv (which downloads its own managed CPython), so it works
# even when the system python3 is ancient and without touching the module
# system. Starts an endpoint whose workers run on the login node
# (LocalProvider, 1 worker, BLAS/OpenMP threads pinned to 1 so login-node
# process/thread limits are respected) — right for Diamond Agent's probe/sbatch
# shell commands. Re-running on an existing endpoint applies the settings and
# restarts it; config.yaml is never overwritten.
# First start opens a Globus login: use the SAME account as `make login`.

set -euo pipefail

ENDPOINT_NAME="${1:-diamond-agent}"
GCE_VENV="${GCE_VENV:-$HOME/.gce-venv}"
PYVER="${PYVER:-3.12}"

# Login nodes cap per-user threads (TACC Vista: `ulimit -u 100`, and that
# counts threads). uv sizes its rayon/tokio pools to the core count (72+ on a
# GH200 login node) and aborts with "failed to initialize global rayon pool:
# ... Resource temporarily unavailable" — so pin them before any uv call.
export RAYON_NUM_THREADS="${RAYON_NUM_THREADS:-2}"
export TOKIO_WORKER_THREADS="${TOKIO_WORKER_THREADS:-2}"
export UV_CONCURRENT_INSTALLS="${UV_CONCURRENT_INSTALLS:-2}"
export UV_CONCURRENT_DOWNLOADS="${UV_CONCURRENT_DOWNLOADS:-2}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
NPROC_LIMIT="$(ulimit -u 2>/dev/null || echo unlimited)"
THREADS_NOW="$(ps -u "$USER" -o nlwp= 2>/dev/null | awk '{s+=$1} END {print s+0}')"
echo "==> login-node budget: ulimit -u $NPROC_LIMIT (counts threads); you use $THREADS_NOW threads now; uv pools pinned to 2"

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv > /dev/null 2>&1; then
    echo "==> installing uv (user-local)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "==> $(uv --version)"

if [ ! -x "$GCE_VENV/bin/globus-compute-endpoint" ]; then
    echo "==> creating venv at $GCE_VENV (python $PYVER via uv managed builds)"
    uv venv "$GCE_VENV" --python "$PYVER"
    uv pip install --python "$GCE_VENV/bin/python" --quiet globus-compute-endpoint
fi
GCE="$GCE_VENV/bin/globus-compute-endpoint"
echo "==> $("$GCE" version 2>/dev/null || echo globus-compute-endpoint installed)"

# The endpoint manager re-execs `globus-compute-endpoint` resolved via PATH
# when spawning the endpoint process; the venv bin is not on PATH, so expose
# the executable through ~/.local/bin (which login shells put on PATH).
mkdir -p "$HOME/.local/bin"
ln -sf "$GCE" "$HOME/.local/bin/globus-compute-endpoint"

EP_DIR="$HOME/.globus_compute/$ENDPOINT_NAME"
if [ ! -d "$EP_DIR" ]; then
    echo "==> configuring endpoint '$ENDPOINT_NAME'"
    "$GCE" configure "$ENDPOINT_NAME"
fi

# Login-node friendly worker settings. Endpoint 4.x runs every endpoint as a
# manager that spawns a user-endpoint process (UEP) from
# user_config_template.yaml.j2 — that template, not config.yaml, holds the
# engine block. The stock template already caps max_workers_per_node at 1
# (one worker is enough for Diamond Agent's probes and sbatch); what bites on
# login nodes is the per-user process/THREAD limit (e.g. `ulimit -u 100` on
# TACC Vista), so also pin BLAS/OpenMP threads to 1 for everything the worker
# runs. config.yaml is left untouched (overwriting it breaks the manager).
TEMPLATE="$EP_DIR/user_config_template.yaml.j2"
MAX_WORKERS="${MAX_WORKERS:-1}"
if [ -f "$TEMPLATE" ] && grep -q '^engine:' "$EP_DIR/config.yaml" 2>/dev/null; then
    # Endpoint 4.x refuses to start when config.yaml carries an `engine` block
    # ("move the engine block to user_config_template.yaml.j2"). Keep every
    # other top-level key, drop the engine mapping, and leave a backup.
    echo "==> config.yaml contains an engine block (not allowed with a UEP template) — removing it"
    cp "$EP_DIR/config.yaml" "$EP_DIR/config.yaml.engine-block.bak"
    awk 'BEGIN{skip=0} /^engine:/{skip=1; next} skip && /^[^[:space:]#]/{skip=0} !skip{print}' \
        "$EP_DIR/config.yaml.engine-block.bak" > "$EP_DIR/config.yaml"
    grep -q '^display_name:' "$EP_DIR/config.yaml" || echo "display_name: $ENDPOINT_NAME ($(hostname -s))" >> "$EP_DIR/config.yaml"
    CHANGED_CFG=1
fi
THREAD_ENV='export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1'
CHANGED=0
if [ -f "$TEMPLATE" ]; then
    cp -n "$TEMPLATE" "$TEMPLATE.bak" 2>/dev/null || true
    if grep -q "max_workers_per_node" "$TEMPLATE"; then
        sed -i.tmp -E "s/^([[:space:]]*max_workers_per_node:).*/\1 $MAX_WORKERS/" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    else
        sed -i.tmp -E "s/^([[:space:]]*type: GlobusComputeEngine)$/\1\n  max_workers_per_node: $MAX_WORKERS/" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    fi
    if grep -q 'worker_init: {{ worker_init|default() }}' "$TEMPLATE"; then
        sed -i.tmp "s|worker_init: {{ worker_init\|default() }}|worker_init: {{ worker_init\|default(\"$THREAD_ENV\") }}|" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    fi
    if ! cmp -s "$TEMPLATE" "$TEMPLATE.bak"; then CHANGED=1; fi
    echo "==> UEP template: max_workers_per_node=$MAX_WORKERS, thread env pinned ($([ $CHANGED = 1 ] && echo updated || echo already set))"
elif [ -f "$EP_DIR/config.yaml" ] && grep -q "^engine:" "$EP_DIR/config.yaml"; then
    # legacy single-user layout: engine block lives in config.yaml
    if ! grep -q "max_workers_per_node" "$EP_DIR/config.yaml"; then
        cp -n "$EP_DIR/config.yaml" "$EP_DIR/config.yaml.bak"
        sed -i.tmp -E "s/^([[:space:]]*type: GlobusComputeEngine)$/\1\n  max_workers_per_node: $MAX_WORKERS/" "$EP_DIR/config.yaml" && rm -f "$EP_DIR/config.yaml.tmp"
        CHANGED=1
        echo "==> config.yaml: added max_workers_per_node=$MAX_WORKERS"
    fi
fi
if { [ "$CHANGED" = 1 ] || [ "${CHANGED_CFG:-0}" = 1 ]; } && "$GCE" list 2>/dev/null | grep -q "$ENDPOINT_NAME.*Running"; then
    echo "==> restarting '$ENDPOINT_NAME' to apply the template"
    "$GCE" stop "$ENDPOINT_NAME" || true
fi

echo "==> starting endpoint '$ENDPOINT_NAME' (first run opens a Globus login)"
"$GCE" start "$ENDPOINT_NAME"

echo
"$GCE" list
echo
echo "Done. Copy this endpoint's UUID into sites.live.yaml on your workstation,"
echo "then run 'make preflight' there. After login-node maintenance, re-run:"
echo "  $GCE start $ENDPOINT_NAME"
