import { describe, expect, it, vi } from "vitest";

import {
  ПОТОЛОК_КУРСОВ,
  действиеДляВкладки,
  собратьВкладки,
  type Курс,
} from "@/shell/tabs";

function курс(id: string, hasWorkToday = false): Курс {
  return { id, title: id, icon: "▤", hasWorkToday };
}

describe("список вкладок строится из курсов", () => {
  it("курсов ноль - календарь и настройки, вкладок курсов нет", () => {
    const вкладки = собратьВкладки([]);

    expect(вкладки.map((в) => в.id)).toEqual(["calendar", "settings"]);
  });

  it("два курса дают две вкладки в том же порядке", () => {
    const вкладки = собратьВкладки([курс("english"), курс("calculus")]);

    expect(вкладки.map((в) => в.id)).toEqual(["calendar", "english", "calculus", "settings"]);
  });

  // Инвариант 3: подпись, глиф и путь берутся из курса. Если бы список был
  // прописан в коде, этот тест прошёл бы только для заранее известных имён.
  it("подпись и глиф приходят из курса, а не из кода", () => {
    const [, вкладка] = собратьВкладки([
      { id: "discrete", title: "Discrete Math", icon: "▩", hasWorkToday: false },
    ]);

    expect(вкладка).toMatchObject({
      подпись: "Discrete Math",
      иконка: "▩",
      путь: "/courses/discrete/",
    });
  });

  it("точка ставится курсу, у которого есть занятие сегодня", () => {
    const вкладки = собратьВкладки([курс("english", true), курс("calculus")]);

    expect(вкладки.filter((в) => в.точка).map((в) => в.id)).toEqual(["english"]);
  });

  it("календарь всегда первый, настройки всегда последние и вне списка курсов", () => {
    const вкладки = собратьВкладки([курс("a"), курс("b"), курс("c")]);

    expect(вкладки[0]?.id).toBe("calendar");
    expect(вкладки.at(-1)).toMatchObject({ id: "settings", внеСписка: true });
    expect(вкладки.filter((в) => в.внеСписка)).toHaveLength(1);
  });
});

describe("потолок капсулы", () => {
  it("курс сверх потолка остаётся видимым, но замечен вслух", () => {
    const предупреждение = vi.spyOn(console, "warn").mockImplementation(() => {});
    const курсы = Array.from({ length: ПОТОЛОК_КУРСОВ + 1 }, (_, i) => курс(`c${i}`));

    const вкладки = собратьВкладки(курсы);

    // Потерянная вкладка - это потерянный курс: прятать нельзя (инвариант 9).
    expect(вкладки).toHaveLength(курсы.length + 2);
    expect(предупреждение).toHaveBeenCalledOnce();
    предупреждение.mockRestore();
  });

  it("на потолке молчит", () => {
    const предупреждение = vi.spyOn(console, "warn").mockImplementation(() => {});

    собратьВкладки(Array.from({ length: ПОТОЛОК_КУРСОВ }, (_, i) => курс(`c${i}`)));

    expect(предупреждение).not.toHaveBeenCalled();
    предупреждение.mockRestore();
  });
});

describe("контекстное действие зависит от вкладки", () => {
  const вкладки = собратьВкладки([курс("english")]);

  it("на календаре создаётся событие", () => {
    expect(действиеДляВкладки(вкладки[0]!)?.подпись).toBe("Event");
  });

  it("в курсе начинается занятие", () => {
    expect(действиеДляВкладки(вкладки[1]!)?.подпись).toBe("Lesson");
  });

  it("в настройках создавать нечего", () => {
    expect(действиеДляВкладки(вкладки.at(-1)!)).toBeNull();
  });
});
