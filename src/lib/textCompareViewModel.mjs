export const TEXT_COMPARE_FILTER_ALL = "all";
export const TEXT_COMPARE_SORT_DEFAULT = "block_asc";

export function normalizeFilterValue(value) {
  const normalized = String(value || "")
    .trim()
    .toLowerCase();
  return normalized || TEXT_COMPARE_FILTER_ALL;
}

export function filterTextCompareRows(
  rows = [],
  {
    query = "",
    severity = TEXT_COMPARE_FILTER_ALL,
    changeType = TEXT_COMPARE_FILTER_ALL,
    matchesQuery = () => true,
  } = {},
) {
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const normalizedSeverity = normalizeFilterValue(severity);
  const normalizedChangeType = normalizeFilterValue(changeType);

  return rows.filter((row) => {
    const rowSeverity = normalizeFilterValue(row?.severity);
    const rowChangeType = normalizeFilterValue(row?.change_type === "insertado" ? "añadido" : row?.change_type);
    if (
      normalizedSeverity !== TEXT_COMPARE_FILTER_ALL &&
      rowSeverity !== normalizedSeverity
    ) {
      return false;
    }
    if (
      normalizedChangeType !== TEXT_COMPARE_FILTER_ALL &&
      rowChangeType !== normalizedChangeType
    ) {
      return false;
    }
    return matchesQuery(row, normalizedQuery);
  });
}

const SEVERITY_WEIGHT = {
  critica: 4,
  alta: 3,
  media: 2,
  baja: 1,
};

const CONFIDENCE_WEIGHT = {
  alta: 3,
  media: 2,
  baja: 1,
};

export function sortTextCompareRows(rows = [], sortKey = TEXT_COMPARE_SORT_DEFAULT) {
  const copy = [...rows];
  const normalizedSort = String(sortKey || TEXT_COMPARE_SORT_DEFAULT).trim().toLowerCase();

  copy.sort((a, b) => {
    if (normalizedSort === "severity_desc") {
      return (
        (SEVERITY_WEIGHT[String(b?.severity || "").toLowerCase()] || 0) -
          (SEVERITY_WEIGHT[String(a?.severity || "").toLowerCase()] || 0) ||
        (Number(a?.block_id) || 0) - (Number(b?.block_id) || 0)
      );
    }
    if (normalizedSort === "confidence_desc") {
      return (
        (CONFIDENCE_WEIGHT[String(b?.confidence || "").toLowerCase()] || 0) -
          (CONFIDENCE_WEIGHT[String(a?.confidence || "").toLowerCase()] || 0) ||
        (Number(a?.block_id) || 0) - (Number(b?.block_id) || 0)
      );
    }
    if (normalizedSort === "change_type") {
      return String(a?.change_type || "").localeCompare(String(b?.change_type || "")) ||
        (Number(a?.block_id) || 0) - (Number(b?.block_id) || 0);
    }
    return (Number(a?.block_id) || 0) - (Number(b?.block_id) || 0);
  });

  return copy;
}

export function deriveTextCompareViewState({
  result = null,
  error = null,
  isLoadingResult = false,
  rows = null,
  filteredRows = null,
} = {}) {
  if (error) {
    return {
      kind: "error",
      title: "La comparación no pudo completarse",
      description:
        "El backend devolvió un error. Revisa el panel de diagnóstico para ver la causa exacta y los siguientes pasos.",
    };
  }

  if (isLoadingResult && !result) {
    return {
      kind: "loading",
      title: "Preparando la tabla final",
      description:
        "El comparador ya terminó o está terminando y la SPA está cargando la tabla estructurada de diferencias.",
    };
  }

  const totalRows = Array.isArray(rows) ? rows.length : result?.rows?.length || 0;
  const visibleRows = Array.isArray(filteredRows) ? filteredRows.length : totalRows;

  if (!result) {
    return {
      kind: "idle",
      title: "",
      description: "",
    };
  }

  if (totalRows === 0) {
    return {
      kind: "empty",
      title: "Sin diferencias materiales detectadas",
      description:
        "No se generaron filas con cambios. Puedes descargar el resultado o lanzar otra comparación para revisar nuevos documentos.",
    };
  }

  if (visibleRows === 0) {
    return {
      kind: "filtered-empty",
      title: "No hay filas con esos filtros",
      description:
        "Ajusta la búsqueda o los filtros rápidos de severidad y tipo para volver a mostrar bloques de diferencia.",
    };
  }

  return {
    kind: "ready",
    title: "",
    description: "",
  };
}