#!/usr/bin/env bash
# Run hpc_endpoint_setup.sh on a supercomputer over ONE SSH connection.
#
#   scripts/endpoint_setup_remote.sh <user@login-host> [endpoint-name]
#
# The old flow (scp, then ssh -t) opened two connections and therefore asked
# for the password/MFA twice. Here the setup script is shipped inline
# (base64) inside a single `ssh -t` command: you authenticate once, the
# script lands at ~/hpc_endpoint_setup.sh on the node (handy for re-runs
# after maintenance), and stdin stays your terminal so the interactive
# Globus login inside the script works.
set -euo pipefail

HOST="${1:?usage: endpoint_setup_remote.sh <user@login-host> [endpoint-name]}"
NAME="${2:-diamond-agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

B64=$(base64 < "$SCRIPT_DIR/hpc_endpoint_setup.sh" | tr -d '\n')

# Endpoint 4.x runs its workers in a venv matched to the SUBMITTING SDK's
# version. A manager newer than that venv passes options the matched worker
# pool does not know (seen: `--logconf`, exit 2, "workers failed to
# register"), so the endpoint is installed at exactly this workstation's
# globus-compute-sdk version. GCE_VERSION=x.y.z overrides.
GCE_VERSION="${GCE_VERSION:-$(cd "$SCRIPT_DIR/.." && uv run --no-sync python -c 'import importlib.metadata as m; print(m.version("globus-compute-sdk"))')}"
echo "==> endpoint version pinned to this workstation's globus-compute-sdk: $GCE_VERSION"

ENV="GCE_VERSION=$GCE_VERSION"
[ -n "${MAX_WORKERS:-}" ] && ENV="$ENV MAX_WORKERS=$MAX_WORKERS"
REMOTE="echo $B64 | base64 -d > \$HOME/hpc_endpoint_setup.sh && chmod +x \$HOME/hpc_endpoint_setup.sh"
REMOTE="$REMOTE && $ENV bash \$HOME/hpc_endpoint_setup.sh $NAME"

exec ssh -t "$HOST" "$REMOTE"
