"""Generate a ResourceEnvelope from a task description.

AI path: Claude reads the task name/description/command and proposes
[min viable, max useful] ranges plus compatibility constraints.
Fallback path: keyword heuristics good enough to demo the flow offline.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel

from ..schemas import ResourceEnvelope
from .ai import ask_structured

_SYSTEM = """You are the resource-envelope assistant of Diamond Agent, a meta-scheduler that
federates HPC systems (Delta A100/A40, DeltaAI GH200, Anvil A100, Frontera RTX5000/CPU,
Lonestar6 A100) behind Globus Compute.

Given a task, produce a ResourceEnvelope: the MINIMUM resources the task can run with and
the MAXIMUM it can usefully exploit. The scheduler will pick a concrete size inside the
interval, so make the interval honest — min too high blocks scheduling, max too high
wastes allocation. Estimate GPU memory needs from model sizes (rule of thumb: ~2GB per
billion params for inference in fp16, ~16-20GB per billion for full training; LoRA much
less). CPU-only work gets gpu_min_count=0. Only list accelerators that satisfy the
per-GPU memory floor. Walltimes: min = shortest useful run, max = generous cap with
checkpointing in mind."""


class EnvelopeDraft(BaseModel):
    gpu_min_count: int
    gpu_max_count: int
    gpu_min_memory_gb: int
    cpu_min_count: int
    cpu_max_count: int
    memory_min_gb: int
    memory_max_gb: int
    walltime_min_minutes: int
    walltime_max_minutes: int
    cpu_arch: list[str]
    accelerators: list[str]
    container_required: bool
    objective: str
    rationale: str
    confidence: float  # 0..1 — how well the task description pinned down the ranges


def _draft_to_envelope(d: EnvelopeDraft) -> ResourceEnvelope:
    objective = d.objective if d.objective in (
        "earliest_completion", "max_throughput", "min_queue_impact"
    ) else "earliest_completion"
    return ResourceEnvelope.model_validate(
        {
            "gpu": {
                "min_count": max(0, d.gpu_min_count),
                "max_count": max(d.gpu_min_count, d.gpu_max_count),
                "min_memory": f"{d.gpu_min_memory_gb}GB" if d.gpu_min_memory_gb > 0 else None,
            },
            "cpu": {"min_count": max(1, d.cpu_min_count), "max_count": max(1, d.cpu_max_count)},
            "memory": {"min": f"{d.memory_min_gb}GB", "max": f"{d.memory_max_gb}GB"},
            "walltime": {"min": f"{max(5, d.walltime_min_minutes)}m", "max": f"{d.walltime_max_minutes}m"},
            "compatibility": {
                "cpu_arch": d.cpu_arch or ["x86_64", "aarch64"],
                "accelerators": d.accelerators,
                "container_required": d.container_required,
            },
            "objective": objective,
        }
    )


def _heuristic(name: str, description: str, command: str) -> tuple[ResourceEnvelope, str]:
    text = f"{name} {description} {command}".lower()
    notes = []

    gpu_words = ("gpu", "cuda", "torch", "train", "llm", "vllm", "inference", "finetune",
                 "fine-tune", "diffusion", "model")
    needs_gpu = any(w in text for w in gpu_words)

    size_b = 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*b(?:illion)?\b", text)
    if m:
        size_b = float(m.group(1))
        notes.append(f"detected ~{size_b:g}B parameter model")

    if needs_gpu:
        heavy = "train" in text or "finetune" in text or "fine-tune" in text
        gpu_mem = 80 if size_b >= 30 else 40 if (size_b >= 7 or heavy) else 16
        gmin, gmax = (2, 8) if heavy or size_b >= 30 else (1, 4)
        env = {
            "gpu": {"min_count": gmin, "max_count": gmax, "min_memory": f"{gpu_mem}GB"},
            "cpu": {"min_count": 8, "max_count": 64},
            "memory": {"min": "64GB", "max": "512GB"},
            "walltime": {"min": "30m", "max": "4h" if not heavy else "12h"},
            "compatibility": {
                "cpu_arch": ["x86_64", "aarch64"],
                "accelerators": [a for a, mem in
                                 [("A40", 48), ("A100", 40), ("H100", 80), ("GH200", 96), ("RTX5000", 16)]
                                 if mem >= gpu_mem],
                "container_required": any(w in text for w in ("apptainer", "singularity", "shifter",
                                                               "podman", "container", "docker")),
            },
            "objective": "earliest_completion",
        }
        notes.append("GPU workload heuristics applied")
    else:
        env = {
            "gpu": {"min_count": 0, "max_count": 0},
            "cpu": {"min_count": 4, "max_count": 128},
            "memory": {"min": "16GB", "max": "256GB"},
            "walltime": {"min": "15m", "max": "2h"},
            "compatibility": {"cpu_arch": ["x86_64", "aarch64"], "accelerators": [],
                              "container_required": "container" in text or "docker" in text},
            "objective": "earliest_completion",
        }
        notes.append("no GPU keywords found; CPU-only envelope")
    return ResourceEnvelope.model_validate(env), "heuristic: " + "; ".join(notes)


def generate_envelope(
    name: str, description: str, command: str
) -> tuple[ResourceEnvelope, str, str, float]:
    """Returns (envelope, rationale, source, confidence); source is 'ai' or 'heuristic'."""
    user = (
        f"Task name: {name}\n"
        f"Description: {description or '(none)'}\n"
        f"Command: {command or '(none)'}\n\n"
        "Produce the resource envelope. objective must be one of "
        "earliest_completion | max_throughput | min_queue_impact. Set confidence in [0,1] "
        "to how completely the description pins down the resource needs."
    )
    draft = ask_structured(_SYSTEM, user, EnvelopeDraft)
    if draft is not None:
        try:
            confidence = min(1.0, max(0.0, draft.confidence))
            return _draft_to_envelope(draft), draft.rationale, "ai", confidence
        except Exception:
            pass
    env, why = _heuristic(name, description, command)
    return env, why, "heuristic", 0.5
