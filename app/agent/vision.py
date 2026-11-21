"""
Vision fallback: when an app exposes few accessible controls (games, canvas apps, remote desktops, some
Electron/Java apps), locate the element to click on a screenshot with an image-capable model.
Coordinates come back in screenshot pixels and are mapped to screen (or page) coordinates by the capture.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VISION_PROMPT = """\
You locate one UI element on a screenshot for a computer-use agent.
The image is {w}x{h} pixels, origin at the top-left. Find: "{target}".
Context (the user's goal): {goal}
Reply only with JSON: {{"found": true, "x": <int>, "y": <int>, "what": "<what you see there>"}}
x,y must be the CENTER of the element, in image pixels. If it is not visible:
{{"found": false, "what": "<what is on screen instead, briefly>"}}
"""

CLICK_ELEMENT_KINDS = ("click", "fill", "select")


def element_count(page: dict[str, Any]) -> int:
    return sum(1 for a in page.get("actions", []) if a.get("kind") in CLICK_ELEMENT_KINDS)


def parse_target(description: str) -> tuple[str, str]:
    """'the Play button (double-click)' -> ('the Play button', 'double')."""
    d = description.strip()
    button = "left"
    if re.search(r"\(?\bdouble[- ]?click\b\)?", d, re.I):
        button = "double"
    elif re.search(r"\(?\bright[- ]?click\b\)?", d, re.I):
        button = "right"
    d = re.sub(r"\s*\((double|right)[- ]?click\)\s*", " ", d, flags=re.I).strip()
    return d or description, button


def parse_point(content: str, w: int, h: int) -> dict[str, Any] | None:
    content = re.sub(r"<think>[\s\S]*?</think>", "", content or "")
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("found"):
        return {"found": False, "what": str(data.get("what", ""))[:200]}
    try:
        x, y = float(data["x"]), float(data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if 0 <= x <= 1 and 0 <= y <= 1 and (w > 2 and h > 2):     # some models answer in fractions
        x, y = x * w, y * h
    if not (0 <= x < w and 0 <= y < h):
        return None
    return {"found": True, "x": int(x), "y": int(y), "what": str(data.get("what", ""))[:200]}


def locate(llm: Any, shot: dict[str, Any], target: str, goal: str) -> dict[str, Any] | None:
    """shot = {"b64", "width", "height", "left", "top", "scale"} from capture.vision_shot()."""
    w, h = int(shot["width"]), int(shot["height"])
    prompt = VISION_PROMPT.format(w=w, h=h, target=target.replace('"', "'"), goal=goal[:400])
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot['b64']}"}},
    ]}]
    try:
        resp = llm.complete(messages)
    except Exception as exc:
        logger.warning("Vision model failed: %s", exc)
        return None
    pt = parse_point(resp.content, w, h)
    if pt and pt.get("found"):
        scale = float(shot.get("scale") or 1.0) or 1.0
        pt["screen_x"] = int(shot.get("left", 0) + pt["x"] / scale)
        pt["screen_y"] = int(shot.get("top", 0) + pt["y"] / scale)
    return pt
