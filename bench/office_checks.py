"""Benchmark setup/check helpers that read and write real Office files through COM (Windows + Office only).
They use a separate, invisible Office instance so they never touch the windows the agent is working in."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def _office(prog: str):  # pragma: no cover - Windows only
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx(prog)
    app.Visible = False
    app.DisplayAlerts = False
    try:
        yield app
    finally:
        try:
            app.Quit()
        except Exception:
            pass


def make_workbook(path: Path, rows: list[list[Any]]) -> None:
    with _office("Excel.Application") as xl:
        wb = xl.Workbooks.Add()
        ws = wb.Worksheets(1)
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row, start=1):
                ws.Cells(r, c).Value = v
        wb.SaveAs(str(path), 51)   # xlOpenXMLWorkbook
        wb.Close(False)


def make_document(path: Path, paragraphs: list[str]) -> None:
    with _office("Word.Application") as wd:
        doc = wd.Documents.Add()
        doc.Content.Text = "\r".join(paragraphs)
        doc.SaveAs2(str(path), 16)  # wdFormatXMLDocument
        doc.Close(False)


def check_excel(path: Path, check: dict[str, Any]) -> tuple[bool, str]:
    with _office("Excel.Application") as xl:
        wb = xl.Workbooks.Open(str(path), ReadOnly=True)
        try:
            kind = check["type"]
            if kind == "excel_cells":
                ws = wb.Worksheets(check.get("sheet", 1))
                bad = {a: ws.Range(a).Text for a, want in check["cells"].items() if str(ws.Range(a).Text) != str(want)}
                return not bad, f"mismatched: {bad}" if bad else "ok"
            if kind == "excel_column_sorted":
                ws = wb.Worksheets(1)
                col = check["column"]
                last = ws.Cells(ws.Rows.Count, col).End(-4162).Row
                vals = [ws.Range(f"{col}{r}").Value for r in range(2 if check.get("header", True) else 1, last + 1)]
                want = sorted(vals, reverse=check.get("descending", False))
                return vals == want and len(vals) > 1, f"values: {vals}"
            if kind == "excel_has_pivot":
                n = sum(s.PivotTables().Count for s in wb.Worksheets)
                return n > 0, f"{n} pivot tables"
            if kind == "excel_autofilter":
                ws = wb.Worksheets(1)
                return bool(ws.AutoFilterMode), f"autofilter={ws.AutoFilterMode} filtered={ws.FilterMode}"
            return False, f"unknown check {kind}"
        finally:
            wb.Close(False)


def check_word(path: Path, check: dict[str, Any]) -> tuple[bool, str]:
    with _office("Word.Application") as wd:
        doc = wd.Documents.Open(str(path), ReadOnly=True)
        try:
            kind = check["type"]
            text = doc.Content.Text
            if kind == "word_contains":
                return check["text"] in text, text[:120]
            if kind == "word_not_contains":
                return check["text"] not in text, text[:120]
            if kind == "word_style":
                for p in doc.Paragraphs:
                    if check["text"] in p.Range.Text:
                        style = str(p.Style)
                        return check["style_contains"].lower() in style.lower(), f"style={style}"
                return False, "paragraph not found"
            if kind == "word_tables":
                return doc.Tables.Count >= check.get("min", 1), f"{doc.Tables.Count} tables"
            if kind == "word_header_contains":
                h = doc.Sections(1).Headers(1).Range.Text
                return check["text"] in h, f"header={h.strip()[:80]}"
            return False, f"unknown check {kind}"
        finally:
            doc.Close(False)
