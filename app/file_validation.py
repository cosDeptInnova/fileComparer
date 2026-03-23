from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_SIGNATURE = b"PK\x03\x04"
PDF_SIGNATURE = b"%PDF"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
BMP_SIGNATURE = b"BM"
TIFF_LE_SIGNATURE = b"II*\x00"
TIFF_BE_SIGNATURE = b"MM\x00*"
WEBP_SIGNATURE = b"RIFF"

TEXT_EXTENSIONS = {".txt"}
ZIP_BASED_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
OLE_EXTENSIONS = {".doc", ".xls", ".ppt"}
IMAGE_EXTENSIONS = {
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".bmp": {"image/bmp"},
    ".tif": {"image/tiff"},
    ".tiff": {"image/tiff"},
    ".webp": {"image/webp"},
}
ALLOWED_MIME_BY_EXTENSION = {
    ".pdf": {"application/pdf"},
    ".doc": {"application/msword", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
    ".txt": {"text/plain", "application/octet-stream"},
    ".rtf": {"application/rtf", "text/rtf", "application/octet-stream"},
    ".xls": {"application/vnd.ms-excel", "application/octet-stream"},
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "application/octet-stream",
    },
    ".ppt": {"application/vnd.ms-powerpoint", "application/octet-stream"},
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
        "application/octet-stream",
    },
    **IMAGE_EXTENSIONS,
}


@dataclass(slots=True)
class FileValidationResult:
    extension: str
    detected_type: str
    declared_mime: str


def sniff_file_type(content: bytes) -> str:
    header = bytes(content[:16])
    if header.startswith(PDF_SIGNATURE):
        return "pdf"
    if header.startswith(PNG_SIGNATURE):
        return "png"
    if header.startswith(JPEG_SIGNATURE):
        return "jpeg"
    if header.startswith(BMP_SIGNATURE):
        return "bmp"
    if header.startswith(TIFF_LE_SIGNATURE) or header.startswith(TIFF_BE_SIGNATURE):
        return "tiff"
    if header.startswith(WEBP_SIGNATURE) and content[8:12] == b"WEBP":
        return "webp"
    if header.startswith(ZIP_SIGNATURE):
        return "zip"
    if header.startswith(OLE_SIGNATURE):
        return "ole"
    sample = bytes(content[:1024])
    if sample.lstrip().startswith((b"{\\rtf", b"{\\RTF")):
        return "rtf"
    if _looks_like_text(sample):
        return "text"
    return "unknown"


def validate_upload_payload(
    *,
    filename: str,
    content: bytes,
    declared_mime: Optional[str],
    max_bytes: int,
) -> FileValidationResult:
    ext = Path(filename or "").suffix.lower()
    if not ext:
        raise ValueError("El archivo debe incluir una extensión reconocible.")
    if len(content) > max_bytes:
        raise ValueError("El archivo supera el tamaño máximo permitido.")

    declared = (declared_mime or "").strip().lower()
    expected_mimes = ALLOWED_MIME_BY_EXTENSION.get(ext, set())
    guessed_mime, _ = mimetypes.guess_type(filename or "")
    if guessed_mime and not declared:
        declared = guessed_mime.lower()

    if declared and expected_mimes and declared not in expected_mimes:
        raise ValueError(
            f"MIME declarado no permitido para {ext}: {declared}."
        )

    detected_type = sniff_file_type(content)
    _validate_signature(ext=ext, detected_type=detected_type)
    return FileValidationResult(extension=ext, detected_type=detected_type, declared_mime=declared)


def _validate_signature(*, ext: str, detected_type: str) -> None:
    if ext in ZIP_BASED_EXTENSIONS and detected_type != "zip":
        raise ValueError(f"El contenido no coincide con un documento OOXML válido para {ext}.")
    if ext in OLE_EXTENSIONS and detected_type != "ole":
        raise ValueError(f"El contenido no coincide con un documento binario válido para {ext}.")
    if ext == ".pdf" and detected_type != "pdf":
        raise ValueError("El contenido no coincide con un PDF válido.")
    if ext == ".rtf" and detected_type != "rtf":
        raise ValueError("El contenido no coincide con un RTF válido.")
    if ext in TEXT_EXTENSIONS and detected_type not in {"text", "unknown"}:
        raise ValueError("El contenido no coincide con un TXT válido.")
    if ext in IMAGE_EXTENSIONS:
        image_type = {
            ".png": "png",
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".bmp": "bmp",
            ".tif": "tiff",
            ".tiff": "tiff",
            ".webp": "webp",
        }[ext]
        if detected_type != image_type:
            raise ValueError(f"El contenido no coincide con una imagen válida para {ext}.")


def _looks_like_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    printable = sum(
        1 for byte in sample if byte in b"\t\n\r\f\b" or 32 <= byte <= 126 or byte >= 128
    )
    return (printable / max(1, len(sample))) >= 0.92
