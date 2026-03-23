import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";

import {
  COMPARATOR_NGINX_BASE_PATH,
  resolveComparatorApiBase,
} from "../src/lib/comparatorApiConfig.mjs";
import {
  deriveTextCompareViewState,
  filterTextCompareRows,
  sortTextCompareRows,
  TEXT_COMPARE_FILTER_ALL,
  TEXT_COMPARE_SORT_DEFAULT,
} from "../src/lib/textCompareViewModel.mjs";

const apiSource = fs.readFileSync(
  path.resolve("src/lib/api.js"),
  "utf8",
);

assert.equal(COMPARATOR_NGINX_BASE_PATH, "/api/comparador");
assert.equal(resolveComparatorApiBase(""), "/api/comparador");
assert.equal(
  resolveComparatorApiBase("http://example.com/api/comparador/"),
  "http://example.com/api/comparador",
);
assert.match(apiSource, /resolveComparatorApiBase/);
assert.match(apiSource, /return comparerFetch\("\/comparar"/);
const panelSource = fs.readFileSync(path.resolve("src/pages/TextCompareMainPanel.jsx"), "utf8");
assert.match(panelSource, /sticky top-0 z-20/);
assert.match(panelSource, /Archivo A · fragmento sensible/);
assert.match(panelSource, /Ordenación/);
assert.doesNotMatch(panelSource, /Metadatos del resultado/);
assert.doesNotMatch(panelSource, /Cargar más bloques desde backend/);
assert.match(panelSource, /tabla muestra todos los bloques detectados/i);

const longText = "Cláusula ".repeat(80);
const rows = [
  {
    block_id: 1,
    change_type: "modificado",
    severity: "alta",
    text_a: "Pago en 30 días",
    text_b: "Pago en 45 días",
  },
  {
    block_id: 2,
    change_type: "eliminado",
    severity: "critica",
    text_a: "[[BLOQUE AUSENTE EN B]]",
    text_b: "",
  },
  {
    block_id: 3,
    change_type: "insertado",
    severity: "media",
    text_a: "",
    text_b: "[[BLOQUE AUSENTE EN A]]",
  },
  {
    block_id: 4,
    change_type: "modificado",
    severity: "baja",
    text_a: longText,
    text_b: longText,
  },
];

assert.equal(
  filterTextCompareRows(rows, {
    severity: "alta",
    changeType: TEXT_COMPARE_FILTER_ALL,
    matchesQuery: (row, query) =>
      `${row.text_a} ${row.text_b}`.toLowerCase().includes(query),
  }).length,
  1,
);
assert.equal(
  filterTextCompareRows(rows, {
    changeType: "eliminado",
    matchesQuery: () => true,
  })[0].block_id,
  2,
);
assert.equal(
  filterTextCompareRows(rows, {
    changeType: "insertado",
    matchesQuery: () => true,
  })[0].block_id,
  3,
);
assert.equal(
  filterTextCompareRows(rows, {
    query: "cláusula",
    matchesQuery: (row, query) =>
      `${row.text_a} ${row.text_b}`.toLowerCase().includes(query),
  }).length,
  1,
);

assert.equal(
  deriveTextCompareViewState({
    result: { rows: [] },
    rows: [],
    filteredRows: [],
  }).kind,
  "empty",
);
assert.equal(
  deriveTextCompareViewState({
    result: { rows },
    rows,
    filteredRows: [],
  }).kind,
  "filtered-empty",
);
assert.equal(
  deriveTextCompareViewState({
    error: "backend exploded",
  }).kind,
  "error",
);

console.log("Frontend comparator validations passed.");


assert.equal(sortTextCompareRows(rows, TEXT_COMPARE_SORT_DEFAULT)[0].block_id, 1);
assert.equal(sortTextCompareRows(rows, "severity_desc")[0].block_id, 2);
assert.equal(sortTextCompareRows(rows, "change_type")[0].change_type, "eliminado");