/**
 * Ручки захвата - §8.4, ADR-042, ADR-054. Типы - из контракта, а не копией.
 */

import { отправить, удалить } from "@/api/client";
import type { components } from "@/api/schema";

export type Черновик = components["schemas"]["CaptureDraftOut"];
export type Разбор = components["schemas"]["CaptureParsedOut"];
export type Подтверждение = components["schemas"]["CaptureConfirmIn"];
export type ЗаписанноеСобытие = components["schemas"]["CapturedEventOut"];

const ЧЕРНОВИКИ = "/api/capture/drafts";

/** Напечатанное или надиктованное - одна ручка, модальность различает. */
export function завестиИзТекста(
  текст: string,
  modality: "text" | "audio",
  signal?: AbortSignal,
): Promise<Черновик> {
  return отправить<Черновик>(ЧЕРНОВИКИ, { modality, text: текст }, signal);
}

/** Фото, скриншот или PDF - формой: base64 в JSON раздул бы файл на треть. */
export function завестиИзФайла(файл: File, signal?: AbortSignal): Promise<Черновик> {
  const форма = new FormData();
  форма.append("file", файл, файл.name);
  return отправить<Черновик>(`${ЧЕРНОВИКИ}/file`, форма, signal);
}

/**
 * Подтверждение идемпотентно (ADR-042): повтор после обрыва связи отдаёт
 * то же событие, а не второе. Поэтому «нажмите ещё раз» на экране честно.
 */
export function подтвердить(id: string, тело: Подтверждение): Promise<ЗаписанноеСобытие> {
  return отправить<ЗаписанноеСобытие>(`${ЧЕРНОВИКИ}/${id}/confirm`, тело);
}

export function отменить(id: string): Promise<void> {
  return удалить(`${ЧЕРНОВИКИ}/${id}`);
}
