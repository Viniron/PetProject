"""Разовая настройка: три календаря JARVIS и доступ к ним у owner.

Запускается руками один раз (`make gcal-setup-apply`), но контракт тот же,
что у ежедневных джобов: по умолчанию dry-run, повторный прогон ничего
не создаёт, отказ громкий.

**Создаются сразу три, наполняется пока один.** Решение owner: расшаривание
календаря - действие руками в чужом интерфейсе, и делать его один раз
дешевле, чем трижды по мере появления этапов. `JARVIS · Занятия`
наполнится сессиями курсов, `JARVIS · События` - подтверждёнными
черновиками захвата; до тех пор они пустые, и это рабочее состояние.

**Идемпотентность проверяется у Google, а не по своей базе.** id в
`settings` может остаться от календаря, который owner удалил, - тогда
календарь заводится заново. Обратный случай (id есть, календарь есть)
не делает ни одного вызова записи.
"""

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from jarvis_api.config import Settings, get_settings
from jarvis_api.db.models import AuditLogEntry, Setting
from jarvis_api.db.session import get_sessionmaker
from jarvis_api.integrations.gcal.client import GcalClient, GcalError, build_service
from jarvis_api.jobs.common import FALLBACK_TIMEZONE

logger = logging.getLogger("jarvis.gcal_setup")

JOB_NAME = "gcal_setup"

# Названия из CLAUDE.md. Здесь они данные, а не текст: по ним owner
# отличает наши календари от своих, и переименование на стороне Google
# нас не касается - связь держится на id в settings, а не на имени.
КАЛЕНДАРИ: tuple[tuple[str, str], ...] = (
    ("gcal_itmo_id", "JARVIS · ИТМО"),
    ("gcal_study_id", "JARVIS · Занятия"),
    ("gcal_events_id", "JARVIS · События"),
)


class SetupError(RuntimeError):
    """Настройка календарей не выполнена."""


@dataclass(slots=True)
class SetupReport:
    """Что настройка сделала бы или сделала."""

    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)

    @property
    def есть_изменения(self) -> bool:
        return bool(self.created)


def настройки_owner(session: Session, *, apply: bool) -> Setting:
    """Строка настроек; её может не быть на чистой базе.

    Единственное место, где строка `settings` создаётся кодом. Ограничение
    `id = 1` в схеме делает вторую строку невозможной физически, поэтому
    гонки здесь нет: параллельный прогон упадёт на ограничении, а не
    заведёт вторую конфигурацию.

    В dry-run строка не заводится вовсе, а возвращается несохранённой.
    Разница не косметическая: "dry-run не пишет ничего" должно держаться
    на том, что писать нечего, а не на аккуратном откате в конце -
    откатом легко зацепить чужую незакоммиченную работу.
    """
    строка = session.get(Setting, 1)
    if строка is not None:
        return строка
    строка = Setting(id=1, timezone=FALLBACK_TIMEZONE)
    if apply:
        session.add(строка)
        session.flush()
    return строка


def setup(
    session: Session,
    settings: Settings,
    client: GcalClient,
    *,
    apply: bool,
) -> SetupReport:
    """Создаёт недостающие календари и отдаёт их owner."""
    почта = settings.google_calendar_owner_email.strip()
    if not почта:
        raise SetupError(
            "GOOGLE_CALENDAR_OWNER_EMAIL пуст: некому отдать календари. "
            "Календари создаёт сервисный аккаунт, и без расшаривания "
            "owner их не увидит вовсе"
        )

    строка = настройки_owner(session, apply=apply)
    отчёт = SetupReport()

    for поле, название in КАЛЕНДАРИ:
        текущий: str | None = getattr(строка, поле)
        if текущий and client.calendar_exists(текущий):
            отчёт.existing.append(название)
            continue

        отчёт.created.append(название)
        if not apply:
            continue

        идентификатор = client.create_calendar(название, строка.timezone)
        # Расшаривание сразу за созданием и в той же транзакции с записью
        # id: календарь, созданный и не отданный owner, невидим - его
        # нельзя ни найти, ни удалить из интерфейса.
        client.share_calendar(идентификатор, почта)
        setattr(строка, поле, идентификатор)
        session.add(
            AuditLogEntry(
                kind="calendar_setup",
                actor=JOB_NAME,
                status="ok",
                target=название,
                provider="google",
                detail={"calendar_id": идентификатор, "shared_with": почта},
            )
        )

    return отчёт


def описать(отчёт: SetupReport, apply: bool) -> list[str]:
    """Человекочитаемый отчёт. Отдельной функцией, чтобы его проверял тест."""
    строки = [
        f"создано {len(отчёт.created)}, уже было {len(отчёт.existing)}",
    ]
    for название in отчёт.created:
        строки.append(f"  + {название}")
    for название in отчёт.existing:
        строки.append(f"  = {название}")
    if not apply:
        строки.append("dry-run: в Google и в базу не записано ничего")
    return строки


def run_once(
    session: Session,
    settings: Settings,
    client: GcalClient,
    *,
    apply: bool,
) -> int:
    """Прогон поверх готовой сессии и клиента."""
    try:
        отчёт = setup(session, settings, client, apply=apply)
    except (SetupError, GcalError) as сбой:
        session.rollback()
        logger.error("настройка календарей не выполнена: %s", сбой)
        return 1

    if apply:
        session.commit()

    for строка in описать(отчёт, apply):
        logger.info("%s", строка)
    if apply and отчёт.есть_изменения:
        logger.info(
            "календари отданы %s - примите их в своём Google Calendar",
            settings.google_calendar_owner_email,
        )
    return 0


def run(settings: Settings, apply: bool) -> int:
    """Прогон целиком: свой сервис Google, своя сессия, свой код возврата."""
    with get_sessionmaker()() as session:
        try:
            client = GcalClient(settings, build_service(settings))
        except GcalError as сбой:
            logger.error("настройка календарей не выполнена: %s", сбой)
            return 1
        return run_once(session, settings, client, apply=apply)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. По умолчанию dry-run."""
    parser = argparse.ArgumentParser(description="Создание календарей JARVIS в Google Calendar")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="создать календари и расшарить их (без флага - только показать)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(get_settings(), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
