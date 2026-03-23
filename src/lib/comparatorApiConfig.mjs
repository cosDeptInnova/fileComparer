export const COMPARATOR_NGINX_BASE_PATH = "/api/comparador";

export function normalizeApiBaseUrl(rawValue, fallbackPath) {
  const fallback = String(fallbackPath || "").trim() || "/";
  const raw = String(rawValue || "").trim();
  const candidate = raw || fallback;

  if (/^https?:\/\//i.test(candidate)) {
    return candidate.replace(/\/+$/, "");
  }

  const normalizedPath = `/${candidate.replace(/^\/+/, "").replace(/\/+$/, "")}`;
  return normalizedPath === "//" ? "/" : normalizedPath;
}

export function resolveComparatorApiBase(rawValue) {
  return normalizeApiBaseUrl(rawValue, COMPARATOR_NGINX_BASE_PATH);
}