// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CalendarTab } from "@/calendar/CalendarTab";
import type { Разбор, Черновик } from "@/capture/api";
import { Capture, СОБЫТИЕ_ЗАПИСАНО } from "@/capture/Capture";

// Сеть подменена целиком (`CLAUDE.md`): каждый тест кладёт ответы сервера
// в очередь, лишний запрос роняет тест, а не уходит наружу.
const запросы: { адрес: string; init: RequestInit | undefined }[] = [];
let очередь: (Response | Error)[] = [];

function ответ(тело: unknown, статус = 200): Response {
  return {
    ok: статус >= 200 && статус < 300,
    status: статус,
    type: "basic",
    json: async () => тело,
  } as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (адрес: RequestInfo | URL, init?: RequestInit) => {
      запросы.push({ адрес: String(адрес), init });
      const следующий = очередь.shift();
      if (!следующий) throw new Error(`лишний запрос: ${String(адрес)}`);
      if (следующий instanceof Error) throw следующий;
      return следующий;
    }),
  );
  // Превью снимка - адрес в памяти браузера; в jsdom его нет.
  URL.createObjectURL = vi.fn(() => "blob:превью");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
  запросы.length = 0;
  очередь = [];
});

function разбор(правки: Partial<Разбор> = {}): Разбор {
  return {
    title: "Встреча с куратором",
    starts_at: "2026-10-22T17:00:00+03:00",
    ends_at: "2026-10-22T18:00:00+03:00",
    location: null,
    description: null,
    confidence: 0.9,
    time_uncertain: false,
    duration_assumed: false,
    ...правки,
  };
}

function черновик(правки: Partial<Черновик> = {}): Черновик {
  return {
    id: "d1",
    modality: "text",
    source_text: "встреча с куратором в четверг после физики",
    extracted: разбор(),
    error: null,
    timezone: "Europe/Moscow",
    created_at: "2026-10-14T09:00:00Z",
    ...правки,
  };
}

// Метки «С» и «До» короткие: нестрогий поиск нашёл бы их внутри других.
// У необязательных полей в метке ещё «(необязательно)» - им нестрогий.
function поле(метка: string, строго = true): HTMLInputElement {
  return screen.getByLabelText(метка, { exact: строго }) as HTMLInputElement;
}

function кнопка(имя: string | RegExp): HTMLButtonElement {
  return screen.getByRole("button", { name: имя }) as HTMLButtonElement;
}

async function дойтиДоЧерновика(ч: Черновик, закрыть = vi.fn()) {
  очередь.push(ответ(ч, 201));
  render(<Capture вид="modal" закрыть={закрыть} />);
  fireEvent.change(поле("Что вставили или написали"), {
    target: { value: "встреча с куратором в четверг после физики" },
  });
  fireEvent.click(кнопка("Извлечь"));
  await screen.findByLabelText("Название");
  return закрыть;
}

describe("текст -> черновик -> подтверждение", () => {
  it("разбор заполняет форму часами зоны owner", async () => {
    await дойтиДоЧерновика(черновик());

    expect(запросы[0]?.адрес).toBe("/api/capture/drafts");
    expect(JSON.parse(String(запросы[0]?.init?.body))).toEqual({
      modality: "text",
      text: "встреча с куратором в четверг после физики",
    });
    expect(поле("Название").value).toBe("Встреча с куратором");
    expect(поле("Дата").value).toBe("2026-10-22");
    expect(поле("С").value).toBe("17:00");
    expect(поле("До").value).toBe("18:00");
    // Исходник виден рядом с полями: сверять форму не с воздухом (макет 3).
    expect(screen.getByText("встреча с куратором в четверг после физики")).toBeTruthy();
  });

  it("подтверждение уходит со смещением, итог честно говорит «записывается»", async () => {
    await дойтиДоЧерновика(черновик());
    const услышал = vi.fn();
    window.addEventListener(СОБЫТИЕ_ЗАПИСАНО, услышал);
    очередь.push(
      ответ({
        key: "capture:d1",
        title: "Встреча с куратором",
        starts_at: "2026-10-22T14:00:00Z",
        ends_at: "2026-10-22T15:00:00Z",
        location: null,
        description: null,
        sync_state: "pending",
        synced_at: null,
      }),
    );

    fireEvent.click(кнопка("Подтвердить и записать"));

    await screen.findByText("Событие сохранено");
    expect(запросы[1]?.адрес).toBe("/api/capture/drafts/d1/confirm");
    expect(JSON.parse(String(запросы[1]?.init?.body))).toMatchObject({
      title: "Встреча с куратором",
      starts_at: "2026-10-22T17:00:00+03:00",
      ends_at: "2026-10-22T18:00:00+03:00",
      location: null,
    });
    // Время итога - со стены owner, а не UTC из ответа базы.
    expect(screen.getByText("Четверг, 22 октября · 17:00–18:00")).toBeTruthy();
    expect(screen.getByText(/Google пока не ответил/)).toBeTruthy();
    expect(услышал).toHaveBeenCalledTimes(1);
    window.removeEventListener(СОБЫТИЕ_ЗАПИСАНО, услышал);
  });

  it("обрыв связи при подтверждении не сбрасывает форму", async () => {
    await дойтиДоЧерновика(черновик());
    очередь.push(new TypeError("Failed to fetch"));

    fireEvent.click(кнопка("Подтвердить и записать"));

    await screen.findByText("Нет связи — нажмите ещё раз");
    expect(поле("Название").value).toBe("Встреча с куратором");
    expect(кнопка("Подтвердить и записать").disabled).toBe(false);
  });
});

describe("сомнение и отказ модели - как в макете 3", () => {
  it("отказ модели: причина сверху, поля пустые и без пунктира", async () => {
    await дойтиДоЧерновика(
      черновик({ extracted: null, error: "модели недоступны из сети платы" }),
    );

    expect(screen.getByText(/модели недоступны из сети платы\. Заполните поля сами/)).toBeTruthy();
    expect(document.querySelectorAll(".field--warn")).toHaveLength(0);
    expect(screen.getByText("Укажите название и дату")).toBeTruthy();
    expect(кнопка("Подтвердить и записать").disabled).toBe(true);

    fireEvent.change(поле("Название"), { target: { value: "Встреча" } });
    fireEvent.change(поле("Дата"), { target: { value: "2026-10-22" } });
    fireEvent.change(поле("С"), { target: { value: "17:00" } });
    fireEvent.change(поле("До"), { target: { value: "18:00" } });

    expect(кнопка("Подтвердить и записать").disabled).toBe(false);
  });

  it("нет даты: пунктир и вопрос, пока owner не впишет день", async () => {
    await дойтиДоЧерновика(черновик({ extracted: разбор({ starts_at: null, ends_at: null }) }));

    expect(поле("Дата").closest(".field")?.classList.contains("field--warn")).toBe(true);
    expect(screen.getByText(/В тексте нет даты/)).toBeTruthy();
    expect(screen.getByText("Укажите дату, чтобы продолжить")).toBeTruthy();
    // Название модель нашла - оно без пометки: сомнение по полю, не по записи.
    expect(поле("Название").closest(".field")?.classList.contains("field--warn")).toBe(false);

    fireEvent.change(поле("Дата"), { target: { value: "2026-10-23" } });

    expect(поле("Дата").closest(".field")?.classList.contains("field--warn")).toBe(false);
  });

  it("подставленная длительность помечена и названа", async () => {
    await дойтиДоЧерновика(черновик({ extracted: разбор({ duration_assumed: true }) }));

    expect(поле("До").closest(".field")?.classList.contains("field--warn")).toBe(true);
    expect(screen.getByText(/Длительность не названа — поставлен час/)).toBeTruthy();
    expect(кнопка("Подтвердить и записать").disabled).toBe(false);
  });

  it("неуверенный день: пунктир на дне и времени, кнопка доступна", async () => {
    await дойтиДоЧерновика(черновик({ extracted: разбор({ time_uncertain: true }) }));

    expect(document.querySelectorAll(".field--warn")).toHaveLength(3);
    expect(screen.getByText(/Модель не уверена в дне и времени/)).toBeTruthy();
    expect(кнопка("Подтвердить и записать").disabled).toBe(false);
  });

  it("описание модели видно owner, раз уйдёт в календарь", async () => {
    await дойтиДоЧерновика(черновик({ extracted: разбор({ description: "взять зачётку" }) }));

    expect(поле("Описание", false).value).toBe("взять зачётку");
  });
});

describe("файл - выбор и перетаскивание", () => {
  const pdf = () => new File(["%PDF-1.4"], "afisha.pdf", { type: "application/pdf" });

  it("файл над формой - подсказка «отпустите»", () => {
    render(<Capture вид="modal" закрыть={vi.fn()} />);

    fireEvent.dragEnter(screen.getByRole("dialog"), {
      dataTransfer: { types: ["Files"], files: [] },
    });

    expect(screen.getByText("Отпустите — файл уйдёт на разбор")).toBeTruthy();
  });

  it("брошенный в режиме «Текст» файл сам переключает на «Файл»", async () => {
    render(<Capture вид="modal" закрыть={vi.fn()} />);

    fireEvent.drop(screen.getByRole("dialog"), {
      dataTransfer: { types: ["Files"], files: [pdf()] },
    });

    await screen.findByText("afisha.pdf");
    expect(screen.getByRole("tab", { name: "Файл" }).getAttribute("aria-selected")).toBe("true");

    очередь.push(ответ(черновик({ modality: "image", source_text: null }), 201));
    fireEvent.click(кнопка("Извлечь"));

    await screen.findByLabelText("Название");
    expect(запросы[0]?.адрес).toBe("/api/capture/drafts/file");
    expect(запросы[0]?.init?.body).toBeInstanceOf(FormData);
    expect(screen.getByText(/PDF · afisha\.pdf/)).toBeTruthy();
  });

  it("брошенная ссылка - просьба перетащить сам файл", () => {
    render(<Capture вид="modal" закрыть={vi.fn()} />);

    fireEvent.drop(screen.getByRole("dialog"), {
      dataTransfer: { types: ["text/uri-list"], files: [] },
    });

    expect(screen.getByText(/Перетащите сам файл — по ссылкам JARVIS не ходит/)).toBeTruthy();
    expect(запросы).toHaveLength(0);
  });

  it("отказ сервера по файлу: причина, и зона снова ждёт файл", async () => {
    render(<Capture вид="modal" закрыть={vi.fn()} />);
    fireEvent.drop(screen.getByRole("dialog"), {
      dataTransfer: { types: ["Files"], files: [pdf()] },
    });
    await screen.findByText("afisha.pdf");
    очередь.push(
      ответ(
        {
          code: "capture_pdf_too_long",
          message: "в файле 12 страниц, а разбираются PDF до 5 - сохраните нужные страницы",
          retryable: false,
        },
        422,
      ),
    );

    fireEvent.click(кнопка("Извлечь"));

    await screen.findByText(/В файле 12 страниц/);
    expect(screen.getByText("Перетащите фото или PDF")).toBeTruthy();
  });
});

describe("модалка и экран", () => {
  it("Escape закрывает модалку и отменяет черновик", async () => {
    const закрыть = await дойтиДоЧерновика(черновик());
    очередь.push(ответ(null, 204));

    fireEvent.keyDown(window, { key: "Escape" });

    expect(закрыть).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(запросы[1]?.адрес).toBe("/api/capture/drafts/d1"));
    expect(запросы[1]?.init?.method).toBe("DELETE");
  });

  it("голос без распознавания в браузере - режим виден и объясняет", () => {
    render(<Capture вид="modal" закрыть={vi.fn()} />);

    fireEvent.click(screen.getByRole("tab", { name: "Голос" }));

    expect(screen.getByText("Этот браузер не распознаёт речь.")).toBeTruthy();
    expect(кнопка(/Начать запись/).disabled).toBe(true);
  });

  it("на телефоне это экран: ни окна, ни крестика", () => {
    render(<Capture вид="screen" />);
    fireEvent.click(screen.getByRole("tab", { name: "Файл" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button", { name: "Закрыть" })).toBeNull();
    expect(кнопка("Снять фото")).toBeTruthy();
  });
});

describe("вкладка Calendar", () => {
  function ширина(телефон: boolean) {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: телефон,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
  }

  // ADR-039: на телефоне сетки нет, и `/api/calendar` не запрашивается вовсе.
  it("на телефоне - захват, и сетку никто не спрашивает", () => {
    ширина(true);

    render(<CalendarTab />);

    expect(screen.getByLabelText("Что вставили или написали")).toBeTruthy();
    expect(запросы).toHaveLength(0);
  });
});
