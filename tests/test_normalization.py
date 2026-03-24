from app.services.normalization import normalize_text


def test_normalize_text_removes_bullets_and_extra_spaces():
    raw = "• Punto uno\n1. Punto dos\n\nHEADER\nHEADER\nTexto   final\t con espacios"
    normalized = normalize_text(raw)
    assert "•" not in normalized
    assert "1." not in normalized
    assert "  " not in normalized


def test_normalize_text_drops_standalone_numbering_and_preserves_paragraphs():
    raw = "PRIMERA.-\nOBJETO DEL CONTRATO\n\nII.\nTexto del apartado\n\n7.\nDetalle final"

    normalized = normalize_text(raw)

    assert "II." not in normalized
    assert "7." not in normalized
    assert normalized == "PRIMERA.- OBJETO DEL CONTRATO\n\nTexto del apartado\n\nDetalle final"


def test_normalize_text_handles_bullets_vs_linearized_text_as_equivalent():
    bullets = "• alcance del servicio\n• plazo de entrega\n• criterios de calidad"
    linearized = "alcance del servicio plazo de entrega criterios de calidad"

    normalized_bullets = normalize_text(bullets)
    normalized_linearized = normalize_text(linearized)

    assert normalized_bullets == normalized_linearized
