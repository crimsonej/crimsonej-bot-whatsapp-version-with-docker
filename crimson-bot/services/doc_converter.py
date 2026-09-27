"""
services/doc_converter.py
==========================
Document and media converter service.
Supports conversions between PDF, DOCX, TXT, images, and document editing.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

from PIL import Image
from core.config import log


def image_to_pdf(image_path: str, output_path: str | None = None) -> str:
    """Convert an image (PNG, JPG, WebP) to a PDF file."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")

    if not output_path:
        base, _ = os.path.splitext(image_path)
        output_path = f"{base}_converted.pdf"

    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")

    img.save(output_path, "PDF", resolution=100.0)
    log.info("[DocConverter] Converted image %s -> %s", image_path, output_path)
    return output_path


def pdf_to_docx(pdf_path: str, output_path: str | None = None) -> str:
    """Convert a PDF document to a DOCX file using pdf2docx."""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    if not output_path:
        base, _ = os.path.splitext(pdf_path)
        output_path = f"{base}_converted.docx"

    try:
        from pdf2docx import Converter
        cv = Converter(pdf_path)
        cv.convert(output_path, start=0, end=None)
        cv.close()
        log.info("[DocConverter] Converted PDF %s -> %s", pdf_path, output_path)
        return output_path
    except Exception as exc:
        log.error("[DocConverter] pdf2docx conversion failed: %s", exc)
        raise exc


def txt_to_pdf(txt_content_or_path: str, output_path: str | None = None) -> str:
    """Convert raw text or a .txt file into a PDF document using reportlab."""
    if os.path.exists(txt_content_or_path) and os.path.isfile(txt_content_or_path):
        with open(txt_content_or_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        if not output_path:
            base, _ = os.path.splitext(txt_content_or_path)
            output_path = f"{base}.pdf"
    else:
        text = txt_content_or_path
        if not output_path:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            output_path = tmp.name
            tmp.close()

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

        doc = SimpleDocTemplate(output_path, pagesize=letter)
        styles = getSampleStyleSheet()
        normal_style = styles["Normal"]
        normal_style.fontSize = 11
        normal_style.leading = 14

        story = []
        for paragraph in text.split("\n"):
            p_text = paragraph.strip()
            if p_text:
                # Escape XML entities for ReportLab
                p_clean = p_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(p_clean, normal_style))
                story.append(Spacer(1, 6))

        if not story:
            story.append(Paragraph("Empty Document", normal_style))

        doc.build(story)
        log.info("[DocConverter] Generated PDF at %s", output_path)
        return output_path
    except Exception as exc:
        log.error("[DocConverter] txt_to_pdf failed: %s", exc)
        raise exc


def docx_to_pdf(docx_path: str, output_path: str | None = None) -> str:
    """Convert a DOCX document to a PDF file by extracting paragraphs and formatting with ReportLab."""
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")

    if not output_path:
        base, _ = os.path.splitext(docx_path)
        output_path = f"{base}_converted.pdf"

    try:
        import docx
        doc_in = docx.Document(docx_path)
        text_lines = []
        for p in doc_in.paragraphs:
            if p.text.strip():
                text_lines.append(p.text)
        for table in doc_in.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    text_lines.append(row_text)

        full_text = "\n".join(text_lines)
        return txt_to_pdf(full_text, output_path)
    except Exception as exc:
        log.error("[DocConverter] docx_to_pdf failed: %s", exc)
        raise exc


def edit_docx(docx_path: str, replacements: Dict[str, str] | None = None,
              additions: List[str] | None = None, output_path: str | None = None) -> str:
    """
    Edit a DOCX document by performing text search-and-replace and appending new paragraphs.
    """
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")

    if not output_path:
        base, _ = os.path.splitext(docx_path)
        output_path = f"{base}_edited.docx"

    import docx
    doc = docx.Document(docx_path)

    replacements = replacements or {}
    additions = additions or []

    if replacements:
        for p in doc.paragraphs:
            for old, new in replacements.items():
                if old in p.text:
                    p.text = p.text.replace(old, new)

        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        for old, new in replacements.items():
                            if old in p.text:
                                p.text = p.text.replace(old, new)

    for add_text in additions:
        doc.add_paragraph(add_text)

    doc.save(output_path)
    log.info("[DocConverter] Edited DOCX saved to %s", output_path)
    return output_path


def convert_document(file_path: str, target_format: str, output_path: str | None = None) -> str:
    """
    Universal document conversion dispatcher.
    Supported target formats: pdf, docx, txt, png.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Source file not found: {file_path}")

    target_fmt = target_format.lower().strip().replace(".", "")
    _, ext = os.path.splitext(file_path)
    src_fmt = ext.lower().replace(".", "")

    if src_fmt == target_fmt:
        return file_path

    if target_fmt == "pdf":
        if src_fmt in ("png", "jpg", "jpeg", "webp", "bmp"):
            return image_to_pdf(file_path, output_path)
        elif src_fmt == "docx":
            return docx_to_pdf(file_path, output_path)
        elif src_fmt in ("txt", "md", "json", "csv"):
            return txt_to_pdf(file_path, output_path)
        else:
            raise ValueError(f"Conversion from {src_fmt} to pdf is not supported")

    elif target_fmt == "docx":
        if src_fmt == "pdf":
            return pdf_to_docx(file_path, output_path)
        elif src_fmt in ("txt", "md"):
            import docx
            doc = docx.Document()
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    doc.add_paragraph(line.rstrip())
            if not output_path:
                output_path = f"{os.path.splitext(file_path)[0]}.docx"
            doc.save(output_path)
            return output_path

    raise ValueError(f"Unsupported conversion: {src_fmt} -> {target_fmt}")
