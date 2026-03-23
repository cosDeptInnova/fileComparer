from app.services.normalization import normalize_text


def test_normalize_text_removes_bullets_and_extra_spaces():
    raw = "• Punto uno\n1. Punto dos\n\nHEADER\nHEADER\nTexto   final\t con espacios"
    normalized = normalize_text(raw)
    assert "•" not in normalized
    assert "1." not in normalized
    assert "  " not in normalized
