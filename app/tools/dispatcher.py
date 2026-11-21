"""Dispatcher for validated, sandboxed, and permissioned tool execution."""

from __future__ import annotations

import concurrent.futures
import json
import logging
from typing import Any, Callable

from .base import ToolResult, ToolRisk
from .registry import CapabilityRegistry, get_registry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_CHARS = 50_000


def validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Lightweight JSON schema validation without external dependencies."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Arguments must be a JSON object / dictionary"]

    # Check required fields
    required = schema.get("required", [])
    for field in required:
        if field not in data or data[field] is None:
            errors.append(f"Missing required parameter: '{field}'")

    props = schema.get("properties", {})
    for key, val in data.items():
        if key not in props:
            # Tolerant of extra args unless additionalProperties is False
            if schema.get("additionalProperties") is False:
                errors.append(f"Unexpected parameter: '{key}'")
            continue

        p_info = props[key]
        expected_type = p_info.get("type")
        if expected_type == "string" and not isinstance(val, str):
            errors.append(f"Parameter '{key}' must be a string, got {type(val).__name__}")
        elif expected_type == "integer" and not (isinstance(val, int) and not isinstance(val, bool)):
            errors.append(f"Parameter '{key}' must be an integer, got {type(val).__name__}")
        elif expected_type == "number" and not (isinstance(val, (int, float)) and not isinstance(val, bool)):
            errors.append(f"Parameter '{key}' must be a number, got {type(val).__name__}")
        elif expected_type == "boolean" and not isinstance(val, bool):
            errors.append(f"Parameter '{key}' must be a boolean, got {type(val).__name__}")
        elif expected_type == "array" and not isinstance(val, list):
            errors.append(f"Parameter '{key}' must be an array / list, got {type(val).__name__}")
        elif expected_type == "object" and not isinstance(val, dict):
            errors.append(f"Parameter '{key}' must be an object / dict, got {type(val).__name__}")

        if "enum" in p_info and val not in p_info["enum"]:
            errors.append(f"Parameter '{key}' must be one of {p_info['enum']}, got '{val}'")

    return errors


class ToolDispatcher:
    """Dispatches tool execution requests safely with validation, timeouts, and reviewer confirmations."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        confirm_callback: Callable[[str, str, dict[str, Any]], bool] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.registry = registry or get_registry()
        self.confirm_callback = confirm_callback
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    def dispatch(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Validate, check permissions, and execute a tool safely."""
        # 1. Parse arguments if passed as JSON string
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"Invalid JSON arguments: {exc}",
                )

        # 2. Check tool exists
        tool = self.registry.get(name)
        if tool is None:
            available = [t.name for t in self.registry.list_tools()]
            return ToolResult(
                success=False,
                error=f"Tool '{name}' is not registered. Available tools: {', '.join(available)}",
            )

        # 3. Check runtime availability
        available, reason = tool.is_available()
        if not available:
            return ToolResult(
                success=False,
                error=f"Tool '{name}' is currently unavailable: {reason}",
            )

        # 4. Schema validation
        schema_errors = validate_schema(arguments, tool.parameters)
        if schema_errors:
            return ToolResult(
                success=False,
                error=f"Invalid parameters for '{name}': " + "; ".join(schema_errors),
            )

        # 5. Permission / confirmation check (Fail-closed)
        if tool.risk_level in (ToolRisk.EXTERNAL_COMMUNICATION, ToolRisk.DESTRUCTIVE):
            if self.confirm_callback is None:
                return ToolResult(
                    success=False,
                    error=f"Tool '{name}' requires confirmation ({tool.risk_level.value}) but no confirmation callback is available (fail-closed).",
                    evidence={"status": "confirmation_unavailable"},
                )
            summary = (
                f"Tool '{name}' will perform a {tool.risk_level.value} action: "
                f"{json.dumps(arguments, ensure_ascii=False)}"
            )
            approved = self.confirm_callback(tool.risk_level.value, summary, arguments)
            if not approved:
                return ToolResult(
                    success=False,
                    error=f"User rejected execution of {tool.risk_level.value} tool '{name}'.",
                    evidence={"status": "rejected_by_user"},
                )

        # 6. Sandboxed execution with timeout (guaranteed not to hang on shutdown)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(tool.execute, arguments, context)
        try:
            res = future.result(timeout=self.timeout)
            executor.shutdown(wait=False, cancel_futures=True)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            return ToolResult(
                success=False,
                error=f"Tool '{name}' timed out after {self.timeout}s",
            )
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            logger.error("Tool execution failed for %s: %s", name, exc)
            return ToolResult(
                success=False,
                error=f"Execution error in '{name}': {exc}",
            )

        # 7. Truncate oversized output if needed to protect context window
        if res.output and isinstance(res.output, str) and len(res.output) > self.max_output_chars:
            res.output = (
                res.output[: self.max_output_chars]
                + f"\n... [Output truncated: exceeded {self.max_output_chars} characters]"
            )

        return res
