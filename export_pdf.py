import os, re
from datetime import datetime
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.platypus import HRFlowable
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

PDF_DIR = "exports"
MAX_ROWS = 300
FONT_BOLD = "Helvetica-Bold"
FONT = "Helvetica"

if FONT == "DejaVu":
    try:
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans-Bold.ttf")))
        FONT_BOLD = "DejaVu-Bold"
    except Exception:
        FONT_BOLD = FONT
registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)

ACCENT = colors.HexColor("#1F4E79")
doc_title  = ParagraphStyle("doc_title",  fontName=FONT_BOLD, fontSize=20, leading=24, textColor=ACCENT, spaceAfter=4)
doc_meta   = ParagraphStyle("doc_meta",   fontName=FONT, fontSize=9, leading=12, textColor=colors.grey, spaceAfter=8)
doc_h      = ParagraphStyle("doc_h",      fontName=FONT_BOLD, fontSize=13, leading=17, textColor=ACCENT, spaceBefore=12, spaceAfter=4)
doc_body   = ParagraphStyle("doc_body",   fontName=FONT, fontSize=10.5, leading=16, alignment=TA_JUSTIFY, spaceAfter=6)
doc_bullet = ParagraphStyle("doc_bullet", parent=doc_body, leftIndent=16, bulletIndent=4, alignment=0, spaceAfter=3)


cell = ParagraphStyle("cell", fontName=FONT, fontSize=8, leading=10)
head = ParagraphStyle("head", fontName=FONT, fontSize=14, leading=18, spaceAfter=6)
line = ParagraphStyle("line", fontName=FONT, fontSize=10, leading=15)
head_cell = ParagraphStyle("head_cell", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=colors.white)
profile_line = ParagraphStyle("profile_line", parent=doc_body, alignment=0, spaceAfter=2)


def _label(key):
    return key.replace(".", " ").replace("_", " ").title()

def _p(value, style=cell):
    return Paragraph(escape(str(value)), style)

def _inline(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(s))

def _footer(canvas, doc):
    w = doc.pagesize[0]
    canvas.saveState()
    canvas.setFont(FONT, 8)
    canvas.setFillColor(colors.grey)
    canvas.drawString(doc.leftMargin, 24, "LIMS Copilot")
    canvas.drawRightString(w - doc.rightMargin, 24, f"Page {doc.page}")
    canvas.restoreState()    
    
def create_pdf(title, sheets, layout="table", label=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    path = os.path.join(PDF_DIR, f"{label or 'report'}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    page = A4 if layout in ("profile", "cards") else landscape(A4)
    margin = 40
    width = page[0] - 2 * margin
    doc = SimpleDocTemplate(path, pagesize=page, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=45, title=title)

    story = [
        _p(title, doc_title),
        _p(f"Generated {datetime.now():%d %b %Y, %H:%M}", doc_meta),
        HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceAfter=10),
    ]
    first = next(iter(sheets), None)

    for name, rows in sheets.items():
        if not rows:
            continue
        if len(sheets) > 1:
            story.append(_p(name, doc_h))

        # drop columns that are empty in every row
        keys = [k for k in dict.fromkeys(k for r in rows for k in r)
                if k not in ("_id", "record_id") and any(r.get(k) not in ("", None) for r in rows)]
        
        if layout == "cards":
            for r in rows[:MAX_ROWS]:
                block= [Paragraph(escape(str(r.get(keys[0], ""))), doc_h)]
                for k in keys[1:]:
                    v= r.get(k,"")
                    if v in ("", None, "0", 0, "NA", "na"):
                        continue
                    block.append(Paragraph(f"<b>{escape(_label(k))}:</b> {escape(str(v))}", profile_line))
                block.append(HRFlowable(width="100%", thickness=0.4, color=colors.HexColor("#C9D3DD"), spaceBefore=4, spaceAfter=4))
                story.append(KeepTogether(block))
            continue

        if layout == "profile" and name == first:
            same = [k for k in keys if all(r.get(k) == rows[0].get(k) for r in rows)]
            for k in same:
                story.append(Paragraph(f"<b>{escape(_label(k))}:</b> {escape(str(rows[0][k]))}", profile_line))
            keys = [k for k in keys if k not in same]
            if not keys:
                continue
            story.append(_p(f"Records ({len(rows)})", doc_h))

        if not keys:
            continue

        data = [[_p(_label(k), head_cell) for k in keys]] + \
               [[_p(r.get(k, "")) for k in keys] for r in rows[:MAX_ROWS]]
        table = Table(data, colWidths=[width / len(keys)] * len(keys), repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C9D3DD")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
        if len(rows) > MAX_ROWS:
            story.append(_p(f"Showing {MAX_ROWS} of {len(rows)} rows. Use Excel for the full set.", doc_meta))
        story.append(Spacer(1, 10))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


def create_text_pdf(title, text, label=None):
    os.makedirs(PDF_DIR, exist_ok=True)
    path = os.path.join(PDF_DIR, f"{label or 'document'}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=50, rightMargin=50,
                            topMargin=50, bottomMargin=50, title=title)
    story = [
        Paragraph(escape(title), doc_title),
        Paragraph(f"Generated {datetime.now():%d %b %Y, %H:%M}", doc_meta),
        HRFlowable(width="100%", thickness=1.2, color=ACCENT, spaceAfter=10),
    ]
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            story.append(Paragraph(_inline(s.lstrip("#").strip()), doc_h))
        elif s[:2] in ("- ", "* ", "• "):
            story.append(Paragraph(_inline(s[2:]), doc_bullet, bulletText="•"))
        else:
            story.append(Paragraph(_inline(s), doc_body))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path