"""Capability Registry for NewJev tools and connectors."""

from __future__ import annotations

import logging
from typing import Any

from .base import Tool, ToolRisk

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """Central registry of all tools, capabilities, and connectors."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool in the capability registry."""
        if tool.name in self._tools:
            logger.info("Overwriting existing tool registration: %s", tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self, filter_available: bool = False) -> list[Tool]:
        """Return registered tools, optionally filtering by current runtime availability."""
        tools = list(self._tools.values())
        if filter_available:
            return [t for t in tools if t.is_available()[0]]
        return tools

    def export_schemas(self, filter_available: bool = True) -> list[dict[str, Any]]:
        """Export tool definitions as OpenAPI / LLM function calling schemas."""
        tools = self.list_tools(filter_available=filter_available)
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": f"{t.description} (Risk: {t.risk_level.value}, Verification: {t.verification_method})",
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def export_manifest(self) -> dict[str, Any]:
        """Export comprehensive capability metadata."""
        return {
            "tools": [t.schema() for t in self.list_tools(filter_available=False)],
            "counts": {
                "total": len(self._tools),
                "by_risk": {
                    level.value: sum(1 for t in self._tools.values() if t.risk_level == level)
                    for level in ToolRisk
                },
            },
        }


# Global default registry
_default_registry = CapabilityRegistry()


def get_registry() -> CapabilityRegistry:
    return _default_registry
