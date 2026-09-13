import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Без этого дерево предыдущего теста остаётся в документе, и запрос
// по роли находит две капсулы вместо одной.
afterEach(() => {
  cleanup();
});
