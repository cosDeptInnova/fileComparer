import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchComparerCsrfToken,
  fetchTextCompareCapabilities,
  startTextCompareJob,
  pollTextCompareProgress,
  fetchTextCompareResult,
} from "../lib/api";
import getFileIconClass from "../components/utils/GetFileIcon";
import {
  DEFAULT_TEXT_COMPARE_CAPABILITIES,
  normalizeTextCompareCapabilities,
} from "../lib/textCompareConfig";
import {
  deriveTextCompareViewState,
  filterTextCompareRows,
  sortTextCompareRows,
  TEXT_COMPARE_FILTER_ALL,
  TEXT_COMPARE_SORT_DEFAULT,
} from "../lib/textCompareViewModel.mjs";
import {
  isTextCompareErrorStatus,
  isTextCompareSuccessStatus,
} from "../lib/textCompareJobState.mjs";

const POLL_MS = 1200;
const RENDER_CHUNK_SIZE = 30;
const INITIAL_RENDER_COUNT = 40;
const PLACEHOLDER_DECISION_VALUES = new Set([
  "",
  "pending",
  "pendiente",
  "pendiente_confirmacion",
  "sin_clasificar",
  "unknown",
  "null",
  "none",
]);
const SEVERITY_FILTER_OPTIONS = [
  { value: TEXT_COMPARE_FILTER_ALL, label: "Todas las severidades" },
  { value: "critica", label: "Crítica" },
  { value: "alta", label: "Alta" },
  { value: "media", label: "Media" },
  { value: "baja", label: "Baja" },
];
const CHANGE_FILTER_OPTIONS = [
  { value: TEXT_COMPARE_FILTER_ALL, label: "Todos los cambios" },
  { value: "modificado", label: "Modificado" },
  { value: "añadido", label: "Solo en B" },
  { value: "eliminado", label: "Solo en A" },
  { value: "pendiente_confirmacion", label: "Sin clasificar" },
];
const SORT_OPTIONS = [
  { value: TEXT_COMPARE_SORT_DEFAULT, label: "Orden natural" },
  { value: "severity_desc", label: "Severidad ↓" },
  { value: "confidence_desc", label: "Confianza ↓" },
  { value: "change_type", label: "Tipo de cambio" },
];

function extOf(name = "") {
  const idx = name.lastIndexOf(".");
  return idx >= 0 ? name.slice(idx).toLowerCase() : "";
}

function bytesToMB(b) {
  return (b / (1024 * 1024)).toFixed(2);
}

function formatCapabilityMessage(template, replacements) {
  return Object.entries(replacements || {}).reduce((acc, [key, value]) => {
    return acc.replaceAll(`{${key}}`, String(value));
  }, template || "");
}

function normalizeTextSegments(value) {
  if (!Array.isArray(value)) return [];

  return value.reduce((acc, segment) => {
    const type = String(segment?.type || "equal")
      .trim()
      .toLowerCase();
    const safeType = ["equal", "insert", "delete", "replace"].includes(type)
      ? type
      : "equal";
    const segmentText = String(segment?.text || "");
    if (!segmentText) return acc;
    const last = acc[acc.length - 1];
    if (last && last.type === safeType) {
      last.text += segmentText;
      return acc;
    }
    acc.push({ type: safeType, text: segmentText });
    return acc;
  }, []);
}

function flattenSegmentsText(segments = []) {
  return normalizeTextSegments(segments)
    .map((segment) => segment.text)
    .join("");
}

function normalizeRowKeyValue(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function deduplicateRowsForRender(rows = []) {
  const unique = new Map();
  rows.forEach((row) => {
    const key = [
      normalizeRowKeyValue(row.change_type),
      normalizeRowKeyValue(row.text_a || row.display_text_a),
      normalizeRowKeyValue(row.text_b || row.display_text_b),
    ].join("||");
    const existing = unique.get(key);
    if (!existing) {
      unique.set(key, row);
      return;
    }
    if (String(row.summary || "").length > String(existing.summary || "").length) {
      unique.set(key, row);
    }
  });
  return Array.from(unique.values()).map((row, index) => ({
    ...row,
    block_id: index + 1,
  }));
}

function textContainsQuery(row, query) {
  if (!query) return true;
  const haystack = [
    String(row.block_id || ""),
    row.pair_id,
    row.text_a,
    row.text_b,
    row.display_text_a,
    row.display_text_b,
    row.llm_comment,
    row.summary,
    row.impact,
    row.justification,
    row.change_type,
    row.materiality,
    row.final_decision,
    row.severity,
    row.decision_source,
    flattenSegmentsText(row.display_segments_a),
    flattenSegmentsText(row.display_segments_b),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function ResultStatPill({ label, value, isDarkMode }) {
  return (
    <span
      className={`break-all rounded-xl px-3 py-2 text-xs 2xl:text-sm ${
        isDarkMode ? "bg-gray-800 text-gray-200" : "bg-gray-100 text-gray-700"
      }`}
    >
      {label}: <strong>{value}</strong>
    </span>
  );
}

function StatusBadge({ label, tone = "neutral", isDarkMode }) {
  const palette = {
    neutral: isDarkMode
      ? "bg-gray-800 text-gray-200 border-gray-700"
      : "bg-gray-100 text-gray-700 border-gray-200",
    info: isDarkMode
      ? "bg-sky-500/15 text-sky-100 border-sky-500/40"
      : "bg-sky-50 text-sky-700 border-sky-200",
    success: isDarkMode
      ? "bg-emerald-500/15 text-emerald-100 border-emerald-500/40"
      : "bg-emerald-50 text-emerald-700 border-emerald-200",
    warning: isDarkMode
      ? "bg-amber-500/15 text-amber-100 border-amber-500/40"
      : "bg-amber-50 text-amber-700 border-amber-200",
    danger: isDarkMode
      ? "bg-rose-500/15 text-rose-100 border-rose-500/40"
      : "bg-rose-50 text-rose-700 border-rose-200",
    purple: isDarkMode
      ? "bg-violet-500/15 text-violet-100 border-violet-500/40"
      : "bg-violet-50 text-violet-700 border-violet-200",
  };

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide ${palette[tone] || palette.neutral}`}
    >
      {label}
    </span>
  );
}

function toneForDecision(finalDecision) {
  switch (String(finalDecision || "").toLowerCase()) {
    case "material":
      return "danger";
    case "inmaterial":
    case "no_material":
      return "success";
    default:
      return "warning";
  }
}

function toneForSeverity(severity) {
  switch (String(severity || "").toLowerCase()) {
    case "critica":
      return "danger";
    case "alta":
      return "warning";
    case "media":
      return "info";
    default:
      return "neutral";
  }
}

function toneForChangeType(changeType) {
  switch (String(changeType || "").toLowerCase()) {
    case "modificado":
      return "purple";
    case "insertado":
    case "añadido":
      return "success";
    case "eliminado":
      return "danger";
    case "reubicado":
      return "warning";
    case "sin_cambios":
      return "info";
    default:
      return "neutral";
  }
}

function getSegmentClasses(type, isDarkMode) {
  if (type === "insert") {
    return isDarkMode
      ? "font-semibold bg-emerald-500/20 text-emerald-100"
      : "font-semibold bg-emerald-100 text-emerald-900";
  }
  if (type === "delete") {
    return isDarkMode
      ? "font-semibold bg-rose-500/20 text-rose-100"
      : "font-semibold bg-rose-100 text-rose-900";
  }
  if (type === "replace") {
    return isDarkMode
      ? "font-semibold bg-amber-500/20 text-amber-100"
      : "font-semibold bg-amber-100 text-amber-900";
  }
  return "";
}

function ResultTextCell({
  segments,
  plainText,
  label,
  title,
  metaLines = [],
  isDarkMode,
}) {
  const normalizedSegments = normalizeTextSegments(segments);
  const fallback = plainText || "—";
  const renderedSegments = normalizedSegments.length
    ? normalizedSegments
    : [{ type: "equal", text: fallback }];

  return (
    <div className="space-y-3">
      {title && (
        <div
          className={`flex items-center justify-between gap-3 text-xs font-semibold uppercase tracking-[0.18em] ${
            isDarkMode ? "text-gray-400" : "text-gray-500"
          }`}
        >
          <span>{title}</span>
        </div>
      )}
      <div
        className={`max-h-80 overflow-auto whitespace-pre-wrap rounded-2xl border p-4 text-sm leading-6 shadow-sm ${
          isDarkMode
            ? "border-gray-700 bg-gray-800/70 text-gray-100"
            : "border-gray-200 bg-white text-gray-900"
        }`}
      >
        {renderedSegments.map((segment, index) => {
          const content =
            segment.type === "equal" ? (
              segment.text
            ) : (
              <strong>{segment.text}</strong>
            );

          return (
            <span
              key={`${label}-${segment.type}-${index}-${segment.text.length}`}
              className={`rounded-sm ${getSegmentClasses(segment.type, isDarkMode)}`}
            >
              {content}
            </span>
          );
        })}
      </div>
      {metaLines.length > 0 && (
        <div
          className={`space-y-1 text-xs leading-5 ${
            isDarkMode ? "text-gray-400" : "text-gray-500"
          }`}
        >
          {metaLines.map((line) => (
            <div key={`${label}-${line}`}>{line}</div>
          ))}
        </div>
      )}
    </div>
  );
}

function DetailToggleButton({ isExpanded, onToggle, isDarkMode }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`inline-flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2 text-xs font-semibold transition-colors ${
        isDarkMode
          ? "border-gray-700 bg-gray-800/80 text-gray-100 hover:bg-gray-700"
          : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50"
      }`}
    >
      <i className={`fas ${isExpanded ? "fa-chevron-up" : "fa-chevron-down"}`} />
      {isExpanded ? "Ocultar detalle" : "Ver detalle"}
    </button>
  );
}

function ToolbarButton({
  icon,
  label,
  onClick,
  disabled = false,
  isDarkMode,
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-xl border px-3 py-2 text-xs font-semibold transition-colors ${
        disabled
          ? isDarkMode
            ? "cursor-not-allowed border-gray-800 bg-gray-900 text-gray-600"
            : "cursor-not-allowed border-gray-200 bg-gray-100 text-gray-400"
          : isDarkMode
            ? "border-gray-700 bg-gray-800 text-gray-100 hover:bg-gray-700"
            : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50"
      }`}
    >
      {icon ? <i className={`fas ${icon}`} /> : null}
      {label}
    </button>
  );
}

function FilterPillGroup({
  label,
  value,
  options,
  onChange,
  isDarkMode,
}) {
  return (
    <div className="space-y-2">
      <div className="text-[11px] font-semibold uppercase tracking-[0.18em] opacity-70">
        {label}
      </div>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => {
          const selected = option.value === value;
          return (
            <button
              key={`${label}-${option.value}`}
              type="button"
              onClick={() => onChange(option.value)}
              className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${
                selected
                  ? isDarkMode
                    ? "border-blue-400 bg-blue-500/20 text-blue-100"
                    : "border-blue-500 bg-blue-50 text-blue-700"
                  : isDarkMode
                    ? "border-gray-700 bg-gray-800 text-gray-300 hover:bg-gray-700"
                    : "border-gray-200 bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ContextSnippet({
  label,
  before,
  after,
  isDarkMode,
}) {
  if (!before && !after) return null;
  return (
    <div
      className={`rounded-2xl border p-4 text-xs leading-6 ${
        isDarkMode
          ? "border-gray-700 bg-gray-800/70 text-gray-300"
          : "border-gray-200 bg-gray-50 text-gray-700"
      }`}
    >
      <div className="text-[11px] font-semibold uppercase tracking-[0.18em] opacity-70">
        {label}
      </div>
      {before && (
        <div className="mt-2">
          <strong>Antes:</strong> {before}
        </div>
      )}
      {after && (
        <div className="mt-1">
          <strong>Después:</strong> {after}
        </div>
      )}
    </div>
  );
}

function EmptyResultsState({ title, description, icon, isDarkMode }) {
  return (
    <div
      className={`rounded-3xl border px-6 py-10 text-center shadow-sm ${
        isDarkMode
          ? "border-gray-700 bg-gray-800/50 text-gray-300"
          : "border-gray-200 bg-gray-50 text-gray-700"
      }`}
    >
      <div className="text-3xl">
        <i className={`fas ${icon}`} />
      </div>
      <div className="mt-4 text-base font-semibold">{title}</div>
      <div className="mx-auto mt-2 max-w-2xl text-sm leading-6 opacity-80">
        {description}
      </div>
    </div>
  );
}

function buildContextMetaLines(row, side = "a") {
  const lines = [];
  const spans = row?.source_spans || {};
  const span = side === "a" ? spans.file_a : spans.file_b;
  if (Array.isArray(span) && span.length >= 2) {
    lines.push(
      `Palabras ${span[0]}–${span[1]} del documento ${side.toUpperCase()}.`,
    );
  }
  const wordCount =
    side === "a" ? row?.block_word_count_a : row?.block_word_count_b;
  if (wordCount) {
    lines.push(`Bloque analizado: ${wordCount} palabra(s).`);
  }
  const contextBefore =
    side === "a" ? row?.context_before_a : row?.context_before_b;
  const contextAfter =
    side === "a" ? row?.context_after_a : row?.context_after_b;
  if (contextBefore || contextAfter) {
    lines.push("Se muestra el bloque con contexto antes/después del cambio.");
  }
  return lines;
}

function safeLabel(value, fallback = "—") {
  const normalized = String(value || "")
    .replaceAll("_", " ")
    .trim();
  if (!normalized) return fallback;
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function isPlaceholderDecisionValue(value) {
  return PLACEHOLDER_DECISION_VALUES.has(
    String(value || "")
      .trim()
      .toLowerCase(),
  );
}

function resolveRowStatus(row) {
  const finalDecision = String(row?.final_decision || "")
    .trim()
    .toLowerCase();
  if (!isPlaceholderDecisionValue(finalDecision)) {
    return {
      label: safeLabel(row.final_decision),
      tone: toneForDecision(row.final_decision),
      helperLabel: "Decisión final",
      helperValue: safeLabel(row.final_decision),
    };
  }

  if (row?.cache_hit) {
    return {
      label: "Resuelto por caché",
      tone: "success",
      helperLabel: "Estado de resolución",
      helperValue: "Caché reutilizada",
    };
  }

  if (row?.llm_success) {
    return {
      label: "Clasificado por IA",
      tone: "info",
      helperLabel: "Estado de resolución",
      helperValue: "Clasificación IA completada",
    };
  }

  return {
    label: "Clasificado",
    tone: "info",
    helperLabel: "Estado de resolución",
    helperValue: "Resultado preparado",
  };
}

function truncateMiddle(value, max = 18) {
  const text = String(value || "");
  if (text.length <= max) return text || "—";
  const edge = Math.max(4, Math.floor((max - 1) / 2));
  return `${text.slice(0, edge)}…${text.slice(-edge)}`;
}

function buildCompareTroubleshooting(errorMessage) {
  const rawMessage = String(errorMessage || "").trim();
  if (!rawMessage) return null;

  const normalized = rawMessage.toLowerCase();

  if (normalized.includes("no hay workers activos del comparador")) {
    return {
      title: "No hay workers del comparador activos",
      tone: "warning",
      summary:
        "El API respondió 503 porque ningún worker dedicado estaba registrando heartbeats en Redis.",
      steps: [
        "Arranca al menos un worker dedicado desde el host donde corre comp_docs.",
        "Consulta `/api/comparador/workers/health` para verificar Redis, TTL, workers activos y workers expirados.",
        "Si el worker se cierra al arrancar, revisa su traceback para detectar dependencias rotas.",
      ],
      commands: ["cd comp_docs && python -m app.worker --queue compare", "cd comp_docs/scripts && ./start_worker.ps1"],
    };
  }

  if (
    normalized.includes("frontend") ||
    normalized.includes("pymupdf") ||
    normalized.includes("fitz") ||
    normalized.includes("backend pdf")
  ) {
    return {
      title: "El worker no puede cargar el backend PDF",
      tone: "danger",
      summary:
        "Suele indicar una instalación conflictiva entre PyMuPDF y un paquete `fitz` ajeno, lo que impide procesar PDFs y puede tumbar el worker al arrancar.",
      steps: [
        "Verifica que el entorno del comparador tenga `pymupdf` accesible.",
        "Elimina el paquete `fitz` conflictivo si el traceback menciona `frontend`.",
        "Reinicia después el worker dedicado del comparador.",
      ],
      commands: [
        "pip uninstall -y fitz",
        "pip install --upgrade pymupdf",
        "cd comp_docs && python -m app.worker --queue compare",
      ],
    };
  }

  if (
    normalized.includes("llm_empty_payload") ||
    normalized.includes("empty_llm_payload") ||
    normalized.includes("llm_invalid_json") ||
    normalized.includes("llm_payload_invalid")
  ) {
    return {
      title: "El runtime LLM devolvió una respuesta no utilizable",
      tone: "danger",
      summary:
        "El worker sí alcanzó al motor de comparación, pero la respuesta del modelo llegó vacía, sin JSON válido o con un payload incompatible con el contrato determinista del comparador.",
      steps: [
        "Verifica que el modelo configurado exponga un endpoint OpenAI-compatible para /chat/completions.",
        "Comprueba que el modelo realmente emita un único objeto JSON y no texto libre o markdown.",
        "Revisa la configuración LLAMA_SERVER_BASE_URL / LLM_BASE_URL / OPENAI_BASE_URL y el modelo activo del worker.",
        "Si cambiaste de proveedor o modelo, reinicia comp_docs y su worker dedicado para limpiar estado y caché.",
      ],
      commands: [
        "cd comp_docs && python -m app.worker --queue compare",
        "cd scripts && ./stop-service.ps1 -Name comp_docs_worker && ./start-service.ps1 -Name comp_docs_worker",
      ],
    };
  }

  return null;
}

export default function TextCompareMainPanel({ isDarkMode }) {
  const [csrfReady, setCsrfReady] = useState(false);
  const [csrfError, setCsrfError] = useState(null);
  const [capabilities, setCapabilities] = useState(
    DEFAULT_TEXT_COMPARE_CAPABILITIES,
  );
  const [fileA, setFileA] = useState(null);
  const [fileB, setFileB] = useState(null);
  const [warn, setWarn] = useState(null);
  const [error, setError] = useState(null);
  const engine = capabilities.engines?.default || "auto";
  const [sid, setSid] = useState(null);
  const [progress, setProgress] = useState({
    percent: 0,
    step: "—",
    detail: "",
    status: "idle",
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [isLoadingResult, setIsLoadingResult] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [severityFilter, setSeverityFilter] = useState(TEXT_COMPARE_FILTER_ALL);
  const [changeTypeFilter, setChangeTypeFilter] = useState(
    TEXT_COMPARE_FILTER_ALL,
  );
  const [sortBy, setSortBy] = useState(TEXT_COMPARE_SORT_DEFAULT);
  const [visibleCount, setVisibleCount] = useState(INITIAL_RENDER_COUNT);
  const [expandedRows, setExpandedRows] = useState({});
  const [dragging, setDragging] = useState(false);
  const pollTimer = useRef(null);
  const abortRef = useRef({ aborted: false });
  const activeSidRef = useRef(null);
  const resultRequestSeqRef = useRef(0);
  const sentinelRef = useRef(null);
  const fetchedResultRef = useRef({ sid: null, completed: false });

  useEffect(() => {
    let cancelled = false;

    Promise.allSettled([
      fetchComparerCsrfToken(),
      fetchTextCompareCapabilities(),
    ]).then(([csrfResult, capabilitiesResult]) => {
      if (cancelled) return;

      if (csrfResult.status === "fulfilled") {
        setCsrfReady(true);
        setCsrfError(null);
      } else {
        setCsrfReady(false);
        setCsrfError(csrfResult.reason?.message || "Error obteniendo CSRF");
      }

      if (capabilitiesResult.status === "fulfilled") {
        setCapabilities(
          normalizeTextCompareCapabilities(capabilitiesResult.value),
        );
      } else {
        console.warn(
          "No se pudieron cargar las capacidades del comparador; se usa fallback local.",
          capabilitiesResult.reason,
        );
        setCapabilities(DEFAULT_TEXT_COMPARE_CAPABILITIES);
      }
    });

    return () => {
      cancelled = true;
    };
  }, []);

  const validateFile = (f) => {
    if (!f) return "Archivo inválido";

    const ext = extOf(f.name);
    const okExt = capabilities.allowed_extensions.includes(ext);
    if (!okExt) {
      return formatCapabilityMessage(
        capabilities.messages.unsupported_extension,
        {
          ext: ext || "sin extensión",
          allowed_extensions: capabilities.allowed_extensions_label,
        },
      );
    }

    if (f.size > capabilities.max_file_bytes) {
      return formatCapabilityMessage(capabilities.messages.file_too_large, {
        name: f.name,
        max_mb: capabilities.max_file_mb,
        size_mb: bytesToMB(f.size),
      });
    }

    return null;
  };

  const putFile = (f) => {
    if (!f) return;
    const msg = validateFile(f);
    if (msg) {
      setWarn(msg);
      setTimeout(() => setWarn(null), 3500);
      return;
    }
    if (!fileA) setFileA(f);
    else if (!fileB) setFileB(f);
    else {
      setWarn("Solo se admiten dos ficheros: A y B.");
      setTimeout(() => setWarn(null), 2500);
    }
  };

  const stopPolling = () => {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  };

  useEffect(() => () => stopPolling(), []);

  const resetAll = () => {
    setFileA(null);
    setFileB(null);
    setSid(null);
    setProgress({ percent: 0, step: "—", detail: "", status: "idle" });
    setIsSubmitting(false);
    setResult(null);
    setIsLoadingResult(false);
    setSearchTerm("");
    setSeverityFilter(TEXT_COMPARE_FILTER_ALL);
    setChangeTypeFilter(TEXT_COMPARE_FILTER_ALL);
    setVisibleCount(INITIAL_RENDER_COUNT);
    setExpandedRows({});
    setSortBy(TEXT_COMPARE_SORT_DEFAULT);
    setError(null);
    setWarn(null);
    abortRef.current.aborted = true;
    fetchedResultRef.current = { sid: null, completed: false };
    activeSidRef.current = null;
    resultRequestSeqRef.current += 1;
    stopPolling();
  };

  const loadResult = async (
    targetSid,
    { append = false, offset = 0, limit = null } = {},
  ) => {
    if (!targetSid) return null;
    const requestSeq = resultRequestSeqRef.current + 1;
    resultRequestSeqRef.current = requestSeq;
    setIsLoadingResult(true);
    try {
      const payload = await fetchTextCompareResult(targetSid, {
        offset,
        limit,
      });
      if (activeSidRef.current && activeSidRef.current !== targetSid) {
        return null;
      }
      if (requestSeq !== resultRequestSeqRef.current) {
        return null;
      }
      setResult((prev) => {
        if (!append || !prev) {
          return {
            ...payload,
            rows: deduplicateRowsForRender(payload.rows || []),
          };
        }
        const mergedRows = deduplicateRowsForRender([
          ...(prev.rows || []),
          ...payload.rows,
        ]);
        return {
          ...payload,
          rows: mergedRows,
        };
      });
      return payload;
    } finally {
      if (requestSeq === resultRequestSeqRef.current) {
        setIsLoadingResult(false);
      }
    }
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer.files || []);
    files.slice(0, 2).forEach(putFile);
  };

  const handleStartCompare = async () => {
    setError(null);

    if (!csrfReady) {
      setError("CSRF no inicializado.");
      return;
    }
    if (!fileA || !fileB) {
      setWarn("Selecciona los dos ficheros (A y B).");
      setTimeout(() => setWarn(null), 2500);
      return;
    }
    const vA = validateFile(fileA);
    const vB = validateFile(fileB);
    if (vA || vB) {
      setWarn(vA || vB);
      setTimeout(() => setWarn(null), 3500);
      return;
    }

    setIsSubmitting(true);
    setResult(null);
    setSearchTerm("");
    setSeverityFilter(TEXT_COMPARE_FILTER_ALL);
    setChangeTypeFilter(TEXT_COMPARE_FILTER_ALL);
    setVisibleCount(INITIAL_RENDER_COUNT);
    setExpandedRows({});
    setSortBy(TEXT_COMPARE_SORT_DEFAULT);
    abortRef.current.aborted = false;

    try {
      await fetchComparerCsrfToken();

      const options = {
        engine,
      };

      const { sid: newSid } = await startTextCompareJob({
        fileA,
        fileB,
        options,
      });

      fetchedResultRef.current = { sid: newSid, completed: false };
      activeSidRef.current = newSid;
      resultRequestSeqRef.current += 1;
      setSid(newSid);
      setProgress({
        percent: 5,
        step: "Empezando",
        detail: "",
        status: "running",
      });

      stopPolling();
      pollTimer.current = setInterval(async () => {
        try {
          if (abortRef.current.aborted || activeSidRef.current !== newSid)
            return;
          const pr = await pollTextCompareProgress(newSid);
          setProgress(pr);
          if (isTextCompareSuccessStatus(pr?.status)) {
            stopPolling();
            setIsSubmitting(false);
          } else if (isTextCompareErrorStatus(pr?.status)) {
            stopPolling();
            setIsSubmitting(false);
            setError(
              pr?.detail ||
                pr?.error ||
                "La comparación terminó con error interno.",
            );
          }
        } catch (err) {
          console.warn("Error en polling:", err);
        }
      }, POLL_MS);
    } catch (err) {
      console.error("Error iniciando comparación:", err);
      setError(err?.message || "No se pudo iniciar la comparación.");
      setIsSubmitting(false);
    }
  };

  useEffect(() => {
    if (!sid || !isTextCompareSuccessStatus(progress?.status)) return;
    if (
      fetchedResultRef.current.sid === sid &&
      fetchedResultRef.current.completed
    )
      return;

    let cancelled = false;
    loadResult(sid)
      .then(() => {
        if (!cancelled) {
          fetchedResultRef.current = { sid, completed: true };
        }
      })
      .catch((fetchErr) => {
        if (!cancelled) {
          console.warn(
            "No se pudo cargar el resultado JSON del comparador:",
            fetchErr,
          );
          setWarn(
            "La comparación terminó, pero no se pudo cargar el resultado estructurado en la SPA.",
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, [sid, progress.status]);

  const canStart = useMemo(() => {
    return !!fileA && !!fileB && !isSubmitting && csrfReady;
  }, [fileA, fileB, isSubmitting, csrfReady]);

  const searchQuery = searchTerm.trim().toLowerCase();

  const filteredRows = useMemo(() => {
    const filtered = filterTextCompareRows(result?.rows || [], {
      query: searchQuery,
      severity: severityFilter,
      changeType: changeTypeFilter,
      matchesQuery: textContainsQuery,
    });
    return sortTextCompareRows(filtered, sortBy);
  }, [result, searchQuery, severityFilter, changeTypeFilter, sortBy]);

  const toggleExpandedRow = (blockId) => {
    setExpandedRows((prev) => ({
      ...prev,
      [blockId]: !prev[blockId],
    }));
  };

  useEffect(() => {
    setVisibleCount(INITIAL_RENDER_COUNT);
  }, [searchQuery, severityFilter, changeTypeFilter, sortBy, result?.sid, result?.rows?.length]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return undefined;
    if (visibleCount >= filteredRows.length) return undefined;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisibleCount((prev) =>
            Math.min(filteredRows.length, prev + RENDER_CHUNK_SIZE),
          );
        }
      },
      { rootMargin: "240px" },
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [filteredRows.length, visibleCount]);

  const visibleRows = useMemo(
    () => filteredRows.slice(0, visibleCount),
    [filteredRows, visibleCount],
  );
  const expandedVisibleCount = useMemo(() => {
    return visibleRows.filter((row) => expandedRows[row.block_id]).length;
  }, [expandedRows, visibleRows]);
  const remainingVisibleRows = Math.max(
    0,
    filteredRows.length - visibleRows.length,
  );
  const isTerminalSuccess = isTextCompareSuccessStatus(progress?.status);
  const isTerminalError = isTextCompareErrorStatus(progress?.status);
  const progressMetrics = progress?.metrics || {};
  const hasResult = Boolean(result);
  const showComparisonSetup = !hasResult && (!sid || isTerminalError);
  const showProgressPanel = sid && !hasResult && !isTerminalSuccess;
  const cacheMeta = result?.meta?.cache || {};
  const auditMeta = result?.meta?.audit || {};
  const diagnosticsMeta = result?.meta?.diagnostics || {};
  const diagnosticsCounts = diagnosticsMeta.counts || {};
  const troubleshooting = useMemo(
    () => buildCompareTroubleshooting(error),
    [error],
  );
  const viewState = useMemo(
    () =>
      deriveTextCompareViewState({
        result,
        error,
        isLoadingResult,
        rows: result?.rows || [],
        filteredRows,
      }),
    [error, filteredRows, isLoadingResult, result],
  );
  const handleExpandVisibleRows = () => {
    setExpandedRows((prev) => {
      const next = { ...prev };
      visibleRows.forEach((row) => {
        next[row.block_id] = true;
      });
      return next;
    });
  };
  const handleCollapseVisibleRows = () => {
    setExpandedRows((prev) => {
      const next = { ...prev };
      visibleRows.forEach((row) => {
        delete next[row.block_id];
      });
      return next;
    });
  };

  return (
    <div
      className={`relative w-full flex flex-col flex-1 overflow-hidden ${
        isDarkMode ? "bg-gray-900 text-white" : "bg-white text-gray-900"
      }`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <div className="w-full h-full flex flex-col overflow-y-auto scrollbar-hide px-4 md:px-10 2xl:px-20">
        <header className="mb-6 md:mb-8 2xl:mb-12">
          <div className="flex flex-col gap-2">
            <div>
              <h1
                className={`text-xl md:text-2xl 2xl:text-4xl font-extrabold tracking-tight transition-all duration-300 ${
                  isDarkMode ? "text-blue-300" : "text-blue-700"
                }`}
              >
                Comparador de documentos
              </h1>
              <p
                className={`mt-2 text-xs md:text-base 2xl:text-lg max-w-5xl transition-all duration-300 ${
                  isDarkMode ? "text-gray-400" : "text-gray-600"
                }`}
              >
                El resultado final se presenta como una tabla operativa: una
                fila por cada pareja de bloques homólogos de 200 palabras
                revisada por el runtime local, con contexto antes y después del
                cambio, decisión IA, severidad y trazabilidad de caché.
              </p>
            </div>
          </div>
        </header>

        <div className="space-y-3 mb-6">
          {!csrfReady && (
            <div
              className={`px-4 py-2 rounded text-sm 2xl:text-base ${isDarkMode ? "bg-yellow-700 text-white" : "bg-yellow-100 text-yellow-800"}`}
            >
              Inicializando protección CSRF…
            </div>
          )}
          {csrfError && (
            <div
              className={`px-4 py-2 rounded text-sm 2xl:text-base ${isDarkMode ? "bg-red-700 text-white" : "bg-red-100 text-red-800"}`}
            >
              {csrfError}
            </div>
          )}
          {warn && (
            <div
              className={`px-4 py-2 rounded text-sm 2xl:text-base ${isDarkMode ? "bg-amber-700 text-white" : "bg-amber-100 text-amber-800"}`}
            >
              {warn}
            </div>
          )}
          {error && (
            <div
              className={`px-4 py-2 rounded text-sm 2xl:text-base ${isDarkMode ? "bg-red-700 text-white" : "bg-red-100 text-red-800"}`}
            >
              <div className="space-y-3">
                <div>{error}</div>
                {troubleshooting && (
                  <div
                    className={`rounded-lg border px-3 py-3 text-xs 2xl:text-sm ${
                      troubleshooting.tone === "warning"
                        ? isDarkMode
                          ? "border-amber-400/40 bg-amber-500/10 text-amber-100"
                          : "border-amber-300 bg-amber-50 text-amber-900"
                        : isDarkMode
                          ? "border-red-400/40 bg-red-500/10 text-red-100"
                          : "border-red-300 bg-red-50 text-red-900"
                    }`}
                  >
                    <div className="font-semibold">{troubleshooting.title}</div>
                    <p className="mt-1 leading-6">{troubleshooting.summary}</p>
                    <ul className="mt-2 list-disc space-y-1 pl-5">
                      {troubleshooting.steps.map((step) => (
                        <li key={step}>{step}</li>
                      ))}
                    </ul>
                    {troubleshooting.commands?.length > 0 && (
                      <div className="mt-3 space-y-2">
                        <div className="font-semibold">Comandos sugeridos</div>
                        {troubleshooting.commands.map((command) => (
                          <code
                            key={command}
                            className={`block overflow-x-auto rounded px-3 py-2 text-[11px] 2xl:text-xs ${
                              isDarkMode
                                ? "bg-black/30 text-red-50"
                                : "bg-white text-red-950"
                            }`}
                          >
                            {command}
                          </code>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {showComparisonSetup && (
          <div
            className={`grid grid-cols-1 md:grid-cols-2 gap-4 2xl:gap-8 ${dragging ? (isDarkMode ? "ring-2 ring-blue-400 rounded-xl" : "ring-2 ring-blue-600 rounded-xl") : ""}`}
          >
            {[
              {
                key: "A",
                file: fileA,
                setFile: setFileA,
                label: "Documento A",
              },
              {
                key: "B",
                file: fileB,
                setFile: setFileB,
                label: "Documento B",
              },
            ].map(({ key, file, setFile, label }) => (
              <div
                key={key}
                className={`p-3 2xl:p-6 rounded-xl border transition-all ${isDarkMode ? "bg-gray-800 border-gray-700" : "bg-gray-50 border-gray-300"}`}
              >
                <div className="flex items-center justify-between mb-3 2xl:mb-5">
                  <h2 className="font-semibold text-xs md:text-sm 2xl:text-base">
                    {label}
                  </h2>
                  {file && (
                    <button
                      onClick={() => setFile(null)}
                      className={`text-xs 2xl:text-sm px-2 py-1 2xl:px-3 2xl:py-1.5 rounded transition-colors ${isDarkMode ? "bg-gray-700 hover:bg-gray-600" : "bg-gray-200 hover:bg-gray-300"}`}
                    >
                      Quitar
                    </button>
                  )}
                </div>

                {!file ? (
                  <label
                    className={`block w-full h-22 md:h-28 2xl:h-36 border-2 border-dashed rounded-xl cursor-pointer flex items-center justify-center text-center transition-all ${isDarkMode ? "border-gray-600 text-gray-300 hover:bg-gray-700" : "border-gray-300 text-gray-600 hover:bg-gray-100"}`}
                  >
                    <input
                      type="file"
                      className="hidden"
                      accept={capabilities.accept}
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) putFile(f);
                        e.target.value = null;
                      }}
                    />
                    <div className="p-4">
                      <i className="fas fa-file-upload text-xl md:text-2xl 2xl:text-4xl mb-2 block opacity-80" />
                      <div className="text-sm 2xl:text-lg font-medium">
                        Arrastra o pulsa para subir
                      </div>
                      <div className="text-xs 2xl:text-sm mt-1 opacity-70">
                        {capabilities.allowed_extensions_label} · máx{" "}
                        {capabilities.max_file_mb} MB
                      </div>
                    </div>
                  </label>
                ) : (
                  <div
                    className={`flex items-center gap-3 2xl:gap-5 p-2 2xl:p-4 rounded border ${isDarkMode ? "bg-gray-700 border-gray-600" : "bg-white border-gray-200"}`}
                  >
                    <div
                      className={`w-12 h-12 2xl:w-16 2xl:h-16 flex items-center justify-center rounded-lg ${isDarkMode ? "bg-gray-800" : "bg-gray-100"}`}
                    >
                      <i
                        className={`fas ${getFileIconClass(extOf(file.name).slice(1))} text-2xl 2xl:text-3xl`}
                      />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm 2xl:text-lg font-medium truncate">
                        {file.name}
                      </div>
                      <div className="text-xs 2xl:text-sm opacity-70">
                        {bytesToMB(file.size)} MB
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <div
          className={`mt-4 2xl:mt-8 flex flex-wrap gap-2 2xl:gap-4 items-center ${
            hasResult
              ? `sticky top-0 z-20 rounded-2xl border px-3 py-3 backdrop-blur md:px-4 ${
                  isDarkMode
                    ? "border-gray-700 bg-gray-900/95"
                    : "border-gray-200 bg-white/95"
                }`
              : ""
          }`}
        >
          {showComparisonSetup ? (
            <>
              <button
                disabled={!canStart}
                onClick={handleStartCompare}
                className={`px-5 py-2.5 2xl:px-8 2xl:py-4 rounded-xl font-bold text-sm 2xl:text-lg transition-all shadow-sm active:scale-95 ${
                  canStart
                    ? isDarkMode
                      ? "bg-blue-500 hover:bg-blue-600 text-white"
                      : "bg-blue-600 hover:bg-blue-700 text-white"
                    : isDarkMode
                      ? "bg-gray-700 text-gray-400"
                      : "bg-gray-200 text-gray-500"
                }`}
                title={csrfReady ? "" : "CSRF no listo"}
              >
                {isSubmitting ? (
                  <span className="inline-flex items-center gap-2">
                    <i className="fas fa-spinner fa-spin" /> Comparando…
                  </span>
                ) : (
                  "Comparar documentos"
                )}
              </button>

              <div className="flex gap-2 2xl:gap-4 flex-wrap">
                <button
                  type="button"
                  onClick={resetAll}
                  className={`px-4 py-2.5 2xl:px-6 2xl:py-3 rounded-xl font-medium text-sm 2xl:text-base border transition-colors ${
                    isDarkMode
                      ? "border-gray-600 text-gray-300 hover:bg-gray-800"
                      : "border-gray-300 text-gray-700 hover:bg-gray-100"
                  }`}
                >
                  Reset
                </button>
              </div>
            </>
          ) : (
            <div className="flex w-full flex-wrap items-center justify-between gap-3">
              <div>
                <div
                  className={`text-sm font-semibold ${
                    isDarkMode ? "text-gray-100" : "text-gray-900"
                  }`}
                >
                  Resultado activo
                </div>
                <div
                  className={`text-xs 2xl:text-sm ${
                    isDarkMode ? "text-gray-400" : "text-gray-600"
                  }`}
                >
                  Usa Reset para iniciar otra comparación y volver a mostrar la
                  carga de archivos.
                </div>
              </div>
              <button
                type="button"
                onClick={resetAll}
                className={`px-4 py-2.5 2xl:px-6 2xl:py-3 rounded-xl font-medium text-sm 2xl:text-base border transition-colors ${
                  isDarkMode
                    ? "border-gray-600 text-gray-300 hover:bg-gray-800"
                    : "border-gray-300 text-gray-700 hover:bg-gray-100"
                }`}
              >
                Reset
              </button>
            </div>
          )}
        </div>

        {showProgressPanel && (
          <div
            className={`mt-4 2xl:mt-8 p-3 2xl:p-5 rounded-xl border ${isDarkMode ? "bg-gray-800 border-gray-700" : "bg-gray-50 border-gray-300"}`}
          >
            <div className="flex items-center justify-between mb-2 gap-4">
              <div className="text-sm 2xl:text-lg font-semibold">
                Progreso: {progress.percent ?? 0}%
              </div>
              <div
                className={`text-xs 2xl:text-sm ${isTerminalError ? (isDarkMode ? "text-red-300" : "text-red-600") : isDarkMode ? "text-gray-300" : "text-gray-600"}`}
              >
                {progress.step}
                {progress.detail ? ` — ${progress.detail}` : ""}
              </div>
            </div>
            <div
              className={`w-full mt-1 h-3 2xl:h-4 rounded-full overflow-hidden ${isDarkMode ? "bg-gray-700" : "bg-gray-200"}`}
            >
              <div
                className={`h-full transition-all duration-300 ease-out ${isTerminalError ? "bg-red-500" : isDarkMode ? "bg-blue-400" : "bg-blue-600"}`}
                style={{
                  width: `${Math.min(100, Math.max(0, progress.percent || 0))}%`,
                }}
              />
            </div>
            {isTerminalError && (
              <div
                className={`mt-3 text-sm 2xl:text-base ${isDarkMode ? "text-red-300" : "text-red-700"}`}
              >
                <i className="fas fa-exclamation-triangle mr-2"></i>
                Se produjo un error en el procesamiento. Revisa el detalle del
                estado y reinicia la comparación si necesitas reintentar.
              </div>
            )}
          </div>
        )}

        {(isLoadingResult || result) && (
          <section
            className={`mt-6 2xl:mt-10 rounded-2xl border ${
              isDarkMode
                ? "border-gray-700 bg-gray-900/70"
                : "border-gray-200 bg-white"
            }`}
          >
            <div
              className={`px-4 py-4 border-b ${isDarkMode ? "border-gray-700 bg-gray-900" : "border-gray-200 bg-gray-50"}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <h2 className="font-semibold text-sm 2xl:text-lg">
                    Tabla final del comparador
                  </h2>
                  <p
                    className={`mt-1 text-xs 2xl:text-sm ${isDarkMode ? "text-gray-400" : "text-gray-600"}`}
                  >
                    Cada fila representa una pareja de bloques homólogos. La
                    tabla muestra todos los bloques detectados, con validación
                    por IA, clasificación operativa del runtime y trazabilidad
                    de caché.
                  </p>
                </div>
                <div
                  className={`text-xs 2xl:text-sm ${isDarkMode ? "text-gray-400" : "text-gray-600"}`}
                >
                  <div>
                    <strong>Archivo A:</strong> {fileA?.name || "—"}
                  </div>
                  <div>
                    <strong>Archivo B:</strong> {fileB?.name || "—"}
                  </div>
                </div>
              </div>
            </div>

            {isLoadingResult && !result && (
              <div className="px-4 py-8 text-sm 2xl:text-base">
                <span className="inline-flex items-center gap-2">
                  <i className="fas fa-spinner fa-spin" /> Cargando resultado
                  estructurado…
                </span>
              </div>
            )}

            {result && (
              <>
                <div
                  className={`border-t ${
                    isDarkMode ? "border-gray-700" : "border-gray-200"
                  }`}
                >
                  <div
                    className={`border-b px-4 py-4 ${
                      isDarkMode
                        ? "border-gray-700 bg-gray-900/80"
                        : "border-gray-200 bg-gray-50"
                    }`}
                  >
                    <div className="grid gap-4 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)] xl:items-start">
                      <div className="space-y-4">
                        <div className="space-y-3">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <label className="block text-xs font-semibold uppercase tracking-wide opacity-70">
                              Buscar por bloque, clasificación o texto
                            </label>
                            <div className="flex flex-wrap gap-2">
                              <ToolbarButton
                                icon="fa-expand"
                                label="Expandir visibles"
                                onClick={handleExpandVisibleRows}
                                disabled={visibleRows.length === 0}
                                isDarkMode={isDarkMode}
                              />
                              <ToolbarButton
                                icon="fa-compress"
                                label="Contraer visibles"
                                onClick={handleCollapseVisibleRows}
                                disabled={expandedVisibleCount === 0}
                                isDarkMode={isDarkMode}
                              />
                            </div>
                          </div>
                          <div className="relative">
                            <input
                              type="search"
                              value={searchTerm}
                              onChange={(e) => setSearchTerm(e.target.value)}
                              placeholder="Ej. #12, penalización, severidad, caché..."
                              className={`w-full rounded-xl border px-4 py-2.5 pr-12 text-sm outline-none transition-colors ${
                                isDarkMode
                                  ? "border-gray-700 bg-gray-800 text-white placeholder:text-gray-500 focus:border-blue-400"
                                  : "border-gray-300 bg-white text-gray-900 placeholder:text-gray-400 focus:border-blue-500"
                              }`}
                            />
                            {searchTerm && (
                              <button
                                type="button"
                                onClick={() => setSearchTerm("")}
                                className={`absolute inset-y-2 right-2 inline-flex items-center rounded-lg px-2 text-xs font-semibold ${
                                  isDarkMode
                                    ? "text-gray-300 hover:bg-gray-700"
                                    : "text-gray-500 hover:bg-gray-100"
                                }`}
                              >
                                Limpiar
                              </button>
                            )}
                          </div>
                          <div
                            className={`rounded-2xl border px-4 py-3 text-xs leading-5 ${
                              isDarkMode
                                ? "border-gray-700 bg-gray-800/60 text-gray-300"
                                : "border-gray-200 bg-white/80 text-gray-600"
                            }`}
                          >
                            <div className="font-semibold uppercase tracking-wide opacity-80">
                              Consejo de revisión
                            </div>
                            <div className="mt-1">
                              Mantén la tabla en dos columnas, usa el scroll interno de cada bloque para textos largos y abre el detalle solo cuando necesites el razonamiento de IA.
                            </div>
                          </div>
                          <FilterPillGroup
                            label="Severidad"
                            value={severityFilter}
                            options={SEVERITY_FILTER_OPTIONS}
                            onChange={setSeverityFilter}
                            isDarkMode={isDarkMode}
                          />
                          <FilterPillGroup
                            label="Tipo de cambio"
                            value={changeTypeFilter}
                            options={CHANGE_FILTER_OPTIONS}
                            onChange={setChangeTypeFilter}
                            isDarkMode={isDarkMode}
                          />
                          <div className="space-y-2">
                            <div className="text-xs font-semibold uppercase tracking-wide opacity-80">
                              Ordenación
                            </div>
                            <select
                              value={sortBy}
                              onChange={(e) => setSortBy(e.target.value)}
                              className={`w-full rounded-xl border px-3 py-2.5 text-sm outline-none ${
                                isDarkMode
                                  ? "border-gray-700 bg-gray-800 text-white"
                                  : "border-gray-300 bg-white text-gray-900"
                              }`}
                            >
                              {SORT_OPTIONS.map((option) => (
                                <option key={option.value} value={option.value}>
                                  {option.label}
                                </option>
                              ))}
                            </select>
                          </div>
                        </div>

                        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
                          <ResultStatPill
                            label="Filas visibles"
                            value={filteredRows.length}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Renderizadas"
                            value={visibleRows.length}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Filtro sev."
                            value={
                              SEVERITY_FILTER_OPTIONS.find(
                                (item) => item.value === severityFilter,
                              )?.label || "—"
                            }
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Filtro tipo"
                            value={
                              CHANGE_FILTER_OPTIONS.find(
                                (item) => item.value === changeTypeFilter,
                              )?.label || "—"
                            }
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Orden"
                            value={
                              SORT_OPTIONS.find((item) => item.value === sortBy)
                                ?.label || "—"
                            }
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Expand. visibles"
                            value={expandedVisibleCount}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="SID"
                            value={result.sid}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Total backend"
                            value={result.meta?.pagination?.total ?? 0}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Filas auditadas"
                            value={auditMeta.all_rows_count ?? 0}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Caché LLM"
                            value={cacheMeta.resolved_from_cache ?? 0}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Inferencias LLM"
                            value={diagnosticsCounts.pairs_sent_to_llm ?? 0}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Auditoría final IA"
                            value={diagnosticsCounts.final_review_actions ?? 0}
                            isDarkMode={isDarkMode}
                          />
                          <ResultStatPill
                            label="Bloque estándar"
                            value={`${cacheMeta.block_size_words || 200} palabras`}
                            isDarkMode={isDarkMode}
                          />
                          {cacheMeta.model_name && (
                            <ResultStatPill
                              label="Modelo"
                              value={cacheMeta.model_name}
                              isDarkMode={isDarkMode}
                            />
                          )}
                          {typeof progressMetrics.queue_latency_ms ===
                            "number" && (
                            <ResultStatPill
                              label="Cola"
                              value={`${Math.round(progressMetrics.queue_latency_ms / 1000)} s`}
                              isDarkMode={isDarkMode}
                            />
                          )}
                          {typeof progressMetrics.total_latency_ms ===
                            "number" && (
                            <ResultStatPill
                              label="Duración"
                              value={`${Math.round(progressMetrics.total_latency_ms / 1000)} s`}
                              isDarkMode={isDarkMode}
                            />
                          )}
                        </div>
                      </div>

                      <div
                        className={`text-sm leading-6 ${
                          isDarkMode ? "text-gray-300" : "text-gray-600"
                        }`}
                      >
                        {filteredRows.length === 0
                          ? "No hay bloques que coincidan con la búsqueda actual."
                          : "La vista principal muestra dos columnas paralelas (Archivo A y Archivo B), con scroll independiente por bloque y detalle expandible para revisar la explicación de IA sin saturar la tabla."}
                      </div>
                    </div>
                  </div>

                  <div className="overflow-visible px-4 py-4">
                    {filteredRows.length === 0 ? (
                      <EmptyResultsState
                        title={viewState.title}
                        description={viewState.description}
                        icon={
                          viewState.kind === "error"
                            ? "fa-triangle-exclamation"
                            : viewState.kind === "empty"
                              ? "fa-file-circle-check"
                              : "fa-filter-circle-xmark"
                        }
                        isDarkMode={isDarkMode}
                      />
                    ) : (
                      <div
                        className={`overflow-hidden rounded-3xl border shadow-sm ${
                          isDarkMode
                            ? "border-gray-800 bg-gray-900/40"
                            : "border-gray-200 bg-white"
                        }`}
                      >
                        <div
                          className={`border-b px-4 py-3 text-xs leading-5 ${
                            isDarkMode
                              ? "border-gray-800 bg-gray-900/80 text-gray-300"
                              : "border-gray-200 bg-gray-50 text-gray-600"
                          }`}
                        >
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <div className="font-semibold uppercase tracking-[0.18em] opacity-80">
                                Vista comparativa final
                              </div>
                              <div className="mt-1">
                                Tabla de dos columnas con scroll vertical y horizontal independiente para revisar grandes volúmenes sin perder el contexto de A/B.
                              </div>
                            </div>
                            <StatusBadge
                              label={`${filteredRows.length} bloque(s) filtrados`}
                              tone="info"
                              isDarkMode={isDarkMode}
                            />
                          </div>
                        </div>
                        <div className="max-h-[72vh] overflow-auto overscroll-contain">
                          <table className="min-w-[1180px] w-full text-sm 2xl:text-base border-separate border-spacing-0">
                            <thead
                              className={
                                isDarkMode
                                  ? "bg-gray-800 text-gray-300"
                                  : "bg-gray-50 text-gray-600"
                              }
                            >
                              <tr>
                                <th
                                  className={`sticky top-0 z-20 px-4 py-3 text-left font-semibold ${
                                    isDarkMode ? "bg-gray-800" : "bg-gray-50"
                                  }`}
                                >
                                  Archivo A · fragmento sensible
                                </th>
                                <th
                                  className={`sticky top-0 z-20 px-4 py-3 text-left font-semibold ${
                                    isDarkMode ? "bg-gray-800" : "bg-gray-50"
                                  }`}
                                >
                                  Archivo B · bloque correlativo
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              {visibleRows.map((row) => {
                                const isExpanded = Boolean(
                                  expandedRows[row.block_id],
                                );
                                const rowStatus = resolveRowStatus(row);
                                const hasMateriality = !isPlaceholderDecisionValue(
                                  row.materiality,
                                );
                                const showReviewStatus =
                                  row.review_status &&
                                  !isPlaceholderDecisionValue(row.review_status);
                                return (
                                  <React.Fragment key={row.block_id}>
                                    <tr
                                      id={`spa-block-${row.block_id}`}
                                      className={
                                        isDarkMode
                                          ? "border-t border-gray-800 bg-gray-950/30"
                                          : "border-t border-gray-100 bg-white"
                                      }
                                    >
                                      <td className="align-top px-4 py-4 min-w-[430px]">
                                        <div className="mb-3 flex items-start justify-between gap-3">
                                          <div>
                                            <div className="text-sm font-semibold">
                                              Bloque #{row.block_id}
                                            </div>
                                            <div
                                              className={`mt-1 text-xs ${
                                                isDarkMode
                                                  ? "text-gray-400"
                                                  : "text-gray-500"
                                              }`}
                                            >
                                              {row.pair_id
                                                ? `ID ${truncateMiddle(row.pair_id, 22)}`
                                                : "Pareja secuencial"}
                                            </div>
                                          </div>
                                          <StatusBadge
                                            label={rowStatus.label}
                                            tone={rowStatus.tone}
                                            isDarkMode={isDarkMode}
                                          />
                                        </div>
                                        <ResultTextCell
                                          label={`a-${row.block_id}`}
                                          title="Texto de referencia"
                                          segments={row.display_segments_a}
                                          plainText={row.display_text_a}
                                          metaLines={buildContextMetaLines(
                                            row,
                                            "a",
                                          )}
                                          isDarkMode={isDarkMode}
                                        />
                                      </td>
                                      <td className="align-top px-4 py-4 min-w-[430px]">
                                        <div className="mb-3 flex items-start justify-between gap-3">
                                          <div className="flex flex-wrap gap-2">
                                            <StatusBadge
                                              label={safeLabel(
                                                row.change_type,
                                              )}
                                              tone={toneForChangeType(
                                                row.change_type,
                                              )}
                                              isDarkMode={isDarkMode}
                                            />
                                            <StatusBadge
                                              label={`Sev. ${safeLabel(row.severity)}`}
                                              tone={toneForSeverity(
                                                row.severity,
                                              )}
                                              isDarkMode={isDarkMode}
                                            />
                                            <StatusBadge
                                              label={`Conf. ${safeLabel(
                                                row.confidence,
                                              )}`}
                                              tone="neutral"
                                              isDarkMode={isDarkMode}
                                            />
                                            <StatusBadge
                                              label={row.cache_hit ? "Cache hit" : "Cache miss"}
                                              tone={row.cache_hit ? "success" : "warning"}
                                              isDarkMode={isDarkMode}
                                            />
                                          </div>
                                          <DetailToggleButton
                                            isExpanded={isExpanded}
                                            onToggle={() =>
                                              toggleExpandedRow(row.block_id)
                                            }
                                            isDarkMode={isDarkMode}
                                          />
                                        </div>
                                        <ResultTextCell
                                          label={`b-${row.block_id}`}
                                          title="Texto comparado"
                                          segments={row.display_segments_b}
                                          plainText={row.display_text_b}
                                          metaLines={buildContextMetaLines(
                                            row,
                                            "b",
                                          )}
                                          isDarkMode={isDarkMode}
                                        />
                                      </td>
                                    </tr>
                                    {isExpanded && (
                                      <tr
                                        className={
                                          isDarkMode
                                            ? "border-t border-gray-800 bg-gray-950/60"
                                            : "border-t border-gray-100 bg-slate-50/80"
                                        }
                                      >
                                        <td colSpan={2} className="px-4 pb-4">
                                          <div
                                            className={`rounded-3xl border p-5 shadow-sm ${
                                              isDarkMode
                                                ? "border-gray-700 bg-gray-900/80"
                                                : "border-gray-200 bg-white"
                                            }`}
                                          >
                                            <div className="flex flex-wrap gap-2">
                                              <StatusBadge
                                                label={safeLabel(
                                                  row.change_type,
                                                )}
                                                tone={toneForChangeType(
                                                  row.change_type,
                                                )}
                                                isDarkMode={isDarkMode}
                                              />
                                              <StatusBadge
                                                label={rowStatus.label}
                                                tone={rowStatus.tone}
                                                isDarkMode={isDarkMode}
                                              />
                                              <StatusBadge
                                                label={`Sev. ${safeLabel(row.severity)}`}
                                                tone={toneForSeverity(
                                                  row.severity,
                                                )}
                                                isDarkMode={isDarkMode}
                                              />
                                              <StatusBadge
                                                label={`Conf. ${safeLabel(row.confidence)}`}
                                                tone="neutral"
                                                isDarkMode={isDarkMode}
                                              />
                                            </div>

                                            <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
                                              <div className="space-y-3 text-sm leading-6">
                                                <div>
                                                  <div className="text-xs font-semibold uppercase tracking-wide opacity-70">
                                                    Comentario corto IA
                                                  </div>
                                                  <div>
                                                    {row.llm_comment ||
                                                      row.summary ||
                                                      "Sin resumen adicional: la fila fue validada por IA."}
                                                  </div>
                                                </div>
                                                {row.summary &&
                                                  row.llm_comment !==
                                                    row.summary && (
                                                    <div>
                                                      <div className="text-xs font-semibold uppercase tracking-wide opacity-70">
                                                        Resumen ampliado
                                                      </div>
                                                      <div>{row.summary}</div>
                                                    </div>
                                                  )}
                                                {row.impact && (
                                                  <div>
                                                    <div className="text-xs font-semibold uppercase tracking-wide opacity-70">
                                                      Impacto
                                                    </div>
                                                    <div>{row.impact}</div>
                                                  </div>
                                                )}
                                                {row.justification && (
                                                  <div>
                                                    <div className="text-xs font-semibold uppercase tracking-wide opacity-70">
                                                      Justificación
                                                    </div>
                                                    <div
                                                      className={
                                                        isDarkMode
                                                          ? "text-gray-400"
                                                          : "text-gray-600"
                                                      }
                                                    >
                                                      {row.justification}
                                                    </div>
                                                  </div>
                                                )}
                                              </div>

                                              <div
                                                className={`space-y-2 text-xs leading-5 ${
                                                  isDarkMode
                                                    ? "text-gray-400"
                                                    : "text-gray-600"
                                                }`}
                                              >
                                                <div>
                                                  {rowStatus.helperLabel}:{" "}
                                                  <strong>
                                                    {rowStatus.helperValue}
                                                  </strong>
                                                </div>
                                                {hasMateriality && (
                                                  <div>
                                                    Materialidad:{" "}
                                                    <strong>
                                                      {safeLabel(
                                                        row.materiality,
                                                      )}
                                                    </strong>
                                                  </div>
                                                )}
                                                <div>
                                                  Procedencia:{" "}
                                                  <strong>
                                                    {safeLabel(
                                                      row.decision_source,
                                                    )}
                                                  </strong>
                                                </div>
                                                <div>
                                                  Caché:{" "}
                                                  <strong>
                                                    {row.cache_hit
                                                      ? "hit"
                                                      : "miss"}
                                                  </strong>
                                                </div>
                                                {row.model_name && (
                                                  <div>
                                                    Modelo:{" "}
                                                    <strong>
                                                      {row.model_name}
                                                    </strong>
                                                  </div>
                                                )}
                                                {showReviewStatus && (
                                                  <div>
                                                    Estado:{" "}
                                                    <strong>
                                                      {row.review_status}
                                                    </strong>
                                                  </div>
                                                )}
                                                <div>
                                                  Origen resultado:{" "}
                                                  <strong>
                                                    {safeLabel(
                                                      row.result_origin,
                                                    )}
                                                  </strong>
                                                </div>
                                                <div>
                                                  Validación resultado:{" "}
                                                  <strong>
                                                    {safeLabel(
                                                      row.result_validation_status,
                                                    )}
                                                  </strong>
                                                </div>
                                                <div>
                                                  Bloques/chunks:{" "}
                                                  <strong>
                                                    A #{row.chunk_index_a || 0}
                                                    {" · "}
                                                    B #{row.chunk_index_b || 0}
                                                  </strong>
                                                </div>
                                                {row.cache_pair_hash && (
                                                  <div>
                                                    Hash caché:{" "}
                                                    <strong>
                                                      {truncateMiddle(
                                                        row.cache_pair_hash,
                                                        26,
                                                      )}
                                                    </strong>
                                                  </div>
                                                )}
                                                {row.related_blocks?.length >
                                                  0 && (
                                                  <div>
                                                    Relacionados:{" "}
                                                    {row.related_blocks
                                                      .map(
                                                        (link) =>
                                                          link.label ||
                                                          `#${link.block_id}`,
                                                      )
                                                      .join(", ")}
                                                  </div>
                                                )}
                                              </div>
                                            </div>
                                            <div className="mt-4 grid gap-3 xl:grid-cols-2">
                                              <ContextSnippet
                                                label="Contexto A"
                                                before={row.context_before_a}
                                                after={row.context_after_a}
                                                isDarkMode={isDarkMode}
                                              />
                                              <ContextSnippet
                                                label="Contexto B"
                                                before={row.context_before_b}
                                                after={row.context_after_b}
                                                isDarkMode={isDarkMode}
                                              />
                                            </div>
                                          </div>
                                        </td>
                                      </tr>
                                    )}
                                  </React.Fragment>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}

                    {remainingVisibleRows > 0 && (
                      <div className="mt-4 text-center text-xs opacity-70">
                        Desplázate para renderizar {remainingVisibleRows}{" "}
                        fila(s) adicionales.
                      </div>
                    )}
                    <div ref={sentinelRef} className="h-6" />
                  </div>
                </div>
              </>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
