"""Bounded tool calling loop: plan -> validate & execute -> observe -> verify -> branch or conclude."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm import get_text_llm
from .base import ToolResult
from .dispatcher import ToolDispatcher
from .registry import CapabilityRegistry, get_registry
from .router import ActionRouter

logger = logging.getLogger(__name__)

TOOL_CALLING_PROMPT = """\
You are an intelligent tool-calling agent in NewJev.
Given a user goal, past observations, and available tools, choose the SINGLE best next tool to call or finish.

Available Tools:
{tools_manifest}

Past Steps and Observations:
{history}

Current Goal: {goal}
Evidence of Success Required: {evidence}

Respond with ONLY a JSON object:
If you need to call a tool:
{{"action": "call", "tool": "<tool_name>", "arguments": {{<arguments>}}, "rationale": "..."}}

If the goal is fully accomplished based on the evidence:
{{"action": "finish", "final_answer": "<concise markdown report or answer>", "rationale": "..."}}

If you need critical clarification from the user that cannot be resolved safely:
{{"action": "ask", "question": "<concise question>", "rationale": "..."}}
"""


@dataclass
class ToolLoopRecord:
    step: int
    tool: str | None
    arguments: dict[str, Any] | None
    output: Any
    success: bool
    rationale: str = ""


@dataclass
class ToolLoopResult:
    status: str  # "done" | "needs_user" | "blocked" | "max_steps" | "error"
    final_answer: str | None = None
    clarification_question: str | None = None
    steps: list[ToolLoopRecord] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class ToolLoop:
    """Bounded, safe loop that selects, executes, observes, and verifies registered capabilities."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        dispatcher: ToolDispatcher | None = None,
        router: ActionRouter | None = None,
        llm: Any = None,
        max_steps: int = 10,
    ) -> None:
        self.registry = registry or get_registry()
        self.dispatcher = dispatcher or ToolDispatcher(registry=self.registry)
        self.router = router or ActionRouter(registry=self.registry)
        self.llm = llm or get_text_llm()
        self.max_steps = max_steps

    def run(
        self,
        goal: str,
        evidence_required: list[str] | None = None,
        context: dict[str, Any] | None = None,
        emit_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> ToolLoopResult:
        context = context or {}
        evidence_req = evidence_required or ["answer_reported"]
        records: list[ToolLoopRecord] = []

        def emit(event: str, data: dict[str, Any]) -> None:
            if emit_callback is not None:
                try:
                    emit_callback(event, data)
                except Exception:
                    pass

        # 1. First consult action router
        route = self.router.route(goal, context=context)
        if route.route_type == "ask_user":
            emit("ask", {"question": route.clarification_question, "rationale": route.rationale})
            return ToolLoopResult(
                status="needs_user",
                clarification_question=route.clarification_question,
            )

        # 2. Bounded execution loop
        for step_idx in range(1, self.max_steps + 1):
            # Format history for LLM
            history_text = "\n".join(
                f"Step {r.step}: Called '{r.tool}' with {json.dumps(r.arguments)} -> "
                f"Success={r.success}: {str(r.output)[:500]}"
                for r in records
            ) or "(No prior actions taken)"

            tools_manifest = json.dumps(self.registry.export_schemas(filter_available=True), indent=2)

            prompt = TOOL_CALLING_PROMPT.format(
                tools_manifest=tools_manifest,
                history=history_text,
                goal=goal,
                evidence=", ".join(evidence_req),
            )

            # LLM selects next step
            try:
                resp = self.llm.complete([{"role": "system", "content": prompt}]).content
                resp_clean = re.sub(r"<think>[\s\S]*?</think>", "", resp).strip()
                fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", resp_clean, re.I)
                raw_json = fence.group(1).strip() if fence else resp_clean
                m = re.search(r"\{[\s\S]*\}", raw_json)
                decision = json.loads(m.group(0)) if m else {}
            except Exception as exc:
                logger.warning("ToolLoop decision parsing error: %s", exc)
                decision = {}

            action = decision.get("action", "")

            # A. Finish
            if action == "finish":
                final_ans = decision.get("final_answer", "Task complete.")
                from ..agent.verifier import Verifier
                verifier = Verifier(jev=None)
                hist = []
                for r in records:
                    op_name = (r.tool or "NOTE").upper()
                    if op_name in ("SEND_EMAIL", "EMAIL_SEND"):
                        op_name = "SEND_EMAIL"
                    elif op_name in ("SEND_MESSAGE", "MESSAGE_SEND"):
                        op_name = "SEND_MESSAGE"
                    hist.append({
                        "operation": op_name,
                        "text": json.dumps(r.arguments or {}),
                        "result": str(r.output),
                        "success": r.success,
                    })

                all_passed, score, details = verifier.verify_criteria(
                    criteria=evidence_req,
                    page=context.get("page", {}),
                    history=hist,
                    final_report=final_ans,
                )

                if not all_passed:
                    if step_idx < self.max_steps:
                        records.append(
                            ToolLoopRecord(
                                step=step_idx,
                                tool=None,
                                arguments=None,
                                output=f"Verification failed: {details['explanation']}. Criteria not yet met: {', '.join(evidence_req)}",
                                success=False,
                                rationale=decision.get("rationale", ""),
                            )
                        )
                        continue
                    else:
                        emit("finish", {"final_answer": final_ans, "steps": len(records), "verified": False})
                        return ToolLoopResult(
                            status="unverified",
                            final_answer=final_ans,
                            steps=records,
                            evidence={"verified": False, "score": score, "details": details},
                        )

                emit("finish", {"final_answer": final_ans, "steps": len(records), "verified": True})
                return ToolLoopResult(
                    status="done",
                    final_answer=final_ans,
                    steps=records,
                    evidence={"verified": True, "score": score, "details": details, "steps_count": len(records)},
                )

            # B. Ask user clarification
            if action == "ask":
                question = decision.get("question", "Could you please clarify the request?")
                emit("ask", {"question": question})
                return ToolLoopResult(
                    status="needs_user",
                    clarification_question=question,
                    steps=records,
                )

            # C. Call tool
            tool_name = decision.get("tool")
            tool_args = decision.get("arguments") or {}
            rationale = decision.get("rationale", "")

            if not tool_name:
                # If LLM didn't return a tool, check if router has a direct tool recommendation for step 1
                if step_idx == 1 and route.recommended_tool:
                    tool_name = route.recommended_tool
                    tool_args = route.tool_arguments or {}
                else:
                    return ToolLoopResult(
                        status="blocked",
                        final_answer="No further tools could make progress.",
                        steps=records,
                    )

            emit("step", {"step": step_idx, "tool": tool_name, "arguments": tool_args, "rationale": rationale})

            # Dispatch tool safely
            res: ToolResult = self.dispatcher.dispatch(tool_name, tool_args, context=context)
            rec = ToolLoopRecord(
                step=step_idx,
                tool=tool_name,
                arguments=tool_args,
                output=res.output if res.success else res.error,
                success=res.success,
                rationale=rationale,
            )
            records.append(rec)
            emit("result", {"step": step_idx, "success": res.success, "output": str(res.output)[:400]})

            # If tool was single-shot extraction or data op and succeeded
            if res.success and tool_name in ("extract_web_content", "read_file", "read_pdf", "read_table") and step_idx >= 2:
                # Give model chance to finish in next iteration
                continue

        # Reached max steps
        return ToolLoopResult(
            status="max_steps",
            final_answer="Reached maximum tool execution steps without final conclusion.",
            steps=records,
        )
