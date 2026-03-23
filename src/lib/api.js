// src/lib/api.js

import { resolveComparatorApiBase } from "./comparatorApiConfig.mjs";

// Base del API del modelo de negocio.
// Detrás de Nginx, /api/modelo/* → FastAPI modelo_negocio
const BASE_API_URL = process.env.REACT_APP_MODELO_API_BASE || "/api/modelo";

// Base del API de chat_document.
// Detrás de Nginx, /api/chatdoc/* → FastAPI chat_document (puerto 8100 en el host)
const CHATDOC_API_BASE =
  process.env.REACT_APP_CHATDOC_API_BASE || "/api/chatdoc";

// Base del API NLP/RAG.
// Detrás de Nginx, /api/nlp/* → FastAPI NLP (puerto 5000 en el host)
const NLP_API_BASE = process.env.REACT_APP_NLP_API_BASE || "/api/nlp";

function normalizeApiBaseUrl(rawValue, fallbackPath) {
  const fallback = String(fallbackPath || "").trim() || "/";
  const raw = String(rawValue || "").trim();
  const candidate = raw || fallback;

  if (/^https?:\/\//i.test(candidate)) {
    return candidate.replace(/\/+$/, "");
  }

  const normalizedPath = `/${candidate.replace(/^\/+/, "").replace(/\/+$/, "")}`;
  return normalizedPath === "//" ? "/" : normalizedPath;
}

// Base del API del COMPARADOR de documentos.
// Detrás de Nginx, /api/comparador/* → FastAPI comparador (puerto 8007 en el host)
const COMPARATOR_API_BASE = resolveComparatorApiBase(
  process.env.REACT_APP_COMPARATOR_API_BASE,
);

const WEBSEARCH_API_BASE =
  process.env.REACT_APP_WEBSEARCH_API_BASE || "/api/websearch";

const LEGALSEARCH_API_BASE =
  process.env.REACT_APP_LEGALSEARCH_API_BASE || "/api/legalsearch";

// CSRF web_search (csrftoken_websearch)
function getWebsearchCsrfToken() {
  return getCookieValue("csrftoken_websearch");
}
// --- Utilidades genéricas de cookies ---

function getCookieValue(name) {
  const decoded = decodeURIComponent(document.cookie || "");
  const parts = decoded.split("; ");
  const prefix = `${name}=`;
  for (const part of parts) {
    if (part.startsWith(prefix)) {
      return part.substring(prefix.length);
    }
  }
  return null;
}

// CSRF modelo_negocio (csrftoken_app)
function getCsrfToken() {
  return getCookieValue("csrftoken_app");
}

// CSRF chat_document (csrftoken_chatdoc)
function getChatdocCsrfToken() {
  return getCookieValue("csrftoken_chatdoc");
}

// CSRF NLP (csrftoken_nlp)
function getNlpCsrfToken() {
  return getCookieValue("csrftoken_nlp");
}

// CSRF COMPARADOR (usa mismo nombre que modelo: csrftoken_app)
function getComparerCsrfToken() {
  return getCookieValue("csrftoken_app");
}

// --- Fetch genérico para modelo_negocio ---

async function apiFetch(path, { method = "GET", headers = {}, body } = {}) {
  const url = `${BASE_API_URL}${path}`;
  const opts = {
    method,
    credentials: "include", // muy importante para SSO / cookies
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    // NO poner Content-Type, el navegador se encarga
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  // CSRF solo en métodos "peligrosos"
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = getCsrfToken();
    if (csrf) {
      opts.headers["X-CSRFToken"] = csrf;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    throw new Error(await parseApiError(res, url));
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

// --- Fetch genérico para chat_document ---

async function chatdocFetch(path, { method = "GET", headers = {}, body } = {}) {
  const url = `${CHATDOC_API_BASE}${path}`;
  const opts = {
    method,
    credentials: "include",
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = getChatdocCsrfToken();
    if (csrf) {
      opts.headers["X-CSRFToken"] = csrf;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    throw new Error(await parseApiError(res, url));
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

// --- Fetch genérico para NLP / RAG ---

async function nlpFetch(path, { method = "GET", headers = {}, body } = {}) {
  const url = `${NLP_API_BASE}${path}`;
  const opts = {
    method,
    credentials: "include", // necesario para enviar cookies de sesión / CSRF
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    // NO poner Content-Type, el navegador se encarga
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  // En el servicio NLP usamos el mismo esquema CSRF (double-submit cookie).
  const csrf = getNlpCsrfToken();
  if (csrf) {
    opts.headers["X-CSRFToken"] = csrf;
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Error ${res.status} en ${url}: ${text}`);
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

async function websearchFetch(
  path,
  { method = "GET", headers = {}, body } = {},
) {
  const url = `${WEBSEARCH_API_BASE}${path}`;
  const opts = {
    method,
    credentials: "include",
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  // CSRF para métodos no-idempotentes
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = getWebsearchCsrfToken();
    if (csrf) {
      opts.headers["X-CSRFToken"] = csrf;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Error ${res.status} en ${url}: ${text}`);
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

async function legalsearchFetch(
  path,
  { method = "GET", headers = {}, body } = {},
) {
  const url = `${LEGALSEARCH_API_BASE}${path}`;
  const opts = {
    method,
    credentials: "include",
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = getWebsearchCsrfToken();
    if (csrf) {
      opts.headers["X-CSRFToken"] = csrf;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Error ${res.status} en ${url}: ${text}`);
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

// --- Fetch genérico para COMPARADOR ---

async function parseApiError(res, url) {
  const rawText = await res.text().catch(() => "");
  const contentType = res.headers.get("content-type") || "";

  if (contentType.includes("application/json")) {
    try {
      const payload = JSON.parse(rawText);
      if (payload?.detail) {
        return `Error ${res.status} en ${url}: ${payload.detail}`;
      }
      if (payload?.message) {
        return `Error ${res.status} en ${url}: ${payload.message}`;
      }
    } catch (_) {
      // Ignoramos parseos fallidos y devolvemos el texto bruto.
    }
  }

  return `Error ${res.status} en ${url}: ${rawText}`;
}

async function comparerFetch(
  path,
  { method = "GET", headers = {}, body, cache } = {},
) {
  const safePath = String(path || "").startsWith("/")
    ? String(path || "")
    : `/${String(path || "")}`;
  const url = `${COMPARATOR_API_BASE}${safePath}`;
  const opts = {
    method,
    cache: cache || (method.toUpperCase() === "GET" ? "no-store" : "default"),
    credentials: "include",
    headers: {
      ...headers,
    },
  };

  if (body instanceof FormData) {
    // NO poner Content-Type, el navegador se encarga
    opts.body = body;
  } else if (body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }

  // CSRF para métodos no-idempotentes
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase())) {
    const csrf = getComparerCsrfToken();
    if (csrf) {
      opts.headers["X-CSRFToken"] = csrf;
    }
  }

  const res = await fetch(url, opts);
  if (!res.ok) {
    throw new Error(await parseApiError(res, url));
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

/* === Endpoints genéricos / helpers === */

/**
 * Inicializa el token CSRF del micro de modelo_negocio.
 *
 * GET /csrf-token (detrás de nginx: /api/modelo/csrf-token)
 * → pone cookie "csrftoken_app" y devuelve { csrf_token: "..." }.
 */
export async function fetchCsrfToken() {
  try {
    return await apiFetch("/csrf-token", { method: "GET" });
  } catch (e) {
    console.error("Error al obtener CSRF token del modelo:", e);
    throw e;
  }
}

/**
 * Inicializa el token CSRF del micro de chat_document.
 *
 * GET /csrf-token (detrás de nginx: /api/chatdoc/csrf-token)
 * → pone cookie "csrftoken_chatdoc" y devuelve { csrf_token: "..." }.
 */
export async function fetchChatdocCsrfToken() {
  try {
    return await chatdocFetch("/csrf-token", { method: "GET" });
  } catch (e) {
    console.error("Error al obtener CSRF token de chat_document:", e);
    throw e;
  }
}

/**
 * Inicializa el token CSRF del micro NLP/RAG.
 *
 * GET /csrf-token (detrás de nginx: /api/nlp/csrf-token)
 * → pone cookie "csrftoken_nlp" y devuelve { csrf_token: "..." }.
 */
export async function fetchNlpCsrfToken() {
  try {
    return await nlpFetch("/csrf-token", { method: "GET" });
  } catch (e) {
    console.error("Error al obtener CSRF token del NLP:", e);
    throw e;
  }
}

/**
 * Función de bootstrap usada por MainLayout.jsx.
 *
 * - Llama a /csrf-token para asegurarse de que la cookie CSRF está presente.
 * - Llama a /me para recuperar los datos del usuario autenticado.
 */
export async function bootstrapModelo() {
  try {
    try {
      await fetchCsrfToken();
    } catch (e) {
      console.warn(
        "bootstrapModelo: no se pudo inicializar CSRF del modelo, continuo:",
        e,
      );
    }

    const me = await fetchMe();

    return {
      ok: true,
      user: me,
    };
  } catch (error) {
    console.error("bootstrapModelo: error durante el bootstrap:", error);
    return {
      ok: false,
      user: null,
      error: error?.message || String(error),
    };
  }
}

/**
 * Bootstrap específico de chat_document.
 *
 * De momento solo se encarga de inicializar el CSRF de chat_document.
 */
export async function bootstrapChatdoc() {
  try {
    await fetchChatdocCsrfToken();
    return { ok: true };
  } catch (error) {
    console.error(
      "bootstrapChatdoc: error inicializando CSRF de chat_document:",
      error,
    );
    return {
      ok: false,
      error: error?.message || String(error),
    };
  }
}

/**
 * Bootstrap específico para el micro NLP/RAG.
 *
 * Inicializa solo su CSRF (csrftoken_nlp). El contexto de subida
 * se obtiene con `fetchNlpUploadContext`.
 */
export async function bootstrapNlp() {
  try {
    await fetchNlpCsrfToken();
    return { ok: true };
  } catch (error) {
    console.error("bootstrapNlp: error inicializando CSRF del NLP:", error);
    return {
      ok: false,
      error: error?.message || String(error),
    };
  }
}

/* === Endpoints modelo_negocio existentes === */

export async function fetchMe() {
  // GET /me
  return apiFetch("/me", { method: "GET" });
}

export async function fetchConversations() {
  // GET /conversations → [{ id, title, created_at }]
  return apiFetch("/conversations", { method: "GET" });
}

export async function deleteConversation(conversationId) {
  return apiFetch(`/conversations/${conversationId}`, { method: "DELETE" });
}

export async function rateMessage(messageId, isLiked) {
  // PUT /messages/{id}/feedback
  // body: { is_liked: true } (o false/null)
  return apiFetch(`/messages/${messageId}/feedback`, {
    method: "PUT",
    body: { is_liked: isLiked },
  });
}

export async function toggleFavoriteConversation(conversationId, isFavorite) {
  // PATCH /conversations/{id}/favorite
  // Body: { is_favorite: true/false }
  return apiFetch(`/conversations/${conversationId}/favorite`, {
    method: "PATCH",
    body: { is_favorite: isFavorite },
  });
}

export async function fetchConversationDetail(id) {
  // GET /conversations/{id} → { id, created_at, messages: [...] }
  return apiFetch(`/conversations/${id}`, { method: "GET" });
}

export async function downloadRagFile({ filename, department = null }) {
  // 1. Construir parámetros
  const params = new URLSearchParams();
  params.append("filename", filename);
  if (department) {
    params.append("department", department);
  }

  // 2. Construir URL usando la variable local NLP_API_BASE
  // Si NLP_API_BASE es "/api/nlp", esto generará "/api/nlp/download_file?..."
  const url = `${NLP_API_BASE}/download_file?${params.toString()}`;

  // 3. Preparar Headers
  const headers = {};

  // Inyectar CSRF usando tu función local
  const csrf = getNlpCsrfToken();
  if (csrf) {
    headers["X-CSRFToken"] = csrf;
  }

  // 4. Fetch Nativo configurado para BLOB
  // Usamos fetch directo porque nlpFetch intenta hacer .text() o .json() y rompería el binario
  const response = await fetch(url, {
    method: "GET",
    headers: headers,
    credentials: "include", // CRÍTICO: Envía las cookies de sesión al backend
  });

  // 5. Manejo de Errores
  if (!response.ok) {
    // Si falla (ej: 404), el backend devuelve texto JSON, así que intentamos leerlo
    const text = await response.text().catch(() => "Error desconocido");
    throw new Error(`Error descargando archivo (${response.status}): ${text}`);
  }

  // 6. Manejo de Éxito (Blob)
  const blob = await response.blob(); // Obtenemos el archivo binario intacto

  // Truco del enlace invisible para forzar la descarga
  const downloadUrl = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = downloadUrl;
  link.setAttribute("download", filename); // Nombre con el que se guardará
  document.body.appendChild(link);
  link.click();

  // Limpieza de memoria
  link.remove();
  window.URL.revokeObjectURL(downloadUrl);
}

export async function fetchFileContent(fileId) {
  // 1. Construir la URL.
  // Como pusimos el backend en el microservicio NLP, la ruta es esta:
  const url = `${NLP_API_BASE || "/api/nlp"}/files/${fileId}/content`;

  // 2. Preparar Headers (CSRF)
  const headers = {};

  // Usamos tu función auxiliar para el CSRF
  // (Aunque es un GET, mal no hace si tu backend lo espera, mantengamos consistencia)
  const csrf = typeof getNlpCsrfToken === "function" ? getNlpCsrfToken() : null;
  if (csrf) {
    headers["X-CSRFToken"] = csrf;
  }

  // 3. Fetch Nativo configurado para BLOB y COOKIES
  const response = await fetch(url, {
    method: "GET",
    headers: headers,

    // Envía la cookie de sesión automáticamente
    credentials: "include",
  });

  // 4. Manejo de Errores
  if (!response.ok) {
    const text = await response.text().catch(() => "Error desconocido");
    throw new Error(`Error visualizando archivo (${response.status}): ${text}`);
  }

  // 5. Devolver el Blob directamente (para que el Modal cree la URL temporal)
  return await response.blob();
}

export async function sendChatMessage({
  message,
  conversationId,
  choice = "C",
  files = [],
}) {
  // POST /query/llm
  return apiFetch("/query/llm", {
    method: "POST",
    body: {
      message, // campo que el backend espera
      choice, // "C" (COSMOS) o "R" (rápido)
      conversation_id: conversationId || null,
      files, // lista de IDs efímeros (si usas /uploadfile)
    },
  });
}

export async function uploadEphemeralFiles(files) {
  // POST /uploadfile/ con multipart
  const formData = new FormData();
  for (const f of files) {
    formData.append("files", f);
  }
  return apiFetch("/uploadfile/", {
    method: "POST",
    body: formData,
  });
}

export async function resetContext() {
  // POST /context/reset
  return apiFetch("/context/reset", { method: "POST" });
}

export async function logout() {
  // POST /logout del micro modelo_negocio
  return apiFetch("/logout", { method: "POST" });
}

/* === Endpoints específicos de chat_document === */

/**
 * Sube un documento al microservicio chat_document
 * → POST /api/chatdoc/document/upload
 */
export async function uploadChatDocDocument(file) {
  const formData = new FormData();
  formData.append("file", file);
  return chatdocFetch("/document/upload", {
    method: "POST",
    body: formData,
  });
}

/**
 * Envía una consulta al microservicio chat_document
 * → POST /api/chatdoc/document/query
 */
export async function sendChatDocMessage({
  prompt,
  docSessionId,
  conversationId = null,
  mode = null,
}) {
  return chatdocFetch("/document/query", {
    method: "POST",
    body: {
      prompt,
      doc_session_id: docSessionId,
      conversation_id: conversationId,
      mode,
    },
  });
}

/* === Endpoints específicos del micro NLP/RAG === */

/**
 * Recupera el contexto de subida al NLP:
 *   { role, departments, user_directory }
 *
 * GET /api/nlp/upload_context → /upload_context en el micro NLP.
 */
export async function fetchNlpUploadContext() {
  return nlpFetch("/upload_context", { method: "GET" });
}

/**
 * Sube archivos al micro NLP, reutilizando la lógica antigua de /upload_file:
 *   - files: array de File
 *   - department: string department_directory o null (privado)
 *
 * POST /api/nlp/upload_file → /upload_file en el micro NLP.
 */
export async function uploadRagFiles({ files, department = null }) {
  const formData = new FormData();
  files.forEach((f) => formData.append("files", f));
  if (department) {
    formData.append("department", department);
  }
  return nlpFetch("/upload_file", {
    method: "POST",
    body: formData,
  });
}

/**
 * Lista archivos subidos (privados o de un departamento concreto).
 *
 * GET /api/nlp/list_files[?department=...]
 */
export async function listRagFiles({ department = null } = {}) {
  const params = new URLSearchParams();
  if (department) params.append("department", department);
  const qs = params.toString();
  const path = qs ? `/list_files?${qs}` : "/list_files";
  return nlpFetch(path, { method: "GET" });
}

/**
 * Borra archivos seleccionados.
 *
 * DELETE /api/nlp/delete_files
 * body: { filenames: [...], department: string | null }
 */
export async function deleteRagFiles({ filenames, department = null }) {
  return nlpFetch("/delete_files", {
    method: "DELETE",
    body: {
      filenames,
      department,
    },
  });
}

/**
 * Lanza el procesamiento e indexado de los ficheros del usuario
 * en su directorio privado.
 *
 * GET /api/nlp/process_user_files[?client_tag=...&scan_dir=...]
 */
export async function processUserRagFiles({ clientTag, scanDir } = {}) {
  const params = new URLSearchParams();
  if (clientTag) params.append("client_tag", clientTag);
  if (scanDir) params.append("scan_dir", scanDir);

  const qs = params.toString();
  const path = qs ? `/process_user_files?${qs}` : "/process_user_files";

  return nlpFetch(path, { method: "GET" });
}

/**
 * Lanza el procesamiento e indexado de los ficheros departamentales
 * (solo rol Supervisor).
 *
 * GET /api/nlp/process_department_files
 */
export async function processDepartmentRagFiles() {
  return nlpFetch("/process_department_files", { method: "GET" });
}

/**
 * Buscador RAG simple (equivalente al JS antiguo de /search).
 *
 * POST /api/nlp/search
 */
export async function searchRag({ query, topK = 5, topKContext = 3 }) {
  return nlpFetch("/search", {
    method: "POST",
    body: {
      query,
      top_k: topK,
      top_k_context: topKContext,
    },
  });
}

/* === Endpoints del COMPARADOR de textos (via /api/comparador) === */

/**
 * Inicializa el token CSRF del micro comparador.
 *
 * GET /api/comparador/csrf-token
 * → pone cookie "csrftoken_app" y devuelve { csrf_token: "..." }.
 */
export async function fetchComparerCsrfToken() {
  return comparerFetch("/csrf-token", { method: "GET" });
}

/**
 * Recupera las capacidades declaradas por el backend del comparador.
 * GET /api/comparador/capabilities
 */
export async function fetchTextCompareCapabilities() {
  return comparerFetch("/capabilities", { method: "GET" });
}

/**
 * Inicia un job de comparación de textos.
 * POST /api/comparador/comparar
 * FormData:
 *  - file_a: File
 *  - file_b: File
 *  - engine: auto | builtin | docling (opcional)
 *  - soffice: ruta a LibreOffice/soffice para formatos condicionales (opcional)
 */
export async function startTextCompareJob({ fileA, fileB, options = {} }) {
  const form = new FormData();
  form.append("file_a", fileA);
  form.append("file_b", fileB);

  if (options?.engine) {
    form.append("engine", String(options.engine));
  }
  if (options?.soffice) {
    form.append("soffice", String(options.soffice));
  }

  return comparerFetch("/comparar", { method: "POST", body: form });
}

/**
 * Consulta el progreso del job.
 * GET /api/comparador/progress/{sid}
 */
export async function pollTextCompareProgress(sid) {
  return comparerFetch(`/progress/${encodeURIComponent(sid)}`, {
    method: "GET",
  });
}

function normalizeTextCompareSegments(value = []) {
  if (!Array.isArray(value)) return [];

  return value.reduce((acc, segment) => {
    const type = String(segment?.type || "equal")
      .trim()
      .toLowerCase();
    const safeType = ["equal", "insert", "delete", "replace"].includes(type)
      ? type
      : "equal";
    const text = String(segment?.text || "");
    if (!text) return acc;
    const last = acc[acc.length - 1];
    if (last && last.type === safeType) {
      last.text += text;
      return acc;
    }
    acc.push({ type: safeType, text });
    return acc;
  }, []);
}

export function normalizeTextCompareResultPayload(payload = {}) {
  const meta = payload?.meta || {};
  const pagination = meta?.pagination || {};
  const audit = meta?.audit || {};
  const cache = meta?.cache || {};
  const pairing = meta?.pairing || {};
  const segmentation = meta?.segmentation || {};
  const rowFormation = meta?.row_formation || {};
  const rows = Array.isArray(payload?.rows)
    ? payload.rows.map((row) => ({
        block_id: Number.parseInt(row?.block_id, 10) || 0,
        pair_id: String(row?.pair_id || ""),
        pair_hash: String(row?.pair_hash || row?.cache_pair_hash || ""),
        text_a: String(row?.text_a || ""),
        text_b: String(row?.text_b || ""),
        display_text_a: String(row?.display_text_a || row?.text_a || ""),
        display_text_b: String(row?.display_text_b || row?.text_b || ""),
        display_segments_a: normalizeTextCompareSegments(
          row?.display_segments_a,
        ),
        display_segments_b: normalizeTextCompareSegments(
          row?.display_segments_b,
        ),
        context_before_a: String(row?.context_before_a || ""),
        context_after_a: String(row?.context_after_a || ""),
        context_before_b: String(row?.context_before_b || ""),
        context_after_b: String(row?.context_after_b || ""),
        change_type: String(row?.change_type || "pendiente_confirmacion"),
        materiality: String(row?.materiality || "pendiente_confirmacion"),
        confidence: String(row?.confidence || "baja"),
        final_decision: String(row?.final_decision || "pendiente_confirmacion"),
        severity: String(row?.severity || "baja"),
        summary: String(row?.summary || ""),
        impact: String(row?.impact || ""),
        llm_comment: String(row?.llm_comment || row?.summary || ""),
        justification: String(row?.justification || ""),
        review_status: String(row?.review_status || ""),
        decision_source: String(row?.decision_source || ""),
        result_origin: String(
          row?.result_origin || (row?.cache_hit ? "cache" : "fallback"),
        ),
        result_validation_status: String(
          row?.result_validation_status ||
            (row?.llm_success ? "validated" : "fallback_applied"),
        ),
        fallback_applied: Boolean(
          row?.fallback_applied ?? !Boolean(row?.llm_success),
        ),
        started_at: String(row?.started_at || ""),
        completed_at: String(row?.completed_at || ""),
        cache_stored_at:
          row?.cache_stored_at === null || row?.cache_stored_at === undefined
            ? null
            : Number(row.cache_stored_at),
        cache_hit: Boolean(row?.cache_hit),
        cache_pair_hash: String(row?.cache_pair_hash || row?.pair_hash || ""),
        llm_success: Boolean(row?.llm_success),
        model_name: String(row?.model_name || ""),
        prompt_version: String(row?.prompt_version || ""),
        prompt_text_a_literal: String(
          row?.prompt_text_a_literal || row?.text_a || "",
        ),
        prompt_text_b_literal: String(
          row?.prompt_text_b_literal || row?.text_b || "",
        ),
        prompt_messages: Array.isArray(row?.prompt_messages)
          ? row.prompt_messages
          : [],
        relation_type: String(row?.relation_type || ""),
        relation_notes: String(row?.relation_notes || ""),
        related_block_ids: Array.isArray(row?.related_block_ids)
          ? row.related_block_ids
              .map((item) => Number(item) || 0)
              .filter((item) => item > 0)
          : [],
        related_blocks: Array.isArray(row?.related_blocks)
          ? row.related_blocks
          : [],
        source_spans: row?.source_spans || {},
        pairing: row?.pairing || {},
        chunk_index_a: Number(row?.chunk_index_a) || 0,
        chunk_index_b: Number(row?.chunk_index_b) || 0,
        offset_start_a: Number(row?.offset_start_a) || 0,
        offset_end_a: Number(row?.offset_end_a) || 0,
        offset_start_b: Number(row?.offset_start_b) || 0,
        offset_end_b: Number(row?.offset_end_b) || 0,
        block_word_count_a: Number(row?.block_word_count_a) || 0,
        block_word_count_b: Number(row?.block_word_count_b) || 0,
        block_size_words: Number(row?.block_size_words) || 0,
        block_overlap_words: Number(row?.block_overlap_words) || 0,
        alignment_score: Number(row?.alignment_score) || 0,
        alignment_strategy: String(row?.alignment_strategy || ""),
        reanchored: Boolean(row?.reanchored),
      }))
    : [];

  return {
    sid: payload?.sid || null,
    status: payload?.status || "idle",
    progress: payload?.progress || {},
    ok: payload?.ok,
    error: payload?.error || null,
    reason: String(payload?.reason || "").trim(),
    rows,
    meta: {
      pagination: {
        offset: Number(pagination?.offset ?? 0),
        limit:
          pagination?.limit === null || pagination?.limit === undefined
            ? null
            : Number(pagination.limit),
        returned: Number(pagination?.returned ?? rows.length),
        total: Number(pagination?.total ?? rows.length),
        has_more: Boolean(pagination?.has_more),
        next_offset:
          pagination?.next_offset === null ||
          pagination?.next_offset === undefined
            ? null
            : Number(pagination.next_offset),
        truncated: Boolean(pagination?.truncated),
      },
      audit: {
        all_rows_count: Number(audit?.all_rows_count ?? rows.length),
        filtered_rows_count: Number(audit?.filtered_rows_count ?? rows.length),
        unchanged_rows_count: Number(audit?.unchanged_rows_count ?? 0),
      },
      cache: {
        policy: String(cache?.policy || "no-store"),
        resolved_from_cache: Number(cache?.resolved_from_cache || 0),
        resolved_by_llm: Number(cache?.resolved_by_llm || 0),
        failed_blocks: Number(cache?.failed_blocks || 0),
        block_size_words: Number(cache?.block_size_words || 0),
        block_overlap_words: Number(cache?.block_overlap_words || 0),
        model_name: String(cache?.model_name || ""),
        comparison_mode: String(cache?.comparison_mode || ""),
      },
      segmentation,
      pairing,
      row_formation: rowFormation,
    },
  };
}

/**
 * Recupera el resultado JSON normalizado del comparador.
 * GET /api/comparador/resultado/{sid}/json
 */
export async function fetchTextCompareResult(
  sid,
  { offset = 0, limit = null } = {},
) {
  const params = new URLSearchParams();
  if (offset) {
    params.set("offset", String(offset));
  }
  if (limit !== null && limit !== undefined && limit !== "") {
    params.set("limit", String(limit));
  }
  const query = params.toString();
  const payload = await comparerFetch(
    `/resultado/${encodeURIComponent(sid)}/json${query ? `?${query}` : ""}`,
    { method: "GET" },
  );
  return normalizeTextCompareResultPayload(payload);
}

/**
 * Descarga el informe consolidado del comparador.
 * El backend mantiene compatibilidad con /descargar/{sid}/informe.pdf, pero
 * actualmente devuelve el JSON consolidado del job.
 */
export async function downloadTextCompareReport(sid) {
  const url = `${COMPARATOR_API_BASE}/descargar/${encodeURIComponent(sid)}/informe.pdf`;
  const res = await fetch(url, {
    method: "GET",
    credentials: "include",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Error ${res.status} al descargar informe: ${text}`);
  }

  const blob = await res.blob();

  // Intenta recuperar filename del Content-Disposition
  const cd = res.headers.get("content-disposition") || "";
  let filename = `resultado-${sid}.json`;
  const m = /filename\*?=(?:UTF-8''|")?([^";\n]+)/i.exec(cd);
  if (m && m[1]) {
    try {
      filename = decodeURIComponent(m[1].replace(/"/g, ""));
    } catch {
      filename = m[1].replace(/"/g, "");
    }
  }

  const href = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(href);
}

export async function downloadTextCompareBlockDiffs(sid) {
  const url = `${COMPARATOR_API_BASE}/resultado/${encodeURIComponent(sid)}/block-diffs.json`;
  const res = await fetch(url, {
    method: "GET",
    credentials: "include",
  });

  if (!res.ok) {
    throw new Error(await parseApiError(res, url));
  }

  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = `block-diffs-${sid}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(href);
}

export async function exportTextCompareResultJson(sid) {
  const url = `${COMPARATOR_API_BASE}/resultado/${encodeURIComponent(sid)}/export.json`;
  const res = await fetch(url, {
    method: "GET",
    credentials: "include",
  });

  if (!res.ok) {
    throw new Error(await parseApiError(res, url));
  }

  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = `resultado-${sid}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(href);
}

export async function fetchWebsearchCsrfToken() {
  try {
    return await websearchFetch("/csrf-token", { method: "GET" });
  } catch (e) {
    console.error("Error al obtener CSRF token de web_search:", e);
    throw e;
  }
}

/**
 * Bootstrap específico de web_search.
 */
export async function bootstrapWebsearch() {
  try {
    await fetchWebsearchCsrfToken();
    return { ok: true };
  } catch (error) {
    console.error("bootstrapWebsearch: error inicializando CSRF:", error);
    return { ok: false, error: error?.message || String(error) };
  }
}

/**
 * Envía una consulta al microservicio web_search.
 * → POST /api/websearch/search/query
 *
 * Devuelve:
 *  {
 *    reply, response, conversation_id, search_session_id,
 *    sources: [{ title, url, snippet, content }]
 *  }
 */
export async function sendWebSearchMessage({
  prompt,
  searchSessionId = null,
  conversationId = null,
  topK = null,
  maxIters = null,
}) {
  return websearchFetch("/search/query", {
    method: "POST",
    body: {
      prompt,
      search_session_id: searchSessionId,
      conversation_id: conversationId,
      top_k: topK,
      max_iters: maxIters,
    },
  });
}

export async function fetchLegalsearchCsrfToken() {
  try {
    return await legalsearchFetch("/csrf-token", { method: "GET" });
  } catch (e) {
    console.error("Error al obtener CSRF token de legal_search:", e);
    throw e;
  }
}

export async function bootstrapLegalsearch() {
  try {
    await fetchLegalsearchCsrfToken();
    return { ok: true };
  } catch (error) {
    console.error("bootstrapLegalsearch: error inicializando CSRF:", error);
    return { ok: false, error: error?.message || String(error) };
  }
}

export async function uploadLegalSearchFiles({
  files,
  searchSessionId,
  conversationId = null,
}) {
  const formData = new FormData();
  (files || []).forEach((f) => formData.append("files", f));

  const params = new URLSearchParams();
  if (searchSessionId) params.set("search_session_id", searchSessionId);
  if (conversationId !== null && conversationId !== undefined) {
    params.set("conversation_id", String(conversationId));
  }

  const qs = params.toString();
  const path = qs ? `/search/uploadfile?${qs}` : "/search/uploadfile";

  return legalsearchFetch(path, {
    method: "POST",
    body: formData,
  });
}

export async function sendLegalSearchMessage({
  prompt,
  searchSessionId = null,
  conversationId = null,
  attachedFileIds = [],
  topK = null,
  maxIters = null,
}) {
  return legalsearchFetch("/search/query", {
    method: "POST",
    body: {
      prompt,
      search_session_id: searchSessionId,
      conversation_id: conversationId,
      attached_file_ids: attachedFileIds,
      top_k: topK,
      max_iters: maxIters,
    },
  });
}

export async function sendNotetakerMeetingsMessage({
  query,
  prompt = null,
  limit = 5,
  history = [],
  userId = null,
  requestContext = null,
}) {
  const normalizedQuery = String(query || prompt || "").trim();

  // Flujo oficial: Front -> modelo_negocio -> notetaker_hybrid_rag (/query).
  return apiFetch("/integrations/notetaker/meetings/query", {
    method: "POST",
    body: {
      // Compatibilidad con variantes de payload (query/prompt)
      query: normalizedQuery,
      prompt: normalizedQuery,
      limit,
      history,
      user_id: userId,
      request_context: requestContext,
    },
  });
}

export async function fetchNotetakerSsoUrl() {
  const url = `/api/notetaker/sso-url`;

  const opts = {
    method: "POST",
    credentials: "include",
    headers: {},
  };

  // CSRF: el backend valida CSRF como modelo_negocio (csrftoken_app)
  const csrf = getCsrfToken();
  if (csrf) {
    opts.headers["X-CSRFToken"] = csrf;
  }

  const res = await fetch(url, opts);

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Error ${res.status} en ${url}: ${text}`);
  }

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  // no debería ocurrir, pero por seguridad:
  const text = await res.text();
  return { url: text };
}

/**
 * Acción directa: abre Notetaker.
 * @param {string} [displayName] - Nombre completo del usuario (se añade a la URL si el backend no lo incluyó).
 */
export async function openNotetaker(displayName) {
  const { url } = await fetchNotetakerSsoUrl();
  if (!url) throw new Error("No se recibió URL de Notetaker.");

  let finalUrl = url;
  if (displayName) {
    const u = new URL(url);
    if (!u.searchParams.has("display_name")) {
      u.searchParams.set("display_name", displayName);
      finalUrl = u.toString();
    }
  }

  window.location.href = finalUrl;
}