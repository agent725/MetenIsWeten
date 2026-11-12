#!/usr/bin/env python3
"""
label_pi.py — Printable Pi Labels

Generates PDF labels for OTGW Logger Raspberry Pis.
Labels include Pi ID (large), QR code, build date, and a blank line for
handwritten operator notes (e.g. installation reference number).

No customer names or addresses are stored in the label or the fleet register.
The operator maintains their own Pi ID → location mapping offline.

Layout: A4 page, 3×6 grid = 18 labels per sheet (~63×45mm each).
Aligned to Avery L7164 / equivalent A4 label sheets.

Usage:
    # Batch: all undeployed Pis from fleet register
    python3 scripts/label_pi.py image/fleet_register.csv --output labels.pdf

    # Single label by ID
    python3 scripts/label_pi.py --id a1b2c3d4 --output label.pdf

    # ASCII preview (no PDF deps needed)
    python3 scripts/label_pi.py --id a1b2c3d4 --ascii

Dependencies:
    pip3 install segno reportlab
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import csv
import io
import sys
from datetime import date
from pathlib import Path
from typing import Optional


# ============================================================================
# Label layout constants (Avery L7164 — A4, 3×8 grid, ~64×45mm)
# Using 3×6 = 18 labels per sheet so labels are slightly taller
# ============================================================================

PAGE_WIDTH_MM = 210.0
PAGE_HEIGHT_MM = 297.0

COLS = 3
ROWS = 6
GUTTER_MM = 3.0        # gap between labels
MARGIN_LEFT_MM = 5.0   # left edge margin
MARGIN_TOP_MM = 10.0   # top edge margin
MARGIN_RIGHT_MM = 5.0
MARGIN_BOTTOM_MM = 10.0

# Computed label dimensions
LABEL_W_MM = (
    PAGE_WIDTH_MM
    - MARGIN_LEFT_MM
    - MARGIN_RIGHT_MM
    - (COLS - 1) * GUTTER_MM
) / COLS

LABEL_H_MM = (
    PAGE_HEIGHT_MM
    - MARGIN_TOP_MM
    - MARGIN_BOTTOM_MM
    - (ROWS - 1) * GUTTER_MM
) / ROWS

# Padding inside each label
PAD_MM = 3.0

# QR code size inside label
QR_SIZE_MM = LABEL_H_MM - 2 * PAD_MM


# ============================================================================
# Fleet register
# ============================================================================

def load_fleet_register(csv_path: Path) -> list[dict]:
    """Load fleet_register.csv; return list of row dicts."""
    rows = []
    with csv_path.open(newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): v.strip() for k, v in row.items()})
    return rows


def filter_undeployed(rows: list[dict]) -> list[dict]:
    """Return rows where 'deployed' is not 'yes'."""
    return [r for r in rows if r.get('deployed', 'no').lower() != 'yes']


def row_to_label_data(row: dict) -> dict:
    """Convert a fleet register row to label fields."""
    return {
        'image_id': row.get('image_id', '').strip(),
        'build_date': row.get('build_date', '').strip(),
    }


# ============================================================================
# ASCII fallback
# ============================================================================

def print_ascii_label(image_id: str, build_date: str) -> None:
    """Print a simple ASCII label to stdout (no dependencies)."""
    width = 35
    border = '─' * (width - 2)
    print(f"┌{border}┐")
    print(f"│{'OTGW Logger':^{width - 2}}│")
    print(f"│{' ' * (width - 2)}│")
    qr_row = f"  ██████  ID: {image_id}"
    print(f"│{qr_row:<{width - 2}}│")
    bd_row = f"  ██████  Built: {build_date}"
    print(f"│{bd_row:<{width - 2}}│")
    notes_row = f"  ██  ██  {'_' * 14}"
    print(f"│{notes_row:<{width - 2}}│")
    print(f"│{'  ██████' + ' ' * (width - 10)}│")
    print(f"│{'  ██████' + ' ' * (width - 10)}│")
    print(f"└{border}┘")
    print(f"QR encodes: {image_id}")
    print()


# ============================================================================
# PDF generation
# ============================================================================

def _mm(value: float) -> float:
    """Convert mm to reportlab points."""
    # reportlab uses 1pt = 1/72 inch; 1 inch = 25.4mm
    return value * 72.0 / 25.4


def generate_qr_png(image_id: str, size_px: int = 120) -> bytes:
    """Generate QR code PNG bytes using segno."""
    import segno
    qr = segno.make_qr(image_id, error='M')
    buf = io.BytesIO()
    qr.save(buf, kind='png', scale=max(1, size_px // (qr.symbol_size()[0])))
    buf.seek(0)
    return buf.read()


def draw_label(
    canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    image_id: str,
    build_date: str,
    draw_cut_marks: bool = True,
) -> None:
    """Draw a single label at position (x_mm, y_mm) on the canvas."""
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader

    x = _mm(x_mm)
    y = _mm(y_mm)
    w = _mm(w_mm)
    h = _mm(h_mm)
    pad = _mm(PAD_MM)
    qr_size = _mm(QR_SIZE_MM)

    # Label border (thin, light grey)
    canvas.setStrokeColor(colors.lightgrey)
    canvas.setLineWidth(0.5)
    canvas.rect(x, y, w, h)

    if draw_cut_marks:
        _draw_cut_marks(canvas, x_mm, y_mm, w_mm, h_mm)

    # QR code (left side)
    qr_png = generate_qr_png(image_id, size_px=int(qr_size * 3))
    qr_img = ImageReader(io.BytesIO(qr_png))
    canvas.drawImage(
        qr_img,
        x + pad,
        y + pad,
        width=qr_size,
        height=qr_size,
        preserveAspectRatio=True,
    )

    # Text (right of QR)
    text_x = x + qr_size + 2 * pad
    text_w = w - qr_size - 3 * pad

    # Title
    canvas.setFont('Helvetica-Bold', 7)
    canvas.setFillColor(colors.black)
    canvas.drawString(text_x, y + h - pad - _mm(3), 'OTGW Logger')

    # Image ID — large
    canvas.setFont('Helvetica-Bold', 11)
    canvas.drawString(text_x, y + h - pad - _mm(9), image_id)

    # Build date
    canvas.setFont('Helvetica', 7)
    canvas.drawString(text_x, y + h - pad - _mm(15), f'Built: {build_date}')

    # Blank notes line (for handwritten operator reference — no digital PII)
    canvas.setStrokeColor(colors.black)
    canvas.setLineWidth(0.5)
    canvas.line(text_x, y + h - pad - _mm(21), text_x + text_w, y + h - pad - _mm(21))


def _draw_cut_marks(
    canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
) -> None:
    """Draw corner cut marks (short lines outside the label border)."""
    from reportlab.lib import colors
    canvas.setStrokeColor(colors.grey)
    canvas.setLineWidth(0.3)
    mark = _mm(2.0)  # length of each cut mark line

    corners = [
        (x_mm, y_mm),                   # bottom-left
        (x_mm + w_mm, y_mm),             # bottom-right
        (x_mm, y_mm + h_mm),             # top-left
        (x_mm + w_mm, y_mm + h_mm),      # top-right
    ]
    for cx_mm, cy_mm in corners:
        cx, cy = _mm(cx_mm), _mm(cy_mm)
        # Horizontal tick
        sign_x = 1 if cx_mm == x_mm else -1
        canvas.line(cx, cy, cx + sign_x * mark, cy)
        # Vertical tick
        sign_y = 1 if cy_mm == y_mm else -1
        canvas.line(cx, cy, cx, cy + sign_y * mark)


def generate_pdf(
    labels: list[dict],
    output_path: Path,
) -> None:
    """Generate a PDF with labels on A4 pages, 3×6 grid."""
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4

    c = rl_canvas.Canvas(str(output_path), pagesize=A4)

    labels_per_page = COLS * ROWS

    for page_start in range(0, len(labels), labels_per_page):
        page_labels = labels[page_start: page_start + labels_per_page]

        for idx, label in enumerate(page_labels):
            col = idx % COLS
            row = idx // COLS

            x_mm = MARGIN_LEFT_MM + col * (LABEL_W_MM + GUTTER_MM)
            # reportlab Y axis: 0 = bottom; we start from top
            y_mm = (
                PAGE_HEIGHT_MM
                - MARGIN_TOP_MM
                - (row + 1) * LABEL_H_MM
                - row * GUTTER_MM
            )

            draw_label(
                c,
                x_mm=x_mm,
                y_mm=y_mm,
                w_mm=LABEL_W_MM,
                h_mm=LABEL_H_MM,
                image_id=label['image_id'],
                build_date=label['build_date'],
            )

        c.showPage()

    c.save()
    print(f"PDF written: {output_path} ({len(labels)} label(s))")


# ============================================================================
# CLI
# ============================================================================

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            'Generate printable Pi labels (PDF or ASCII). '
            'Pass a fleet_register.csv for batch mode, or --id for single.'
        )
    )
    p.add_argument(
        'csv',
        nargs='?',
        metavar='fleet_register.csv',
        help='Fleet register CSV (batch mode: all undeployed Pis)',
    )
    p.add_argument(
        '--id',
        metavar='IMAGE_ID',
        help='Single-label mode: Image ID (e.g. a1b2c3d4)',
    )
    p.add_argument(
        '--build-date',
        metavar='DATE',
        default=date.today().isoformat(),
        help='Build date for single-label mode (default: today)',
    )
    p.add_argument(
        '--output', '-o',
        metavar='FILE',
        help='Output PDF path (required unless --ascii)',
    )
    p.add_argument(
        '--ascii',
        action='store_true',
        help='ASCII fallback: print label(s) to stdout, no PDF deps',
    )
    return p


def main() -> int:
    p = build_argparser()
    args = p.parse_args()

    # Collect labels to render
    labels: list[dict] = []

    if args.id:
        labels.append({
            'image_id': args.id,
            'build_date': args.build_date,
        })
    elif args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Error: {csv_path} not found", file=sys.stderr)
            return 1
        rows = load_fleet_register(csv_path)
        undeployed = filter_undeployed(rows)
        if not undeployed:
            print("No undeployed Pis found in fleet register.")
            return 0
        labels = [row_to_label_data(r) for r in undeployed]
        print(f"Found {len(labels)} undeployed Pi(s) in {csv_path}")
    else:
        p.print_help()
        return 1

    if args.ascii:
        for lbl in labels:
            print_ascii_label(
                lbl['image_id'],
                lbl.get('build_date', ''),
            )
        return 0

    # PDF mode
    if not args.output:
        print(
            "Error: --output is required for PDF mode "
            "(or use --ascii for text preview)",
            file=sys.stderr,
        )
        return 1

    try:
        import segno  # noqa: F401
    except ImportError:
        print(
            "Error: 'segno' not installed. "
            "Run: pip3 install segno reportlab",
            file=sys.stderr,
        )
        return 1

    try:
        import reportlab  # noqa: F401
    except ImportError:
        print(
            "Error: 'reportlab' not installed. "
            "Run: pip3 install segno reportlab",
            file=sys.stderr,
        )
        return 1

    output_path = Path(args.output)
    generate_pdf(labels, output_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
