// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ОшибкаAPI } from "@/api/client";
import { CalendarScreen } from "@/calendar/CalendarScreen";
import type { День, Событие, Сетка } from "@/calendar/layout";

const адрес = vi.hoisted(() => ({ параметры: new URLSearchParams() }));
const переходы = vi.hoisted(() => ({ список: [] as string[] }));

// Роутер и адрес подменяются целиком: проверяется экран, а не навигация Next.
vi.mock("next/navigation", () => ({
  useSearchParams: () => адрес.параметры,
  useRouter: () => ({
    push: (куда: string) => {
      переходы.список.push(куда);
    },
  }),
}));

function событие(параметры: Partial<Событие> & { key: string }): Событие {
  return {
    key: параметры.key,
    source: параметры.source ?? "itmo",
    starts_at: параметры.starts_at ?? "2026-10-14T10:00:00+03:00",
    ends_at: параметры.ends_at ?? "2026-10-14T11:30:00+03:00",
    title: параметры.title ?? "Мат. анализ",
    conflict: параметры.conflict ?? false,
    lesson_kind: параметры.lesson_kind ?? "лекция",
    teacher: null,
    room: параметры.room ?? "ауд. 285",
    building: null,
    mode: null,
    online_url: null,
    location: параметры.location ?? null,
    description: null,
  };
}

function день(параметры: Partial<День> & { date: string }): День {
  return {
    date: параметры.date,
    is_today: параметры.is_today ?? false,
    mirror_covers: параметры.mirror_covers ?? true,
    flags: параметры.flags ?? [],
    events: параметры.events ?? [],
  };
}

function неделя(правки: Partial<Сетка> = {}): Сетка {
  return {
    timezone: "Europe/Moscow",
    period: { view: "week", starts_on: "2026-10-12", ends_on: "2026-10-18", today: "2026-10-14" },
    freshness: {
      state: "fresh",
      portal: "ok",
      fetched_at: "2026-10-14T07:10:00+03:00",
      covered_from: "2026-10-07",
      covered_to: "2026-10-21",
    },
    days: [
      день({ date: "2026-10-12", events: [событие({ key: "a" })] }),
      день({ date: "2026-10-13" }),
      день({ date: "2026-10-14", is_today: true }),
      день({ date: "2026-10-15" }),
      день({ date: "2026-10-16" }),
      день({ date: "2026-10-17" }),
      день({ date: "2026-10-18" }),
    ],
    ...правки,
  };
}

function ответ(тело: unknown, статус = 200): Response {
  return {
    ok: статус >= 200 && статус < 300,
    status: статус,
    json: async () => тело,
  } as Response;
}

function подменитьFetch(результат: Response | Error) {
  const шпион = vi.fn<(адрес: string | URL | Request, настройки?: RequestInit) => Promise<Response>>(
    async () => {
      if (результат instanceof Error) throw результат;
      return результат;
    },
  );
  vi.stubGlobal("fetch", шпион);
  return шпион;
}

afterEach(() => {
  адрес.параметры = new URLSearchParams();
  переходы.список = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("запрос", () => {
  it("масштаб и дата уходят из адреса страницы", async () => {
    адрес.параметры = new URLSearchParams("view=day&date=2026-10-14");
    const шпион = подменитьFetch(
      ответ(
        неделя({
          period: { view: "day", starts_on: "2026-10-14", ends_on: "2026-10-14", today: "2026-10-14" },
          days: [день({ date: "2026-10-14", is_today: true })],
        }),
      ),
    );

    render(<CalendarScreen />);

    await waitFor(() =>
      expect(шпион.mock.calls[0]?.[0]).toBe("/api/calendar?view=day&date=2026-10-14"),
    );
  });

  // Мусор в адресе не превращается в 422: умолчание держит сервер.
  it("негодная дата из адреса не уходит в запрос", async () => {
    адрес.параметры = new URLSearchParams("view=буква&date=вчера");
    const шпион = подменитьFetch(ответ(неделя()));

    render(<CalendarScreen />);

    await waitFor(() => expect(шпион.mock.calls[0]?.[0]).toBe("/api/calendar?view=week"));
  });

  it("уход с периода отменяет запрос", async () => {
    const шпион = подменитьFetch(ответ(неделя()));
    const { unmount } = render(<CalendarScreen />);
    await waitFor(() => expect(шпион).toHaveBeenCalled());

    const сигнал = шпион.mock.calls[0]?.[1]?.signal;
    unmount();

    expect(сигнал?.aborted).toBe(true);
  });
});

describe("сетка", () => {
  it("неделя рисуется по ответу: заголовок, шапка дней и блоки", async () => {
    подменитьFetch(ответ(неделя()));

    render(<CalendarScreen />);

    expect(await screen.findByText("12–18 October")).toBeTruthy();
    expect(screen.getByText("Мат. анализ")).toBeTruthy();
    expect(screen.getByText("10:00–11:30")).toBeTruthy();
    expect(screen.getAllByText("Пн")).toHaveLength(1);
  });

  // Инвариант 9: правдоподобная выдумка хуже пустоты. До ответа блоков нет.
  it("до ответа сетка не выдумывается", () => {
    подменитьFetch(ответ(неделя()));

    const { container } = render(<CalendarScreen />);

    expect(container.querySelectorAll(".cal-ev")).toHaveLength(0);
    expect(screen.getByText(/загружается/i)).toBeTruthy();
  });

  it("день вне окна забора помечен словом, а не пустотой", async () => {
    подменитьFetch(
      ответ(
        неделя({
          days: [
            день({ date: "2026-10-12" }),
            день({ date: "2026-10-13" }),
            день({ date: "2026-10-14", is_today: true }),
            день({ date: "2026-10-15" }),
            день({ date: "2026-10-16", mirror_covers: false }),
            день({ date: "2026-10-17", mirror_covers: false }),
            день({ date: "2026-10-18", mirror_covers: false }),
          ],
        }),
      ),
    );

    render(<CalendarScreen />);

    expect(await screen.findAllByText("нет данных")).toHaveLength(3);
  });

  it("устаревшее расписание получает полосу и пометку на паре", async () => {
    подменитьFetch(
      ответ(
        неделя({
          freshness: {
            state: "stale",
            portal: "failing",
            fetched_at: "2026-10-12T07:10:00+03:00",
            covered_from: "2026-10-05",
            covered_to: "2026-10-19",
          },
        }),
      ),
    );

    const { container } = render(<CalendarScreen />);

    expect(await screen.findByText(/Расписание от 12 октября, 07:10/)).toBeTruthy();
    expect(screen.getByText("от 12.10")).toBeTruthy();
    expect(container.querySelector(".cal-ev.is-stale")).toBeTruthy();
  });

  it("периодом помеченный день приглушается, но пары в нём видны", async () => {
    подменитьFetch(
      ответ(
        неделя({
          days: [
            день({
              date: "2026-10-12",
              events: [событие({ key: "a" })],
              flags: [
                {
                  id: 1,
                  reason: "отъезд",
                  note: null,
                  starts_on: "2026-10-10",
                  ends_on: "2026-10-20",
                  manual: true,
                },
              ],
            }),
          ],
        }),
      ),
    );

    const { container } = render(<CalendarScreen />);

    expect(await screen.findByText("отъезд")).toBeTruthy();
    expect(container.querySelector(".cal-col.is-flagged .cal-ev")).toBeTruthy();
  });

  it("конфликт помечен, блоки делят ширину колонки", async () => {
    подменитьFetch(
      ответ(
        неделя({
          days: [
            день({
              date: "2026-10-14",
              is_today: true,
              events: [
                событие({
                  key: "пара",
                  conflict: true,
                  starts_at: "2026-10-14T15:20:00+03:00",
                  ends_at: "2026-10-14T16:50:00+03:00",
                }),
                событие({
                  key: "врач",
                  source: "event",
                  conflict: true,
                  title: "Стоматолог",
                  starts_at: "2026-10-14T16:30:00+03:00",
                  ends_at: "2026-10-14T17:30:00+03:00",
                }),
              ],
            }),
          ],
        }),
      ),
    );

    const { container } = render(<CalendarScreen />);

    await screen.findByText("Стоматолог");
    expect(container.querySelectorAll(".cal-ev--clash")).toHaveLength(2);
    const доли = [...container.querySelectorAll<HTMLElement>(".cal-ev--split")];
    expect(доли).toHaveLength(2);
    expect(доли.map((блок) => блок.style.getPropertyValue("--col"))).toEqual(["0", "1"]);
  });

  it("на экране дня пустой день объясняется словами", async () => {
    адрес.параметры = new URLSearchParams("view=day&date=2026-10-18");
    подменитьFetch(
      ответ(
        неделя({
          period: { view: "day", starts_on: "2026-10-18", ends_on: "2026-10-18", today: "2026-10-14" },
          days: [день({ date: "2026-10-18" })],
        }),
      ),
    );

    render(<CalendarScreen />);

    expect(await screen.findByText("Ничего не запланировано.")).toBeTruthy();
  });

  // Линия текущего времени - только на дне и только сегодня (этап 2 дизайна).
  it("линия «сейчас» есть на сегодняшнем дне и её нет на неделе", async () => {
    адрес.параметры = new URLSearchParams("view=day");
    подменитьFetch(
      ответ(
        неделя({
          period: { view: "day", starts_on: "2026-10-14", ends_on: "2026-10-14", today: "2026-10-14" },
          days: [день({ date: "2026-10-14", is_today: true, events: [событие({ key: "a" })] })],
        }),
      ),
    );

    const { container, unmount } = render(<CalendarScreen />);
    await screen.findByText("Мат. анализ");
    expect(container.querySelector(".cal-now")).toBeTruthy();
    unmount();

    адрес.параметры = new URLSearchParams();
    подменитьFetch(ответ(неделя({ days: [день({ date: "2026-10-14", is_today: true, events: [событие({ key: "a" })] })] })));
    const неделяЭкран = render(<CalendarScreen />);
    await неделяЭкран.findByText("Мат. анализ");
    expect(неделяЭкран.container.querySelector(".cal-now")).toBeNull();
  });
});

describe("листание", () => {
  it("стрелка двигает дату из ответа, а не свой счётчик", async () => {
    подменитьFetch(ответ(неделя()));

    render(<CalendarScreen />);
    await screen.findByText("Мат. анализ");
    screen.getByRole("button", { name: "Следующий период" }).click();

    expect(переходы.список).toEqual(["/?view=week&date=2026-10-19"]);
  });

  it("«Сегодня» убирает дату: сегодняшний день считает сервер", async () => {
    адрес.параметры = new URLSearchParams("view=week&date=2026-11-02");
    подменитьFetch(ответ(неделя()));

    render(<CalendarScreen />);
    (await screen.findByRole("button", { name: "Сегодня" })).click();

    expect(переходы.список).toEqual(["/?view=week"]);
  });

  it("смена масштаба открывает сегодня, когда оно в периоде", async () => {
    подменитьFetch(ответ(неделя()));

    render(<CalendarScreen />);
    (await screen.findByRole("button", { name: "День" })).click();

    expect(переходы.список).toEqual(["/?view=day&date=2026-10-14"]);
  });
});

describe("деградация", () => {
  it("сеть не дошла - причина и повтор, а не пустой экран", async () => {
    подменитьFetch(new TypeError("failed to fetch"));

    render(<CalendarScreen />);

    expect(await screen.findByText(/Сервер не отвечает/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeTruthy();
  });

  it("повтор шлёт запрос заново", async () => {
    const шпион = подменитьFetch(new TypeError("failed to fetch"));

    render(<CalendarScreen />);
    (await screen.findByRole("button", { name: "Повторить" })).click();

    await waitFor(() => expect(шпион.mock.calls.length).toBeGreaterThan(1));
  });

  // 401 и 403 повтором не лечатся: вход проводит Access на переходе (ADR-033).
  it("кончившаяся сессия зовёт перезагрузить страницу, а не повторить запрос", async () => {
    подменитьFetch(
      ответ({ code: "unauthorized", message: "нет токена", retryable: false }, 401) as Response,
    );

    render(<CalendarScreen />);

    expect(await screen.findByRole("button", { name: "Обновить страницу" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });

  it("решение о повторе берётся из тела ответа, а не из кода статуса", async () => {
    подменитьFetch(
      ответ({ code: "db_unavailable", message: "база недоступна", retryable: true }, 503) as Response,
    );

    render(<CalendarScreen />);

    expect(await screen.findByText("база недоступна")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeTruthy();
  });

  // 502 от прокси и 500 из-под упавшего API тела не несут: решения сервера
  // о повторе нет, а инфраструктурный сбой повтором как раз и лечится.
  it("чужой ответ без тела контракта всё равно даёт повтор", async () => {
    подменитьFetch(ответ("<html>502 Bad Gateway</html>", 502) as Response);

    render(<CalendarScreen />);

    expect(await screen.findByText(/не из контракта/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeTruthy();
  });

  it("неповторяемый отказ кнопки повтора не предлагает", async () => {
    подменитьFetch(
      ответ({ code: "bad_request", message: "дата не разобрана", retryable: false }, 422) as Response,
    );

    render(<CalendarScreen />);

    expect(await screen.findByText("дата не разобрана")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Повторить" })).toBeNull();
  });

  it("отказ поверх загруженного расписания не стирает его с экрана", async () => {
    const шпион = vi.fn<
      (адрес: string | URL | Request, настройки?: RequestInit) => Promise<Response>
    >();
    шпион.mockResolvedValueOnce(ответ(неделя()));
    шпион.mockRejectedValueOnce(new TypeError("failed to fetch"));
    vi.stubGlobal("fetch", шпион);

    render(<CalendarScreen />);
    await screen.findByText("Мат. анализ");
    (await screen.findByRole("button", { name: "Следующий период" })).click();

    // Кнопка навигации не меняет адрес сама - его подменяет тест; повтор
    // запроса вызывается кнопкой «Повторить» из плашки отказа.
    адрес.параметры = new URLSearchParams("view=week&date=2026-10-19");
    expect(screen.getByText("Мат. анализ")).toBeTruthy();
  });

  it("отказ ОшибкаAPI описывается телом контракта", () => {
    const отказ = new ОшибкаAPI({
      статус: 503,
      код: "db_unavailable",
      сообщение: "база недоступна",
      повторить: true,
    });

    expect(отказ.повторить).toBe(true);
    expect(отказ.нуженВход).toBe(false);
  });
});
