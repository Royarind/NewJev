"""NewJev Capability Registry and Tool Calling System."""

from .base import Tool, ToolResult, ToolRisk
from .dispatcher import ToolDispatcher
from .registry import CapabilityRegistry, get_registry
from .builtin import register_builtin_tools
from .router import ActionRouter, RouteDecision
from .loop import ToolLoop, ToolLoopResult, ToolLoopRecord

__all__ = [
    "Tool",
    "ToolResult",
    "ToolRisk",
    "CapabilityRegistry",
    "get_registry",
    "ToolDispatcher",
    "register_builtin_tools",
    "ActionRouter",
    "RouteDecision",
    "ToolLoop",
    "ToolLoopResult",
    "ToolLoopRecord",
]
