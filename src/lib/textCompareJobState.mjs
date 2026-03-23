const SUCCESS_TERMINAL_STATUSES = new Set([
  "done",
  "done_with_warnings",
  "completed",
  "success",
]);

const ERROR_TERMINAL_STATUSES = new Set([
  "error",
  "failed",
  "failure",
  "cancelled",
  "canceled",
]);

function normalizeJobStatus(status) {
  return String(status || "")
    .trim()
    .toLowerCase();
}

export function isTextCompareSuccessStatus(status) {
  return SUCCESS_TERMINAL_STATUSES.has(normalizeJobStatus(status));
}

export function isTextCompareErrorStatus(status) {
  return ERROR_TERMINAL_STATUSES.has(normalizeJobStatus(status));
}

export function isTextCompareTerminalStatus(status) {
  return (
    isTextCompareSuccessStatus(status) || isTextCompareErrorStatus(status)
  );
}

