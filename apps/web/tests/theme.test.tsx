// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemePicker } from "@/settings/ThemePicker";
import { КЛЮЧ_ТЕМЫ, СКРИПТ_ТЕМЫ, текущаяТема } from "@/theme/theme";

// Подписчик на смену темы системы: тест дёргает его сам, как браузер.
let системная_тёмная = false;
let смена_системы: (() => void) | null = null;

beforeEach(() => {
  системная_тёмная = false;
  смена_системы = null;
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return системная_тёмная;
      },
      addEventListener: (_: string, уведомить: () => void) => {
        смена_системы = уведомить;
      },
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Строка из `<head>` выполняется так же, как её выполнит браузер. */
function выполнитьСкрипт() {
  new Function(СКРИПТ_ТЕМЫ)();
}

function карточка(имя: string): HTMLElement {
  return screen.getByRole("radio", { name: имя });
}

function выбрана(имя: string): boolean {
  return карточка(имя).getAttribute("aria-checked") === "true";
}

describe("строка темы в <head>", () => {
  it("без выбора атрибута нет - тема системы", () => {
    выполнитьСкрипт();

    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("сохранённый выбор ставится до отрисовки", () => {
    window.localStorage.setItem(КЛЮЧ_ТЕМЫ, "light");
    системная_тёмная = true;

    выполнитьСкрипт();

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(текущаяТема()).toBe("light");
  });

  // Чужое значение в data-theme дало бы страницу без палитры вовсе.
  it("мусор в хранилище - это «выбора нет»", () => {
    window.localStorage.setItem(КЛЮЧ_ТЕМЫ, "sepia");

    выполнитьСкрипт();

    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  // Приватное окно, запрещённые данные сайта: тема системы, не белый экран.
  it("закрытое хранилище не роняет страницу", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("закрыто", "SecurityError");
    });

    expect(выполнитьСкрипт).not.toThrow();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});

describe("выбор темы", () => {
  it("первое открытие: выбрана карточка темы системы", () => {
    системная_тёмная = true;

    render(<ThemePicker />);

    expect(выбрана("Тёмная")).toBe(true);
    expect(выбрана("Светлая")).toBe(false);
    // Выкатка не перекрашивает экран без спроса и ничего не запоминает.
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(КЛЮЧ_ТЕМЫ)).toBeNull();
  });

  it("без выбора карточка идёт за системой", () => {
    render(<ThemePicker />);
    expect(выбрана("Светлая")).toBe(true);

    act(() => {
      системная_тёмная = true;
      смена_системы?.();
    });

    expect(выбрана("Тёмная")).toBe(true);
  });

  it("нажатие применяет сразу и запоминает на устройстве", () => {
    системная_тёмная = true;
    render(<ThemePicker />);

    fireEvent.click(карточка("Светлая"));

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem(КЛЮЧ_ТЕМЫ)).toBe("light");
    expect(выбрана("Светлая")).toBe(true);
    expect(выбрана("Тёмная")).toBe(false);
  });

  it("сделанный выбор сильнее смены системы", () => {
    render(<ThemePicker />);
    fireEvent.click(карточка("Светлая"));

    act(() => {
      системная_тёмная = true;
      смена_системы?.();
    });

    expect(выбрана("Светлая")).toBe(true);
  });

  it("выбор переживает перезагрузку", () => {
    const { unmount } = render(<ThemePicker />);
    fireEvent.click(карточка("Тёмная"));
    unmount();
    document.documentElement.removeAttribute("data-theme");

    выполнитьСкрипт();
    render(<ThemePicker />);

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(выбрана("Тёмная")).toBe(true);
  });

  // Хранилище записи не приняло: тема действует до перезагрузки, карточка
  // показывает правду, а не системную тему.
  it("закрытое хранилище не мешает выбрать", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("закрыто", "QuotaExceededError");
    });
    render(<ThemePicker />);

    fireEvent.click(карточка("Тёмная"));

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(выбрана("Тёмная")).toBe(true);
  });

  it("Tab входит в группу на выбранную карточку", () => {
    системная_тёмная = true;
    render(<ThemePicker />);

    expect(карточка("Тёмная").tabIndex).toBe(0);
    expect(карточка("Светлая").tabIndex).toBe(-1);
  });

  it("стрелка двигает выбор, применяет тему и переносит фокус", () => {
    render(<ThemePicker />);
    карточка("Светлая").focus();

    fireEvent.keyDown(карточка("Светлая"), { key: "ArrowRight" });

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(выбрана("Тёмная")).toBe(true);
    expect(document.activeElement).toBe(карточка("Тёмная"));

    // Карточек две: стрелка дальше по кругу возвращает к первой.
    fireEvent.keyDown(карточка("Тёмная"), { key: "ArrowRight" });
    expect(выбрана("Светлая")).toBe(true);

    fireEvent.keyDown(карточка("Светлая"), { key: "ArrowLeft" });
    expect(выбрана("Тёмная")).toBe(true);
  });

  it("миниатюра рисуется своей темой и скрыта от чтения с экрана", () => {
    render(<ThemePicker />);

    const миниатюра = карточка("Тёмная").querySelector(".mini");
    expect(миниатюра?.getAttribute("data-theme")).toBe("dark");
    expect(миниатюра?.getAttribute("aria-hidden")).toBe("true");
  });
});
