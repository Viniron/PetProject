/**
 * Распознавание речи браузером - ADR-046: голос расшифровывает не модель.
 *
 * Типов Web Speech API в стандартной библиотеке TypeScript нет, поэтому
 * здесь объявлено ровно то подмножество, которым экран пользуется, - без
 * `any`. Chrome и Safari отдают конструктор под префиксом `webkit`.
 */

type Альтернатива = { readonly transcript: string };
type Результат = { readonly isFinal: boolean; readonly length: number; readonly 0: Альтернатива };
export type СобытиеРечи = {
  readonly results: { readonly length: number; readonly [номер: number]: Результат };
};
export type ОтказРечи = { readonly error: string };

export type Распознаватель = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((событие: СобытиеРечи) => void) | null;
  onerror: ((событие: ОтказРечи) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
};

type Конструктор = new () => Распознаватель;

export function найтиРаспознаватель(): Конструктор | null {
  if (typeof window === "undefined") return null;
  const окно = window as unknown as {
    SpeechRecognition?: Конструктор;
    webkitSpeechRecognition?: Конструктор;
  };
  return окно.SpeechRecognition ?? окно.webkitSpeechRecognition ?? null;
}

/** Весь распознанный текст: окончательные куски и текущий промежуточный. */
export function собратьТекст(событие: СобытиеРечи): string {
  const куски: string[] = [];
  for (let номер = 0; номер < событие.results.length; номер += 1) {
    const кусок = событие.results[номер]?.[0]?.transcript;
    if (кусок) куски.push(кусок.trim());
  }
  return куски.join(" ");
}

/** Отказ браузера словами формы (макет 3, раздел 5). */
export function причинаОтказа(код: string): string {
  if (код === "not-allowed" || код === "service-not-allowed") {
    return "Нет доступа к микрофону — разрешите его в настройках браузера.";
  }
  if (код === "no-speech") return "Ничего не расслышал — попробуйте ещё раз.";
  return "Распознавание прервалось — попробуйте ещё раз или напечатайте в режиме «Текст».";
}
