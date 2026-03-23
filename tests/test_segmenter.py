from app.services.segmenter import build_blocks


def test_build_blocks_generates_multiple_blocks():
    text = " ".join([f"Frase {index}." for index in range(1, 45)])
    blocks = build_blocks(text, target_chars=125, overlap_chars=10)
    assert len(blocks) >= 2
    assert blocks[0].text
    assert blocks[1].start_char > blocks[0].start_char


def test_build_blocks_preserves_paragraph_boundaries_and_ignores_short_numbering_markers():
    text = "I.\n\nObjeto del contrato\n\nII.\n\nDescripción del proyecto"

    blocks = build_blocks(text, target_chars=80, overlap_chars=10)

    assert len(blocks) == 1
    assert blocks[0].text == "Objeto del contrato Descripción del proyecto"


def test_build_blocks_creates_contiguous_non_overlapping_ranges():
    text = " ".join([f"Cláusula {index} con contenido relevante." for index in range(1, 120)])

    blocks = build_blocks(text, target_chars=125, overlap_chars=40)

    assert len(blocks) >= 2
    for previous, current in zip(blocks, blocks[1:]):
        assert current.start_char >= previous.end_char
