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
# Logs in first (same Globus account as `make login`), then starts the
# endpoint with --detach (4.x otherwise stays in the foreground and dies with
# the SSH session).

set -euo pipefail

ENDPOINT_NAME="${1:-diamond-agent}"
GCE_VENV="${GCE_VENV:-$HOME/.gce-venv}"
PYVER="${PYVER:-3.12}"
# The endpoint version to install: the workstation's globus-compute-sdk version
# (endpoint_setup_remote.sh passes it), so the manager never outruns the
# worker venv the endpoint builds to match the submitting SDK. Empty = latest.
GCE_VERSION="${GCE_VERSION:-}"
GCE_SPEC="globus-compute-endpoint${GCE_VERSION:+==$GCE_VERSION}"

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

REINSTALLED=0
if [ ! -x "$GCE_VENV/bin/globus-compute-endpoint" ]; then
    echo "==> creating venv at $GCE_VENV (python $PYVER via uv managed builds)"
    uv venv "$GCE_VENV" --python "$PYVER"
    uv pip install --python "$GCE_VENV/bin/python" --quiet "$GCE_SPEC"
elif [ -n "$GCE_VERSION" ]; then
    HAVE="$("$GCE_VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("globus-compute-endpoint"))' 2>/dev/null || echo none)"
    if [ "$HAVE" != "$GCE_VERSION" ]; then
        echo "==> endpoint is $HAVE, workstation SDK is $GCE_VERSION: reinstalling to match (the endpoint restarts below)"
        uv pip install --python "$GCE_VENV/bin/python" --quiet "$GCE_SPEC"
        REINSTALLED=1
    fi
fi
# `globus-compute-endpoint list` reaches into texttable's private `_width`,
# which texttable 1.7 removed (AttributeError: 'Texttable' object has no
# attribute '_width'); keep the older texttable until the endpoint catches up.
uv pip install --python "$GCE_VENV/bin/python" --quiet "texttable<1.7"
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
CHANGED_CFG=0
if grep -q '^engine:' "$EP_DIR/config.yaml" 2>/dev/null; then
    # Endpoint 4.x refuses to start when config.yaml carries an `engine` block
    # ("move the engine block to user_config_template.yaml.j2"). Two ways to
    # get there: a legacy single-user endpoint (3.x layout: engine block in
    # config.yaml, no template at all — e.g. an endpoint created before the
    # package was upgraded), or a hand-edited config.yaml next to an existing
    # template. In the first case move the block, verbatim, into the template
    # (a template without expressions is plain YAML); in the second just drop
    # the stray copy. Every other top-level key stays; a backup is left behind.
    cp "$EP_DIR/config.yaml" "$EP_DIR/config.yaml.engine-block.bak"
    if [ ! -f "$TEMPLATE" ]; then
        echo "==> legacy layout: moving the engine block from config.yaml to $(basename "$TEMPLATE")"
        awk 'BEGIN{p=0} /^engine:/{p=1} p && !/^engine:/ && /^[^[:space:]#]/{p=0} p{print}' \
            "$EP_DIR/config.yaml.engine-block.bak" > "$TEMPLATE"
    else
        echo "==> config.yaml contains an engine block (not allowed with a UEP template) — removing it"
    fi
    awk 'BEGIN{skip=0} /^engine:/{skip=1; next} skip && /^[^[:space:]#]/{skip=0} !skip{print}' \
        "$EP_DIR/config.yaml.engine-block.bak" > "$EP_DIR/config.yaml"
    grep -q '^display_name:' "$EP_DIR/config.yaml" || echo "display_name: $ENDPOINT_NAME ($(hostname -s))" >> "$EP_DIR/config.yaml"
    CHANGED_CFG=1
fi
THREAD_ENV='export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1'
CHANGED=0
if [ -f "$TEMPLATE" ]; then
    cp -n "$TEMPLATE" "$TEMPLATE.bak" 2>/dev/null || true   # original, kept
    cp "$TEMPLATE" "$TEMPLATE.pre"                           # this run's before-image
    if grep -q "max_workers_per_node" "$TEMPLATE"; then
        sed -i.tmp -E "s/^([[:space:]]*max_workers_per_node:).*/\1 $MAX_WORKERS/" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    else
        sed -i.tmp -E "s/^([[:space:]]*type: GlobusComputeEngine)$/\1\n  max_workers_per_node: $MAX_WORKERS/" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    fi
    if grep -q 'worker_init: {{ worker_init|default() }}' "$TEMPLATE"; then
        sed -i.tmp "s|worker_init: {{ worker_init\|default() }}|worker_init: {{ worker_init\|default(\"$THREAD_ENV\") }}|" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    elif ! grep -q 'worker_init' "$TEMPLATE"; then
        # An engine block moved out of a legacy config.yaml has no worker_init:
        # give the provider one so the thread pinning applies there too.
        sed -i.tmp -E "s/^([[:space:]]*)type: LocalProvider$/\1type: LocalProvider\n\1worker_init: \"$THREAD_ENV\"/" "$TEMPLATE" && rm -f "$TEMPLATE.tmp"
    fi
    if ! cmp -s "$TEMPLATE" "$TEMPLATE.pre"; then CHANGED=1; fi
    rm -f "$TEMPLATE.pre"
    echo "==> UEP template: max_workers_per_node=$MAX_WORKERS, thread env pinned ($([ $CHANGED = 1 ] && echo updated || echo already set))"
fi
# Always restart: the template or the version may have changed, and a manager
# that was started by hand as `./globus-compute-endpoint` from inside the bin
# directory cannot launch its user endpoint process later (it re-execs its own
# argv[0] from another working directory: "Unable to start user endpoint
# process ... No such file or directory: './globus-compute-endpoint'"). The
# start below uses the absolute venv path.
"$GCE" stop "$ENDPOINT_NAME" > /dev/null 2>&1 || true

# Log in first, interactively, so the detached start below never has to
# prompt. Use the SAME Globus account as `make login` on your workstation.
if ! "$GCE" whoami > /dev/null 2>&1; then
    echo "==> Globus login for the endpoint (one-time; same account as \`make login\`)"
    "$GCE" login
fi

# Endpoint 4.x runs `start` in the FOREGROUND unless told to detach; a
# foreground endpoint dies with the SSH session. Always detach.
echo "==> starting endpoint '$ENDPOINT_NAME' (detached — survives logout)"
"$GCE" start --detach "$ENDPOINT_NAME"
sleep 3

echo
# The id is what sites.live.yaml needs; print it from the endpoint's own
# record so the table below is informational only (and may not crash the run).
EP_ID="$("$GCE_VENV/bin/python" -c "import json,sys; print(json.load(open(sys.argv[1]))['endpoint_id'])" "$EP_DIR/endpoint.json" 2>/dev/null || echo unknown)"
echo "==> endpoint '$ENDPOINT_NAME' on $(hostname -f 2>/dev/null || hostname): id $EP_ID"
"$GCE" list || true
echo
echo "Done. On your workstation: make endpoints && make init-sites (then make preflight)."
echo "After login-node maintenance, restart with (absolute path, never ./globus-compute-endpoint):"
echo "  $GCE start --detach $ENDPOINT_NAME"
echo "(optional, if the site allows systemd user services: $GCE enable-on-boot $ENDPOINT_NAME)"
