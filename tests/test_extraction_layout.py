from app.document_compare.extraction_layout import ExtractionBlock, ExtractionLayout


def test_canonical_text_ignores_drop_in_canonical_and_preserves_breaks():
    layout = ExtractionLayout(
        source_engine="pdf",
        blocks=[
            ExtractionBlock(
                text="Page 1",
                page=1,
                block_type="page",
                source_engine="pdf",
                metadata={"drop_in_canonical": True},
            ),
            ExtractionBlock(
                text="Encabezado repetido",
                page=1,
                block_type="header",
                source_engine="pdf",
                metadata={"drop_in_canonical": True, "is_repeated_header": True},
            ),
            ExtractionBlock(text="Primer párrafo", page=1, block_type="paragraph", source_engine="pdf"),
            ExtractionBlock(text="", page=1, block_type="page_break", source_engine="pdf"),
            ExtractionBlock(text="Segundo párrafo", page=2, block_type="paragraph", source_engine="pdf"),
            ExtractionBlock(
                text="línea interna",
                page=2,
                block_type="line",
                source_engine="pdf",
                metadata={"drop_in_canonical": True},
            ),
        ],
    )

    assert layout.canonical_text() == "Primer párrafo\n\nSegundo párrafo"


def test_quality_signals_report_table_density_and_repeated_headers():
    layout = ExtractionLayout(
        source_engine="pdf",
        blocks=[
            ExtractionBlock(
                text="Aviso legal",
                page=1,
                block_type="header",
                source_engine="pdf",
                metadata={"is_repeated_header": True, "drop_in_canonical": True},
            ),
            ExtractionBlock(text="Col A | Col B | Col C", page=1, block_type="table_row", source_engine="pdf"),
            ExtractionBlock(text="1 | 2 | 3", page=1, block_type="table_row", source_engine="pdf"),
            ExtractionBlock(text="Contenido principal", page=1, block_type="paragraph", source_engine="pdf"),
        ],
    )

    signals = layout.quality_signals()

    assert signals["has_repeated_headers"] is True
    assert signals["table_like_density"] > 0.0
    assert signals["avg_line_length"] > 0.0


def test_canonical_text_inserts_paragraph_breaks_between_paragraph_like_blocks():
    layout = ExtractionLayout(
        source_engine="docx",
        blocks=[
            ExtractionBlock(text="PRIMERA.- OBJETO", page=None, block_type="paragraph", source_engine="docx"),
            ExtractionBlock(text="SEGUNDA.- DESCRIPCIÓN", page=None, block_type="paragraph", source_engine="docx"),
        ],
    )

    assert layout.canonical_text() == "PRIMERA.- OBJETO\n\nSEGUNDA.- DESCRIPCIÓN"
