"use client";

import { useRef, useSyncExternalStore, type KeyboardEvent } from "react";

import {
  ТЕМЫ,
  выбратьТему,
  подписатьсяНаТему,
  текущаяТема,
  type Тема,
} from "@/theme/theme";

const ПОДПИСЬ: Record<Тема, string> = { light: "Светлая", dark: "Тёмная" };

/**
 * На сервере (статический экспорт) ответ - `null`: тема устройства там
 * неизвестна, и выбранной не показана ни одна карточка до гидратации.
 * Угадать «светлую» значило бы на долю секунды отметить не ту карточку.
 */
function useTheme(): Тема | null {
  return useSyncExternalStore<Тема | null>(подписатьсяНаТему, текущаяТема, () => null);
}

/**
 * Выбор темы - макет `design/mockups/16-settings-theme.html`.
 *
 * Классы (`theme-opts`, `theme-opt`, `mini`) утверждены owner и живут
 * в `design/tokens.css`. Группа - `radiogroup`: Tab входит в неё одним
 * шагом, на выбранную карточку, а стрелки двигают выбор и сразу применяют
 * тему, как нажатие (раздел 3 макета).
 */
export function ThemePicker() {
  const тема = useTheme();
  const кнопки = useRef<Partial<Record<Тема, HTMLButtonElement | null>>>({});

  function стрелка(событие: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(событие.key)) {
      return;
    }
    событие.preventDefault();
    // Карточек две, и любая стрелка ведёт на соседнюю - по кругу это
    // одно и то же место. Третья тема потребует шага по списку.
    const куда: Тема = тема === "dark" ? "light" : "dark";
    выбратьТему(куда);
    кнопки.current[куда]?.focus();
  }

  return (
    <div className="theme-opts" role="radiogroup" aria-label="Тема" onKeyDown={стрелка}>
      {ТЕМЫ.map((вариант) => {
        const выбрана = вариант === тема;
        // До гидратации тема неизвестна - в группу ведёт первая карточка,
        // иначе Tab не попал бы в группу вовсе.
        const вход = тема === null ? вариант === "light" : выбрана;
        return (
          <button
            key={вариант}
            ref={(узел) => {
              кнопки.current[вариант] = узел;
            }}
            type="button"
            role="radio"
            aria-checked={выбрана}
            tabIndex={вход ? 0 : -1}
            className={выбрана ? "theme-opt is-active" : "theme-opt"}
            onClick={() => выбратьТему(вариант)}
          >
            {/* Миниатюра - тот же экран в своей теме, а не цветной кружок:
                темы различаются всей палитрой (макет 16, раздел 1). */}
            <div className="mini" data-theme={вариант} aria-hidden="true">
              <div className="mini-rail">
                <i />
                <i />
                <i />
              </div>
              <div className="mini-body">
                <i />
                <i />
                <i />
                <i />
              </div>
            </div>
            <div className="theme-opt-name">{ПОДПИСЬ[вариант]}</div>
          </button>
        );
      })}
    </div>
  );
}
