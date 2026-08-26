#!/usr/bin/env bash
# Diamond Agent — per-supercomputer endpoint setup (Phase 0, run ON the login node).
#
#   scp scripts/hpc_endpoint_setup.sh <machine>:
#   ssh -t <machine> 'bash hpc_endpoint_setup.sh'
#
# Bootstraps via uv (which downloads its own managed CPython), so it works
# even when the system python3 is ancient and without touching the module
# system. Starts an endpoint whose workers run on the login node (default
# LocalProvider) — right for Diamond Agent's probe/sbatch shell commands.
# First start opens a Globus login: use the SAME account as `make login`.

set -euo pipefail

ENDPOINT_NAME="${1:-diamond-agent}"
GCE_VENV="${GCE_VENV:-$HOME/.gce-venv}"
PYVER="${PYVER:-3.12}"

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

if [ ! -d "$HOME/.globus_compute/$ENDPOINT_NAME" ]; then
    echo "==> configuring endpoint '$ENDPOINT_NAME' (default LocalProvider config)"
    "$GCE" configure "$ENDPOINT_NAME"
fi

echo "==> starting endpoint '$ENDPOINT_NAME' (first run opens a Globus login)"
"$GCE" start "$ENDPOINT_NAME"

echo
"$GCE" list
echo
echo "Done. Copy this endpoint's UUID into sites.live.yaml on your workstation,"
echo "then run 'make preflight' there. After login-node maintenance, re-run:"
echo "  $GCE start $ENDPOINT_NAME"
