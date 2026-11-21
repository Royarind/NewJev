"""
Office automation shortcut (OFFICE_ACTION): run a small, allow-listed Python snippet against the Excel/Word
window that is already open, through Office's own COM automation interface.

Safety model
- Off by default (OFFICE_AUTOMATION=false).
- Code is parsed and checked before it runs: no imports, no while-loops, no function/class definitions,
  no private/dunder attributes, only a few safe builtins, and a deny-list of members that could open, save,
  close, export or run anything (Workbooks, Documents, SaveAs, Quit, Run, Shell, VBProject, Dialogs, …).
- Code may only reach the active workbook/document through the provided objects.
- Anything that deletes or clears content (Delete, Clear, ClearContents, …) needs the user's confirmation.
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any

EXCEL_CONST = dict(
    xlAscending=1, xlDescending=2, xlYes=1, xlNo=2, xlGuess=0, xlSortOnValues=0, xlTopToBottom=1,
    xlDatabase=1, xlRowField=1, xlColumnField=2, xlPageField=3, xlDataField=4, xlHidden=0,
    xlSum=-4157, xlCount=-4112, xlAverage=-4106, xlMax=-4136, xlMin=-4139,
    xlUp=-4162, xlDown=-4121, xlToLeft=-4159, xlToRight=-4161, xlCenter=-4108, xlLeft=-4131, xlRight=-4152,
    xlAnd=1, xlOr=2, xlFilterValues=7, xlTop10Items=3, xlSrcRange=1, xlPivotTableVersion15=5,
    xlColumnClustered=51, xlLine=4, xlPie=5, xlBarClustered=57, xlCellTypeVisible=12,
    xlExpression=2, xlCellValue=1, xlGreater=5, xlLess=6, xlEqual=3, xlBetween=1,
)
WORD_CONST = dict(
    wdStory=6, wdLine=5, wdParagraph=4, wdWord=2, wdCharacter=1, wdFindContinue=1, wdFindStop=0,
    wdReplaceAll=2, wdReplaceOne=1, wdStyleNormal=-1, wdStyleHeading1=-2, wdStyleHeading2=-3,
    wdStyleHeading3=-4, wdStyleTitle=-63, wdStyleListBullet=-49, wdStyleListNumber=-50,
    wdAlignParagraphLeft=0, wdAlignParagraphCenter=1, wdAlignParagraphRight=2, wdAlignParagraphJustify=3,
    wdHeaderFooterPrimary=1, wdFieldPage=33, wdCollapseEnd=0, wdCollapseStart=1, wdLineStyleSingle=1,
)

SAFE_BUILTINS = {"range": range, "len": len, "min": min, "max": max, "str": str, "int": int, "float": float,
                 "round": round, "abs": abs, "enumerate": enumerate, "list": list, "dict": dict, "tuple": tuple,
                 "sum": sum, "sorted": sorted, "zip": zip, "bool": bool, "True": True, "False": False, "None": None}

DENY_MEMBERS = {
    # leaving the open document / touching files, apps or code
    "Workbooks", "Documents", "Presentations", "Open", "OpenText", "OpenXML", "Add2", "SaveAs", "SaveAs2",
    "SaveCopyAs", "Save", "Close", "Quit", "Run", "RunAutoMacros", "ExecuteExcel4Macro", "Evaluate", "OnTime",
    "OnKey", "Shell", "SendKeys", "SendMail", "SendForReview", "FollowHyperlink", "VBProject", "VBE", "Macros",
    "AddIns", "COMAddIns", "FileDialog", "Dialogs", "GetOpenFilename", "GetSaveAsFilename", "PrintOut",
    "PrintPreview", "ExportAsFixedFormat", "Export", "ChangeFileAccess", "Kill", "MailMerge", "Hyperlinks",
    "Connections", "QueryTables", "CreateObject", "GetObject", "DisplayAlerts", "EnableEvents",
    "AutomationSecurity", "Parent", "Application", "AddOLEObject", "OLEObjects", "AddPicture", "InsertFile",
    "ChangeLink", "UpdateLink", "LinkSources", "OpenLinks", "Wait", "ShellExecute", "Copy", "Move",
}
BANNED_STRING_PARTS = (":\\", ":/", "\\\\", "file:", "http:", "https:", "ftp:", "webservice", "filterxml", "call(",
                       "register(", "=dde", "dde(", "cmd|", "powershell", "rtd(", "hyperlink(")
DELETE_MEMBERS = {"Delete", "Clear", "ClearContents", "ClearFormats", "ClearComments", "ClearNotes",
                  "RemoveDuplicates", "Cut", "DeleteColumns", "DeleteRows", "Remove", "RemoveAll"}
MAX_CODE = 4000


class UnsafeCode(ValueError):
    pass


def check_code(code: str, app: str) -> bool:
    """Validate the snippet. Returns True if it deletes/clears content (needs confirmation). Raises UnsafeCode."""
    if not code or len(code) > MAX_CODE:
        raise UnsafeCode("Code is empty or too long")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise UnsafeCode(f"Syntax error: {exc}") from None
    allowed_names = set(SAFE_BUILTINS) | {"result"} | (
        {"app", "wb", "ws", "sel", "xl"} if app == "excel" else {"app", "doc", "sel", "wd"})
    assigned: set[str] = set()
    deletes = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.While, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
                             ast.Try, ast.Raise, ast.Delete, ast.Await, ast.Yield, ast.YieldFrom)):
            raise UnsafeCode(f"'{type(node).__name__}' is not allowed")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise UnsafeCode(f"Private attribute '{node.attr}' is not allowed")
            if node.attr in DENY_MEMBERS:
                raise UnsafeCode(f"'{node.attr}' is not allowed in Office actions")
            if node.attr in DELETE_MEMBERS:
                deletes = True
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.For, ast.comprehension)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:   # only plain names (x = …, for a, b in …), not obj.attr = …
                stack = [t]
                while stack:
                    n = stack.pop()
                    if isinstance(n, ast.Name):
                        assigned.add(n.id)
                    elif isinstance(n, (ast.Tuple, ast.List)):
                        stack.extend(n.elts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise UnsafeCode("Dunder names are not allowed")
            if isinstance(node.ctx, ast.Load) and node.id not in allowed_names | assigned:
                raise UnsafeCode(f"Unknown name '{node.id}'")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in SAFE_BUILTINS and node.func.id not in assigned:
                raise UnsafeCode(f"Calling '{node.func.id}' is not allowed")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            if any(part in low for part in BANNED_STRING_PARTS):
                raise UnsafeCode("File paths, links, web or external-call formulas are not allowed in Office actions")
    return deletes


def _com_app(app: str) -> Any:  # pragma: no cover - Windows only
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    prog = {"excel": "Excel.Application", "word": "Word.Application"}[app]
    return win32com.client.GetActiveObject(prog)


def _env(app: str, obj: Any) -> dict[str, Any]:  # pragma: no cover - Windows only
    if app == "excel":
        return {"app": obj, "wb": obj.ActiveWorkbook, "ws": obj.ActiveSheet, "sel": obj.Selection,
                "xl": SimpleNamespace(**EXCEL_CONST), "result": None}
    return {"app": obj, "doc": obj.ActiveDocument, "sel": obj.Selection,
            "wd": SimpleNamespace(**WORD_CONST), "result": None}


def run_office_code(app: str, code: str) -> str:
    """Validate and run. The caller has already obtained confirmation when check_code() said it deletes."""
    check_code(code, app)
    obj = _com_app(app)
    env = _env(app, obj)
    exec(compile(ast.parse(code), "<office_action>", "exec"), {"__builtins__": SAFE_BUILTINS}, env)  # noqa: S102
    result = env.get("result")
    return "Office action done" + (f": {str(result)[:200]}" if result not in (None, "") else ".")


def describe(app: str) -> dict[str, Any]:  # pragma: no cover - Windows only
    """Compact view of the open workbook/document for the code-writing LLM."""
    try:
        obj = _com_app(app)
        if app == "excel":
            ws = obj.ActiveSheet
            used = ws.UsedRange
            rows = used.Rows.Count
            cols = min(used.Columns.Count, 30)
            first = used.Row
            left = used.Column
            sample = []
            for r in range(first, first + min(rows, 4)):
                sample.append([ws.Cells(r, c).Text for c in range(left, left + cols)])
            return {"app": "excel", "workbook": obj.ActiveWorkbook.Name, "sheets": [s.Name for s in obj.ActiveWorkbook.Sheets],
                    "active_sheet": ws.Name, "used_range": used.Address(False, False), "rows": rows,
                    "first_rows": sample, "selection": obj.Selection.Address(False, False),
                    "pivot_tables": ws.PivotTables().Count}
        doc = obj.ActiveDocument
        paras = []
        for i, p in enumerate(doc.Paragraphs):
            if i >= 15:
                break
            paras.append({"text": p.Range.Text.strip()[:120], "style": str(p.Style)})
        return {"app": "word", "document": doc.Name, "paragraphs": doc.Paragraphs.Count, "first_paragraphs": paras,
                "tables": doc.Tables.Count, "selection": obj.Selection.Text[:200]}
    except Exception as exc:
        return {"app": app, "error": f"could not read the document: {exc}"}
