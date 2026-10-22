import os
from datetime import datetime
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

PDF_DIR = "exports"
MAX_ROWS = 300
FONT = "Helvetica"
try:  # optional: a Unicode font so symbols like the rupee sign print
    pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")))
    FONT = "DejaVu"
except Exception:
    pass

cell = ParagraphStyle("cell", fontName=FONT, fontSize=8, leading=10)
head = ParagraphStyle("head", fontName=FONT, fontSize=14, leading=18, spaceAfter=6)
line = ParagraphStyle("line", fontName=FONT, fontSize=10, leading=15)

def _label(key):
    return key.replace(".", " ").replace("_", " ").title()

def _p(value, style=cell):
    return Paragraph(escape(str(value)), style)


def create_pdf(title, sheets, layout="table", label=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    path = os.path.join(PDF_DIR, f"{label or 'report'}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    page = A4 if layout == "profile" else landscape(A4)
    width = page[0] - 60
    doc = SimpleDocTemplate(path, pagesize=page, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)

    story = [_p(title, head), _p(f"Generated {datetime.now():%d %b %Y, %H:%M}"), Spacer(1, 12)]
    first = next(iter(sheets), None)

    for name, rows in sheets.items():
        if not rows:
            continue
        if len(sheets) > 1:
            story += [Spacer(1, 10), _p(name, head)]

        # drop columns that are empty in every row
        keys = [k for k in dict.fromkeys(k for r in rows for k in r)
                if k not in ("_id", "record_id") and any(r.get(k) not in ("", None) for r in rows)]
        
        if layout == "profile" and name == first:
            same = [k for k in keys if all(r.get(k) == rows[0].get(k) for r in rows)]
            for k in same:
                story.append(Paragraph(f"<b>{escape(_label(k))}:</b> {escape(str(rows[0][k]))}", line))
            keys = [k for k in keys if k not in same]
            if not keys:
                continue
            story += [Spacer(1, 12), _p(f"Records ({len(rows)})", head)]

        data = [[_p(_label(k)) for k in keys]] + [[_p(r.get(k, "")) for k in keys] for r in rows[:MAX_ROWS]]
        table = Table(data, colWidths=[width / len(keys)] * len(keys), repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(table)
        if len(rows) > MAX_ROWS:
            story.append(_p(f"Showing {MAX_ROWS} of {len(rows)} rows. Use Excel for the full set."))

    doc.build(story)
    return path