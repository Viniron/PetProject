"use client";

import { Capture } from "@/capture/Capture";
import { usePhone } from "@/shell/phone";

import { CalendarScreen } from "./CalendarScreen";

/**
 * Вкладка `Calendar` - SPEC §8.4, ADR-039.
 *
 * На ПК это сетка дня и недели, захват открывается кнопкой в капсуле.
 * На телефоне сетки нет вовсе: вкладка и есть захват (макет 3, раздел 7),
 * и `/api/calendar` телефон не запрашивает - расписание owner смотрит
 * в Google Calendar, куда JARVIS его и пишет.
 */
export function CalendarTab() {
  const телефон = usePhone();
  // Ширина ещё не прочитана (статическая разметка до гидратации): ни сетки,
  // ни захвата - только заголовок, общий для обоих.
  if (телефон === null) return <h1 className="screen-title">Calendar</h1>;
  if (!телефон) return <CalendarScreen />;
  return (
    <>
      <h1 className="screen-title">Calendar</h1>
      <Capture вид="screen" />
    </>
  );
}
