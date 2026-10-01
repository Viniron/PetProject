import { Suspense } from "react";

import { CalendarTab } from "@/calendar/CalendarTab";

/**
 * Экран календаря - SPEC §8.4.
 *
 * Страница серверная и пустая: вся работа в клиентском `CalendarTab` -
 * на ПК это сетка, на телефоне захват (ADR-039).
 * `Suspense` здесь обязателен, а не для красоты - масштаб и дата живут
 * в адресе (ADR-035), а `useSearchParams` в статическом экспорте требует
 * границы ожидания: без неё сборка падает.
 */
export default function CalendarPage() {
  return (
    <Suspense fallback={<h1 className="screen-title">Calendar</h1>}>
      <CalendarTab />
    </Suspense>
  );
}
