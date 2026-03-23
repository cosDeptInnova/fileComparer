from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook
from PIL import Image
import docx
import fitz
import pytesseract


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_docx(path: Path) -> str:
    document = docx.Document(str(path))
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    tables = []
    for table in document.tables:
        for row in table.rows:
            values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if values:
                tables.append(" | ".join(values))
    return "\n".join(paragraphs + tables)


def _read_xlsx(path: Path) -> str:
    workbook = load_workbook(str(path), data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(sheet.title)
        for row in sheet.iter_rows(values_only=True):
            values = [str(value).strip() for value in row if value is not None and str(value).strip()]
            if values:
                parts.append(" | ".join(values))
    return "\n".join(parts)


def _read_pdf(path: Path) -> str:
    with fitz.open(str(path)) as document:
        pages = [page.get_text("text") for page in document]
    return "\n".join(page.strip() for page in pages if page and page.strip())


def _read_image(path: Path) -> str:
    image = Image.open(path)
    return pytesseract.image_to_string(image, lang="spa+eng").strip()


def _read_legacy_office(path: Path) -> str:
    raise ValueError(
        f"Formato {path.suffix.lower()} no soportado sin conversión externa (LibreOffice headless). "
        "Usa .docx o .xlsx para pruebas locales."
    )


EXTRACTORS = {
    ".txt": _read_text,
    ".docx": _read_docx,
    ".xlsx": _read_xlsx,
    ".pdf": _read_pdf,
    ".png": _read_image,
    ".jpg": _read_image,
    ".jpeg": _read_image,
    ".doc": _read_legacy_office,
    ".xls": _read_legacy_office,
    ".rtf": _read_legacy_office,
}


def extract_text_from_path(path: str | Path) -> tuple[str, dict[str, object]]:
    source = Path(path)
    extension = source.suffix.lower()
    extractor = EXTRACTORS.get(extension)
    if extractor is None:
        raise ValueError(f"Formato no soportado: {extension or 'sin extensión'}")
    text = extractor(source)
    return text, {"extension": extension, "extractor": extractor.__name__}
