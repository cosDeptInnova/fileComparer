export const TEXT_COMPARE_PANEL_NAME = "TextCompareMainPanel";
export const TEXT_COMPARE_CANONICAL_ROUTE = "/main/text-compare";
export const TEXT_COMPARE_PUBLIC_ROUTE = "/text-compare";
export const TEXT_COMPARE_LEGACY_ROUTE = "/main/text_compare";

export const DEFAULT_TEXT_COMPARE_CAPABILITIES = {
  service: "comparador",
  panel_name: TEXT_COMPARE_PANEL_NAME,
  route: TEXT_COMPARE_CANONICAL_ROUTE,
  accept: ".pdf,.doc,.docx,.txt,.rtf,.xls,.xlsx,.ppt,.pptx,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp",
  allowed_extensions: [
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".rtf",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
  ],
  allowed_extensions_label:
    ".pdf, .doc, .docx, .txt, .rtf, .xls, .xlsx, .ppt, .pptx, .png, .jpg, .jpeg, .bmp, .tif, .tiff, .webp",
  max_file_mb: 40,
  max_file_bytes: 40 * 1024 * 1024,
  messages: {
    unsupported_extension:
      "Formato no soportado ({ext}). Extensiones admitidas: {allowed_extensions}.",
    file_too_large:
      'El archivo "{name}" supera el máximo de {max_mb} MB ({size_mb} MB).',
    file_too_large_backend: "Archivo demasiado grande. Máximo {max_mb} MB por fichero.",
    empty_file: "Alguno de los archivos está vacío.",
  },
};

export function normalizeTextCompareCapabilities(payload = {}) {
  const allowedExtensions = Array.isArray(payload.allowed_extensions)
    && payload.allowed_extensions.length > 0
    ? payload.allowed_extensions.map((ext) => String(ext).toLowerCase())
    : DEFAULT_TEXT_COMPARE_CAPABILITIES.allowed_extensions;

  const maxFileMb = Number(payload.max_file_mb);
  const normalizedMaxFileMb = Number.isFinite(maxFileMb) && maxFileMb > 0
    ? maxFileMb
    : DEFAULT_TEXT_COMPARE_CAPABILITIES.max_file_mb;

  return {
    ...DEFAULT_TEXT_COMPARE_CAPABILITIES,
    ...payload,
    panel_name: payload.panel_name || DEFAULT_TEXT_COMPARE_CAPABILITIES.panel_name,
    route: payload.route || DEFAULT_TEXT_COMPARE_CAPABILITIES.route,
    allowed_extensions: allowedExtensions,
    allowed_extensions_label:
      payload.allowed_extensions_label
      || allowedExtensions.join(", ")
      || DEFAULT_TEXT_COMPARE_CAPABILITIES.allowed_extensions_label,
    accept:
      payload.accept
      || allowedExtensions.join(",")
      || DEFAULT_TEXT_COMPARE_CAPABILITIES.accept,
    max_file_mb: normalizedMaxFileMb,
    max_file_bytes: normalizedMaxFileMb * 1024 * 1024,
    messages: {
      ...DEFAULT_TEXT_COMPARE_CAPABILITIES.messages,
      ...(payload.messages || {}),
    },
  };
}

export function isTextComparePath(pathname = "") {
  return String(pathname).includes("/text-compare");
}