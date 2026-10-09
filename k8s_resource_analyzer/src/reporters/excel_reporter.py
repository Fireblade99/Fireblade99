"""
Excel reporter – generates a formatted .xlsx report for business stakeholders.

Sheets
------
1. Summary   – total waste stats + top-10 wasteful workloads
2. Report    – full recommendations table with colour-coded waste %
"""

import datetime
from typing import List, Optional

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, GradientFill, PatternFill, Side
)
from openpyxl.utils import get_column_letter

from ..core.recommender import Recommendation
from ..utils import fmt_bytes, fmt_cores

# ── Colour palette ─────────────────────────────────────────────────────────
_RED    = "FFDD2222"
_ORANGE = "FFFF6600"
_YELLOW = "FFFFC000"
_GREEN  = "FF00AA44"
_LIGHT_RED    = "FFFFC7C7"
_LIGHT_ORANGE = "FFFFDDB0"
_LIGHT_YELLOW = "FFFFF0B0"
_LIGHT_GREEN  = "FFD6F5D6"
_HEADER_BG    = "FF1F4E79"
_SUMMARY_BG   = "FF2E75B6"
_RISKY_BG     = "FFFFF2CC"


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _font(bold: bool = False, color: str = "FF000000", size: int = 11) -> Font:
    return Font(bold=bold, color=color, size=size)


def _border() -> Border:
    thin = Side(style="thin", color="FFD0D0D0")
    return Border(left=thin, right=thin, top=thin, bottom=thin)


def _waste_fill(ratio: Optional[float]) -> Optional[PatternFill]:
    if ratio is None or ratio <= 0:
        return None
    if ratio >= 0.80:
        return _fill(_LIGHT_RED)
    if ratio >= 0.60:
        return _fill(_LIGHT_ORANGE)
    if ratio >= 0.40:
        return _fill(_LIGHT_YELLOW)
    return _fill(_LIGHT_GREEN)


def _waste_label(ratio: Optional[float]) -> str:
    if ratio is None or ratio <= 0:
        return ""
    if ratio >= 0.80:
        return f"{ratio:.0%} ⚠ HIGH"
    if ratio >= 0.60:
        return f"{ratio:.0%} ↑ MED"
    return f"{ratio:.0%}"


# ── Public API ─────────────────────────────────────────────────────────────

def generate(
    recommendations: List[Recommendation],
    output_path: str,
    show_only_waste: bool = True,
    lookback_days: int = 7,
) -> str:
    """Write the report to *output_path* and return the path."""
    items = list(recommendations)
    if show_only_waste:
        items = [r for r in items if r.is_wasteful or r.is_risky]
    items.sort(
        key=lambda r: (r.memory_waste_bytes or 0) if (r.memory_waste_bytes or 0) > 0 else 0,
        reverse=True,
    )

    wb = Workbook()
    _sheet_summary(wb, items, lookback_days)
    _sheet_report(wb, items)
    wb.save(output_path)
    return output_path


# ── Sheet 1: Summary ───────────────────────────────────────────────────────

def _sheet_summary(wb: Workbook, items: List[Recommendation], lookback_days: int) -> None:
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 22

    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    wasteful = [r for r in items if r.is_wasteful]
    risky    = [r for r in items if r.is_risky]
    total_mem = sum((r.memory_waste_bytes or 0) for r in wasteful if (r.memory_waste_bytes or 0) > 0)
    total_cpu = sum((r.cpu_waste_cores   or 0) for r in wasteful if (r.cpu_waste_cores   or 0) > 0)

    def _hdr(row: int, text: str) -> None:
        c = ws.cell(row=row, column=1, value=text)
        c.font = _font(bold=True, color="FFFFFFFF", size=13)
        c.fill = _fill(_SUMMARY_BG)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[row].height = 22
        ws.merge_cells(f"A{row}:B{row}")

    def _row(row: int, label: str, value: str, bold_val: bool = False) -> None:
        lc = ws.cell(row=row, column=1, value=label)
        vc = ws.cell(row=row, column=2, value=value)
        lc.font = _font(size=11)
        vc.font = _font(bold=bold_val, size=11)
        lc.border = vc.border = _border()
        vc.alignment = Alignment(horizontal="right")
        ws.row_dimensions[row].height = 18

    # Title
    title = ws.cell(row=1, column=1, value="☁  Kubernetes Resource Waste Report")
    title.font = _font(bold=True, color="FFFFFFFF", size=16)
    title.fill = _fill(_HEADER_BG)
    title.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30
    ws.merge_cells("A1:B1")

    ws.cell(row=2, column=1, value=f"Generated: {date_str}  |  Lookback: {lookback_days} days")
    ws.cell(row=2, column=1).font = _font(color="FF666666", size=10)
    ws.merge_cells("A2:B2")
    ws.row_dimensions[2].height = 16

    ws.row_dimensions[3].height = 8  # spacer

    _hdr(4, "Overview")
    _row(5,  "Analysis period",          f"{lookback_days} days")
    _row(6,  "Over-provisioned workloads", str(len(wasteful)), bold_val=True)
    _row(7,  "Under-provisioned (risky)",  str(len(risky)),    bold_val=True)
    _row(8,  "Total wasted memory",        fmt_bytes(total_mem), bold_val=True)
    _row(9,  "Total wasted CPU",           f"{fmt_cores(total_cpu)} cores", bold_val=True)

    ws.row_dimensions[10].height = 8  # spacer

    _hdr(11, "Top 10 — Highest Memory Waste")
    top10_headers = ["Workload", "Namespace", "Container",
                     "Mem Request", "Mem Max", "Mem Waste", "Waste %", "Recommended Request"]
    col_widths = [42, 24, 20, 14, 14, 14, 10, 22]
    for i, h in enumerate(top10_headers, start=1):
        c = ws.cell(row=12, column=i, value=h)
        c.font = _font(bold=True, color="FFFFFFFF", size=10)
        c.fill = _fill(_SUMMARY_BG)
        c.border = _border()
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = col_widths[i - 1]

    for idx, rec in enumerate(items[:10], start=13):
        g = rec.group
        row_data = [
            g.base_name,
            g.namespace,
            g.container,
            fmt_bytes(g.memory_request),
            fmt_bytes(g.max_memory_usage),
            fmt_bytes(rec.memory_waste_bytes) if (rec.memory_waste_bytes or 0) > 0 else "-",
            _waste_label(rec.memory_waste_ratio),
            fmt_bytes(rec.recommended_memory_request),
        ]
        fill = _waste_fill(rec.memory_waste_ratio) or PatternFill()
        for col, val in enumerate(row_data, start=1):
            c = ws.cell(row=idx, column=col, value=val)
            c.font = _font(size=10)
            c.border = _border()
            c.alignment = Alignment(horizontal="left" if col <= 3 else "right")
            if col in (6, 7):
                c.fill = fill


# ── Sheet 2: Full report ───────────────────────────────────────────────────

_REPORT_HEADERS = [
    "Namespace", "Workload", "Container", "Pods",
    "CPU Request", "CPU Max", "CPU Rec",
    "Mem Request", "Mem Max", "Mem Rec",
    "CPU Waste", "CPU Waste %",
    "Mem Waste", "Mem Waste %",
    "Status", "Notes",
]
_REPORT_WIDTHS = [24, 48, 20, 7, 12, 12, 12, 14, 14, 14, 12, 12, 14, 12, 14, 50]


def _sheet_report(wb: Workbook, items: List[Recommendation]) -> None:
    ws = wb.create_sheet(title="Recommendations")
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A2"

    for i, (h, w) in enumerate(zip(_REPORT_HEADERS, _REPORT_WIDTHS), start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = _font(bold=True, color="FFFFFFFF", size=10)
        c.fill = _fill(_HEADER_BG)
        c.border = _border()
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 28

    for row_idx, rec in enumerate(items, start=2):
        g = rec.group
        mem_waste_val = rec.memory_waste_bytes if (rec.memory_waste_bytes or 0) > 0 else None
        cpu_waste_val = rec.cpu_waste_cores   if (rec.cpu_waste_cores   or 0) > 0 else None

        if rec.is_risky and rec.is_wasteful:
            status = "⚠ RISKY + WASTE"
        elif rec.is_risky:
            status = "⚠ UNDER-PROVISIONED"
        elif rec.is_wasteful:
            status = "↓ OVER-PROVISIONED"
        else:
            status = "✓ OK"

        row_data = [
            g.namespace,
            g.base_name,
            g.container,
            len(g.pod_names),
            fmt_cores(g.cpu_request),
            fmt_cores(g.max_cpu_usage),
            fmt_cores(rec.recommended_cpu_request),
            fmt_bytes(g.memory_request),
            fmt_bytes(g.max_memory_usage),
            fmt_bytes(rec.recommended_memory_request),
            fmt_cores(cpu_waste_val),
            f"{rec.cpu_waste_ratio:.0%}" if rec.cpu_waste_ratio and rec.cpu_waste_ratio > 0 else "-",
            fmt_bytes(mem_waste_val),
            _waste_label(rec.memory_waste_ratio) if (rec.memory_waste_bytes or 0) > 0 else "-",
            status,
            " | ".join(rec.reasons),
        ]

        mem_fill  = _waste_fill(rec.memory_waste_ratio)
        cpu_fill  = _waste_fill(rec.cpu_waste_ratio)
        risky_row = _fill(_RISKY_BG) if rec.is_risky and not rec.is_wasteful else None

        for col, val in enumerate(row_data, start=1):
            c = ws.cell(row=row_idx, column=col, value=val)
            c.font = _font(size=10)
            c.border = _border()
            c.alignment = Alignment(
                horizontal="left" if col in (1, 2, 3, 15, 16) else "right",
                wrap_text=(col == 16),
            )
            # Apply colour
            if col in (11, 12) and cpu_fill:
                c.fill = cpu_fill
            elif col in (13, 14) and mem_fill:
                c.fill = mem_fill
            elif risky_row and col not in (11, 12, 13, 14):
                c.fill = risky_row

        ws.row_dimensions[row_idx].height = 16

