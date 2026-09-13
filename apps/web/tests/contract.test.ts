import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import openapiTS, { astToString } from "openapi-typescript";
import { describe, expect, it } from "vitest";

/**
 * Клиент генерируется из контракта, а не пишется руками.
 *
 * Тот же приём, что у `make contract` на стороне API: контракт коммитится,
 * генерация - ручной шаг, а расхождение ловит обычный прогон тестов.
 * Без этого теста правка эндпоинта доезжает до фронта молча и всплывает
 * в браузере у owner.
 */
const КОНТРАКТ = new URL("../../../packages/contracts/openapi.json", import.meta.url);
const СХЕМА = fileURLToPath(new URL("../src/api/schema.d.ts", import.meta.url));

describe("сгенерированные типы соответствуют контракту", () => {
  it("schema.d.ts совпадает с результатом генерации", async () => {
    const ast = await openapiTS(КОНТРАКТ);
    const ожидаемое = astToString(ast);
    const текущее = await readFile(СХЕМА, "utf8");

    // Перегенерировать: make web-client
    expect(нормализовать(текущее)).toBe(нормализовать(ожидаемое));
  });
});

/**
 * Сверяется содержание, а не оформление файла.
 *
 * Снимается перевод строк - на Windows и в Docker он разный - и шапка
 * «auto-generated», которую дописывает консольная команда, но не даёт
 * генерация из кода.
 */
const ШАПКА = /^\/\*\*[\s\S]*?\*\/\s*/;

function нормализовать(текст: string): string {
  return текст.replace(/\r\n/g, "\n").replace(ШАПКА, "").trimEnd();
}
