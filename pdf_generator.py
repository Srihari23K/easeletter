
import os
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LETTERHEAD_PATH = os.path.join(BASE_DIR, "static", "LetterTemplate.pdf")
HEADER_HEIGHT_PT = 205


def _build_overlay(letter_text: str, title: str = "") -> PdfReader:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=HEADER_HEIGHT_PT,
        bottomMargin=50,
    )

    styles = getSampleStyleSheet()
    body_style = styles["Normal"]
    body_style.fontSize = 11
    body_style.leading = 17

    title_style = ParagraphStyle(
        "LetterTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0b1a2b"),
        spaceAfter=18,
    )

    story = []
    if title and title.strip():
        story.append(Paragraph(title.strip(), title_style))

    for para in letter_text.split("\n\n"):
        story.append(Paragraph(para.replace("\n", "<br/>"), body_style))
        story.append(Spacer(1, 12))

    doc.build(story)
    buffer.seek(0)
    return PdfReader(buffer)


def generate_letter_pdf(letter_text: str, title: str = "", letterhead_path: str = None) -> bytes:
    letterhead_path = letterhead_path or DEFAULT_LETTERHEAD_PATH
    overlay_reader = _build_overlay(letter_text, title=title)

    writer = PdfWriter()
    letterhead_exists = os.path.exists(letterhead_path)

    for overlay_page in overlay_reader.pages:
        if letterhead_exists:
            letterhead_page = PdfReader(letterhead_path).pages[0]
            letterhead_page.merge_page(overlay_page)
            writer.add_page(letterhead_page)
        else:
            writer.add_page(overlay_page)

    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output.read()