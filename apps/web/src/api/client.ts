/**
 * Клиент API: тонкая обёртка поверх типов, выведенных из контракта.
 *
 * Запросы руками не пишутся - `src/api/schema.d.ts` генерируется из
 * `packages/contracts/openapi.json` (`make web-client`), и путь, которого
 * в контракте нет, не компилируется. Сверку файла с контрактом держит
 * `tests/contract.test.ts`: правка эндпоинтов без перегенерации роняет
 * прогон, а не всплывает в браузере.
 *
 * Базовый путь относительный и один на все окружения - `/api`. Фронт и API
 * живут на одном источнике (ADR-020, ADR-031 п. 3): на плате их сводит
 * Caddy, в разработке - next dev (next.config.ts). Поэтому здесь нет ни
 * адреса, ни переменной окружения с адресом: их появление означало бы
 * кросс-доменные запросы, а CORS не настроен намеренно.
 */

import type { paths } from "./schema";

export const БАЗА = "/api";

/** Путь контракта, у которого есть GET. */
type ПутиGET = {
  [P in keyof paths]: paths[P] extends { get: { responses: unknown } } ? P : never;
}[keyof paths];

type ОперацияGET<P extends ПутиGET> = paths[P] extends { get: infer O } ? O : never;

/** Тело успешного ответа - то, что объявлено на 200 в контракте. */
type Успех<O> = O extends {
  responses: { 200: { content: { "application/json": infer T } } };
}
  ? T
  : never;

/** Параметры строки запроса - ровно те, что объявлены в контракте. */
type Запрос<O> = O extends { parameters: { query?: infer Q } } ? Q : never;

/** Тело отказа API - схема `ErrorBody` из контракта, а не наша копия. */
export type ТелоОтказа = paths["/api/calendar"]["get"]["responses"][422]["content"]["application/json"];

/**
 * Отказ, разобранный до полей контракта.
 *
 * `retryable` берётся из тела: решение о повторе принимает сервер, а не
 * клиент по коду статуса. Когда тела нет или оно не наше (502 от прокси,
 * страница входа Cloudflare), поля заполняются честно - `код: "unknown"`,
 * `повторить: false`, - но отказ всё равно остаётся отказом с номером
 * статуса: интерфейс обязан деградировать, а не падать (инвариант 9).
 */
export class ОшибкаAPI extends Error {
  readonly статус: number;
  readonly код: string;
  readonly повторить: boolean;
  readonly подробности: readonly string[] | null;

  constructor(параметры: {
    статус: number;
    код: string;
    сообщение: string;
    повторить: boolean;
    подробности?: readonly string[] | null;
  }) {
    super(параметры.сообщение);
    this.name = "ОшибкаAPI";
    this.статус = параметры.статус;
    this.код = параметры.код;
    this.повторить = параметры.повторить;
    this.подробности = параметры.подробности ?? null;
  }

  /**
   * Сессия Cloudflare Access кончилась или её не было.
   *
   * Лечится не повтором запроса, а перезагрузкой страницы: вход проводит
   * Access на навигации, а не на fetch - редирект на свою страницу входа
   * браузер для fetch заблокирует (ADR-033).
   */
  get нуженВход(): boolean {
    return this.статус === 401 || this.статус === 403;
  }
}

/** Сеть не дошла до сервера: Pi выключен, туннель лёг, телефон офлайн. */
export class ОшибкаСети extends Error {
  constructor(readonly причина: unknown) {
    super("сеть недоступна");
    this.name = "ОшибкаСети";
  }
}

function строкаЗапроса(параметры: Record<string, unknown> | undefined): string {
  if (!параметры) return "";
  const части = new URLSearchParams();
  for (const [имя, значение] of Object.entries(параметры)) {
    // undefined - это «параметр не задан», и посылать его как пустую строку
    // нельзя: у view и date в контракте есть умолчания на стороне сервера,
    // а пустая строка прошла бы валидацию как заданное значение.
    if (значение === undefined || значение === null) continue;
    части.set(имя, String(значение));
  }
  const строка = части.toString();
  return строка ? `?${строка}` : "";
}

async function разобратьОтказ(ответ: Response): Promise<ОшибкаAPI> {
  let тело: unknown = null;
  try {
    тело = await ответ.json();
  } catch {
    тело = null;
  }
  if (
    typeof тело === "object" &&
    тело !== null &&
    "code" in тело &&
    "message" in тело &&
    typeof (тело as ТелоОтказа).code === "string" &&
    typeof (тело as ТелоОтказа).message === "string"
  ) {
    const наше = тело as ТелоОтказа;
    return new ОшибкаAPI({
      статус: ответ.status,
      код: наше.code,
      сообщение: наше.message,
      повторить: наше.retryable === true,
      подробности: наше.details ?? null,
    });
  }
  return new ОшибкаAPI({
    статус: ответ.status,
    код: "unknown",
    сообщение: `ответ ${ответ.status} не из контракта`,
    повторить: false,
  });
}

/**
 * GET по пути контракта.
 *
 * `signal` нужен экранам: уход со вкладки обязан отменять запрос, иначе
 * ответ прилетает в размонтированный экран.
 */
export async function получить<P extends ПутиGET>(
  путь: P,
  параметры?: Запрос<ОперацияGET<P>>,
  signal?: AbortSignal,
): Promise<Успех<ОперацияGET<P>>> {
  // Пути в контракте уже начинаются с /api - база в них не дописывается.
  const адрес = `${String(путь)}${строкаЗапроса(параметры as Record<string, unknown> | undefined)}`;

  let ответ: Response;
  try {
    ответ = await fetch(адрес, {
      method: "GET",
      headers: { Accept: "application/json" },
      // Токен Access живёт в cookie, которую ставит сам Cloudflare:
      // без этой строки fetch в некоторых браузерах её не приложит,
      // и каждый запрос будет отказом 401 при живой сессии.
      credentials: "same-origin",
      // Протухшая сессия Access - это 302 на страницу входа Cloudflare,
      // то есть на ЧУЖОЙ источник. С "follow" браузер пошёл бы туда
      // кросс-доменным запросом, получил бы отказ CORS и отдал нам
      // TypeError - неотличимый от «Pi выключен». Экран говорил бы
      // «нет связи с платой» и предлагал повтор, который не лечит.
      // С "manual" редирект приходит сюда ответом типа opaqueredirect,
      // и мы называем причину своим именем. Своих редиректов у API нет
      // (ADR-031), так что терять на этом нечего.
      redirect: "manual",
      signal: signal ?? null,
    });
  } catch (причина) {
    if (причина instanceof DOMException && причина.name === "AbortError") throw причина;
    throw new ОшибкаСети(причина);
  }

  // Ответ на перехваченный редирект пуст по стандарту: ни статуса, ни тела
  // из него не достать (status 0). Единственный, кто у нас редиректит
  // на сторону, - страница входа Cloudflare Access, поэтому причина
  // называется прямо, а не выводится из пустоты.
  if (ответ.type === "opaqueredirect" || ответ.status === 0) {
    throw new ОшибкаAPI({
      статус: 401,
      код: "unauthenticated",
      сообщение: "Cloudflare Access отправил на страницу входа",
      повторить: false,
    });
  }

  if (!ответ.ok) throw await разобратьОтказ(ответ);

  try {
    return (await ответ.json()) as Успех<ОперацияGET<P>>;
  } catch (причина) {
    throw new ОшибкаAPI({
      статус: ответ.status,
      код: "unknown",
      сообщение: "тело ответа не разобралось как JSON",
      повторить: false,
      подробности: [String(причина)],
    });
  }
}
