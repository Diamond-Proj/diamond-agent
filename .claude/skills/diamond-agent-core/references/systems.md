# Supported systems — quirks and caveats

One section per machine Diamond Agent knows how to drive. Read the section for your
target before the first submission to it in a session: it says which account form and
QOS a job needs, which `#SBATCH` directives the site rejects (and the catalog switch that
turns them off), where `python`/`torch` come from inside a job, how containers are
launched, and what trips people up on the login node.

Served by the running core at `GET /skill/systems`; `diamond-agent-core skill --systems`
prints it; MCP tool `system_notes`. Hardware shapes: `diamond_agent/data/known_machines.yaml`.
The switches themselves: `docs/ARCHITECTURE.md` §5.1; they are set per system in
`sites.live.yaml`.

## Common to every system

- **One login-node endpoint you own per machine.** Probes and `sbatch` run on the login
  node through it. Multi-user ("MEP") endpoints that sites publish are not usable.
- **Jobs start from a clean shell** in the system's workdir (default `$HOME/diamond`).
  Load modules and activate environments *inside the command*. stdout/stderr land in
  `<workdir>/logs/<task>.stdout|.stderr`.
- **Wall-time caps in the catalog are policy** (4 h on most live sites), not the site's
  limit: checkpoint and chunk. The live `sinfo` limit and the catalog cap are combined as
  the minimum.
- **The executor writes the `#SBATCH` lines.** You never add directives; you pick the
  account form, QOS and modules the system wants (below) and the catalog does the rest:

  | switch | level | effect |
  |---|---|---|
  | `account` | partition | bills this partition to a sub-account (`proj-gpu`, `m1234_g`) |
  | `qos` | partition | `#SBATCH --qos=…` |
  | `constraint` | partition | `#SBATCH --constraint=…` |
  | `slurm_partition` | partition | the `sinfo`/`squeue` name when the catalog name is logical |
  | `partition_directive: false` | system | no `#SBATCH --partition` (site routes by constraint + QOS) |
  | `mem_directive: false` | system | no `#SBATCH --mem` (whole-node sites) |
  | `gpu_directive: false` | system | no `#SBATCH --gpus-per-node` (sites that reject gres) |
  | `gres_probe: false` | system | skip the per-node GRES query in the probe (huge systems); free GPUs = idle nodes × GPUs/node |
  | `container_runtime` | system | how an image runs: `apptainer`/`singularity` → `exec --nv`; `shifter` → `#SBATCH --image` + `shifter`; `podman-hpc` → `run --gpu` |
  | `diamond_dir` | system | remote workdir (default `$HOME/diamond`) |

- **`env` first, then `exec` for anything specific.** `diamond-agent-core env <system>`
  returns host/arch, workdir, python/torch, modules of interest, container runtime and
  the Slurm accounts + QOS you hold. `exec` is bounded and for looking around only.
- **Endpoint will not start: "contains an `engine` field".** The endpoint directory has the
  3.x layout (engine block in `config.yaml`, no `user_config_template.yaml.j2`) and the
  package is 4.x. Re-run `make endpoint-setup HOST=<user>@<login host> NAME=<endpoint name>`:
  the script moves the block into the template (backup `config.yaml.engine-block.bak`)
  and starts the endpoint detached.
- **Verification status.** Sections say whether the shape was confirmed on a live run.
  Where it was not, the "Verify" list gives the one-line checks; `make preflight` warns
  when a catalog partition has no live `sinfo` data (wrong name or `slurm_partition`).

---

## Delta@NCSA

- **Reach**: `login.delta.ncsa.illinois.edu` (Duo MFA). Endpoint hostnames
  `*.delta.ncsa.illinois.edu` — but `gh-login*` / `dtai-*` are DeltaAI, not Delta.
- **Hardware** (x86_64, shared nodes): `gpuA100x4` 4× A100 40 GB, 64 CPUs, 256 GB,
  ~100 nodes · `gpuA40x4` 4× A40 48 GB, 64 CPUs, 256 GB, ~100 nodes · `cpu` 128 CPUs,
  256 GB, 132 nodes. Also on the machine, not in the shape: `gpuA100x8`, `gpuMI100x8`
  (AMD) — add only if your allocation has access.
- **Submitting**: GPU partitions reject jobs that request no GPU, so the executor always
  asks for ≥ 1 GPU per node there. `--mem` and `--gpus-per-node` are honoured (shared
  nodes). Allocations are usually split into GPU and CPU sub-accounts (`proj-gpu`,
  `proj-cpu`): set `account` per partition.
- **Software**: `module load pytorch-conda` gives torch; apptainer is available and pulls
  `docker://` images at run time.
- **Verify**: shape from the bundled catalog, counts from the live probe; nothing
  outstanding.

## DeltaAI@NCSA

- **Reach**: `gh-login01.delta.ncsa.illinois.edu` — an **aarch64** login node; the
  endpoint itself runs on arm64 (the setup script's uv-managed CPython handles it).
  Endpoint hostnames `gh-login*` / `dtai-*`.
- **Hardware**: `ghx4` 4× GH200 (120 GB variant per `scontrol`: `nvidia_gh200_120gb`),
  288 CPUs, 476 GB, ~150 nodes. Also `ghx4-interactive` (2 h cap), `test`, `full` — add
  if needed.
- **Submitting**: arm64 only — the envelope's `compatibility.cpu_arch` must include
  `aarch64`, and x86 wheels/containers will not run. `--mem`, `--gpus-per-node` honoured.
- **Software**: `module load python/miniforge3_pytorch` for torch; apptainer with
  arm64 image builds only.
- **Verify**: verified live (node shape via `scontrol`).

## Anvil@RCAC

- **Reach**: `anvil.rcac.purdue.edu` (lands on a `loginNN.anvil` node; Duo). Endpoint
  hostnames `*.anvil.rcac.purdue.edu`. Purdue also publishes an "Anvil Multi-User Globus
  Compute Endpoint" — unusable here; create your own with `make endpoint-setup`.
- **Hardware** (x86_64, shared nodes): `gpu` 4× A100 40 GB, 128 CPUs, 512 GB, 16 nodes ·
  `ai` 4× H100 80 GB, 96 CPUs, ~1 TB, 21 nodes.
- **Submitting**: every account is bound to exactly one QOS in the association table, so
  the per-partition `qos` (`gpu` / `ai`) is mandatory — the wrong QOS is rejected at
  submit. `sinfo` reports busy nodes as `mixed-` (the probe strips state flags).
- **Software**: the login node's `/usr/bin/python3` is 3.6 (the probe is written for it);
  apptainer present; module names for pytorch/conda via `env`.
- **Verify**: endpoint and `ai` shape verified live.

## Vista@TACC

- **Reach**: `vista.tacc.utexas.edu` (→ `login1`/`login2`, **aarch64**; TACC token MFA).
  Endpoint hostnames `*.vista.tacc.utexas.edu`.
- **Hardware**: `gh` 1× GH200 96 GB, 72 CPUs, 120 GB, 576 nodes · `gh-dev` same, 20 nodes
  (dev queue: fast turnaround, use it for smoke tests: `--target Vista@TACC:gh-dev`) ·
  `gg` Grace-Grace CPU, 144 CPUs, 240 GB, 251 nodes. `gb` (Grace-Blackwell, 18 nodes)
  exists; add once your allocation has access.
- **Submitting**: whole-node allocation. TACC rejects `--mem` → `mem_directive: false`;
  rejects gres directives ("Invalid generic resource specification") →
  `gpu_directive: false`. The submit filter validates the project name as the portal
  spells it (**uppercase**) although `sacctmgr` prints it lowercase — `accounts` must be
  the uppercase form.
- **Software**: no torch module — bring a conda env or an arm64 container (apptainer).
- **Login node**: `ulimit -u 100`, and it counts **threads**. The endpoint setup script
  pins uv/BLAS thread pools; VS Code remote servers on the same login node eat the
  budget. "can't start new thread" → `ps -u $USER -o nlwp= | awk '{s+=$1} END {print s}'`.
- **Verify**: verified live.

## Frontera@TACC

- **Reach**: `frontera.tacc.utexas.edu`. Endpoint hostnames `*.frontera.tacc.utexas.edu`.
- **Hardware** (x86_64): `rtx` 4× Quadro RTX 5000 16 GB, 32 CPUs, 128 GB, 84 nodes ·
  `normal` 56 CPUs (Cascade Lake), 192 GB, ~8000 nodes. 48 h catalog caps.
- **Submitting**: TACC conventions (whole nodes, uppercase project name) are expected to
  apply; the bundled shape does not yet switch anything off. If `sbatch` rejects `--mem`
  or gres, set `mem_directive: false` / `gpu_directive: false` as on Vista.
- **Login node**: TACC thread limits as on Vista.
- **Verify**: no live run recorded in this repo — `make preflight`, then
  `make smoke SYSTEM=Frontera@TACC`.

## Lonestar6@TACC

- **Reach**: `ls6.tacc.utexas.edu`. Endpoint hostnames `*.ls6.tacc.utexas.edu`.
- **Hardware** (x86_64): `gpu-a100` 3× A100 40 GB, 128 CPUs, 256 GB, 84 nodes. A
  `gpu-h100` queue exists; not in the shape.
- **Submitting / login node**: as Frontera (TACC conventions, not yet switched on).
- **Verify**: no live run recorded — `make preflight`, then `make smoke SYSTEM=Lonestar6@TACC`.

## Perlmutter@NERSC

- **Reach**: `perlmutter.nersc.gov` (→ `loginNN`). SSH needs NERSC MFA: password + OTP on
  every connection, or a 24 h key from `sshproxy.sh`; `make endpoint-setup` uses a single
  SSH connection, so one prompt. The endpoint reports its hostname as the internal name
  **`loginNN.chn`** (no FQDN) — `make endpoints` maps `login\d+.chn` to Perlmutter@NERSC.
  `perlmutter.nersc.gov` load-balances across ~40 login nodes; the endpoint directory lives
  on the shared home filesystem, so (re)starting it from any login node works — one
  instance at a time.
- **Hardware** (x86_64, AMD EPYC 7763 "Milan"; whole nodes under `regular`/`debug`):
  GPU nodes 4× A100 **40 GB** (1,536 nodes) and 4× A100 **80 GB** (256 nodes, feature
  `hbm80g`), 1 CPU = 64 cores = **128 hardware threads** (Slurm counts threads), 256 GB ·
  CPU nodes 2 CPUs = 128 cores = 256 threads, 512 GB, 3,072 nodes. Logical partitions in
  the shape: `gpu` (→ Slurm `gpu_ss11`, `-C gpu`, `-q regular`) and `cpu`
  (→ `regular_milan_ss11`, `-C cpu`, `-q regular`). `sites.example.yaml` has optional
  `gpu-hbm80g` (`-C gpu&hbm80g`, 80 GB) and `gpu-debug` (`-q debug`) blocks.
  *Verified live (2026-10-01)*: `sinfo` shows `gpu_ss11` = 1,536 nodes with features
  `gpu,ss11,a100,hbm40g` + 256 with `hbm80g` (`gpu:a100:4`), `regular_milan_ss11` = 3,002
  nodes `cpu,milan,ss11`; `shared_gpu_ss11` also exists (shared QOS).
- **Submitting**:
  - *Routing*: NERSC selects the node type with `-C gpu|cpu` and the queue with
    `-q <qos>`; its job_submit plugin assigns the Slurm partition. Hence
    `partition_directive: false` (no `--partition`), and per-partition `constraint` +
    `qos`. The Slurm partition names appear only as `slurm_partition`, for matching
    `sinfo`/`squeue` in the probe. *Verified*: a job rendered with `--constraint=gpu
    --qos=debug --account=m4431_g --gpus-per-node=1` was accepted and shows
    `Partition=gpu_ss11 QOS=gpu_debug Features=gpu&a100` in `scontrol` — the plugin
    remaps the user-facing QOS (`regular`/`debug`/`shared`) to internal ones
    (`gpu_regular`, `gpu_debug`, `regular_1`, …); `sacctmgr show qos regular` is empty
    because `regular` is virtual.
  - *Accounts*: GPU jobs **must** use the GPU form of the allocation, `mXXXX_g`; CPU
    jobs use `mXXXX`. The wrong suffix is rejected at submit. `init-sites` pre-fills
    `TODO-your-allocation_g` on the GPU entry; fill both forms. Check which GPU account
    can actually run: `sacctmgr -nP show assoc user=$USER format=account,qos` — a `_g`
    account whose QOS list lacks `gpu_regular`/`gpu_debug` (e.g. an exhausted GPU
    allocation; seen live: `m4410_g` had only `overrun,xfer,…`) cannot run GPU jobs.
  - *QOS*: `regular` (production), `debug` (short, small, fast turnaround — tests only, by
    NERSC policy), `shared` (a fraction of a node), `preempt`, `premium`, `interactive`.
    Walltime/node limits per QOS are NERSC's queue policy and change over time — the
    catalog `max_walltime` is Diamond Agent's cap, not NERSC's. Live on 2026-10-01
    (`sacctmgr show qos`): `debug` 30 min, 2 running / 5 submitted per user;
    `interactive` 4 h; `shared`, `preempt`, `premium` 48 h.
  - *Auto placement and debug*: with a `gpu-debug` entry in the catalog the meta-scheduler
    will pick it for any GPU job ≤ 30 min (verified: the AI ranked it first because it
    bypasses the ~5,000-job `regular` backlog). NERSC reserves debug for development and
    testing — keep the entry only while testing, or pin to it explicitly.
  - *Memory*: `regular`/`debug` allocate whole nodes → `mem_directive: false` (a `--mem`
    below node size would only cap the job's cgroup). The switch is system-wide: a
    `shared` QOS entry on the same system also goes without `--mem`.
  - *GPUs*: `--gpus-per-node=N` is NERSC's recommended form; `gpu_directive` stays on.
    **Whole-node QOS bill the whole node**: under `regular`/`debug` a job that asks for 1 GPU
    still gets (and is charged for) the full node — `scontrol` showed the allocation as
    `cpu=128, gres/gpu=4, billing=128` for a `--gpus-per-node=1` job, and
    `CUDA_VISIBLE_DEVICES=0,1,2,3` inside it. Size GPU work in whole nodes, or add a
    `shared` QOS entry for sub-node jobs. `-C gpu` alone may land on an 80 GB node (the
    constraint matches both kinds); `gpu-hbm80g` (`gpu&hbm80g`) guarantees 80 GB, and
    `gpu&hbm40g` would pin the 40 GB kind.
  - *Probe*: `sinfo -N … GresUsed` over ~5,000 nodes takes longer than 60 s on Perlmutter
    (verified live: the first probe timed out there), so the shape sets `gres_probe: false`;
    "free now" for `gpu` is idle nodes × 4, which is exact under whole-node QOS.
  - *Charging*: GPU node-hours bill the `_g` allocation; GPU and CPU hours are separate budgets.
- **Software** (*module names verified live*): `module load pytorch` → `pytorch/2.13.0`
  (default; also 2.6.0, 2.8.0, 2.11.0), `tensorflow/2.15.0`, `python/3.9…3.13-<release>`,
  `conda/Miniforge3-*`, `cray-python/3.11|3.12`, `cudatoolkit/12.2…13.2`, `nccl/2.18…2.24`.
  The login node's `/usr/bin/python3` is 3.6.15 (the probe runs on it). Containers:
  **no apptainer**; `/usr/bin/shifter`, `/usr/bin/shifterimg`, `/usr/bin/podman-hpc` are present
  (`shifterimg pull docker:ubuntu:22.04` took ~16 s to READY).
  *Shifter* (`container_runtime: shifter`, the default in the shape): give the image as
  `docker://repo/image:tag` — the executor rewrites it to Shifter's `docker:repo/image:tag`,
  adds `#SBATCH --image=…` and runs `srun shifter <command>`; pull first on the login node
  with `shifterimg pull docker:repo/image:tag`. *podman-hpc* (`container_runtime:
  podman-hpc`): run `podman-hpc pull <image> && podman-hpc migrate <image>` on the login
  node first; the job runs `podman-hpc run --rm --gpu <image> <command>`.
- **Storage**: `$HOME` (`/global/homes/<initial>/<user>`) has a small quota — fine for the
  default workdir `$HOME/diamond` (scripts and logs). Data and checkpoints belong in
  `$SCRATCH` (`/pscratch/sd/<initial>/<user>`, purged of files unused for ~8 weeks) or
  `$CFS/<project>` (`/global/cfs/cdirs/<project>`).
  `diamond_dir: /pscratch/sd/<initial>/<user>/diamond` moves the workdir there (and under
  the purge policy).
- **Login node**: per-user limits are cgroup CPU/memory shares, not a tiny `ulimit -u`;
  the endpoint is light. Login nodes reboot at the monthly maintenance — restart the
  endpoint afterwards (`ssh perlmutter.nersc.gov '~/.gce-venv/bin/globus-compute-endpoint
  start --detach <endpoint name>'`), or let NERSC's `scrontab` do it.
- **Verified live (2026-10-01)**: endpoint recognition, probe (with `gres_probe: false`),
  `env`, `exec`, `put-file`, partition names and features, `_g` accounts, constraint + QOS
  routing, `--gpus-per-node`, no `--partition`/`--mem`, Shifter `--image` rendering, pinned
  and auto placement onto `gpu-debug` (see the run results at the end of this section).
  Re-check after NERSC maintenance with:
  `diamond-agent-core exec Perlmutter@NERSC 'sinfo -h -o "%R|%D|%f|%G" | sort -u; sacctmgr -nP show assoc user=$USER format=account,qos'`
  and `make smoke SYSTEM=Perlmutter@NERSC` (1 CPU, 5 min on `cpu`, cancelled right away).
- **Run results (2026-10-01, debug QOS, ~8 min queue wait each)**:
  - Raw command, pinned to `gpu-debug`: ran on an A100-40GB node as `Partition=gpu_ss11
    QOS=gpu_debug Account=m4431_g Features=gpu&a100`; `nvidia-smi` saw 4 GPUs; the
    monitor reported COMPLETED via `sacct`, `logs` returned both files.
  - Shifter, pinned to `gpu-debug`, image `docker://ubuntu:22.04`: `#SBATCH
    --image=docker:ubuntu:22.04` + `srun --cpu-bind=none shifter bash -c "…"` ran Ubuntu
    22.04.5 with `nvidia-smi -L` working inside (only the step's 1 GPU visible: `srun`
    inherits `--gpus-per-node=1`). Pull the image on the login node first
    (`shifterimg pull docker:ubuntu:22.04`, ~16 s).
  - Auto placement (envelope x86_64 + A100 40 GB, 10 min): 7 candidates, 3 feasible
    (Vista excluded by `cpu_arch`/`accelerator`, `cpu` by `accelerator`); the AI ranked
    `gpu-debug` first; the job ran on an **80 GB** node (`-C gpu` matches both kinds).
  - Torch on GPU, pinned to `gpu-debug`: `module load pytorch` → `pytorch/2.13.0`
    (`/global/common/software/nersc9/pytorch/2.13.0/bin/python`, Python 3.12),
    `torch 2.13.0+cu130`, `cuda True`, 4 devices, 8192² matmul ×10 in 0.77 s; stderr empty.
  - Gotcha found by the test script itself: `module load pytorch 2>&1 | tail` runs the
    `module` function in a pipeline subshell and loses the PATH change (`python: command
    not found`). Load modules on their own line: `module load pytorch >/dev/null 2>&1`.
