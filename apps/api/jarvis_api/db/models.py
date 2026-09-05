"""Схема БД v1 - календарная часть (Э2).

Границу объёма задаёт ADR-019 с поправкой ADR-020: первый релиз - календарь
без курсов. Поэтому здесь девять таблиц, обслуживающих расписание, календарь
и захват событий, и ни одной курсовой из списка `SPEC.md` §9: их поля
выводятся из манифеста курса, которого ещё нет, а спроектированные вслепую
они всё равно переделываются.

Две таблицы вместо одной там, где речь о расписании, - осознанно.
`itmo_lessons` хранит то, что портал отдал в последний успешный забор,
`calendar_events` - то, что записано в Google. Reconcile сравнивает первое
со вторым; в одной таблице «пара исчезла из расписания» и «запись в Google
не удалась» были бы одним и тем же состоянием строки.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jarvis_api.db.base import Base, CreatedAt, Timestamp

# Длина строковых полей ограничена не из экономии, а чтобы мусор из внешнего
# источника не растекался по базе незамеченным: портал, отдавший килобайт
# в поле «аудитория», должен уронить джоб, а не тихо записаться.
SHORT = 64
MEDIUM = 256
LONG = 2048


class Setting(Base):
    """Настройки owner. Ровно одна строка.

    `user_id` в схеме нет по решению `SPEC.md` §9 - пользователь один.
    Единственная точка расширения, если решение изменится, это таблица.
    """

    __tablename__ = "settings"
    __table_args__ = (
        # Строка одна не по договорённости, а физически: без этого ограничения
        # вторая строка настроек появляется молча, и какая из них применяется -
        # зависит от порядка выборки.
        CheckConstraint("id = 1", name="singleton"),
    )

    # autoincrement=False: единственному целочисленному первичному ключу
    # SQLAlchemy по умолчанию заводит последовательность, а строке-одиночке
    # она не нужна и означала бы, что вторую строку кто-то планировал.
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)
    # Зона нужна, чтобы вычислять границы дня: в БД всё в UTC (инвариант 7),
    # а «сегодня» у пользователя - местное. Значение по умолчанию - зона
    # ИТМО; меняется строкой в базе, а не пересборкой образа.
    timezone: Mapped[str] = mapped_column(String(SHORT), server_default=text("'Europe/Moscow'"))
    updated_at: Mapped[Timestamp] = mapped_column(server_default=func.now(), onupdate=func.now())


class IntegrationToken(Base):
    """Секреты внешних систем в шифрованном виде.

    Ключ шифрования (`ISU_CRED_KEY`) живёт только в env и в базу не попадает
    никогда: дампы уезжают в B2 нешифрованными (ADR-020), и замок вместе
    с ключом в одном файле - это отсутствие замка.

    Google здесь не появляется: доступ к календарю идёт через service account,
    ключ которого лежит в env (ADR-019), а не через токены пользователя.
    """

    __tablename__ = "integration_tokens"
    __table_args__ = (
        CheckConstraint(
            "kind in ('password', 'refresh_token', 'access_token')",
            name="kind_known",
        ),
    )

    provider: Mapped[str] = mapped_column(String(SHORT), primary_key=True)
    # Вид секрета отдельной колонкой, а не отдельной колонкой на каждый вид:
    # у ИСУ их три (пароль, refresh, access), и появляются они в разное время.
    kind: Mapped[str] = mapped_column(String(SHORT), primary_key=True)
    value_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[Timestamp | None] = mapped_column(nullable=True)
    updated_at: Mapped[Timestamp] = mapped_column(server_default=func.now(), onupdate=func.now())


class ItmoLesson(Base):
    """Зеркало расписания my.itmo.ru: что портал отдал в последний забор.

    Существует ради инварианта 9: портал недоступен - показываем последнее
    сохранённое расписание с честной пометкой давности (`fetched_at`), а не
    пустой экран и не догадки.

    Строки этого зеркала при успешном заборе периода заменяются целиком:
    пара, исчезнувшая из расписания, должна исчезнуть и здесь, иначе
    reconcile никогда не удалит её из календаря.
    """

    __tablename__ = "itmo_lessons"
    __table_args__ = (Index("ix_itmo_lessons_starts_at", "starts_at"),)

    # Ключ детерминирован от данных источника и не содержит ни id строки, ни
    # имени хоста, ни времени генерации (инвариант хоста 5): восстановление
    # базы из дампа не должно задвоить расписание при первом же reconcile.
    source_key: Mapped[str] = mapped_column(String(MEDIUM), primary_key=True)
    # Дата занятия по местному времени - та, что видна в расписании портала.
    # Хранится отдельно от starts_at, потому что выборка «пары на такой-то
    # день» иначе зависит от зоны, в которой выполняется запрос.
    lesson_date: Mapped[date] = mapped_column(Date, index=True)
    starts_at: Mapped[Timestamp] = mapped_column()
    ends_at: Mapped[Timestamp] = mapped_column()
    subject: Mapped[str] = mapped_column(String(MEDIUM))
    kind: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    teacher: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    room: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    building: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    # Очно или дистанционно: у второго вместо аудитории ссылка.
    mode: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    online_url: Mapped[str | None] = mapped_column(String(LONG), nullable=True)
    # Время успешного ответа портала. Отсюда берётся полоса давности на
    # экране календаря («Расписание от 12 октября, 07:10»).
    fetched_at: Mapped[Timestamp] = mapped_column()


class CalendarEvent(Base):
    """Журнал того, что записано в Google Calendar.

    Одна строка - одно событие в календаре JARVIS. Хранит и желаемое
    состояние (что должно быть в календаре), и фактическое (что там уже
    есть, `google_event_id`), поэтому переживший перезапуск джоб знает,
    надо ли писать, обновлять или ничего не делать.
    """

    __tablename__ = "calendar_events"
    __table_args__ = (
        # Три календаря JARVIS перечислены в CLAUDE.md; событие обязано лежать
        # в одном из них, иначе удаление курса «по префиксу» промахнётся.
        CheckConstraint("calendar in ('itmo', 'study', 'events')", name="calendar_known"),
        CheckConstraint("source in ('itmo', 'capture')", name="source_known"),
        CheckConstraint("sync_state in ('pending', 'synced', 'failed')", name="sync_state_known"),
        Index("ix_calendar_events_starts_at", "starts_at"),
        # По этому индексу джоб находит то, что осталось рассинхронизированным
        # после сбоя записи (§10): без него поиск идёт по всей таблице.
        Index("ix_calendar_events_sync_state", "sync_state"),
    )

    # Инвариант 5 и §4: ключ детерминирован от данных источника. Двойной
    # запуск джоба (штатный сценарий при catch-up, §11.2) обязан попасть
    # в ту же строку, а не создать вторую.
    external_key: Mapped[str] = mapped_column(String(MEDIUM), primary_key=True)
    calendar: Mapped[str] = mapped_column(String(SHORT))
    source: Mapped[str] = mapped_column(String(SHORT))
    title: Mapped[str] = mapped_column(String(MEDIUM))
    starts_at: Mapped[Timestamp] = mapped_column()
    ends_at: Mapped[Timestamp] = mapped_column()
    location: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Хэш полезной нагрузки события. Сравнение хэшей отвечает на вопрос
    # «изменилось ли», не дёргая Google на каждую строку: событий за семестр
    # сотни, а квота на API общая.
    content_hash: Mapped[str] = mapped_column(String(SHORT))
    # Пусто, пока событие не записано. Уникален: два наших ключа не могут
    # указывать на одно и то же событие в Google - это и есть задвоение,
    # только с другой стороны.
    google_event_id: Mapped[str | None] = mapped_column(String(MEDIUM), unique=True, nullable=True)
    sync_state: Mapped[str] = mapped_column(String(SHORT), server_default=text("'pending'"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[Timestamp | None] = mapped_column(nullable=True)
    created_at: Mapped[CreatedAt] = mapped_column()
    updated_at: Mapped[Timestamp] = mapped_column(server_default=func.now(), onupdate=func.now())


class DayFlag(Base):
    """Периоды, исключённые из учёбы и из расчёта отставания (§2.5).

    Вносятся owner вручную; причина `late_classes` проставляется джобом
    автоматически (§4), поэтому у неё своё ограничение уникальности -
    повторный прогон за тот же день не должен множить строки.
    """

    __tablename__ = "day_flags"
    __table_args__ = (
        CheckConstraint("ends_on >= starts_on", name="range_ordered"),
        # Автопометка «занятия кончаются слишком поздно» - это ровно один
        # день. Частичный уникальный индекс делает джоб идемпотентным на
        # уровне базы, а не на уровне аккуратности кода.
        Index(
            "uq_day_flags_late_classes_day",
            "starts_on",
            unique=True,
            postgresql_where=text("reason = 'late_classes'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    starts_on: Mapped[date] = mapped_column(Date, index=True)
    # Включительно: период «с 1 по 7» содержит седьмое. Полуоткрытый интервал
    # читался бы иначе и путал бы при вводе руками.
    ends_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(SHORT))
    note: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    created_at: Mapped[CreatedAt] = mapped_column()


class AuditLogEntry(Base):
    """Аудит записей наружу и вызовов моделей (инвариант 8, §10).

    Сюда же пишется каждая деградация: без этого вопрос «почему у меня
    расписание трёхдневной давности» остаётся без ответа.

    Колонки стоимости не пустуют «на будущее»: месячный потолок расходов
    (§5.2) считается суммой по ним, и считать его по jsonb пришлось бы
    приведением типа на каждой строке.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("status in ('ok', 'error', 'degraded')", name="status_known"),
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_kind_at", "kind", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[CreatedAt] = mapped_column()
    # Что за событие: calendar_write, llm_call, degradation. Списком значений
    # не ограничено намеренно - виды прибавляются с каждым этапом, а миграция
    # ради нового слова в CHECK ничего не защищает.
    kind: Mapped[str] = mapped_column(String(SHORT))
    # Кто это сделал: имя джоба или эндпоинта.
    actor: Mapped[str] = mapped_column(String(SHORT))
    status: Mapped[str] = mapped_column(String(SHORT))
    # На что подействовали: external_key события, id черновика, имя курса.
    target: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    model: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Доли цента считаются: один захват стоит ~$0.0001 (ADR-019), и округление
    # до копейки обнулило бы всю статистику расходов.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class JobRun(Base):
    """Факт «джоб отработал сегодня» - основа catch-up (§11.2, §9).

    Расписание APScheduler пересоздаётся при каждом старте процесса, поэтому
    после перезагрузки Pi единственный способ узнать, был ли утренний прогон,
    это заглянуть сюда.
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        # Одна строка на джоб и день. Именно это ограничение и делает
        # догоняющий запуск безопасным: второй прогон за день обновляет
        # строку, а не заводит новую.
        UniqueConstraint("job", "run_date", name="uq_job_runs_job_run_date"),
        CheckConstraint("status in ('running', 'ok', 'failed')", name="status_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(SHORT))
    # Дата по местной зоне owner, а не по UTC: «джоб за сегодня» - это про
    # день пользователя. В UTC ночной прогон попал бы во вчера.
    run_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[Timestamp] = mapped_column(server_default=func.now())
    finished_at: Mapped[Timestamp | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(SHORT), server_default=text("'running'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaptureDraft(Base):
    """Черновик события из захвата: живёт от загрузки до подтверждения (§9).

    Без подтверждения в календарь не пишется ничего (CLAUDE.md), поэтому
    черновик - обязательная промежуточная сущность, а не удобство. Брошенный
    убирается джобом по сроку из конфига, отсюда индекс по `created_at`.
    """

    __tablename__ = "capture_drafts"
    __table_args__ = (
        CheckConstraint("modality in ('text', 'image', 'audio')", name="modality_known"),
        Index("ix_capture_drafts_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    modality: Mapped[str] = mapped_column(String(SHORT))
    # Текст, который вставили или надиктовали. Для фото пусто.
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Структура, извлечённая моделью: название, дата, время, место,
    # уверенность. Схема ответа принадлежит слою моделей (Э8), поэтому
    # здесь jsonb, а не колонки: разложить их сейчас значило бы угадать.
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Разбор не удался - показываем честно (инвариант 9), а не выдумываем
    # правдоподобные поля.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Подтверждён ли черновик пользователем. Строка после подтверждения
    # удаляется в одной транзакции с созданием события, поэтому флаг живёт
    # ровно между нажатием и записью - и переживает обрыв на этом промежутке.
    confirmed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[CreatedAt] = mapped_column()
    updated_at: Mapped[Timestamp] = mapped_column(server_default=func.now(), onupdate=func.now())

    # Связь объявлена не ради удобства обхода, а ради порядка записи: сам по
    # себе внешний ключ порядок вставки в SQLAlchemy не задаёт, и сырьё
    # уходит в базу раньше черновика, на который ссылается.
    # passive_deletes: удаляет база каскадом, ORM не должен ходить за строкой
    # с bytea только чтобы её удалить.
    blob: Mapped["CaptureBlob | None"] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CaptureBlob(Base):
    """Сырьё захвата: фото или аудио.

    Отдельной таблицей, чтобы выборка списка черновиков не читала bytea (§9).
    Каскад по внешнему ключу - не оптимизация, а требование: сырьё обязано
    исчезнуть вместе с черновиком, в той же транзакции.
    """

    __tablename__ = "capture_blobs"

    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("capture_drafts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    draft: Mapped[CaptureDraft] = relationship(back_populates="blob")
    mime_type: Mapped[str] = mapped_column(String(SHORT))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Файл в базе, а не на диске: инвариант хоста 1 - всё, что записано мимо
    # Postgres, не попадёт в pg_dump и не восстановится.
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[CreatedAt] = mapped_column()
