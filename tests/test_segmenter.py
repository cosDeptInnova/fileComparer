from app.services.segmenter import build_blocks


def test_build_blocks_generates_multiple_blocks():
    text = "Primera frase. Segunda frase. Tercera frase. Cuarta frase."
    blocks = build_blocks(text, target_chars=25, overlap_chars=10)
    assert len(blocks) >= 2
    assert blocks[0].text
    assert blocks[1].start_char > blocks[0].start_char


def test_build_blocks_preserves_paragraph_boundaries_and_ignores_short_numbering_markers():
    text = "I.\n\nObjeto del contrato\n\nII.\n\nDescripción del proyecto"

    blocks = build_blocks(text, target_chars=80, overlap_chars=10)

    assert [block.text for block in blocks] == [
        "Objeto del contrato",
        "Descripción del proyecto",
    ]
