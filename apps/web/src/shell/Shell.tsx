"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { установленныеКурсы } from "@/courses/installed";
import { собратьВкладки, действиеДляВкладки, type Вкладка } from "@/shell/tabs";

/**
 * Оболочка приложения - SPEC §8.1, макет `design/mockups/01-shell.html`.
 *
 * Классы (`app`, `rail`, `tab`, `rail-action`, `content`) утверждены на
 * этапе 1 дизайна и живут в `design/tokens.css`: значение, прописанное
 * мимо токенов, ломает системность стиля. Здесь - только разметка
 * и поведение.
 */
export function Shell({ children }: { children: ReactNode }) {
  const путь = usePathname();
  const вкладки = собратьВкладки(установленныеКурсы());
  const активная = выбратьАктивную(вкладки, путь);
  const действие = активная ? действиеДляВкладки(активная) : null;

  // Разделитель ставится перед первой вкладкой вне списка курсов: Settings
  // не курс, и потолок в пять её не касается (этап 1 дизайна).
  const первая_вне_списка = вкладки.find((вкладка) => вкладка.внеСписка)?.id;

  return (
    <div className="app">
      <nav className="rail" aria-label="Разделы">
        {вкладки.map((вкладка) => (
          <РядВкладки
            key={вкладка.id}
            вкладка={вкладка}
            активна={вкладка.id === активная?.id}
            разделитель={вкладка.id === первая_вне_списка}
          />
        ))}
        {действие ? (
          <>
            <div className="rail-sep" />
            <button
              type="button"
              className="rail-action"
              disabled
              title={действие.пояснение}
              aria-label={`${действие.подпись}: ${действие.пояснение}`}
            >
              <div className="tab-icon" aria-hidden="true">
                {действие.иконка}
              </div>
              <div className="tab-text">{действие.подпись}</div>
            </button>
          </>
        ) : null}
      </nav>
      <div className="content">{children}</div>
    </div>
  );
}

function РядВкладки({
  вкладка,
  активна,
  разделитель,
}: {
  вкладка: Вкладка;
  активна: boolean;
  разделитель: boolean;
}) {
  return (
    <>
      {разделитель ? <div className="rail-sep" /> : null}
      <Link
        href={вкладка.путь}
        className={активна ? "tab is-active" : "tab"}
        aria-current={активна ? "page" : undefined}
        // Глиф декоративный, и без явного имени он попадает в озвучку
        // как «квадрат Calendar».
        aria-label={вкладка.подпись}
      >
        <div className="tab-icon" aria-hidden="true">
          {вкладка.иконка}
        </div>
        <div className="tab-text">{вкладка.подпись}</div>
        {вкладка.точка ? <span className="tab-dot" aria-label="есть занятие сегодня" /> : null}
      </Link>
    </>
  );
}

/**
 * Какая вкладка открыта.
 *
 * Сравнение по префиксу, а не по равенству: внутри курса будут вложенные
 * пути (занятие, разбор), и вкладка курса обязана оставаться активной.
 * Календарь - путь `/` - совпал бы с чем угодно, поэтому он проверяется
 * точным равенством и служит запасным вариантом для неизвестного пути.
 */
export function выбратьАктивную(
  вкладки: readonly Вкладка[],
  путь: string | null,
): Вкладка | undefined {
  const календарь = вкладки.find((вкладка) => вкладка.путь === "/");
  if (путь === null) return календарь;
  const нормализованный = путь.endsWith("/") ? путь : `${путь}/`;
  const совпавшая = вкладки
    .filter((вкладка) => вкладка.путь !== "/")
    .find((вкладка) => нормализованный.startsWith(вкладка.путь));
  return совпавшая ?? календарь;
}
