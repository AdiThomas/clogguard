from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from design import build_templates as bt

SHEETS = ["Pairwise", "HouseOfQuality", "Pugh", "WeightedMatrix", "FMEA", "BOM"]
COMMITTED = Path(bt.__file__).with_name("templates.xlsx")


@pytest.fixture(scope="module")
def wb(tmp_path_factory: pytest.TempPathFactory) -> openpyxl.Workbook:
    path = bt.build(tmp_path_factory.mktemp("design") / "t.xlsx")
    return openpyxl.load_workbook(path)


def _input_cells(ws):  # type: ignore[no-untyped-def]
    for row in ws.iter_rows():
        for c in row:
            if c.fill is not None and c.fill.fill_type == "solid":
                rgb = str(c.fill.start_color.rgb)
                if rgb.endswith(bt.INPUT_RGB):
                    yield c


def test_sheets_and_instructions(wb: openpyxl.Workbook) -> None:
    assert wb.sheetnames == SHEETS
    for name in SHEETS:
        ws = wb[name]
        assert isinstance(ws["A1"].value, str) and len(ws["A1"].value) > 80
        assert any(str(r).startswith("A1:") for r in ws.merged_cells.ranges)


def test_judgment_cells_empty(wb: openpyxl.Workbook) -> None:
    counts = {}
    for name in SHEETS:
        cells = list(_input_cells(wb[name]))
        counts[name] = len(cells)
        assert all(c.value is None for c in cells), name
    assert counts["Pairwise"] == 28  # 8 criteria, upper triangle
    assert counts["Pugh"] == 1 + 8 * 7  # datum + ratings
    assert counts["WeightedMatrix"] == 8 * 7
    assert counts["HouseOfQuality"] >= 12 * 9


def test_key_formulas(wb: openpyxl.Workbook) -> None:
    pw = wb["Pairwise"]
    assert pw["K4"].value == '=IF(SUM($J$4:$J$11)=0,"",J4/SUM($J$4:$J$11))'
    assert pw["B5"].value == '=IF(C4="","",1-C4)'  # lower triangle mirrors upper
    assert pw["J4"].value == "=SUM(B4:I4)"
    assert pw["A4"].value == "Warning lead time" and pw["I3"].value == "Installation"
    hoq = wb["HouseOfQuality"]
    assert hoq["B7"].value == '=IF(Pairwise!$K$4="","",Pairwise!$K$4)'
    assert hoq["C19"].value.startswith("=SUMPRODUCT($B$7:$B$18,C7:C18")
    pugh = wb["Pugh"]
    assert pugh["C14"].value == '=IF(C$5=$B$3,"datum",COUNTIF(C6:C13,"=+"))'
    assert "COUNTIF" in pugh["D16"].value
    assert [pugh.cell(row=5, column=c).value for c in range(3, 10)] == bt.CONCEPTS
    wm = wb["WeightedMatrix"]
    assert wm["B4"].value == '=IF(Pairwise!$K$4="","",Pairwise!$K$4)'
    assert "SUMPRODUCT($B$4:$B$11,C4:C11)" in wm["C13"].value
    assert "RANK(" in wm["C14"].value
    fm = wb["FMEA"]
    assert fm["J6"].value == '=IF(COUNT(D6,G6,I6)=3,D6*G6*I6,"")'
    assert "HIGH" in fm["K6"].value
    assert [fm.cell(row=r, column=1).value for r in range(6, 17)] == bt.FMEA_ITEMS
    bom = wb["BOM"]
    assert bom["F4"].value == '=IF(OR(D4="",E4=""),"",D4*E4)'
    assert bom["C33"].value == 385 and bom["C34"].value == 400
    assert bom["C36"].value == '=IF(C30<=C33,"OK","OVER")'
    assert len(bom.conditional_formatting) >= 2


def test_validations(wb: openpyxl.Workbook) -> None:
    def formulas(ws):  # type: ignore[no-untyped-def]
        return {dv.formula1 for dv in ws.data_validations.dataValidation}

    assert '"1,0.5,0"' in formulas(wb["Pairwise"])
    assert '"9,3,1"' in formulas(wb["HouseOfQuality"])
    assert '"+,S,-"' in formulas(wb["Pugh"])
    fm = wb["FMEA"].data_validations.dataValidation
    assert any(dv.formula1 == "1" and dv.formula2 == "10" for dv in fm)
    wm = wb["WeightedMatrix"].data_validations.dataValidation
    assert any(dv.formula1 == "1" and dv.formula2 == "5" for dv in wm)


def test_committed_workbook_reloads() -> None:
    assert COMMITTED.exists(), "run python design/build_templates.py"
    wb = openpyxl.load_workbook(COMMITTED)
    assert wb.sheetnames == SHEETS
    assert all(c.value is None for n in SHEETS for c in _input_cells(wb[n]))
