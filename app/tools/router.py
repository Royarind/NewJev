"""Intelligent Action Router — selects the optimal route among direct tools, browser automation, desktop UI, or user clarification."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .base import ToolRisk
from .registry import CapabilityRegistry, get_registry

logger = logging.getLogger(__name__)

DIRECT_CAPABILITY_PATTERNS = {
    "read_table": re.compile(r"\b(read|summary|summarize|column|sheet|rows?)\b.*\b(csv|xlsx?|spreadsheet|table)\b", re.I),
    "transform_table": re.compile(r"\b(sort|filter|aggregate|totals?|convert)\b.*\b(csv|xlsx?|table)\b", re.I),
    "read_pdf": re.compile(r"\b(read|extract|content of|text of)\b.*\b\.?pdf\b", re.I),
    "search_pdf": re.compile(r"\b(search|find|locate)\b.*\b\.?pdf\b", re.I),
    "read_file": re.compile(r"\b(read|view|content of|open and read)\b.*\b(\.txt|\.md|\.json|\.csv|\.py|file)\b", re.I),
    "search_files": re.compile(r"\b(find|search for|locate)\b.*\b(file|folder|document|invoice|report)\b", re.I),
    "semantic_search": re.compile(r"\b(find the document about|search for notes on|passages? about|information on)\b", re.I),
    "extract_web_content": re.compile(r"\b(extract|scrape|get (the )?content of|read (the )?page)\b.*https?://", re.I),
    "send_email": re.compile(r"\b(send|draft) (an? )?email\b", re.I),
    "search_email": re.compile(r"\b(search|find|check) (my )?(e-?mails?|inbox)\b", re.I),
    "send_message": re.compile(r"\b(send|post) (a )?(slack|teams|chat) message\b", re.I),
}


@dataclass
class RouteDecision:
    """Outcome of action router analysis."""
    route_type: str  # "direct_tool" | "browser" | "desktop" | "ask_user" | "inspect_first"
    recommended_tool: str | None = None
    tool_arguments: dict[str, Any] | None = None
    rationale: str = ""
    clarification_question: str | None = None
    confidence: float = 0.9


class ActionRouter:
    """Intelligently routes goals to the most direct, reliable, and secure execution route."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or get_registry()

    def route(self, goal: str, context: dict[str, Any] | None = None) -> RouteDecision:
        context = context or {}
        text = goal.strip()

        # 1. Check for material missing information that requires asking the user
        if re.search(r"\b(send (an? )?email|email (them|him|her))\b", text, re.I) and not re.search(r"[\w.-]+@[\w.-]+\.\w+", text):
            return RouteDecision(
                route_type="ask_user",
                clarification_question="Who should I send this email to? Please provide the recipient email address.",
                rationale="Email sending requested but recipient address is not specified.",
                confidence=1.0,
            )

        if re.search(r"\b(send (a )?(chat |slack |teams )?message)\b", text, re.I) and not re.search(r"\b(to\s+[@#\w-]+|in\s+[#\w-]+)\b", text, re.I):
            return RouteDecision(
                route_type="ask_user",
                clarification_question="Which person or channel should I send this message to?",
                rationale="Message sending requested without a specified destination channel or recipient.",
                confidence=1.0,
            )

        # 2. Check for inspection-first opportunities (e.g. "latest invoice", "organize it", "check if exists")
        if re.search(r"\b(get (the )?(latest|newest)|find (the )?newest|organize)\b", text, re.I):
            return RouteDecision(
                route_type="inspect_first",
                recommended_tool="search_files",
                tool_arguments={"query": "invoice" if "invoice" in text.lower() else "*"},
                rationale="Request requires discovering the latest or target item before deciding on subsequent action.",
                confidence=0.85,
            )

        # 2b. Check for ecommerce shopping or checkout
        try:
            from ..ecommerce.bridge import is_ecommerce_goal, extract_ecommerce_details
            if is_ecommerce_goal(text):
                details = extract_ecommerce_details(text)
                tool_name = "ecommerce_checkout" if details.get("is_checkout") and details.get("url") else "ecommerce_shop"
                return RouteDecision(
                    route_type="direct_tool",
                    recommended_tool=tool_name,
                    tool_arguments={"query": details.get("query", text), **details},
                    rationale="Goal matches agentic ecommerce shopping, product search, or checkout.",
                    confidence=0.95,
                )
        except Exception as e:
            logger.debug("Ecommerce routing check: %s", e)

        # 3. Check for direct tool matches (faster, deterministic, avoids GUI overhead)
        for tool_name, pattern in DIRECT_CAPABILITY_PATTERNS.items():
            if pattern.search(text):
                tool = self.registry.get(tool_name)
                if tool is not None and tool.is_available()[0]:
                    args = self._infer_tool_args(tool_name, text)
                    return RouteDecision(
                        route_type="direct_tool",
                        recommended_tool=tool_name,
                        tool_arguments=args,
                        rationale=f"Goal matches direct API capability '{tool_name}' which is faster and more reliable than UI automation.",
                        confidence=0.9,
                    )

        # 4. Check for Web Browser Automation vs Desktop UI
        from ..agent.planner import guess_mode
        mode = guess_mode(text)
        return RouteDecision(
            route_type=mode,
            rationale=f"Goal involves interactive {mode} automation.",
            confidence=0.75,
        )

    def _infer_tool_args(self, tool_name: str, text: str) -> dict[str, Any]:
        """Extract baseline arguments from request text."""
        args: dict[str, Any] = {}
        # Match quoted or unquoted paths / filenames
        file_match = re.search(r"[\"']([^\"']+\.[a-zA-Z0-9]{2,5})[\"']|(?<![\w/\\])([\w\-.()]+\.[a-zA-Z0-9]{2,5})\b", text)
        matched_file = file_match.group(1) or file_match.group(2) if file_match else None

        if matched_file:
            args["path"] = matched_file

        if tool_name in ("search_files", "semantic_search"):
            q_match = re.search(r"(?:about|for|named)\s+[\"']?([^\"',\n]+)[\"']?", text, re.I)
            args["query"] = q_match.group(1).strip() if q_match else (matched_file or text)

        if tool_name == "transform_table":
            for op in ("sort", "filter", "aggregate", "convert"):
                if re.search(rf"\b{op}\b", text, re.I):
                    args["operation"] = op
                    break
            args.setdefault("operation", "summary")

        if tool_name == "send_email":
            email_match = re.search(r"[\w.-]+@[\w.-]+\.\w+", text)
            if email_match:
                args["to"] = email_match.group(0)

        if tool_name == "extract_web_content":
            url_match = re.search(r"https?://\S+", text)
            if url_match:
                args["url"] = url_match.group(0).rstrip(".,)")

        return args
