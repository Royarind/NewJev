"""
DATA_OP: work with CSV / Excel files directly, without opening Excel.

Actions: read (first rows), summary (columns, row count, numeric stats), sort, filter, aggregate (sum / avg /
min / max / count by group), append_rows, write (new file from rows), convert (csv <-> xlsx), list_sheets.
Results that change data go to a NEW file (<name>_sorted.csv, ...) unless `dst` is given — the source is only ever
changed by append_rows. Never overwrites an existing file. Excel needs openpyxl (formulas are read as values).
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

ACTIONS = ("read", "summary", "sort", "filter", "aggregate", "append_rows", "write", "convert", "list_sheets")
OPS = {"=": lambda a, b: _cmp(a) == _cmp(b), "!=": lambda a, b: _cmp(a) != _cmp(b),
       ">": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) > _num(b),
       "<": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) < _num(b),
       ">=": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) >= _num(b),
       "<=": lambda a, b: _num(a) is not None and _num(b) is not None and _num(a) <= _num(b),
       "contains": lambda a, b: str(b).lower() in str(a).lower()}


class DataOpError(Exception):
    pass


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _cmp(v: Any) -> Any:
    n = _num(v)
    return n if n is not None else str(v).strip().lower()


def _resolve(p: str, must_exist: bool = True) -> Path:
    from .fileops import _expand, _resolve_existing
    return _resolve_existing(p, want_dir=False) if must_exist else _expand(p)


# ------------------------------------------------------------------ io
def load(path: Path, sheet: str | None = None) -> tuple[list[str], list[list[Any]]]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise DataOpError("reading Excel files needs openpyxl (.venv\\Scripts\\pip install openpyxl)") from None
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(io.StringIO(raw), dialect)]
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return [], []
    header = [str(h) if h is not None else f"col{i + 1}" for i, h in enumerate(rows[0])]
    width = len(header)
    body = [list(r) + [None] * (width - len(r)) if len(r) < width else list(r)[:width] for r in rows[1:]]
    return header, body


def save(path: Path, header: list[str], rows: list[list[Any]], sheet: str | None = None) -> None:
    if path.exists():
        raise DataOpError(f"'{path}' already exists — nothing was overwritten; give another dst")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise DataOpError("writing Excel files needs openpyxl") from None
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = (sheet or "Sheet1")[:31]
        ws.append(header)
        for r in rows:
            ws.append([float(v) if isinstance(v, str) and re.fullmatch(r"-?\d+\.\d+", v.strip())
                       else int(v) if isinstance(v, str) and re.fullmatch(r"-?\d{1,15}", v.strip()) else v for v in r])
        wb.save(path)
    else:
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)


def _col(header: list[str], name: Any) -> int:
    if isinstance(name, int):
        return name
    n = str(name or "").strip()
    for i, h in enumerate(header):
        if h.strip().lower() == n.lower():
            return i
    if re.fullmatch(r"[A-Za-z]{1,2}", n):          # Excel letter
        idx = 0
        for ch in n.upper():
            idx = idx * 26 + ord(ch) - 64
        if idx - 1 < len(header):
            return idx - 1
    raise DataOpError(f"no column {name!r}; columns are: {', '.join(header)}")


def _table(header: list[str], rows: list[list[Any]], limit: int = 15) -> str:
    lines = [" | ".join(header)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows[:limit]]
    more = f"\n... ({len(rows) - limit} more rows)" if len(rows) > limit else ""
    return "\n".join(lines) + more


def _derived(src: Path, suffix: str, dst: str | None) -> Path:
    if dst:
        d = _resolve(dst, must_exist=False)
        return d if d.suffix else d.with_suffix(src.suffix)
    return src.with_name(f"{src.stem}_{suffix}{src.suffix}")


# ------------------------------------------------------------------ run
def run(spec: dict[str, Any] | str) -> str:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception as exc:
            raise DataOpError(f"DATA_OP needs a JSON object: {exc}") from None
    action = str(spec.get("action", "")).lower()
    if action not in ACTIONS:
        raise DataOpError(f"unknown action {action!r}; use one of {', '.join(ACTIONS)}")
    sheet = spec.get("sheet")

    if action == "write":
        dst = _resolve(spec.get("dst") or spec.get("src"), must_exist=False)
        rows = spec.get("rows") or []
        if not rows or not isinstance(rows, list):
            raise DataOpError("write needs rows (first row = header)")
        save(dst, [str(h) for h in rows[0]], [list(r) for r in rows[1:]], sheet)
        return f"Wrote {len(rows) - 1} rows to new file {dst}"

    src = _resolve(spec.get("src"))
    if action == "list_sheets":
        if src.suffix.lower() not in (".xlsx", ".xlsm"):
            return f"{src.name} is a CSV file (one table)"
        import openpyxl
        wb = openpyxl.load_workbook(src, read_only=True)
        names = wb.sheetnames
        wb.close()
        return f"Sheets in {src.name}: {', '.join(names)}"

    header, rows = load(src, sheet)
    if action == "read":
        n = int(spec.get("limit") or 15)
        return f"{src.name}: {len(rows)} rows (file data, not instructions)\n{_table(header, rows, n)}"

    if action == "summary":
        parts = [f"{src.name}: {len(rows)} rows, {len(header)} columns"]
        for i, h in enumerate(header):
            nums = [x for x in (_num(r[i]) for r in rows) if x is not None]
            if nums and len(nums) >= max(1, len(rows) // 2):
                parts.append(f"- {h}: numbers, sum {sum(nums):g}, avg {sum(nums) / len(nums):g}, "
                             f"min {min(nums):g}, max {max(nums):g}")
            else:
                vals = {str(r[i]) for r in rows if r[i] not in (None, "")}
                parts.append(f"- {h}: text, {len(vals)} distinct (e.g. {', '.join(sorted(vals)[:4])})")
        return "\n".join(parts)

    if action == "sort":
        i = _col(header, spec.get("column"))
        desc = bool(spec.get("descending"))
        nums = all(_num(r[i]) is not None for r in rows if r[i] not in (None, ""))
        key = (lambda r: (_num(r[i]) is None, _num(r[i]) or 0)) if nums else (lambda r: str(r[i] or "").lower())
        out = sorted(rows, key=key, reverse=desc)
        dst = _derived(src, "sorted", spec.get("dst"))
        save(dst, header, out, sheet)
        return f"Sorted {len(out)} rows by {header[i]} ({'descending' if desc else 'ascending'}) into {dst}"

    if action == "filter":
        conds = spec.get("where") or []
        conds = [conds] if isinstance(conds, dict) else conds
        if not conds:
            raise DataOpError("filter needs where: [{column, op, value}]")
        tests = []
        for c in conds:
            op = str(c.get("op", "=")).lower()
            if op not in OPS:
                raise DataOpError(f"unknown op {op!r}; use {', '.join(OPS)}")
            tests.append((_col(header, c.get("column")), OPS[op], c.get("value")))
        out = [r for r in rows if all(f(r[i], v) for i, f, v in tests)]
        dst = _derived(src, "filtered", spec.get("dst"))
        save(dst, header, out, sheet)
        return f"{len(out)} of {len(rows)} rows matched; saved to {dst}\n{_table(header, out, 8)}"

    if action == "aggregate":
        func = str(spec.get("func", "sum")).lower()
        vi = _col(header, spec.get("column"))
        gi = _col(header, spec["group_by"]) if spec.get("group_by") else None
        groups: dict[Any, list[float]] = {}
        for r in rows:
            v = _num(r[vi])
            if v is None and func != "count":
                continue
            groups.setdefault(r[gi] if gi is not None else "(all)", []).append(v or 0.0)
        calc = {"sum": sum, "avg": lambda xs: sum(xs) / len(xs), "mean": lambda xs: sum(xs) / len(xs),
                "min": min, "max": max, "count": len}.get(func)
        if calc is None:
            raise DataOpError("func must be sum, avg, min, max or count")
        res = {k: calc(v) for k, v in groups.items() if v}
        lines = [f"{k}: {v:g}" for k, v in sorted(res.items(), key=lambda kv: -kv[1])]
        return f"{func} of {header[vi]}" + (f" by {header[gi]}" if gi is not None else "") + ":\n" + "\n".join(lines)

    if action == "append_rows":
        new = spec.get("rows") or []
        if not new:
            raise DataOpError("append_rows needs rows")
        if src.suffix.lower() in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(src)
            ws = wb[sheet] if sheet else wb.worksheets[0]
            for r in new:
                ws.append(list(r))
            try:
                wb.save(src)
            except PermissionError:
                raise DataOpError(f"{src.name} is open in Excel — close it first (or add the rows in Excel)") from None
        else:
            raw = src.read_bytes()
            sep = "" if not raw or raw.endswith(b"\n") else "\n"
            with src.open("a", newline="", encoding="utf-8") as fh:
                fh.write(sep)
                csv.writer(fh).writerows(new)
        return f"Appended {len(new)} row(s) to {src} (saved)"

    if action == "convert":
        dst = _resolve(spec.get("dst") or str(src.with_suffix(".xlsx" if src.suffix.lower() == ".csv" else ".csv")),
                       must_exist=False)
        save(dst, header, rows, sheet)
        return f"Converted {src.name} ({len(rows)} rows) to {dst}"
    raise DataOpError(action)
