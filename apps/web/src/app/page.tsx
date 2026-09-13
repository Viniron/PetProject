import { Suspense } from "react";

import { CalendarScreen } from "@/calendar/CalendarScreen";

/**
 * Экран календаря - SPEC §8.4.
 *
 * Страница серверная и пустая: вся работа в клиентском `CalendarScreen`.
 * `Suspense` здесь обязателен, а не для красоты - масштаб и дата живут
 * в адресе (ADR-035), а `useSearchParams` в статическом экспорте требует
 * границы ожидания: без неё сборка падает.
 */
export default function CalendarPage() {
  return (
    <Suspense fallback={<h1 className="screen-title">Calendar</h1>}>
      <CalendarScreen />
    </Suspense>
  );
}
