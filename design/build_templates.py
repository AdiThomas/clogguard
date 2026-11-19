"""Generate ``design/templates.xlsx``: the design-process worksheets for concept selection.

Every judgment cell (comparisons, relationships, scores, ratings, BOM lines) is left EMPTY and
lightly shaded; formulas fill in as the team enters values. Nothing in this workbook is a
judgment made for the team.

    python design/build_templates.py            # writes design/templates.xlsx
    python design/build_templates.py out.xlsx   # writes elsewhere
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

OUT = Path(__file__).with_name("templates.xlsx")

# Fill colours. INPUT marks every judgment cell; tests rely on it.
INPUT_RGB = "FFF2CC"
GREY_RGB = "BFBFBF"
HEAD_RGB = "D9E1F2"
INPUT_FILL = PatternFill("solid", start_color=INPUT_RGB, end_color=INPUT_RGB)
GREY_FILL = PatternFill("solid", start_color=GREY_RGB, end_color=GREY_RGB)
HEAD_FILL = PatternFill("solid", start_color=HEAD_RGB, end_color=HEAD_RGB)
OK_FILL = PatternFill("solid", start_color="C6EFCE", end_color="C6EFCE")
BAD_FILL = PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE")
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN = Side(style="thin", color="999999")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# docs/plan.md, Criteria (proposal Table 2)
CRITERIA: list[tuple[str, str]] = [
    ("Warning lead time", "Alert at least 5 min before the first visible defect"),
    ("Recovery rate", "At least 70% of recoverable clog runs finish with no visible defect"),
    ("False alarms", "At most 1 unnecessary intervention per 10 normal prints"),
    ("Filament waste", "Down at least 50% against clog runs with no intervention"),
    ("Part quality", "Recovered parts within 5% of the mass of a defect-free reference"),
    ("Response time", "Assessed at least once per second; command within 2 s of a decision"),
    ("Hardware cost", "Under $400 per printer; project allowance $350 plus 10%"),
    ("Installation", "Installed and calibrated by a technician in under 3 hours"),
]
# Customer-need wording for the house of quality (one per criterion, same order)
NEEDS = [
    "Warns well before a visible defect appears",
    "Rescues prints that start to clog",
    "Does not interrupt normal prints",
    "Wastes less filament on clogged prints",
    "Recovered parts are as good as normal parts",
    "Notices and reacts quickly",
    "Affordable to fit to each printer",
    "Quick for a technician to install and calibrate",
]
# docs/plan.md, Constraints (proposal Table 1): must-haves, importance left to the team
CONSTRAINT_NEEDS = [
    "Thermal-runaway protection stays enabled and independent",
    "Hotend never exceeds the filament's maximum rated temperature",
    "Low-voltage DC electronics only; no mains modification",
    "No mainboard replacement and no change to the slicer workflow",
]
ENG_CHARS: list[tuple[str, str, str, str]] = [  # name, unit, direction, target (from criteria)
    ("Lead time", "min", "Higher is better", ">= 5"),
    ("Recovery rate", "%", "Higher is better", ">= 70"),
    ("False alarm rate", "per print", "Lower is better", "<= 0.1"),
    ("Waste reduction", "%", "Higher is better", ">= 50"),
    ("Mass deviation", "%", "Lower is better", "<= 5"),
    ("Assessment rate", "Hz", "Higher is better", ">= 1"),
    ("Command latency", "s", "Lower is better", "<= 2"),
    ("Cost per printer", "CAD", "Lower is better", "< 400"),
    ("Install time", "h", "Lower is better", "< 3"),
]
CONCEPTS = [
    "Filament motion",
    "Vision",
    "Vibration",
    "Motor current",
    "Acoustic emission",
    "Extrusion force",
    "Heater power",
]
FMEA_ITEMS = [
    "Sense signal",
    "Log data",
    "Assess condition",
    "Decide action",
    "Send command",
    "Adjust temperature",
    "Adjust speed",
    "Purge",
    "Pause",
    "Alert user",
    "Power electronics",
]
PROJECT_ALLOWANCE_CAD = 385  # $350 plus 10% (docs/plan.md); the proposal also states $1,200
PER_PRINTER_LIMIT_CAD = 400

N_CRIT = len(CRITERIA)
PW_FIRST = 4  # first criterion row on the pairwise sheet
PW_WEIGHT_COL = get_column_letter(2 + N_CRIT + 1)  # K: weight column


def _instructions(ws: Worksheet, text: str, width_cols: int) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width_cols)
    c = ws.cell(row=1, column=1, value=text)
    c.alignment = WRAP
    c.font = Font(italic=True)
    ws.row_dimensions[1].height = 75


def _head(cell, text: str) -> None:  # type: ignore[no-untyped-def]
    cell.value = text
    cell.font = BOLD
    cell.fill = HEAD_FILL
    cell.alignment = CENTER
    cell.border = BOX


def _input(cell) -> None:  # type: ignore[no-untyped-def]
    cell.value = None
    cell.fill = INPUT_FILL
    cell.border = BOX
    cell.alignment = CENTER


def _formula(cell, text: str, fmt: str | None = None) -> None:  # type: ignore[no-untyped-def]
    cell.value = text
    cell.border = BOX
    cell.alignment = CENTER
    if fmt:
        cell.number_format = fmt


def _weight_ref(i: int) -> str:
    return f"Pairwise!${PW_WEIGHT_COL}${PW_FIRST + i}"


# ------------------------------------------------------------------ sheets


def pairwise(wb: Workbook) -> None:
    ws = wb.active
    ws.title = "Pairwise"
    last_col = 2 + N_CRIT + 1
    _instructions(
        ws,
        "PAIRWISE COMPARISON. For each shaded cell above the diagonal, compare the ROW criterion "
        "with the COLUMN criterion: 1 = row more important, 0.5 = equally important, 0 = row "
        "less important. The lower triangle fills itself (1 - mirror). Row totals and weights "
        "are formulas; weights feed the House of Quality and Weighted Matrix sheets. Note: the "
        "least important criterion can receive weight 0; the team decides whether that is "
        "acceptable. Every comparison is the team's judgment.",
        last_col,
    )
    ws.cell(row=2, column=1, value="Comparisons entered:")
    _formula(ws.cell(row=2, column=2), f"=COUNT(B{PW_FIRST}:I{PW_FIRST + N_CRIT - 1})/2")
    ws.cell(row=2, column=3, value=f"of {N_CRIT * (N_CRIT - 1) // 2}")
    _head(ws.cell(row=3, column=1), "Criterion (row vs column)")
    for j, (name, _) in enumerate(CRITERIA):
        _head(ws.cell(row=3, column=2 + j), name)
    _head(ws.cell(row=3, column=2 + N_CRIT), "Row total")
    _head(ws.cell(row=3, column=last_col), "Weight")
    dv = DataValidation(type="list", formula1='"1,0.5,0"', allow_blank=True)
    dv.error = "Enter 1, 0.5 or 0"
    ws.add_data_validation(dv)
    for i, (name, target) in enumerate(CRITERIA):
        r = PW_FIRST + i
        c = ws.cell(row=r, column=1, value=name)
        c.font = BOLD
        c.border = BOX
        for j in range(N_CRIT):
            cell = ws.cell(row=r, column=2 + j)
            if i == j:
                cell.fill = GREY_FILL
                cell.border = BOX
            elif j > i:
                _input(cell)
                dv.add(cell)
            else:
                mirror = f"{get_column_letter(2 + i)}{PW_FIRST + j}"
                _formula(cell, f'=IF({mirror}="","",1-{mirror})')
        rng = f"B{r}:{get_column_letter(1 + N_CRIT)}{r}"
        total_col = get_column_letter(2 + N_CRIT)
        _formula(ws.cell(row=r, column=2 + N_CRIT), f"=SUM({rng})")
        tot = f"${total_col}${PW_FIRST}:${total_col}${PW_FIRST + N_CRIT - 1}"
        _formula(
            ws.cell(row=r, column=last_col),
            f'=IF(SUM({tot})=0,"",{total_col}{r}/SUM({tot}))',
            "0.0%",
        )
        ws.cell(row=r, column=last_col + 2, value=target).alignment = WRAP
    rsum = PW_FIRST + N_CRIT
    ws.cell(row=rsum, column=1, value="Sum").font = BOLD
    total_col = get_column_letter(2 + N_CRIT)
    _formula(
        ws.cell(row=rsum, column=2 + N_CRIT),
        f"=SUM({total_col}{PW_FIRST}:{total_col}{rsum - 1})",
    )
    _formula(
        ws.cell(row=rsum, column=last_col),
        f"=SUM({PW_WEIGHT_COL}{PW_FIRST}:{PW_WEIGHT_COL}{rsum - 1})",
        "0.0%",
    )
    ws.cell(row=3, column=last_col + 2, value="Target (docs/plan.md)").font = BOLD
    ws.column_dimensions["A"].width = 26
    for j in range(2, last_col + 1):
        ws.column_dimensions[get_column_letter(j)].width = 12
    ws.column_dimensions[get_column_letter(last_col + 2)].width = 60
    ws.row_dimensions[3].height = 32
    ws.freeze_panes = "B4"


def house_of_quality(wb: Workbook) -> None:
    ws = wb.create_sheet("HouseOfQuality")
    n_ec = len(ENG_CHARS)
    first_ec = 3
    last_ec = first_ec + n_ec - 1
    _instructions(
        ws,
        "HOUSE OF QUALITY. Rows are customer needs (from the criteria and constraints in "
        "docs/plan.md); columns are engineering characteristics. In each shaded cell enter the "
        "strength of the relationship: 9 strong, 3 medium, 1 weak, blank none. Importance of the "
        "criterion rows is pulled from the Pairwise sheet; constraint rows are pass/fail "
        "must-haves, so their importance is the team's choice (blank = excluded). Technical "
        "importance = SUMPRODUCT(importance, column). The roof (correlations between "
        "characteristics, ++ + - --) is below the main table. All entries are the team's.",
        last_ec + 1,
    )
    _head(ws.cell(row=3, column=1), "Customer need")
    _head(ws.cell(row=3, column=2), "Importance")
    for k, (name, unit, direction, target) in enumerate(ENG_CHARS):
        col = first_ec + k
        _head(ws.cell(row=3, column=col), name)
        ws.cell(row=4, column=col, value=unit).alignment = CENTER
        ws.cell(row=5, column=col, value=direction).alignment = CENTER
        ws.cell(row=6, column=col, value=target).alignment = CENTER
    for r, label in ((4, "Unit"), (5, "Direction of improvement"), (6, "Target (criteria)")):
        ws.cell(row=r, column=1, value=label).font = Font(italic=True)
    dv = DataValidation(type="list", formula1='"9,3,1"', allow_blank=True)
    dv.error = "Enter 9, 3 or 1 (or leave blank)"
    ws.add_data_validation(dv)
    first = 7
    rows = [(n, _weight_ref(i)) for i, n in enumerate(NEEDS)]
    rows += [(f"(Constraint) {n}", None) for n in CONSTRAINT_NEEDS]
    for i, (need, ref) in enumerate(rows):
        r = first + i
        ws.cell(row=r, column=1, value=need).alignment = WRAP
        ws.cell(row=r, column=1).border = BOX
        imp = ws.cell(row=r, column=2)
        if ref:
            _formula(imp, f'=IF({ref}="","",{ref})', "0.0%")
        else:
            _input(imp)
        for k in range(n_ec):
            cell = ws.cell(row=r, column=first_ec + k)
            _input(cell)
            dv.add(cell)
    last = first + len(rows) - 1
    tr = last + 1
    ws.cell(row=tr, column=1, value="Technical importance").font = BOLD
    ws.cell(row=tr + 1, column=1, value="Relative importance").font = BOLD
    ws.cell(row=tr + 2, column=1, value="Rank").font = BOLD
    first_l, last_l = get_column_letter(first_ec), get_column_letter(last_ec)
    for k in range(n_ec):
        col = get_column_letter(first_ec + k)
        _formula(
            ws.cell(row=tr, column=first_ec + k),
            f"=SUMPRODUCT($B${first}:$B${last},{col}{first}:{col}{last})",
            "0.00",
        )
        _formula(
            ws.cell(row=tr + 1, column=first_ec + k),
            f'=IF(SUM(${first_l}${tr}:${last_l}${tr})=0,"",'
            f"{col}{tr}/SUM(${first_l}${tr}:${last_l}${tr}))",
            "0.0%",
        )
        _formula(
            ws.cell(row=tr + 2, column=first_ec + k),
            f'=IF({col}{tr + 1}="","",RANK({col}{tr},${first_l}${tr}:${last_l}${tr}))',
        )
    # roof as a correlation matrix
    roof = tr + 5
    ws.cell(row=roof - 1, column=1, value="Roof: correlation between characteristics").font = BOLD
    dv2 = DataValidation(type="list", formula1='"++,+,-,--"', allow_blank=True)
    ws.add_data_validation(dv2)
    for k, (name, *_rest) in enumerate(ENG_CHARS):
        _head(ws.cell(row=roof, column=first_ec + k), name)
        ws.cell(row=roof + 1 + k, column=2, value=name).font = BOLD
        for m in range(n_ec):
            cell = ws.cell(row=roof + 1 + k, column=first_ec + m)
            if m > k:
                _input(cell)
                dv2.add(cell)
            elif m == k:
                cell.fill = GREY_FILL
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 18
    for k in range(n_ec):
        ws.column_dimensions[get_column_letter(first_ec + k)].width = 14
    ws.row_dimensions[3].height = 32
    ws.freeze_panes = "C7"


def pugh(wb: Workbook) -> None:
    ws = wb.create_sheet("Pugh")
    n = len(CONCEPTS)
    first_c = 3
    last_c = first_c + n - 1
    _instructions(
        ws,
        "PUGH SCREENING. Choose the datum concept in the shaded dropdown (B3). For every other "
        "concept and criterion, enter + (better than datum), S (same) or - (worse). The datum "
        "column is greyed and skipped. Counts and net score use COUNTIF. The choice of datum and "
        "every rating are the team's.",
        last_c,
    )
    ws.cell(row=3, column=1, value="Datum concept:").font = BOLD
    datum = ws.cell(row=3, column=2)
    _input(datum)
    dv_d = DataValidation(type="list", formula1=f"=$C$5:${get_column_letter(last_c)}$5")
    ws.add_data_validation(dv_d)
    dv_d.add(datum)
    _head(ws.cell(row=5, column=1), "Criterion")
    _head(ws.cell(row=5, column=2), "Target")
    for k, name in enumerate(CONCEPTS):
        _head(ws.cell(row=5, column=first_c + k), name)
    dv = DataValidation(type="list", formula1='"+,S,-"', allow_blank=True)
    dv.error = "Enter +, S or -"
    ws.add_data_validation(dv)
    first = 6
    for i, (name, target) in enumerate(CRITERIA):
        r = first + i
        ws.cell(row=r, column=1, value=name).font = BOLD
        ws.cell(row=r, column=2, value=target).alignment = WRAP
        for k in range(n):
            cell = ws.cell(row=r, column=first_c + k)
            _input(cell)
            dv.add(cell)
    last = first + N_CRIT - 1
    labels = [("Count of +", "=+"), ("Count of S", "=S"), ("Count of -", "=-")]
    for j, (label, crit) in enumerate(labels):
        r = last + 1 + j
        ws.cell(row=r, column=1, value=label).font = BOLD
        for k in range(n):
            col = get_column_letter(first_c + k)
            _formula(
                ws.cell(row=r, column=first_c + k),
                f'=IF({col}$5=$B$3,"datum",COUNTIF({col}{first}:{col}{last},"{crit}"))',
            )
    rn = last + 4
    ws.cell(row=rn, column=1, value="Net score (+ minus -)").font = BOLD
    ws.cell(row=rn + 1, column=1, value="Rank").font = BOLD
    lc = get_column_letter(last_c)
    for k in range(n):
        col = get_column_letter(first_c + k)
        _formula(
            ws.cell(row=rn, column=first_c + k),
            f'=IF({col}$5=$B$3,"datum",IF(COUNTA({col}{first}:{col}{last})=0,"",'
            f"{col}{last + 1}-{col}{last + 3}))",
        )
        _formula(
            ws.cell(row=rn + 1, column=first_c + k),
            f'=IF(ISNUMBER({col}{rn}),RANK({col}{rn},$C${rn}:${lc}${rn}),"")',
        )
    ws.conditional_formatting.add(
        f"C{first}:{lc}{last}",
        FormulaRule(formula=["C$5=$B$3"], fill=GREY_FILL, stopIfTrue=True),
    )
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 44
    for k in range(n):
        ws.column_dimensions[get_column_letter(first_c + k)].width = 14
    ws.row_dimensions[5].height = 32
    ws.freeze_panes = "C6"


def weighted(wb: Workbook) -> None:
    ws = wb.create_sheet("WeightedMatrix")
    n = len(CONCEPTS)
    first_c = 3
    last_c = first_c + n - 1
    lc = get_column_letter(last_c)
    _instructions(
        ws,
        "WEIGHTED DECISION MATRIX. Weights come from the Pairwise sheet. Score each concept "
        "against each criterion from 1 (poor) to 5 (excellent) in the shaded cells; use January "
        "bake-off measurements where they exist and record the evidence beside the sheet. "
        "Weighted total = SUMPRODUCT(weights, scores); rank is a formula. Every score is the "
        "team's judgment.",
        last_c,
    )
    _head(ws.cell(row=3, column=1), "Criterion")
    _head(ws.cell(row=3, column=2), "Weight")
    for k, name in enumerate(CONCEPTS):
        _head(ws.cell(row=3, column=first_c + k), name)
    dv = DataValidation(type="whole", operator="between", formula1="1", formula2="5")
    dv.allow_blank = True
    dv.error = "Enter a whole number from 1 to 5"
    ws.add_data_validation(dv)
    first = 4
    for i, (name, _) in enumerate(CRITERIA):
        r = first + i
        ws.cell(row=r, column=1, value=name).font = BOLD
        ref = _weight_ref(i)
        _formula(ws.cell(row=r, column=2), f'=IF({ref}="","",{ref})', "0.0%")
        for k in range(n):
            cell = ws.cell(row=r, column=first_c + k)
            _input(cell)
            dv.add(cell)
    last = first + N_CRIT - 1
    rt = last + 1
    ws.cell(row=rt, column=1, value="Weight sum").font = BOLD
    _formula(ws.cell(row=rt, column=2), f"=SUM(B{first}:B{last})", "0.0%")
    ws.cell(row=rt + 1, column=1, value="Weighted total (max 5)").font = BOLD
    ws.cell(row=rt + 2, column=1, value="Rank").font = BOLD
    ws.cell(row=rt + 3, column=1, value="Scores entered").font = BOLD
    for k in range(n):
        col = get_column_letter(first_c + k)
        _formula(
            ws.cell(row=rt + 1, column=first_c + k),
            f'=IF(COUNT({col}{first}:{col}{last})=0,"",'
            f"SUMPRODUCT($B${first}:$B${last},{col}{first}:{col}{last}))",
            "0.00",
        )
        _formula(
            ws.cell(row=rt + 2, column=first_c + k),
            f'=IF(ISNUMBER({col}{rt + 1}),RANK({col}{rt + 1},$C${rt + 1}:${lc}${rt + 1}),"")',
        )
        _formula(
            ws.cell(row=rt + 3, column=first_c + k),
            f'=COUNT({col}{first}:{col}{last})&" of {N_CRIT}"',
        )
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 10
    for k in range(n):
        ws.column_dimensions[get_column_letter(first_c + k)].width = 14
    ws.row_dimensions[3].height = 32
    ws.freeze_panes = "C4"


FMEA_COLS: list[tuple[str, int, str]] = [  # header, width, kind: text/input/num/S/O/D/rpn/ap
    ("Item / function", 22, "item"),
    ("Potential failure mode", 26, "input"),
    ("Potential effect(s) of failure", 28, "input"),
    ("Severity (S) 1-10", 10, "rating"),
    ("Potential cause(s) / mechanism", 28, "input"),
    ("Current prevention controls", 24, "input"),
    ("Occurrence (O) 1-10", 10, "rating"),
    ("Current detection controls", 24, "input"),
    ("Detection (D) 1-10", 10, "rating"),
    ("RPN = S x O x D", 10, "rpn"),
    ("Action priority", 12, "ap"),
    ("Recommended action", 26, "input"),
    ("Responsibility and target date", 20, "input"),
    ("Action taken", 24, "input"),
    ("S after", 8, "rating"),
    ("O after", 8, "rating"),
    ("D after", 8, "rating"),
    ("RPN after", 10, "rpn2"),
]


def fmea(wb: Workbook) -> None:
    ws = wb.create_sheet("FMEA")
    ncol = len(FMEA_COLS)
    _instructions(
        ws,
        "FMEA (IEC 60812 worksheet columns). Functions are pre-listed from the specification; "
        "every other column is for the team. Rate Severity, Occurrence and Detection from 1 to "
        "10 (validated). RPN = S x O x D. Action priority: HIGH if S >= the severity threshold "
        "or RPN >= the high threshold, MEDIUM if RPN >= the medium threshold, else LOW. The "
        "thresholds in row 3 are the team's choice and start empty; until they are set the "
        "flag reads 'set thresholds'. Add rows for further failure modes of the same function.",
        ncol,
    )
    ws.cell(row=3, column=1, value="Thresholds:").font = BOLD
    ws.cell(row=3, column=2, value="Severity >=")
    _input(ws.cell(row=3, column=3))
    ws.cell(row=3, column=4, value="RPN high >=")
    _input(ws.cell(row=3, column=5))
    ws.cell(row=3, column=6, value="RPN medium >=")
    _input(ws.cell(row=3, column=7))
    for j, (head, width, _) in enumerate(FMEA_COLS):
        _head(ws.cell(row=5, column=j + 1), head)
        ws.column_dimensions[get_column_letter(j + 1)].width = width
    ws.row_dimensions[5].height = 45
    dv = DataValidation(type="whole", operator="between", formula1="1", formula2="10")
    dv.allow_blank = True
    dv.error = "Enter a whole number from 1 to 10"
    ws.add_data_validation(dv)
    first = 6
    for i, item in enumerate(FMEA_ITEMS):
        r = first + i
        for j, (_, _, kind) in enumerate(FMEA_COLS):
            cell = ws.cell(row=r, column=j + 1)
            if kind == "item":
                cell.value = item
                cell.font = BOLD
                cell.border = BOX
            elif kind in ("input", "rating"):
                _input(cell)
                if kind == "rating":
                    dv.add(cell)
            elif kind == "rpn":
                _formula(cell, f'=IF(COUNT(D{r},G{r},I{r})=3,D{r}*G{r}*I{r},"")')
            elif kind == "rpn2":
                _formula(cell, f'=IF(COUNT(O{r},P{r},Q{r})=3,O{r}*P{r}*Q{r},"")')
            elif kind == "ap":
                _formula(
                    cell,
                    f'=IF(J{r}="","",IF(COUNT($C$3,$E$3,$G$3)<3,"set thresholds",'
                    f'IF(OR(D{r}>=$C$3,J{r}>=$E$3),"HIGH",IF(J{r}>=$G$3,"MEDIUM","LOW"))))',
                )
    last = first + len(FMEA_ITEMS) - 1
    ws.conditional_formatting.add(
        f"K{first}:K{last}", CellIsRule(operator="equal", formula=['"HIGH"'], fill=BAD_FILL)
    )
    ws.freeze_panes = "B6"


def bom(wb: Workbook) -> None:
    ws = wb.create_sheet("BOM")
    heads = [
        ("Item", 28),
        ("Part number", 18),
        ("Supplier", 16),
        ("Qty", 7),
        ("Unit cost (CAD)", 12),
        ("Extended cost (CAD)", 14),
        ("Per printer? (Y/N)", 11),
        ("Link", 36),
        ("Notes", 30),
    ]
    _instructions(
        ws,
        "BILL OF MATERIALS. Enter each purchased part in the shaded rows. Extended cost = qty x "
        "unit cost. Mark Y for parts every printer needs (counts toward the $400 per-printer "
        "criterion) and N for one-off project items (tools, spares). Totals are checked against "
        "the project allowance ($350 plus 10% = $385, docs/plan.md; the proposal also states "
        "$1,200, so confirm the budget and edit C33 if needed) and the per-printer criterion.",
        len(heads),
    )
    for j, (h, w) in enumerate(heads):
        _head(ws.cell(row=3, column=j + 1), h)
        ws.column_dimensions[get_column_letter(j + 1)].width = w
    ws.row_dimensions[3].height = 32
    dv_qty = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0")
    dv_qty.allow_blank = True
    dv_yn = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv_qty)
    ws.add_data_validation(dv_yn)
    first, n_rows = 4, 25
    last = first + n_rows - 1
    for r in range(first, last + 1):
        for j in range(len(heads)):
            cell = ws.cell(row=r, column=j + 1)
            if j == 5:
                _formula(cell, f'=IF(OR(D{r}="",E{r}=""),"",D{r}*E{r})', '"$"#,##0.00')
            else:
                _input(cell)
                if j in (3, 4):
                    dv_qty.add(cell)
                    if j == 4:
                        cell.number_format = '"$"#,##0.00'
                elif j == 6:
                    dv_yn.add(cell)
    s = last + 2  # 30
    money = '"$"#,##0.00'
    rows = [
        ("Total, all items", f"=SUM(F{first}:F{last})"),
        ("Per-printer cost (items marked Y)", f'=SUMIF(G{first}:G{last},"Y",F{first}:F{last})'),
        ("Items with no Y/N mark", f'=SUMPRODUCT((F{first}:F{last}<>"")*(G{first}:G{last}=""))'),
        ("Project hardware allowance", PROJECT_ALLOWANCE_CAD),
        ("Per-printer criterion (under)", PER_PRINTER_LIMIT_CAD),
        ("Allowance remaining", f"=C{s + 3}-C{s}"),
        ("Check: total within allowance", f'=IF(C{s}<=C{s + 3},"OK","OVER")'),
        ("Check: per printer under criterion", f'=IF(C{s + 1}<C{s + 4},"OK","OVER")'),
    ]
    for k, (label, value) in enumerate(rows):
        r = s + k
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.cell(row=r, column=1, value=label).font = BOLD
        cell = ws.cell(row=r, column=3, value=value)
        cell.border = BOX
        if k not in (2, 6, 7):
            cell.number_format = money
    checks = f"C{s + 6}:C{s + 7}"
    ws.conditional_formatting.add(
        checks, CellIsRule(operator="equal", formula=['"OK"'], fill=OK_FILL)
    )
    ws.conditional_formatting.add(
        checks, CellIsRule(operator="equal", formula=['"OVER"'], fill=BAD_FILL)
    )
    ws.conditional_formatting.add(
        f"C{s + 5}", CellIsRule(operator="lessThan", formula=["0"], fill=BAD_FILL)
    )
    ws.freeze_panes = "A4"


def build(path: str | Path = OUT) -> Path:
    wb = Workbook()
    pairwise(wb)
    house_of_quality(wb)
    pugh(wb)
    weighted(wb)
    fmea(wb)
    bom(wb)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out = build(args[0] if args else OUT)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
