// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import type { AnchorHTMLAttributes } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Shell, выбратьАктивную } from "@/shell/Shell";
import { собратьВкладки } from "@/shell/tabs";

const путь = vi.hoisted(() => ({ текущий: "/" }));

// Оба модуля Next требуют живого роутера; в тесте проверяется разметка
// капсулы, а не навигация, поэтому они подменяются целиком.
vi.mock("next/navigation", () => ({ usePathname: () => путь.текущий }));
vi.mock("next/link", () => ({
  default: (props: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) => <a {...props} />,
}));

afterEach(() => {
  путь.текущий = "/";
  vi.restoreAllMocks();
});

describe("оболочка", () => {
  it("на свежей установке в капсуле только календарь и настройки", () => {
    render(
      <Shell>
        <p>экран</p>
      </Shell>,
    );

    const ссылки = screen.getAllByRole("link").map((узел) => узел.getAttribute("aria-label"));
    expect(ссылки).toEqual(["Calendar", "Settings"]);
  });

  it("активная вкладка помечена для чтения с экрана", () => {
    путь.текущий = "/settings/";

    render(
      <Shell>
        <p>экран</p>
      </Shell>,
    );

    expect(screen.getByRole("link", { current: "page" })).toBe(
      screen.getByRole("link", { name: "Settings" }),
    );
  });

  // Кнопка обещала бы работу, которой нет: захват событий - Э8.
  it("кнопка действия видна, но неактивна", () => {
    render(
      <Shell>
        <p>экран</p>
      </Shell>,
    );

    const кнопка = screen.getByRole("button", { name: /Event/ }) as HTMLButtonElement;
    expect(кнопка.disabled).toBe(true);
  });

  it("содержимое экрана отрисовано внутри оболочки", () => {
    render(
      <Shell>
        <p>экран</p>
      </Shell>,
    );

    // getByText бросает, если узла нет: отдельный матчер присутствия
    // потребовал бы @testing-library/jest-dom, а лишних зависимостей нет.
    expect(screen.getByText("экран").tagName).toBe("P");
  });
});

describe("выбор активной вкладки по адресу", () => {
  const вкладки = собратьВкладки([
    { id: "english", title: "English", icon: "▤", hasWorkToday: false },
  ]);

  it("корень - календарь", () => {
    expect(выбратьАктивную(вкладки, "/")?.id).toBe("calendar");
  });

  // Внутри курса будут вложенные экраны - занятие и разбор; вкладка курса
  // обязана оставаться активной, иначе капсула на занятии показывает
  // календарь.
  it("вложенный путь курса оставляет активной вкладку курса", () => {
    expect(выбратьАктивную(вкладки, "/courses/english/lesson/12")?.id).toBe("english");
  });

  it("путь без завершающего слэша считается тем же", () => {
    expect(выбратьАктивную(вкладки, "/settings")?.id).toBe("settings");
  });

  it("неизвестный путь не гасит капсулу целиком", () => {
    expect(выбратьАктивную(вкладки, "/nowhere/")?.id).toBe("calendar");
  });
});
