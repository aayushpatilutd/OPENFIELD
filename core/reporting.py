"""
The exportable workbook: a summary, a payment plan, every exception, and then
one tab per vendor.

The per-vendor tab is the point. An AP clerk does not work a 600-row flat file;
they work one supplier at a time, because that is how the money goes out and
how the phone call goes. So each vendor gets a sheet that stands on its own —
their terms, whether they sent a statement, what to pay, what is held back and
why — and the totals are live formulas so the sheet survives being edited.

Sheet-name rules that bite if ignored: Excel caps a name at 31 characters,
forbids : \\ / ? * [ ], and will not take two sheets differing only by case.
All three are handled in `_sheet_name`.
"""

import os
import re
import logging
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

import db
import config
from core import matching
from core.util import current_period

log = logging.getLogger(__name__)

FONT = "Arial"

NAVY    = "1F3864"
GREY    = "F2F2F2"
GREEN   = "C6EFCE"
AMBER   = "FFEB9C"
RED     = "FFC7CE"
BLUE    = "DDEBF7"

H_FONT  = Font(name=FONT, bold=True, color="FFFFFF", size=10)
H_FILL  = PatternFill("solid", fgColor=NAVY)
BODY    = Font(name=FONT, size=10)
BOLD    = Font(name=FONT, size=10, bold=True)
TITLE   = Font(name=FONT, size=14, bold=True, color=NAVY)
SUB     = Font(name=FONT, size=9, italic=True, color="666666")
THIN    = Side(style="thin", color="D0D7E5")
BORDER  = Border(bottom=THIN)

MONEY   = '$#,##0.00;($#,##0.00);"—"'

VERDICT_FILL = {
    "matched":           GREEN,
    "matched_no_doc":    GREEN,
    "credit":            BLUE,
    "paid":              GREY,
    "amount_mismatch":   RED,
    "statement_only":    RED,
    "qbo_only":          RED,
    "missing_bill":      AMBER,
    "missing_invoice":   AMBER,
    "missing_statement": AMBER,
    "invoice_only":      AMBER,
}

_INVALID = re.compile(r"[:\\/?*\[\]]")


def _sheet_name(name: str, used: set) -> str:
    """A legal, unique Excel sheet name."""
    base = _INVALID.sub("-", str(name or "Vendor")).strip() or "Vendor"
    base = base[:31]
    candidate, n = base, 2
    while candidate.lower() in used:
        suffix = f" ({n})"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate


def _header(ws, headers, row=1, widths=None):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font, cell.fill = H_FONT, H_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 26
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w


def _style_body(ws, first_row, last_row, ncols, money_cols=()):
    for r in range(first_row, last_row + 1):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=r, column=c)
            cell.font = BODY
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top",
                                       wrap_text=(c == ncols))
            if c in money_cols:
                cell.number_format = MONEY
                cell.alignment = Alignment(vertical="top", horizontal="right")


def build_workbook(period: str = "", created_by: str = "") -> dict:
    """Generate the reconciliation workbook. Returns {path, filename, tabs}."""
    period = period or current_period()
    rep = matching.report(period)
    plan = matching.payment_plan(period)
    groups = rep["groups"]
    totals = rep["totals"]
    labels = rep["verdict_labels"]

    company = db.get_setting("company_name", "Your Company")
    wb = Workbook()
    used_names = set()

    # ── Summary ──────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = _sheet_name("Summary", used_names)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 14
    ws.column_dimensions["F"].width = 52

    ws["A1"] = f"{company} — accounts payable reconciliation"
    ws["A1"].font = TITLE
    ws["A2"] = (f"Period {period} · generated "
                f"{datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}"
                + (f" by {created_by}" if created_by else ""))
    ws["A2"].font = SUB
    if db.demo_mode():
        ws["A3"] = ("DEMO DATA — this workbook was generated with the portal in "
                    "demo mode. None of these figures are real.")
        ws["A3"].font = Font(name=FONT, size=10, bold=True, color="C00000")

    r = 5
    for label, value, fmt in (
        ("Vendors in the run",        totals["vendors"],    None),
        ("Invoice numbers compared",  totals["rows"],       None),
        ("Matched and payable",       totals["matched"],    None),
        ("Needing attention",         totals["exceptions"], None),
        ("Already paid",              totals["paid"],       None),
        ("", "", None),
        ("Gross to pay",              totals["to_pay"],     MONEY),
        ("Credits to apply",          totals["credits"],    MONEY),
        ("NET TO PAY",                totals["net_to_pay"], MONEY),
    ):
        if label:
            ws.cell(row=r, column=1, value=label).font = (
                BOLD if label.isupper() else BODY)
            c = ws.cell(row=r, column=2, value=value)
            c.font = BOLD if label.isupper() else BODY
            if fmt:
                c.number_format = fmt
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="By vendor").font = BOLD
    r += 1
    head_row = r
    _header(ws, ["Vendor", "Statement received", "To pay", "Credits",
                 "Net", "Exceptions"], row=head_row)
    ws.column_dimensions["B"].width = 20

    first = head_row + 1
    for g in groups:
        ws.cell(row=r + 1, column=1, value=g["vendor_name"])
        ws.cell(row=r + 1, column=2,
                value="yes" if g["statement_received"] else "NOT RECEIVED")
        ws.cell(row=r + 1, column=3, value=g["to_pay"]).number_format = MONEY
        ws.cell(row=r + 1, column=4, value=g["credits"]).number_format = MONEY
        ws.cell(row=r + 1, column=5, value=g["net_to_pay"]).number_format = MONEY
        ws.cell(row=r + 1, column=6, value=g["exceptions"])
        if not g["statement_received"]:
            ws.cell(row=r + 1, column=2).fill = PatternFill("solid", fgColor=AMBER)
        if g["exceptions"]:
            ws.cell(row=r + 1, column=6).fill = PatternFill("solid", fgColor=RED)
        r += 1

    if groups:
        _style_body(ws, first, r, 6, money_cols=(3, 4, 5))
        # Live totals, so the sheet still adds up after someone edits a cell.
        r += 1
        ws.cell(row=r, column=1, value="TOTAL").font = BOLD
        for col in (3, 4, 5):
            c = ws.cell(row=r, column=col,
                        value=f"=SUM({get_column_letter(col)}{first}:"
                              f"{get_column_letter(col)}{r-1})")
            c.font, c.number_format = BOLD, MONEY
        c = ws.cell(row=r, column=6, value=f"=SUM(F{first}:F{r-1})")
        c.font = BOLD

    if totals["no_statement"]:
        r += 2
        ws.cell(row=r, column=1, value="No statement received this period").font = BOLD
        r += 1
        ws.cell(row=r, column=1,
                value=", ".join(totals["no_statement"])).font = BODY
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True,
                                                       vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)

    ws.freeze_panes = f"A{head_row + 1}"

    # ── Payment plan ─────────────────────────────────────────────────────────
    ws = wb.create_sheet(_sheet_name("Payment plan", used_names))
    _header(ws, ["Vendor", "Method", "Account", "Invoices", "Pay now",
                 "Held back", "Why held"],
            widths=[32, 14, 18, 10, 15, 11, 64])
    r = 2
    for p in plan["plan"]:
        ws.cell(row=r, column=1, value=p["vendor"])
        ws.cell(row=r, column=2, value=p["method"])
        ws.cell(row=r, column=3, value=p.get("account") or "")
        ws.cell(row=r, column=4, value=p["invoice_count"])
        ws.cell(row=r, column=5, value=p["pay_now"]).number_format = MONEY
        ws.cell(row=r, column=6, value=len(p["held_back"]))
        ws.cell(row=r, column=7,
                value="; ".join(f"{h['number']}: {h['verdict']}"
                                for h in p["held_back"][:6]))
        if p["held_back"]:
            ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor=AMBER)
        r += 1
    if plan["plan"]:
        _style_body(ws, 2, r - 1, 7, money_cols=(5,))
        ws.cell(row=r, column=4, value="TOTAL").font = BOLD
        c = ws.cell(row=r, column=5, value=f"=SUM(E2:E{r-1})")
        c.font, c.number_format = BOLD, MONEY
    ws.freeze_panes = "A2"

    # ── Exceptions ───────────────────────────────────────────────────────────
    ws = wb.create_sheet(_sheet_name("Exceptions", used_names))
    _header(ws, ["Vendor", "Invoice", "Invoice date", "Due", "Invoice",
                 "Statement", "QuickBooks", "Verdict", "What it means"],
            widths=[28, 18, 13, 13, 14, 14, 14, 24, 66])
    r = 2
    for g in groups:
        for row in g["rows"]:
            if row["verdict"] not in matching.EXCEPTIONS or row.get("resolved"):
                continue
            ws.cell(row=r, column=1, value=g["vendor_name"])
            ws.cell(row=r, column=2, value=row["invoice_number"])
            ws.cell(row=r, column=3, value=row.get("invoice_date") or "")
            ws.cell(row=r, column=4, value=row.get("due_date") or "")
            ws.cell(row=r, column=5, value=row.get("amt_invoice"))
            ws.cell(row=r, column=6, value=row.get("amt_statement"))
            ws.cell(row=r, column=7, value=row.get("amt_qbo"))
            v = ws.cell(row=r, column=8, value=labels.get(row["verdict"],
                                                          row["verdict"]))
            v.fill = PatternFill("solid",
                                 fgColor=VERDICT_FILL.get(row["verdict"], GREY))
            ws.cell(row=r, column=9, value=row.get("note") or "")
            r += 1
    if r > 2:
        _style_body(ws, 2, r - 1, 9, money_cols=(5, 6, 7))
    else:
        ws.cell(row=2, column=1,
                value="Nothing needed attention this period.").font = SUB
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:I{max(r - 1, 2)}"

    # ── one tab per vendor ───────────────────────────────────────────────────
    tabs = 0
    for g in groups:
        ws = wb.create_sheet(_sheet_name(g["vendor_name"], used_names))
        tabs += 1
        ws.sheet_view.showGridLines = False

        ws.column_dimensions["A"].width = 20
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 13
        ws.column_dimensions["D"].width = 14
        ws.column_dimensions["E"].width = 14
        ws.column_dimensions["F"].width = 14
        ws.column_dimensions["G"].width = 26
        ws.column_dimensions["H"].width = 62

        ws["A1"] = g["vendor_name"]
        ws["A1"].font = TITLE
        bits = [f"Period {period}"]
        if g.get("terms_days"):
            bits.append(f"net {g['terms_days']}")
        if g.get("payment_method"):
            bits.append(f"pay by {g['payment_method']}")
        if g.get("account_number"):
            bits.append(f"account {g['account_number']}")
        ws["A2"] = " · ".join(bits)
        ws["A2"].font = SUB

        ws["A3"] = ("Statement received" if g["statement_received"]
                    else "NO STATEMENT RECEIVED THIS PERIOD")
        ws["A3"].font = Font(name=FONT, size=10, bold=not g["statement_received"],
                             color="006100" if g["statement_received"] else "9C0006")

        ws["A5"] = "Net to pay"
        ws["A5"].font = BOLD
        c = ws["B5"]
        c.value = g["net_to_pay"]
        c.font, c.number_format = BOLD, MONEY
        ws["C5"] = f"{g['exceptions']} needing attention"
        ws["C5"].font = SUB

        head = 7
        _header(ws, ["Invoice", "Invoice date", "Due", "Invoice amt",
                     "Statement amt", "QuickBooks", "Verdict", "What it means"],
                row=head)
        r = head + 1
        first_data = r
        for row in sorted(g["rows"], key=lambda x: (x["verdict"] != "matched",
                                                    x["invoice_number"])):
            ws.cell(row=r, column=1, value=row["invoice_number"])
            ws.cell(row=r, column=2, value=row.get("invoice_date") or "")
            ws.cell(row=r, column=3, value=row.get("due_date") or "")
            ws.cell(row=r, column=4, value=row.get("amt_invoice"))
            ws.cell(row=r, column=5, value=row.get("amt_statement"))
            ws.cell(row=r, column=6, value=row.get("amt_qbo"))
            v = ws.cell(row=r, column=7,
                        value=labels.get(row["verdict"], row["verdict"]))
            v.fill = PatternFill("solid",
                                 fgColor=VERDICT_FILL.get(row["verdict"], GREY))
            note = row.get("note") or ""
            if row.get("paid_at"):
                note = f"PAID {row['paid_at'][:10]}" + (
                    f" ref {row['paid_ref']}" if row.get("paid_ref") else "") \
                    + ". " + note
            ws.cell(row=r, column=8, value=note)
            r += 1

        if r > first_data:
            _style_body(ws, first_data, r - 1, 8, money_cols=(4, 5, 6))
            ws.cell(row=r, column=3, value="TOTAL").font = BOLD
            for col in (4, 5, 6):
                L = get_column_letter(col)
                cc = ws.cell(row=r, column=col,
                             value=f"=SUM({L}{first_data}:{L}{r-1})")
                cc.font, cc.number_format = BOLD, MONEY
            # The three totals sitting side by side is the fastest way to see
            # that a vendor's statement and your books disagree in aggregate,
            # even when every individual line looks plausible.
            r += 2
            ws.cell(row=r, column=1,
                    value="Statement total less QuickBooks total").font = SUB
            d = ws.cell(row=r, column=4,
                        value=f"=E{r-2}-F{r-2}")
            d.number_format, d.font = MONEY, BOLD
        else:
            ws.cell(row=first_data, column=1,
                    value="Nothing on record for this vendor this period."
                    ).font = SUB

        ws.freeze_panes = f"A{head + 1}"

    # ── write it out ─────────────────────────────────────────────────────────
    reports_dir = os.path.join(config.STORAGE, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    filename = f"AP-reconciliation-{period}-{stamp}.xlsx"
    path = os.path.join(reports_dir, filename)
    wb.save(path)

    size = os.path.getsize(path)
    con = db.get_db()
    try:
        con.execute("""INSERT INTO reports
            (period, filename, path, size_bytes, vendor_tabs, created_at, created_by)
            VALUES (?,?,?,?,?,?,?)""",
            (period, filename, path, size, tabs, db.utcnow(), created_by))
        con.commit()
    finally:
        con.close()

    db.say("report", f"Built the {period} reconciliation workbook",
           f"{tabs} vendor tab(s), {totals['rows']} invoice number(s), "
           f"net {totals['net_to_pay']:,.2f} to pay.", actor=created_by or "system")

    return {"ok": True, "path": path, "filename": filename,
            "vendor_tabs": tabs, "size_bytes": size, "period": period}
