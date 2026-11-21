"""Spreadsheet helpers: cell addresses and parsing the TYPE_TABLE value ("A1" line + tab-separated rows)."""

from __future__ import annotations

import json
import re
from typing import Any

_ADDR = re.compile(r"^\s*(?:(?P<sheet>[^!]+)!)?\$?(?P<col>[A-Za-z]{1,3})\$?(?P<row>\d{1,7})\s*$")


def col_to_index(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def index_to_col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def parse_address(addr: str) -> tuple[str | None, str, int] | None:
    m = _ADDR.match(addr or "")
    if not m:
        return None
    return m.group("sheet"), m.group("col").upper(), int(m.group("row"))


def parse_table(value: str) -> tuple[str, list[list[str]]]:
    """
    Accepts:  "A1\\nRegion\\tProduct\\nWest\\tPens"   (first line = start address, optional)
              or JSON {"start": "A1", "rows": [["Region", "Product"], ["West", "Pens"]]}
    Returns (start_address, rows). Raises ValueError if there is no data.
    """
    value = (value or "").strip("\n")
    if value.lstrip().startswith("{"):
        try:
            data = json.loads(value)
            rows = [[str(c) for c in r] for r in data.get("rows", [])]
            if rows:
                return str(data.get("start") or "A1"), rows
        except Exception:
            pass
    lines = [ln for ln in value.replace("\r\n", "\n").split("\n")]
    start = "A1"
    if lines and parse_address(lines[0]) and "\t" not in lines[0]:
        start = lines.pop(0).strip()
    rows = []
    for ln in lines:
        if not ln.strip():
            continue
        cells = ln.split("\t") if "\t" in ln else [c.strip() for c in ln.split("|")] if "|" in ln else [ln]
        rows.append([c.strip() for c in cells])
    if not rows:
        raise ValueError("TYPE_TABLE got no rows to enter")
    return start, rows


def as_number(v: str) -> Any:
    """'120' -> 120, '1,234.5' -> 1234.5; anything else stays text (for the Office fast path)."""
    s = v.replace(",", "").strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return v


def block_range(start: str, rows: list[list[str]]) -> tuple[str, str]:
    """Top-left and bottom-right addresses of the block."""
    p = parse_address(start)
    if not p:
        raise ValueError(f"Bad start address '{start}'")
    _, col, row = p
    width = max(len(r) for r in rows)
    end = f"{index_to_col(col_to_index(col) + width - 1)}{row + len(rows) - 1}"
    return f"{col}{row}", end
