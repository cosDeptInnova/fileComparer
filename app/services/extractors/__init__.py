from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from app.extractors import extract_document_result


_DEPRECATION_MESSAGE = (
    "app.services.extractors está deprecado; usa app.extractors.extract_document_result/extract_document_text "
    "para evitar divergencia entre pipelines."
)


def extract_text_from_path(
    path: str | Path,
    *,
    soffice_path: str | None = None,
    drop_headers: bool = True,
    engine: str = "auto",
) -> tuple[str, dict[str, Any]]:
    warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
    result = extract_document_result(
        str(path),
        soffice_path=soffice_path,
        drop_headers=drop_headers,
        engine=engine,
    )
    return result.text, {
        "extension": Path(path).suffix.lower(),
        "extractor": "app.extractors.extract_document_result",
        "engine": result.engine,
        **result.to_quality_dict(),
    }