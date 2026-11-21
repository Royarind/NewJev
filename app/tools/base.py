"""Base definitions for NewJev's Tool Calling and Capability Registry."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolRisk(str, enum.Enum):
    """Risk tier of a capability."""
    READ_ONLY = "read_only"
    SAFE_WRITE = "safe_write"
    EXTERNAL_COMMUNICATION = "external_communication"
    DESTRUCTIVE = "destructive"


@dataclass
class ToolResult:
    """Structured result from a tool execution."""
    success: bool
    output: Any = None
    error: str | None = None
    evidence: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "evidence": self.evidence,
            "metadata": self.metadata,
        }


class Tool:
    """Declarative capability specification."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        risk_level: ToolRisk = ToolRisk.READ_ONLY,
        requirements: list[str] | None = None,
        preconditions: list[str] | None = None,
        side_effects: list[str] | None = None,
        verification_method: str = "result_check",
        handler: Callable[..., Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.risk_level = risk_level
        self.requirements = requirements or []
        self.preconditions = preconditions or []
        self.side_effects = side_effects or []
        self.verification_method = verification_method
        self._handler = handler

    def is_available(self) -> tuple[bool, str]:
        """Check if prerequisites/dependencies for this tool are met."""
        return True, ""

    def execute(self, args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
        """Run the tool with the validated arguments."""
        if self._handler is None:
            raise NotImplementedError(f"Tool {self.name} has no execution handler")
        try:
            res = self._handler(args, context=context)
            if isinstance(res, ToolResult):
                return res
            return ToolResult(success=True, output=res)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    def schema(self) -> dict[str, Any]:
        """Export tool description and schema in OpenAPI/JSON-Schema compatible format."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk_level": self.risk_level.value,
            "requirements": self.requirements,
            "preconditions": self.preconditions,
            "side_effects": self.side_effects,
            "verification_method": self.verification_method,
        }
