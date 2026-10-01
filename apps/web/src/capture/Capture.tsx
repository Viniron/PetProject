"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";

import { ОшибкаAPI, ОшибкаСети } from "@/api/client";

import {
  завестиИзТекста,
  завестиИзФайла,
  отменить,
  подтвердить,
  type Черновик,
  type ЗаписанноеСобытие,
} from "./api";
import { ПРИНИМАЕМЫЕ, размер, страниц, страницPdf, ужать, этоPdf } from "./files";
import { найтиРаспознаватель, причинаОтказа, собратьТекст, type Распознаватель } from "./speech";
import { вМомент, местное, минутМежду } from "./zone";

/**
 * Захват события - SPEC §8.4, макет `design/mockups/03-capture.html`.
 *
 * Путь один и обязательный: вход -> модель -> черновик -> **подтверждение** ->
 * запись (`CLAUDE.md`). Разбирает сервер, и разбор приходит уже с пометками
 * сомнения (ADR-054): экран их показывает, но не выводит сам. Модель
 * отказала - форма открывается пустой и рабочей, с причиной словами
 * (инвариант 9).
 *
 * Один компонент на обе раскладки. На ПК это модалка над календарём
 * (разделы 1-6 макета), на телефоне - содержимое вкладки `Calendar`
 * (раздел 7, ADR-039): ни скрима, ни крестика, закрывать нечего.
 */
export type Вид = "modal" | "screen";

type Режим = "text" | "file" | "voice";

const ПОДПИСЬ_РЕЖИМА: Record<Режим, string> = { text: "текст", file: "файл", voice: "голос" };

type Выбранный = {
  readonly файл: File;
  /** Адрес превью снимка; у PDF превью нет - формат назван словом. */
  readonly превью: string | null;
  readonly страниц: number | null;
};

type Поля = {
  название: string;
  дата: string;
  с: string;
  до: string;
  место: string;
  описание: string;
};

type ИмяПоля = keyof Поля;

type Шаг =
  | { readonly вид: "ввод" }
  | { readonly вид: "разбор" }
  | { readonly вид: "черновик"; readonly черновик: Черновик }
  | { readonly вид: "итог"; readonly событие: ЗаписанноеСобытие; readonly поля: Поля };

/** Событие окна, по которому календарь перечитывает неделю. */
export const СОБЫТИЕ_ЗАПИСАНО = "jarvis:capture-confirmed";

export function Capture({ вид, закрыть }: { вид: Вид; закрыть?: () => void }) {
  const [режим, установитьРежим] = useState<Режим>("text");
  const [шаг, установитьШаг] = useState<Шаг>({ вид: "ввод" });
  const [текст, установитьТекст] = useState("");
  const [выбранный, установитьВыбранный] = useState<Выбранный | null>(null);
  const [отказВвода, установитьОтказВвода] = useState<string | null>(null);
  const [перетаскивание, установитьПеретаскивание] = useState(false);

  const [поля, установитьПоля] = useState<Поля>(ПУСТЫЕ);
  const [тронуто, установитьТронуто] = useState<ReadonlySet<ИмяПоля>>(new Set());
  const [подтверждается, установитьПодтверждается] = useState(false);
  const [отказПодтверждения, установитьОтказПодтверждения] = useState<string | null>(null);

  const речь = useSpeech();
  const управление = useRef<AbortController | null>(null);
  const глубинаПеретаскивания = useRef(0);

  // Уход с экрана обрывает разбор: иначе ответ прилетел бы в размонтированную форму.
  useEffect(() => () => управление.current?.abort(), []);

  // Превью снимка - адрес в памяти браузера; его нужно отпустить, иначе
  // каждая сменённая фотография остаётся в памяти до закрытия вкладки.
  useEffect(() => {
    const адрес = выбранный?.превью;
    return () => {
      if (адрес) URL.revokeObjectURL(адрес);
    };
  }, [выбранный]);

  const черновик = шаг.вид === "черновик" ? шаг.черновик : null;

  const сбросить = useCallback(() => {
    управление.current?.abort();
    речь.остановить();
    установитьШаг({ вид: "ввод" });
    установитьРежим("text");
    установитьТекст("");
    установитьВыбранный(null);
    установитьОтказВвода(null);
    установитьПоля(ПУСТЫЕ);
    установитьТронуто(new Set());
    установитьОтказПодтверждения(null);
  }, [речь]);

  const отказаться = useCallback(() => {
    // Отказ уносит черновик вместе с сырьём (§9): фотография, которую решили
    // не сохранять, не должна дожить до ночного дампа. Не дошёл запрос -
    // черновик уберёт джоб по сроку, форме ждать его незачем.
    if (черновик) void отменить(черновик.id).catch(() => undefined);
    if (закрыть) закрыть();
    else сбросить();
  }, [черновик, закрыть, сбросить]);

  // Escape закрывает модалку - так же, как крестик. У телефонного экрана
  // закрывать нечего.
  useEffect(() => {
    if (вид !== "modal") return;
    const поКлавише = (событие: KeyboardEvent) => {
      if (событие.key === "Escape") отказаться();
    };
    window.addEventListener("keydown", поКлавише);
    return () => window.removeEventListener("keydown", поКлавише);
  }, [вид, отказаться]);

  // Файл, отпущенный мимо формы, браузер открыл бы вместо JARVIS - вместе
  // с потерей всего, что было в форме. Пока форма открыта, такое падение
  // гасится.
  useEffect(() => {
    const погасить = (событие: globalThis.DragEvent) => событие.preventDefault();
    window.addEventListener("dragover", погасить);
    window.addEventListener("drop", погасить);
    return () => {
      window.removeEventListener("dragover", погасить);
      window.removeEventListener("drop", погасить);
    };
  }, []);

  const принятьФайл = useCallback(async (файл: File) => {
    установитьОтказВвода(null);
    установитьРежим("file");
    const pdf = этоPdf(файл);
    const готовый = pdf ? файл : await ужать(файл);
    установитьВыбранный({
      файл: готовый,
      превью: pdf ? null : URL.createObjectURL(готовый),
      страниц: pdf ? await страницPdf(готовый) : null,
    });
  }, []);

  const извлечь = useCallback(async () => {
    управление.current?.abort();
    const новое = new AbortController();
    управление.current = новое;
    установитьОтказВвода(null);
    установитьШаг({ вид: "разбор" });
    try {
      const ответ =
        режим === "file" && выбранный
          ? await завестиИзФайла(выбранный.файл, новое.signal)
          : await завестиИзТекста(
              (режим === "voice" ? речь.текст : текст).trim(),
              режим === "voice" ? "audio" : "text",
              новое.signal,
            );
      установитьПоля(поляИз(ответ));
      установитьТронуто(new Set());
      установитьОтказПодтверждения(null);
      установитьШаг({ вид: "черновик", черновик: ответ });
    } catch (причина) {
      if (причина instanceof DOMException && причина.name === "AbortError") return;
      установитьШаг({ вид: "ввод" });
      установитьОтказВвода(описать(причина));
      // Файл, отвергнутый сервером, из превью уходит: зона снова ждёт
      // следующий (макет 3, раздел 6).
      if (режим === "file") установитьВыбранный(null);
    }
  }, [режим, выбранный, речь.текст, текст]);

  const записать = useCallback(async () => {
    if (!черновик) return;
    const начало = вМомент(поля.дата, поля.с, черновик.timezone);
    const конец = вМомент(поля.дата, поля.до, черновик.timezone);
    if (!начало || !конец) return;
    установитьПодтверждается(true);
    установитьОтказПодтверждения(null);
    try {
      const событие = await подтвердить(черновик.id, {
        title: поля.название.trim(),
        starts_at: начало,
        ends_at: конец,
        location: поля.место.trim() || null,
        description: поля.описание.trim() || null,
      });
      установитьШаг({ вид: "итог", событие, поля });
      window.dispatchEvent(new Event(СОБЫТИЕ_ЗАПИСАНО));
    } catch (причина) {
      // Форма не закрывается и не сбрасывается: повтор безопасен, ключ
      // события выводится из захвата (ADR-042).
      установитьОтказПодтверждения(
        причина instanceof ОшибкаСети ? "Нет связи — нажмите ещё раз" : описать(причина),
      );
    } finally {
      установитьПодтверждается(false);
    }
  }, [черновик, поля]);

  // --- перетаскивание ----------------------------------------------------
  // Принимает вся форма, а не только зона (макет 3, раздел 6). Счётчик
  // глубины нужен потому, что dragleave приходит на каждом дочернем узле:
  // без него подсказка мигала бы на каждом поле под курсором.
  const можноБросить = шаг.вид === "ввод";
  const наВход = (событие: DragEvent) => {
    if (!можноБросить || !несётФайлИлиСсылку(событие)) return;
    событие.preventDefault();
    глубинаПеретаскивания.current += 1;
    установитьПеретаскивание(true);
  };
  const надФормой = (событие: DragEvent) => {
    if (!можноБросить || !несётФайлИлиСсылку(событие)) return;
    событие.preventDefault();
  };
  const наВыход = () => {
    глубинаПеретаскивания.current = Math.max(0, глубинаПеретаскивания.current - 1);
    if (глубинаПеретаскивания.current === 0) установитьПеретаскивание(false);
  };
  const наСброс = (событие: DragEvent) => {
    if (!можноБросить) return;
    событие.preventDefault();
    глубинаПеретаскивания.current = 0;
    установитьПеретаскивание(false);
    const файл = событие.dataTransfer.files[0];
    if (файл) {
      void принятьФайл(файл);
      return;
    }
    // Картинку со страницы сайта браузер часто отдаёт ссылкой, а не файлом.
    // По чужим адресам сервер не ходит (ADR-054 п. 6) - просим сам файл.
    установитьРежим("file");
    установитьОтказВвода("Перетащите сам файл — по ссылкам JARVIS не ходит.");
  };

  const подзаголовок =
    шаг.вид === "итог"
      ? шаг.событие.sync_state === "synced"
        ? "JARVIS · События"
        : "Записывается в JARVIS · События"
      : `Захват · ${ПОДПИСЬ_РЕЖИМА[черновик ? режимЧерновика(черновик) : режим]}`;
  const заголовок =
    шаг.вид === "итог"
      ? шаг.событие.sync_state === "synced"
        ? "Событие записано"
        : "Событие сохранено"
      : "Новое событие";

  const форма = (
    <div
      className={вид === "modal" ? "capture" : "capture capture--screen"}
      role={вид === "modal" ? "dialog" : undefined}
      aria-modal={вид === "modal" ? true : undefined}
      aria-labelledby="capture-title"
      onDragEnter={наВход}
      onDragOver={надФормой}
      onDragLeave={наВыход}
      onDrop={наСброс}
    >
      <div className="capture-head">
        <div>
          <h3 id="capture-title">{заголовок}</h3>
          <p>{подзаголовок}</p>
        </div>
        {вид === "modal" ? (
          <button type="button" className="capture-close" aria-label="Закрыть" onClick={отказаться}>
            ✕
          </button>
        ) : null}
      </div>

      {шаг.вид === "ввод" || шаг.вид === "разбор" ? (
        <CaptureInput
          вид={вид}
          режим={режим}
          сменитьРежим={(новый) => {
            установитьОтказВвода(null);
            установитьРежим(новый);
          }}
          разбор={шаг.вид === "разбор"}
          перетаскивание={перетаскивание}
          текст={текст}
          сменитьТекст={установитьТекст}
          выбранный={выбранный}
          принятьФайл={(файл) => void принятьФайл(файл)}
          сброситьФайл={() => установитьВыбранный(null)}
          речь={речь}
          отказ={отказВвода}
          извлечь={() => void извлечь()}
        />
      ) : null}

      {черновик ? (
        <ФормаЧерновика
          черновик={черновик}
          выбранный={выбранный}
          поля={поля}
          тронуто={тронуто}
          сменить={(имя, значение) => {
            установитьПоля((прежние) => ({ ...прежние, [имя]: значение }));
            // Поправленное owner - уже не догадка модели: пунктир снимается.
            установитьТронуто((прежние) => new Set(прежние).add(имя));
          }}
          подтверждается={подтверждается}
          отказ={отказПодтверждения}
          отмена={отказаться}
          записать={() => void записать()}
        />
      ) : null}

      {шаг.вид === "итог" ? (
        <Итог
          вид={вид}
          событие={шаг.событие}
          поля={шаг.поля}
          ещё={сбросить}
          готово={закрыть}
        />
      ) : null}
    </div>
  );

  return вид === "modal" ? <div className="scrim">{форма}</div> : форма;
}

// --- ввод -------------------------------------------------------------------

// Имена компонентов и хуков, в которых зовутся хуки, - латиницей: правило
// react-hooks узнаёт их только по заглавной латинской букве и `use[A-Z]`,
// а кириллица для него - обычная функция, и проверка молча выключается.
function CaptureInput(свойства: {
  вид: Вид;
  режим: Режим;
  сменитьРежим: (режим: Режим) => void;
  разбор: boolean;
  перетаскивание: boolean;
  текст: string;
  сменитьТекст: (текст: string) => void;
  выбранный: Выбранный | null;
  принятьФайл: (файл: File) => void;
  сброситьФайл: () => void;
  речь: Речь;
  отказ: string | null;
  извлечь: () => void;
}) {
  const { вид, режим, разбор, перетаскивание, выбранный, речь, отказ } = свойства;
  const выборФайла = useRef<HTMLInputElement>(null);
  const камера = useRef<HTMLInputElement>(null);

  const можноИзвлечь =
    !разбор &&
    (режим === "text"
      ? свойства.текст.trim().length > 0
      : режим === "file"
        ? выбранный !== null
        : !речь.идёт && речь.текст.trim().length > 0);

  const подпись = разбор ? "Извлекаю…" : "Извлечь";

  const поле = (файл: File | undefined) => {
    if (файл) свойства.принятьФайл(файл);
  };

  let содержимое: ReactNode;
  if (перетаскивание) {
    содержимое = (
      <div className="drop-over">
        <b>Отпустите — файл уйдёт на разбор</b>
        <div>Фото, скриншот или PDF до 5 страниц</div>
      </div>
    );
  } else if (режим === "text") {
    содержимое = (
      <div className="field">
        <label className="field-label" htmlFor="capture-text">
          Что вставили или написали
        </label>
        <textarea
          id="capture-text"
          className="field-input"
          value={свойства.текст}
          readOnly={разбор}
          autoFocus={вид === "modal"}
          onChange={(событие) => свойства.сменитьТекст(событие.target.value)}
        />
      </div>
    );
  } else if (режим === "file") {
    содержимое = выбранный ? (
      <div className="capture-file">
        <Миниатюра выбранный={выбранный} />
        <div className="capture-file-meta">
          <b>{выбранный.файл.name}</b>
          <span>
            {выбранный.страниц !== null ? `${страниц(выбранный.страниц)} · ` : ""}
            {размер(выбранный.файл.size)}
          </span>
          {разбор ? null : (
            <button type="button" className="link-btn" onClick={() => выборФайла.current?.click()}>
              {этоPdf(выбранный.файл) ? "Выбрать другой" : "Выбрать другое"}
            </button>
          )}
        </div>
      </div>
    ) : вид === "screen" ? (
      <>
        <div className="btn-pair">
          <button type="button" className="rec-btn" onClick={() => камера.current?.click()}>
            Снять фото
          </button>
          <button type="button" className="rec-btn" onClick={() => выборФайла.current?.click()}>
            Выбрать файл
          </button>
        </div>
        <div className="field-hint" style={{ justifyContent: "center" }}>
          Фото, скриншот или PDF до 5 страниц
        </div>
      </>
    ) : (
      <div
        className="drop"
        role="button"
        tabIndex={0}
        onClick={() => выборФайла.current?.click()}
        onKeyDown={(событие) => {
          if (событие.key === "Enter" || событие.key === " ") выборФайла.current?.click();
        }}
      >
        <div className="drop-icon" aria-hidden="true">
          ▣
        </div>
        <div>
          <b style={{ color: "var(--text)" }}>Перетащите фото или PDF</b> или выберите файл
        </div>
        <div>Расписание, афиша, скриншот переписки, PDF до 5 страниц</div>
      </div>
    );
  } else {
    содержимое = <ГолосовойВвод речь={речь} разбор={разбор} />;
  }

  return (
    <div className="capture-body">
      <div className="seg seg--flat" style={{ alignSelf: "flex-start" }} role="tablist">
        {(["text", "file", "voice"] as const).map((вариант) => (
          <button
            key={вариант}
            type="button"
            role="tab"
            aria-selected={режим === вариант}
            className={режим === вариант ? "is-active" : undefined}
            disabled={разбор}
            onClick={() => свойства.сменитьРежим(вариант)}
          >
            {вариант === "text" ? "Текст" : вариант === "file" ? "Файл" : "Голос"}
          </button>
        ))}
      </div>
      {содержимое}
      {отказ ? <div className="field-hint">{отказ}</div> : null}
      {режим !== "file" || выбранный ? (
        <button
          type="button"
          className="btn"
          style={{ alignSelf: "flex-start" }}
          disabled={!можноИзвлечь}
          onClick={свойства.извлечь}
        >
          {подпись}
        </button>
      ) : null}
      <input
        ref={выборФайла}
        type="file"
        accept={ПРИНИМАЕМЫЕ}
        hidden
        aria-label="Выбрать файл"
        onChange={(событие) => {
          поле(событие.target.files?.[0]);
          событие.target.value = "";
        }}
      />
      <input
        ref={камера}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        aria-label="Снять фото"
        onChange={(событие) => {
          поле(событие.target.files?.[0]);
          событие.target.value = "";
        }}
      />
    </div>
  );
}

function ГолосовойВвод({ речь, разбор }: { речь: Речь; разбор: boolean }) {
  if (!речь.доступна) {
    return (
      <>
        <div className="capture-note">
          <b>Этот браузер не распознаёт речь.</b> Надиктуйте в режиме «Текст» — микрофон
          на клавиатуре телефона делает то же самое.
        </div>
        <button type="button" className="rec-btn" disabled>
          <span className="rec-dot" />
          Начать запись
        </button>
      </>
    );
  }
  return (
    <>
      <button
        type="button"
        className={речь.идёт ? "rec-btn is-live" : "rec-btn"}
        disabled={разбор}
        onClick={речь.идёт ? речь.остановить : речь.начать}
      >
        <span className="rec-dot" />
        {речь.идёт ? `Остановить · ${секундыЗаписи(речь.секунд)}` : "Начать запись"}
      </button>
      {речь.отказ ? <div className="field-hint">{речь.отказ}</div> : null}
      {речь.текст || речь.идёт ? (
        <div className="field">
          <label className="field-label" htmlFor="capture-speech">
            Распознано
          </label>
          <textarea
            id="capture-speech"
            className="field-input"
            value={речь.текст}
            readOnly={речь.идёт || разбор}
            onChange={(событие) => речь.поправить(событие.target.value)}
          />
        </div>
      ) : (
        <div className="field-hint" style={{ justifyContent: "center" }}>
          Скажите: что, когда, во сколько
        </div>
      )}
    </>
  );
}

// --- черновик -----------------------------------------------------------------

function ФормаЧерновика(свойства: {
  черновик: Черновик;
  выбранный: Выбранный | null;
  поля: Поля;
  тронуто: ReadonlySet<ИмяПоля>;
  сменить: (имя: ИмяПоля, значение: string) => void;
  подтверждается: boolean;
  отказ: string | null;
  отмена: () => void;
  записать: () => void;
}) {
  const { черновик, поля, тронуто } = свойства;
  const разбор = черновик.extracted ?? null;

  // Пунктир - «модель ответила, но это не факт» (макет 3). Без ответа
  // модели пунктира нет вовсе: помечать неуверенность того, кто молчал,
  // значило бы соврать о происхождении поля.
  const нетДаты = разбор !== null && !разбор.starts_at;
  const сомнение = разбор?.time_uncertain ?? false;
  const подставлен = разбор?.duration_assumed ?? false;
  const пунктир = (имя: ИмяПоля, условие: boolean) => условие && !тронуто.has(имя);

  const пометки = {
    название: пунктир("название", разбор !== null && !разбор.title),
    дата: пунктир("дата", нетДаты || сомнение),
    с: пунктир("с", нетДаты || сомнение),
    до: пунктир("до", нетДаты || сомнение || подставлен),
  };

  let подсказкаРяда: string | null = null;
  if (сомнение && (пометки.дата || пометки.с || пометки.до)) {
    подсказкаРяда = "Модель не уверена в дне и времени — проверьте, прежде чем записать.";
  } else if (подставлен && пометки.до && разбор?.starts_at && разбор.ends_at) {
    подсказкаРяда = `Длительность не названа — поставлен ${длительность(
      минутМежду(разбор.starts_at, разбор.ends_at),
    )}. Поправьте, если дольше.`;
  }

  const нехватка = чегоНет(поля);
  const подпись = свойства.отказ ?? нехватка;

  return (
    <>
      <div className="capture-body">
        <Исходник черновик={черновик} выбранный={свойства.выбранный} />
        {черновик.error ? (
          <div className="capture-note">
            <b>Разобрать не вышло:</b> {черновик.error}. Заполните поля сами — событие запишется
            так же.
          </div>
        ) : null}
        <Поле
          имя="название"
          метка="Название"
          значение={поля.название}
          пунктир={пометки.название}
          подсказка="Что это"
          сменить={свойства.сменить}
        />
        <div className="field-row field-row--when">
          <Поле
            имя="дата"
            метка="Дата"
            тип="date"
            значение={поля.дата}
            пунктир={пометки.дата}
            сменить={свойства.сменить}
            пояснение={
              нетДаты && пометки.дата
                ? "В тексте нет даты. Модель не угадывает — укажите день сами."
                : null
            }
          />
          <Поле
            имя="с"
            метка="С"
            тип="time"
            значение={поля.с}
            пунктир={пометки.с}
            сменить={свойства.сменить}
          />
          <Поле
            имя="до"
            метка="До"
            тип="time"
            значение={поля.до}
            пунктир={пометки.до}
            сменить={свойства.сменить}
          />
        </div>
        {подсказкаРяда ? <div className="field-hint">{подсказкаРяда}</div> : null}
        <Поле
          имя="место"
          метка="Место"
          необязательно
          значение={поля.место}
          подсказка="Не указано"
          сменить={свойства.сменить}
        />
        {/* Описание в макете не нарисовано, но модель его извлекает, и оно
            уйдёт в календарь. Писать туда то, чего owner не видел, нельзя -
            поэтому поле появляется ровно тогда, когда в нём что-то есть. */}
        {поля.описание || разбор?.description ? (
          <Поле
            имя="описание"
            метка="Описание"
            необязательно
            многострочное
            значение={поля.описание}
            сменить={свойства.сменить}
          />
        ) : null}
      </div>
      <div className="capture-foot">
        <button type="button" className="btn btn--ghost" onClick={свойства.отмена}>
          Отмена
        </button>
        <div className="capture-foot-actions">
          {подпись ? <span className="capture-block-hint">{подпись}</span> : null}
          <button
            type="button"
            className="btn"
            disabled={нехватка !== null || свойства.подтверждается}
            onClick={свойства.записать}
          >
            Подтвердить и записать
          </button>
        </div>
      </div>
    </>
  );
}

function Исходник({ черновик, выбранный }: { черновик: Черновик; выбранный: Выбранный | null }) {
  if (черновик.modality !== "image") {
    return <div className="source-quote">{черновик.source_text}</div>;
  }
  // Файл черновика - тот, что owner выбрал в этой форме. Список черновиков
  // с сервера байтов не несёт, поэтому без выбранного - только слово.
  const pdf = выбранный ? этоPdf(выбранный.файл) : false;
  const имя = выбранный?.файл.name;
  return (
    <div className="source-quote source-quote--photo">
      {выбранный ? <Миниатюра выбранный={выбранный} /> : <div className="capture-thumb" />}
      <span>
        {pdf ? "PDF" : "Фото"}
        {имя ? ` · ${имя}` : ""}
        {pdf && выбранный?.страниц ? ` · ${выбранный.страниц} стр.` : ""}
      </span>
    </div>
  );
}

function Миниатюра({ выбранный }: { выбранный: Выбранный }) {
  if (!выбранный.превью) return <div className="capture-thumb capture-thumb--pdf">PDF</div>;
  return (
    <div className="capture-thumb">
      {/* Не `next/image`: превью - адрес `blob:` в памяти браузера, а статический
          экспорт оптимизатора картинок не имеет. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={выбранный.превью} alt="" />
    </div>
  );
}

function Поле(свойства: {
  имя: ИмяПоля;
  метка: string;
  значение: string;
  сменить: (имя: ИмяПоля, значение: string) => void;
  тип?: "text" | "date" | "time";
  пунктир?: boolean;
  подсказка?: string;
  пояснение?: string | null;
  необязательно?: boolean;
  многострочное?: boolean;
}) {
  const id = `capture-${свойства.имя}`;
  const общее = {
    id,
    className: "field-input",
    value: свойства.значение,
    placeholder: свойства.подсказка,
  };
  return (
    <div className={свойства.пунктир ? "field field--warn" : "field"}>
      <label className="field-label" htmlFor={id}>
        {свойства.метка}
        {свойства.необязательно ? (
          <span style={{ textTransform: "none", fontWeight: 400 }}> (необязательно)</span>
        ) : null}
      </label>
      {свойства.многострочное ? (
        <textarea {...общее} onChange={(с) => свойства.сменить(свойства.имя, с.target.value)} />
      ) : (
        <input
          {...общее}
          type={свойства.тип ?? "text"}
          onChange={(с) => свойства.сменить(свойства.имя, с.target.value)}
        />
      )}
      {свойства.пояснение ? <div className="field-hint">{свойства.пояснение}</div> : null}
    </div>
  );
}

// --- итог ---------------------------------------------------------------------

function Итог(свойства: {
  вид: Вид;
  событие: ЗаписанноеСобытие;
  поля: Поля;
  ещё: () => void;
  готово: (() => void) | undefined;
}) {
  const { событие, поля } = свойства;
  // Время итога - то, что owner подтвердил в форме, на его стене. Ответ
  // сервера несёт моменты из базы, и в UTC они показали бы «14:00».
  return (
    <>
      <div className="capture-body">
        <div className="capture-summary">
          <b>{событие.title}</b>
          <span>
            {деньСловами(поля.дата)} · {поля.с}–{поля.до}
          </span>
        </div>
        <p className="capture-status">
          {событие.sync_state === "synced"
            ? "Уже в Google Calendar."
            : "Google пока не ответил. Событие допишется само при ближайшей синхронизации — повторять не нужно."}
        </p>
      </div>
      <div className="capture-foot">
        {свойства.вид === "modal" ? (
          <>
            <button type="button" className="btn btn--ghost" onClick={свойства.ещё}>
              Ещё событие
            </button>
            <div className="capture-foot-actions">
              <button type="button" className="btn" onClick={свойства.готово}>
                Готово
              </button>
            </div>
          </>
        ) : (
          // Телефон: закрывать нечего, это вкладка (макет 3, раздел 7).
          <div className="capture-foot-actions">
            <button type="button" className="btn" onClick={свойства.ещё}>
              Ещё событие
            </button>
          </div>
        )}
      </div>
    </>
  );
}

// --- речь ---------------------------------------------------------------------

type Речь = {
  readonly доступна: boolean;
  readonly идёт: boolean;
  readonly текст: string;
  readonly секунд: number;
  readonly отказ: string | null;
  начать: () => void;
  остановить: () => void;
  поправить: (текст: string) => void;
};

function useSpeech(): Речь {
  const [доступна] = useState(() => найтиРаспознаватель() !== null);
  const [идёт, установитьИдёт] = useState(false);
  const [текст, установитьТекст] = useState("");
  const [секунд, установитьСекунд] = useState(0);
  const [отказ, установитьОтказ] = useState<string | null>(null);
  const распознаватель = useRef<Распознаватель | null>(null);

  useEffect(() => {
    if (!идёт) return;
    const таймер = setInterval(() => установитьСекунд((было) => было + 1), 1000);
    return () => clearInterval(таймер);
  }, [идёт]);

  useEffect(() => () => распознаватель.current?.stop(), []);

  const начать = useCallback(() => {
    const Конструктор = найтиРаспознаватель();
    if (!Конструктор) return;
    const новый = new Конструктор();
    новый.lang = "ru-RU";
    новый.continuous = true;
    новый.interimResults = true;
    новый.onresult = (событие) => установитьТекст(собратьТекст(событие));
    новый.onerror = (событие) => установитьОтказ(причинаОтказа(событие.error));
    новый.onend = () => установитьИдёт(false);
    распознаватель.current = новый;
    установитьТекст("");
    установитьОтказ(null);
    установитьСекунд(0);
    установитьИдёт(true);
    новый.start();
  }, []);

  const остановить = useCallback(() => {
    распознаватель.current?.stop();
    установитьИдёт(false);
  }, []);

  return { доступна, идёт, текст, секунд, отказ, начать, остановить, поправить: установитьТекст };
}

// --- мелочи -------------------------------------------------------------------

const ПУСТЫЕ: Поля = { название: "", дата: "", с: "", до: "", место: "", описание: "" };

function поляИз(черновик: Черновик): Поля {
  const разбор = черновик.extracted;
  const начало = местное(разбор?.starts_at);
  const конец = местное(разбор?.ends_at);
  return {
    название: разбор?.title ?? "",
    дата: начало.дата,
    с: начало.время,
    до: конец.время,
    место: разбор?.location ?? "",
    описание: разбор?.description ?? "",
  };
}

function режимЧерновика(черновик: Черновик): Режим {
  return черновик.modality === "image" ? "file" : черновик.modality === "audio" ? "voice" : "text";
}

/** Чего не хватает, чтобы подтвердить. `null` - хватает всего. */
export function чегоНет(поля: Поля): string | null {
  const нетНазвания = !поля.название.trim();
  const нетДаты = !поля.дата;
  if (нетНазвания && нетДаты) return "Укажите название и дату";
  if (нетДаты) return "Укажите дату, чтобы продолжить";
  if (нетНазвания) return "Укажите название";
  if (!поля.с || !поля.до) return "Укажите время начала и конца";
  if (поля.до <= поля.с) return "Конец раньше начала — поправьте время";
  return null;
}

function несётФайлИлиСсылку(событие: DragEvent): boolean {
  const типы = Array.from(событие.dataTransfer.types);
  return типы.includes("Files") || типы.includes("text/uri-list");
}

function описать(причина: unknown): string {
  if (причина instanceof ОшибкаAPI) {
    if (причина.нуженВход) return "Сессия кончилась — обновите страницу.";
    const текст = причина.message;
    return текст.charAt(0).toUpperCase() + текст.slice(1) + (/[.!?]$/.test(текст) ? "" : ".");
  }
  if (причина instanceof ОшибкаСети) return "Нет связи с JARVIS — попробуйте ещё раз.";
  return "Что-то пошло не так — попробуйте ещё раз.";
}

function длительность(минут: number): string {
  if (минут === 60) return "час";
  if (минут % 60 === 0) return `${минут / 60} ч`;
  return `${минут} мин`;
}

function секундыЗаписи(секунд: number): string {
  return `${Math.floor(секунд / 60)}:${String(секунд % 60).padStart(2, "0")}`;
}

/** «Четверг, 22 октября» - дата формы, которая уже календарная. */
function деньСловами(дата: string): string {
  const части = /^(\d{4})-(\d{2})-(\d{2})$/.exec(дата);
  if (!части) return дата;
  const момент = new Date(Date.UTC(Number(части[1]), Number(части[2]) - 1, Number(части[3])));
  const текст = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "UTC",
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(момент);
  return текст.charAt(0).toUpperCase() + текст.slice(1);
}
