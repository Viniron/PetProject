import { useSyncExternalStore } from "react";

/**
 * Телефон или ПК - по той же границе, что у телефонной раскладки капсулы.
 *
 * Число обязано совпадать с `@media (max-width: 640px)` в
 * `src/app/shell-mobile.css`: разойдутся - и на ширине между ними капсула
 * встанет по-телефонному, а вкладка `Calendar` покажет сетку ПК (или
 * наоборот). Проверяется тестом `tests/phone.test.ts`.
 */
export const ШИРИНА_ТЕЛЕФОНА = 640;

const ЗАПРОС = `(max-width: ${ШИРИНА_ТЕЛЕФОНА}px)`;

function подписаться(уведомить: () => void): () => void {
  const запрос = window.matchMedia(ЗАПРОС);
  запрос.addEventListener("change", уведомить);
  return () => запрос.removeEventListener("change", уведомить);
}

/**
 * Решение принимает не только вёрстка, но и код: телефонная вкладка
 * `Calendar` не должна даже запрашивать сетку (ADR-039), а одним CSS
 * запрос не отменить.
 *
 * На сервере (статический экспорт) ответ - `null`, «ещё неизвестно», а не
 * «ПК»: иначе телефон при гидратации успел бы отрисовать сетку и запросить
 * её, а ADR-039 этот запрос запрещает. После гидратации хук перечитает
 * ширину, и React перерисует без рассинхрона разметки.
 */
export function usePhone(): boolean | null {
  return useSyncExternalStore<boolean | null>(
    подписаться,
    () => window.matchMedia(ЗАПРОС).matches,
    () => null,
  );
}
