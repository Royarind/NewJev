import pytest

from app.capture.office import UnsafeCode, check_code

SORT = 'ws.UsedRange.Sort(Key1=ws.Range("C1"), Order1=xl.xlDescending, Header=xl.xlYes)\nresult = "sorted"'
PIVOT = """src = ws.UsedRange
pc = wb.PivotCaches().Create(SourceType=xl.xlDatabase, SourceData=src)
sh = wb.Sheets.Add()
pt = pc.CreatePivotTable(TableDestination=sh.Range("A3"), TableName="Pivot1")
pt.PivotFields("Region").Orientation = xl.xlRowField
pt.AddDataField(pt.PivotFields("Sales"), "Sum of Sales", xl.xlSum)
result = "pivot"
"""
REPLACE = 'f = doc.Content.Find\nf.Execute(FindText="draft", ReplaceWith="final", Replace=wd.wdReplaceAll)'


def test_allowed_snippets():
    assert check_code(SORT, "excel") is False
    assert check_code(PIVOT, "excel") is False
    assert check_code(REPLACE, "word") is False
    assert check_code("for r in range(2, 50):\n    ws.Cells(r, 4).Formula = '=B%d*C%d' % (r, r)", "excel") is False


def test_deleting_content_needs_confirmation():
    assert check_code('ws.Range("A2:A10").ClearContents()', "excel") is True
    assert check_code('ws.Rows(3).Delete()', "excel") is True


@pytest.mark.parametrize("code", [
    "import os\nos.remove('x')",
    "app.Workbooks.Open('C:\\\\x.xlsx')",
    "wb.SaveAs('report.xlsx')",
    "wb.Close()",
    "app.Run('Macro1')",
    "ws.__class__",
    "open('x.txt', 'w')",
    "eval('1')",
    "while True:\n    pass",
    "def f():\n    return 1",
    "ws.Range('A1').Value = 'C:\\\\Windows'",
    "wb.Application.Quit()",
])
def test_rejected_snippets(code):
    with pytest.raises(UnsafeCode):
        check_code(code, "excel")


def test_word_names_not_available_in_excel():
    with pytest.raises(UnsafeCode):
        check_code("doc.Content.Text = 'x'", "excel")


@pytest.mark.parametrize("code", [
    "ws.Range('A1').Formula = '=WEBSERVICE(\"x\")'",
    "ws.Shapes.AddPicture('pic.png', 0, 1, 0, 0, 10, 10)",
    "sel.InsertFile('notes.txt')",
    "ws.Range('A1').Value = 'see https://example.com'",
    "ws.Range('A1').Value = 'D:/data/x.csv'",
    "ws.Copy()",
])
def test_rejected_external_access(code):
    with pytest.raises(UnsafeCode):
        check_code(code, "excel" if "sel." not in code else "word")


def test_ordinary_words_are_fine():
    assert check_code("ws.Range('E1').Value = 'Revenue'\nresult = 'added Revenue column'", "excel") is False
