from pathlib import Path

from docx import Document
from openpyxl import Workbook

from app.extractors import extract_document_text


def test_extract_text_from_txt(tmp_path: Path):
    path = tmp_path / "sample.txt"
    path.write_text("hola mundo", encoding="utf-8")
    text, metadata = extract_document_text(path)
    assert text == "hola mundo"
    assert metadata["metadata"]["source_format"] == "txt"


def test_extract_text_from_docx(tmp_path: Path):
    path = tmp_path / "sample.docx"
    doc = Document()
    doc.add_paragraph("Linea uno")
    doc.save(path)
    text, metadata = extract_document_text(path)
    assert "Linea uno" in text
    assert metadata["metadata"]["source_format"] == "docx"


def test_extract_text_from_xlsx(tmp_path: Path):
    path = tmp_path / "sample.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Hoja1"
    ws["A1"] = "Valor"
    wb.save(path)
    text, metadata = extract_document_text(path)
    assert "Valor" in text
    assert metadata["metadata"]["source_format"] == "xlsx"
