import { afterEach, describe, expect, it, vi } from "vitest";

import { ОшибкаAPI, ОшибкаСети, получить } from "@/api/client";

/** Подменённый fetch. Сеть в тестах не трогается никогда (CLAUDE.md). */
function ответ(тело: unknown, статус = 200, какТекст = false): Response {
  return {
    ok: статус >= 200 && статус < 300,
    status: статус,
    json: async () => {
      if (какТекст) throw new SyntaxError("не JSON");
      return тело;
    },
  } as Response;
}

function подменитьFetch(результат: Response | Error | DOMException) {
  // Сигнатура задана типом, а не параметрами: тело их не использует,
  // но без неё `mock.calls` типизирован пустым кортежем и адрес не проверить.
  const шпион = vi.fn<(адрес: string | URL | Request, настройки?: RequestInit) => Promise<Response>>(
    async () => {
      // DOMException в jsdom не наследует Error, поэтому проверяются оба.
      if (результат instanceof Error || результат instanceof DOMException) throw результат;
      return результат;
    },
  );
  vi.stubGlobal("fetch", шпион);
  return шпион;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("адрес запроса", () => {
  it("параметры уходят строкой запроса, путь берётся из контракта", async () => {
    const шпион = подменитьFetch(ответ({ ok: true }));

    await получить("/api/calendar", { view: "week", date: "2026-09-07" });

    expect(шпион.mock.calls[0]?.[0]).toBe("/api/calendar?view=week&date=2026-09-07");
  });

  // У view и date есть умолчания на стороне сервера; пустая строка прошла бы
  // валидацию как заданное значение и сломала бы их.
  it("незаданный параметр не превращается в пустую строку", async () => {
    const шпион = подменитьFetch(ответ({ ok: true }));

    await получить("/api/calendar", { view: "day", date: undefined });

    expect(шпион.mock.calls[0]?.[0]).toBe("/api/calendar?view=day");
  });

  it("без параметров адрес остаётся чистым", async () => {
    const шпион = подменитьFetch(ответ({ ok: true }));

    await получить("/health");

    expect(шпион.mock.calls[0]?.[0]).toBe("/health");
  });
});

describe("отказ разбирается по телу контракта", () => {
  it("422 отдаёт код, текст и подробности", async () => {
    подменитьFetch(
      ответ(
        {
          code: "bad_request",
          message: "период задом наперёд",
          retryable: false,
          details: ["date_from=2026-09-10", "date_to=2026-09-01"],
        },
        422,
      ),
    );

    const отказ = await получить("/api/calendar").catch((e: unknown) => e);

    expect(отказ).toBeInstanceOf(ОшибкаAPI);
    const ошибка = отказ as ОшибкаAPI;
    expect(ошибка.код).toBe("bad_request");
    expect(ошибка.повторить).toBe(false);
    expect(ошибка.подробности).toHaveLength(2);
  });

  // Решение о повторе принимает сервер: у 503 при недоступной базе
  // retryable=true, и клиент обязан читать его из тела, а не из статуса.
  it("повтор берётся из тела, а не из номера статуса", async () => {
    подменитьFetch(ответ({ code: "db_unavailable", message: "база молчит", retryable: true }, 503));

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка.повторить).toBe(true);
  });

  it("401 просит вход, а не повтор запроса", async () => {
    подменитьFetch(ответ({ code: "unauthorized", message: "нет токена", retryable: false }, 401));

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка.нуженВход).toBe(true);
  });

  // 502 от прокси и страница входа Cloudflare приходят не нашим телом:
  // интерфейс обязан деградировать, а не падать на разборе (инвариант 9).
  it("чужое тело отказа не роняет разбор", async () => {
    подменитьFetch(ответ("<html>502 Bad Gateway</html>", 502, true));

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка).toBeInstanceOf(ОшибкаAPI);
    expect(ошибка.статус).toBe(502);
    expect(ошибка.код).toBe("unknown");
    expect(ошибка.повторить).toBe(false);
  });

  it("успешный ответ с испорченным телом - тоже отказ, а не пустой объект", async () => {
    подменитьFetch(ответ(null, 200, true));

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка).toBeInstanceOf(ОшибкаAPI);
    expect(ошибка.код).toBe("unknown");
  });
});

describe("сеть", () => {
  it("недоступная сеть отличается от отказа сервера", async () => {
    подменитьFetch(new TypeError("Failed to fetch"));

    const ошибка = await получить("/api/calendar").catch((e: unknown) => e);

    expect(ошибка).toBeInstanceOf(ОшибкаСети);
  });

  // Уход со вкладки отменяет запрос: отмена не должна выглядеть как сбой сети,
  // иначе экран покажет «нет связи» там, где связь в порядке.
  it("отмена остаётся отменой", async () => {
    const отмена = new DOMException("прервано", "AbortError");
    подменитьFetch(отмена);

    const ошибка = await получить("/api/calendar").catch((e: unknown) => e);

    expect(ошибка).toBe(отмена);
  });
});

describe("вход Cloudflare Access", () => {
  /**
   * Протухшая сессия приходит редиректом на чужой источник, и перехватывать
   * его обязан клиент: иначе браузер ушёл бы туда сам, упёрся в CORS и отдал
   * TypeError - тот же, что при выключенной плате. Экран тогда обещает
   * «нет связи» и повтор, которым сессия не чинится.
   */
  it("перехваченный редирект - это нужен вход, а не обрыв сети", async () => {
    подменитьFetch({ ok: false, status: 0, type: "opaqueredirect" } as Response);

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка).toBeInstanceOf(ОшибкаAPI);
    expect(ошибка.нуженВход).toBe(true);
    expect(ошибка.повторить).toBe(false);
  });

  it("за редиректом клиент не идёт сам", async () => {
    const шпион = подменитьFetch(ответ({ ok: true }));

    await получить("/api/calendar");

    expect(шпион.mock.calls[0]?.[1]?.redirect).toBe("manual");
  });

  it("401 с телом Access вместо нашего - тоже вход", async () => {
    подменитьFetch(ответ("<html>redirecting</html>", 401, true));

    const ошибка = (await получить("/api/calendar").catch((e: unknown) => e)) as ОшибкаAPI;

    expect(ошибка).toBeInstanceOf(ОшибкаAPI);
    expect(ошибка.нуженВход).toBe(true);
  });
});
