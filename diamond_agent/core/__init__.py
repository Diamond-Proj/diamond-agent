"""diamond-agent-core — HPC interaction, task tracking and federation monitoring
behind one facade (``DiamondAgentCore``), with an HTTP client, a CLI and an
optional MCP server so other agents can drive it directly."""

from .client import CoreClient, CoreError
from .service import DiamondAgentCore, UnknownTarget, summarize_task

__all__ = ["DiamondAgentCore", "CoreClient", "CoreError", "UnknownTarget", "summarize_task"]
