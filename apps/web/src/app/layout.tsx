import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Shell } from "@/shell/Shell";
import { СКРИПТ_ТЕМЫ } from "@/theme/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: "JARVIS",
  description: "Расписание, события и учебная очередь",
};

/**
 * Корневая раскладка: оболочка одна на все экраны (§8.1).
 *
 * Шрифты системные - `design/tokens.css` задаёт их переменной `--font`.
 * `next/font` не подключается намеренно: он качает файлы шрифта в сборку,
 * а сборка образа на плате обязана обходиться без похода наружу.
 *
 * Тему ставит строка в `<head>` до первой отрисовки (§8.1, ADR-057):
 * выбор хранится на устройстве, и сервер сборки его не знает. Поэтому
 * `suppressHydrationWarning` - `data-theme` на `<html>` появляется
 * в браузере, а не в разметке, и React не должен считать это рассинхроном.
 * Подавление действует только на атрибуты самого `<html>`, не на дерево.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: СКРИПТ_ТЕМЫ }} />
      </head>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
