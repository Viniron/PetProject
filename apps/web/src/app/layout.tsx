import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Shell } from "@/shell/Shell";

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
 * Атрибута `data-theme` здесь нет: без него тема берётся из настройки
 * системы, и это поведение по умолчанию, утверждённое на этапе 0 дизайна.
 * Переключатель тем - экран настроек, его ещё нет.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ru">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
