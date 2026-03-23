import os
from pathlib import Path
from typing import Any

_ALLOWED_EXTENSIONS = {
    ".txt",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".rtf",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
_DEFAULT_MAX_MB = int(os.getenv("TEXT_COMPARE_MAX_FILE_MB", "40"))
_PANEL_NAME = "TextCompareMainPanel"
_ROUTE = "/main/text-compare"


def get_text_compare_max_file_mb() -> int:
    return _DEFAULT_MAX_MB


def ext_allowed(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _ALLOWED_EXTENSIONS


def format_unsupported_extension_message(ext: str) -> str:
    allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
    return f"La extensión {ext} no está admitida. Formatos soportados: {allowed}."


def format_file_too_large_backend_message() -> str:
    return f"Cada archivo debe ocupar como máximo {get_text_compare_max_file_mb()} MB."


def build_capabilities_payload(runtime_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_settings = runtime_settings or {}
    allowed_extensions = sorted(_ALLOWED_EXTENSIONS)
    return {
        "service": "comparador",
        "panel_name": _PANEL_NAME,
        "route": _ROUTE,
        "accept": ",".join(allowed_extensions),
        "allowed_extensions_label": ", ".join(allowed_extensions),
        "allowed_extensions": sorted(_ALLOWED_EXTENSIONS),
        "max_file_mb": get_text_compare_max_file_mb(),
        "max_file_bytes": get_text_compare_max_file_mb() * 1024 * 1024,
        "messages": {
            "unsupported_extension": "Formato no soportado ({ext}). Extensiones admitidas: {allowed_extensions}.",
            "file_too_large": 'El archivo "{name}" supera el máximo de {max_mb} MB ({size_mb} MB).',
            "file_too_large_backend": "Archivo demasiado grande. Máximo {max_mb} MB por fichero.",
            "empty_file": "Alguno de los archivos está vacío.",
        },
        "runtime": {
            "ai_only_enabled": bool(runtime_settings.get("ai_only_enabled", False)),
            "comparison_mode": str(runtime_settings.get("comparison_mode") or "llm_strict_sequential"),
            "ocr_available": bool(runtime_settings.get("ocr_available", False)),
            "llm_available": bool(runtime_settings.get("llm_available", False)),
            "timeouts": {
                "extract_seconds": float(runtime_settings.get("extract_timeout_seconds", 45.0)),
                "llm_seconds": float(runtime_settings.get("llm_timeout_seconds", 120.0)),
            },
        },
    }
