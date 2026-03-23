from __future__ import annotations

import json
from typing import Any

LOCAL_COMPARE_PROMPT_VERSION = "compare-block-local-v6"
GLOBAL_REVIEW_PROMPT_VERSION = "compare-block-global-v1"
GLOBAL_TABLE_REVIEW_PROMPT_VERSION = "compare-block-global-table-v1"

COMPARE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "review_label",
        "change_type",
        "summary",
        "severity",
        "confidence",
        "text_a",
        "text_b",
        "display_text_a",
        "display_text_b",
        "display_segments_a",
        "display_segments_b",
        "justification",
    ],
    "properties": {
        "review_label": {"type": "string"},
        "change_type": {"type": "string"},
        "summary": {"type": "string"},
        "severity": {"type": "string"},
        "confidence": {"type": "string"},
        "text_a": {"type": "string"},
        "text_b": {"type": "string"},
        "display_text_a": {"type": "string"},
        "display_text_b": {"type": "string"},
        "display_segments_a": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "text"],
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "display_segments_b": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "text"],
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "justification": {"type": "string"},
        "impact": {"type": "string"},
    },
}

GLOBAL_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["disposition", "row_ids", "review_label", "rationale"],
                "properties": {
                    "disposition": {"type": "string"},
                    "row_ids": {"type": "array", "items": {"type": "integer"}},
                    "review_label": {"type": "string"},
                    "rationale": {"type": "string"},
                    "target_change_type": {"type": "string"},
                },
            },
        },
    },
}


def build_compare_messages(
    *,
    text_a: str,
    text_b: str,
    previous_text_a: str,
    next_text_a: str,
    previous_text_b: str,
    next_text_b: str,
    alignment_score: float,
    alignment_strategy: str,
) -> list[dict[str, str]]:
    system = (
        "Eres un comparador jurídico/documental asistido por LLM. "
        "Debes devolver un único JSON válido, sin markdown ni texto adicional. "
        "La alineación previa es útil pero no perfecta: usa el score, la estrategia y los bloques vecinos "
        "para decidir si el par representa un cambio real, una resegmentación inocua o un posible mal emparejamiento. "
        "Ignora saltos de línea, maquetación, paginación, cabeceras, pies, viñetas equivalentes, tablas linealizadas, "
        "estilos, OCR ruidoso y separaciones artificiales producidas por extracción. "
        "Tu clasificación interna debe usar review_label con uno de estos valores exactos: "
        "sin_cambios_por_reflujo, posible_mal_emparejamiento, cambio_real. "
        "Luego rellena change_type con el tipo público más cercano: sin_cambios, modificado, insertado o eliminado. "
        "Usa solo texto plano. Marca cambios inline así: A=[-texto-], B={+texto+}. "
        "Si un lado carece realmente de contenido equivalente dentro del par recibido, usa [[BLOQUE AUSENTE EN A]] "
        "o [[BLOQUE AUSENTE EN B]]. No inventes texto ni conserves metadatos visuales."
    )
    user = (
        "Tarea: compara el par actual sin asumir que la alineación es perfecta. "
        "Si el contenido es equivalente pero fue refluido/resegmentado, usa review_label='sin_cambios_por_reflujo' "
        "y change_type='sin_cambios'. Si sospechas que este bloque debería corresponder a un vecino, usa "
        "review_label='posible_mal_emparejamiento'. Usa review_label='cambio_real' solo cuando el cambio textual/material "
        "sea auténtico.\n\n"
        "Devuelve JSON con claves exactas: review_label, change_type, summary, severity, confidence, text_a, text_b, "
        "display_text_a, display_text_b, display_segments_a, display_segments_b, justification, impact.\n"
        "Responde SOLO con un único objeto JSON válido y completo, sin markdown ni texto extra.\n"
        "- text_a/text_b: texto limpio comparable, sin ruido visual.\n"
        "- display_*: mismo texto limpio con marcas inline mínimas y estables.\n"
        "- display_segments_*: segmentos compactos usando tipos equal/insert/delete/replace/context.\n"
        "- summary/justification: concretos y breves.\n\n"
        f"ALIGNMENT_SCORE: {alignment_score:.4f}\n"
        f"ALIGNMENT_STRATEGY: {alignment_strategy}\n\n"
        f"BLOQUE_A_TEXTO:\n{text_a}\n\n"
        f"BLOQUE_A_ANTERIOR:\n{previous_text_a}\n\n"
        f"BLOQUE_A_SIGUIENTE:\n{next_text_a}\n\n"
        f"BLOQUE_B_TEXTO:\n{text_b}\n\n"
        f"BLOQUE_B_ANTERIOR:\n{previous_text_b}\n\n"
        f"BLOQUE_B_SIGUIENTE:\n{next_text_b}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_global_review_messages(
    *,
    rows: list[dict[str, Any]],
    window_start: int,
    window_end: int,
    total_rows: int,
) -> list[dict[str, str]]:
    system = (
        "Eres un revisor global de diferencias documentales. "
        "Debes devolver un único JSON válido, sin markdown ni texto adicional. "
        "Recibirás una ventana limitada de filas ya detectadas localmente. "
        "Tu objetivo es detectar cascadas de falsos positivos, filas recíprocas, resegmentación inocua "
        "y posibles malos emparejamientos entre filas contiguas. "
        "Usa review_label con uno de estos valores exactos: sin_cambios_por_reflujo, "
        "posible_mal_emparejamiento, cambio_real. "
        "Solo propone acciones cuando aporten una mejora clara."
    )
    serialized_rows = json.dumps(rows, ensure_ascii=False, indent=2)
    user = (
        "Devuelve SOLO un objeto JSON válido con la clave actions. No uses markdown ni texto extra.\n"
        "Cada acción debe incluir:\n"
        "- disposition: keep, merge, drop o sin_cambios.\n"
        "- row_ids: ids de fila afectados dentro de esta ventana.\n"
        "- review_label: sin_cambios_por_reflujo, posible_mal_emparejamiento o cambio_real.\n"
        "- rationale: explicación breve.\n"
        "- target_change_type: opcional, usando solo tipos públicos.\n\n"
        "Criterios:\n"
        "- Usa sin_cambios o drop cuando la diferencia sea un falso positivo por reflujo, resegmentación o reciprocidad.\n"
        "- Usa merge cuando varias filas contiguas parezcan una única diferencia real o un mal emparejamiento recuperable.\n"
        "- Usa keep implícitamente omitiendo filas sin cambios de revisión.\n\n"
        f"VENTANA_FILAS: {window_start}-{window_end} de {total_rows}\n"
        f"FILAS:\n{serialized_rows}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_global_table_review_messages(
    *,
    rows: list[dict[str, Any]],
    total_rows: int,
) -> list[dict[str, str]]:
    system = (
        "Eres el último auditor de una tabla completa de diferencias documentales. "
        "Debes devolver un único JSON válido, sin markdown ni texto adicional. "
        "Ya existe una revisión local por bloque y una revisión global por ventanas; "
        "tu trabajo final es detectar falsos positivos residuales, cascadas de filas, "
        "epígrafes mal emparejados, cláusulas desplazadas, reciprocidades y fusiones "
        "o divisiones inocuas que solo se aprecian viendo la tabla completa. "
        "Usa review_label con uno de estos valores exactos: sin_cambios_por_reflujo, "
        "posible_mal_emparejamiento, cambio_real. "
        "No inventes texto nuevo ni conviertas una duda en cambio real."
    )
    serialized_rows = json.dumps(rows, ensure_ascii=False, indent=2)
    user = (
        "Devuelve SOLO un objeto JSON válido con la clave actions. No uses markdown ni texto extra.\n"
        "Cada acción debe incluir:\n"
        "- disposition: keep, merge, drop o sin_cambios.\n"
        "- row_ids: ids de fila afectados.\n"
        "- review_label: sin_cambios_por_reflujo, posible_mal_emparejamiento o cambio_real.\n"
        "- rationale: explicación breve.\n"
        "- target_change_type: opcional, usando solo tipos públicos.\n\n"
        "Criterios reforzados para esta última pasada:\n"
        "- Si una fila contiene solo un encabezado o número de sección y la siguiente contiene el cuerpo equivalente, "
        "corrige la cascada con drop o merge.\n"
        "- Si hay una inserción/eliminación aparente causada por resegmentación o desplazamiento, elimínala.\n"
        "- Si varias filas contiguas representan una única diferencia real, usa merge.\n"
        "- Omite filas sin mejora clara.\n\n"
        f"TOTAL_FILAS: {total_rows}\n"
        f"TABLA_COMPLETA:\n{serialized_rows}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]