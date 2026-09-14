import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    // По умолчанию node: браузерное окружение нужно ровно одному файлу
    // с разметкой, а поднимается jsdom дольше, чем идут все тесты вместе.
    // Файл просит его сам строкой `@vitest-environment jsdom`.
    environment: "node",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    setupFiles: ["./tests/setup.ts"],
    // Сеть в тестах замокана всегда (CLAUDE.md): fetch подменяется в каждом
    // тесте клиента, а глобальная подмена здесь оставила бы забытый вызов
    // незамеченным - он бы просто ушёл в сеть.
  },
});
