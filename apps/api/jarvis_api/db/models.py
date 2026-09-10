"""Схема БД: календарная часть (Э2) и финансовая книжка (Ф1).

Границу объёма задаёт ADR-019 с поправкой ADR-020: первый релиз - календарь
без курсов. Поэтому здесь девять таблиц, обслуживающих расписание, календарь
и захват событий, шесть таблиц `fin_*` финансовой книжки (§15.2) - и ни одной
курсовой из списка `SPEC.md` §9: их поля выводятся из манифеста курса,
которого ещё нет, а спроектированные вслепую они всё равно переделываются.

Книжка стоит в этом же файле, а не в своём, по той же причине, по которой
у неё префикс `fin_`: `Base.metadata` одна на схему, и Alembic сверяется
именно с ней. Разделять пришлось бы не файлы, а метаданные.

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
    ForeignKeyConstraint,
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
    # Идентификаторы трёх календарей JARVIS в Google (Э4). Здесь, а не в env,
    # потому что это результат работы, а не конфигурация: календари создаёт
    # сервисный аккаунт, и их id обязаны вернуться вместе с базой,
    # восстановленной из дампа, - иначе следующий прогон заведёт вторые три
    # календаря рядом с живыми.
    #
    # Пусто = календарь ещё не создан. Рабочее состояние, а не ошибка:
    # `make gcal-setup` заполняет эти колонки, а джоб записи до тех пор
    # отказывается работать с внятным текстом.
    gcal_itmo_id: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    gcal_study_id: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    gcal_events_id: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
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


# --- Финансовая книжка (Ф1, §15.2) --------------------------------------
#
# Шесть таблиц с префиксом `fin_`. Префикс не косметика: `transactions`
# и `categories` без него читаются как что-то платформенное, а книжка -
# отдельная подсистема, которая ничего не знает ни о курсах, ни о календаре.


class FinImport(Base):
    """Факт загрузки одного файла выписки (§15.3).

    Строка на файл, а не на заход: выписок за раз приезжает несколько,
    по одной с каждого банка (ADR-030), и отказ на одном файле не должен
    отменять учёт по остальным.

    `sha256` уникален - это и есть идемпотентность импорта: тот же файл
    второй раз не создаёт вторую строку, а значит и вторых операций.
    Проверять «а не грузили ли мы уже это» в коде было бы слабее: гонка
    двух прогонов прошла бы такую проверку дважды.
    """

    __tablename__ = "fin_imports"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_fin_imports_sha256"),
        Index("ix_fin_imports_bank_imported_at", "bank", "imported_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Имя банка, а не «источник»: адаптер парсера выбирается по нему.
    bank: Mapped[str] = mapped_column(String(SHORT))
    filename: Mapped[str] = mapped_column(String(MEDIUM))
    # Хэш содержимого, hex sha256 - ровно 64 символа.
    sha256: Mapped[str] = mapped_column(String(64))
    # Период, за который выгружен файл. Из содержимого, а не из имени файла:
    # имя переименовывают. Nullable, потому что не всякий банк его отдаёт.
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    imported_at: Mapped[CreatedAt] = mapped_column()
    rows_added: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_updated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_skipped: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class FinAccount(Base):
    """Свой счёт owner: банк, имя счёта, роль (§15.5).

    Появилась вместе с мультибанком (ADR-030). Пока банк был один, роль
    угадывалась по имени счёта («Накопительный счёт»); с несколькими банками
    угадывание ошибается в деньгах - именно по этой таблице считается статья
    «Отложено», и она же отличает перевод себе от перевода человеку.

    Роль `unknown` - не заглушка, а рабочее состояние: новое имя счёта
    заводится импортом само и ждёт разметки owner. Пока роль неизвестна,
    «Отложено» показывает «счета не размечены», а не ноль (§10).
    """

    __tablename__ = "fin_accounts"
    __table_args__ = (
        # Составной ключ кандидат нужен не для порядка: на него ссылается
        # операция (`fin_transactions.bank` + `account`), и без него счёт
        # операции мог бы оказаться строкой, которой нет в этой таблице.
        UniqueConstraint("bank", "name", name="uq_fin_accounts_bank_name"),
        CheckConstraint(
            "role in ('checking', 'savings', 'unknown')",
            name="role_known",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bank: Mapped[str] = mapped_column(String(SHORT))
    name: Mapped[str] = mapped_column(String(MEDIUM))
    role: Mapped[str] = mapped_column(String(SHORT), server_default=text("'unknown'"))
    created_at: Mapped[CreatedAt] = mapped_column()


class FinCategory(Base):
    """Категория owner - версией на месяц (§15.4).

    Помесячная версия, а не одна строка на категорию: owner решил, что
    созданное моделью обнуляется каждый месяц, а история остаётся неизменной
    (ADR-030). Операция ссылается на версию своего месяца, поэтому разбор
    закрытого месяца не меняется, когда модель в новом месяце передумает.

    **`key` - стабильный ключ, и он важнее `title`.** Сравнение «больше, чем
    в среднем» (§15.9) сопоставляет месяцы по ключу: по имени оно ломалось бы
    от переименования, по `id` - от того, что каждый месяц это новая строка.

    **Два уровня, и это держит база, а не соглашение.** Ограничение собрано
    из трёх частей: `level` объявляет уровень, `parent_level` дублирует
    уровень родителя с проверкой «= 1», а составной внешний ключ связывает
    их с настоящей строкой родителя. Отсюда сразу два инварианта: третьего
    уровня не существует, и подкатегория не может принадлежать категории
    **другого месяца** - `period_month` входит в тот же ключ. Выразить это
    обычным CHECK нельзя: он не видит других строк.
    """

    __tablename__ = "fin_categories"
    __table_args__ = (
        # Цель этого UNIQUE - быть целью внешнего ключа ниже. Как ограничение
        # уникальности он тривиален (`id` и так первичный ключ).
        UniqueConstraint("id", "level", "period_month", name="uq_fin_categories_id_level_month"),
        ForeignKeyConstraint(
            ["parent_id", "parent_level", "period_month"],
            ["fin_categories.id", "fin_categories.level", "fin_categories.period_month"],
            name="fk_fin_categories_parent",
        ),
        CheckConstraint("level in (1, 2)", name="level_known"),
        # Форма строки: основная категория без родителя, подкатегория -
        # с родителем первого уровня. Третий уровень не проходит здесь,
        # а несуществующий родитель - на внешнем ключе.
        CheckConstraint(
            "(level = 1 and parent_id is null and parent_level is null)"
            " or (level = 2 and parent_id is not null and parent_level = 1)",
            name="parent_shape",
        ),
        CheckConstraint("origin in ('owner', 'ai')", name="origin_known"),
        CheckConstraint("status in ('active', 'proposed', 'rejected')", name="status_known"),
        # Уникальность ключа внутри месяца - двумя частичными индексами,
        # а не одним UNIQUE по (period_month, parent_id, key). Причина
        # в NULL: в Postgres два NULL не равны друг другу, и обычный UNIQUE
        # пропустил бы две основные категории с одним ключом в одном месяце.
        Index(
            "uq_fin_categories_month_key_main",
            "period_month",
            "key",
            unique=True,
            postgresql_where=text("parent_id is null"),
        ),
        Index(
            "uq_fin_categories_month_key_sub",
            "period_month",
            "parent_id",
            "key",
            unique=True,
            postgresql_where=text("parent_id is not null"),
        ),
        Index("ix_fin_categories_period_month", "period_month"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(SHORT))
    # Первое число месяца в зоне owner. `date`, а не год с месяцем двумя
    # колонками: границы месяца всё равно считаются датами (§15.5).
    period_month: Mapped[date] = mapped_column(Date)
    level: Mapped[int] = mapped_column(SmallInteger)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parent_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(MEDIUM))
    # Кто её создал. Различие не косметическое: набор основных категорий
    # owner утверждает сам, а созданное моделью живёт один месяц.
    origin: Mapped[str] = mapped_column(String(SHORT))
    # `proposed` - предложение модели на новый месяц, ждущее owner.
    # В разбор такие не участвуют, пока не станут `active`.
    status: Mapped[str] = mapped_column(String(SHORT), server_default=text("'active'"))
    created_at: Mapped[CreatedAt] = mapped_column()


class FinCategoryRule(Base):
    """Детерминированное правило разбора (§15.4, §15.5).

    Ступени 1-4 порядка разбора: MCC, категория банка, мерчант и отправитель
    перевода. Модель зовётся только на то, что здесь не нашлось, - платить
    за сравнение строк незачем (§5.1).

    **Ссылка на `category_key`, а не на `fin_categories.id`.** Категория -
    версия месяца, и правило, указывающее на строку сентября, в октябре
    осиротело бы. Ключ переживает смену месяца, потому что он и есть то,
    что в категории постоянно.

    Правило по отправителю - единственное, что определяет не категорию,
    а `kind`: родные дают доход, прочие входящие переводы по умолчанию
    гасят расход (§15.5).
    """

    __tablename__ = "fin_category_rules"
    __table_args__ = (
        UniqueConstraint("rule_type", "pattern", name="uq_fin_category_rules_type_pattern"),
        CheckConstraint(
            "rule_type in ('mcc', 'bank_category', 'merchant', 'sender')",
            name="rule_type_known",
        ),
        CheckConstraint("kind is null or kind in ('income', 'refund')", name="kind_known"),
        # Правило обязано что-то определять. Пустое правило - не безобидная
        # строка: разбор молча проходит мимо него, и причину «почему операция
        # без категории» потом не найти.
        CheckConstraint(
            "(rule_type = 'sender' and kind is not null)"
            " or (rule_type <> 'sender' and category_key is not null)",
            name="rule_decides_something",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_type: Mapped[str] = mapped_column(String(SHORT))
    # Образец: код MCC, название категории банка, имя мерчанта или имя
    # отправителя из колонки «Описание».
    pattern: Mapped[str] = mapped_column(String(MEDIUM))
    category_key: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    created_at: Mapped[CreatedAt] = mapped_column()


class FinTransaction(Base):
    """Операция книжки (§15.2).

    **Уникальность по `(bank, fingerprint, occurrence_no)`, а не по одному
    `fingerprint`.** Он повторяется законно: две покупки на 200 ₽ в одном
    месте в один день дают один ключ и две настоящие операции, поэтому
    порядковый номер внутри группы входит в ключ (§15.3). Банк входит тоже -
    без него одинаковая покупка из двух разных банков схлопнулась бы
    в одну группу кратности, и импорт добавил бы ноль строк вместо одной.

    **Счёт - составным внешним ключом на `fin_accounts`.** Операция не может
    сослаться на счёт, которого нет в разметке: иначе статья «Отложено»
    считалась бы по счетам, часть которых книжке неизвестна, и расхождение
    было бы тихим.

    **`offsets_transaction_id` - гашение конкретного расхода** (ADR-030):
    «оплатил стол, потом скинули доли». Много поступлений на один расход,
    поэтому ссылка живёт у поступления, а не список у расхода. Окно привязки
    («текущий месяц и предыдущий») здесь не выражено намеренно: оно зависит
    от «сейчас», CHECK такого не умеет, и живёт оно в сервисе - под тестом.
    """

    __tablename__ = "fin_transactions"
    __table_args__ = (
        UniqueConstraint(
            "bank",
            "fingerprint",
            "occurrence_no",
            name="uq_fin_transactions_bank_fingerprint_occurrence",
        ),
        ForeignKeyConstraint(
            ["bank", "account"],
            ["fin_accounts.bank", "fin_accounts.name"],
            name="fk_fin_transactions_account",
        ),
        CheckConstraint("status in ('posted', 'pending', 'reverted')", name="status_known"),
        CheckConstraint(
            "kind in ('expense', 'income', 'transfer', 'refund')",
            name="kind_known",
        ),
        CheckConstraint("occurrence_no >= 1", name="occurrence_no_positive"),
        # Операция не гасит саму себя. Без этой проверки цикл из одной строки
        # был бы законным, и эффективная сумма расхода считалась бы вечно.
        CheckConstraint(
            "offsets_transaction_id is null or offsets_transaction_id <> id",
            name="offsets_not_self",
        ),
        # Гасить может только приход. Расход, «погашающий» другой расход, -
        # не деньги от друзей, а ошибка привязки, и стоит она сразу двух
        # неверных месяцев (§15.5).
        CheckConstraint(
            "offsets_transaction_id is null or amount > 0",
            name="offsets_only_incoming",
        ),
        Index("ix_fin_transactions_occurred_at", "occurred_at"),
        # Гашения расхода читаются при каждом показе строки и при расчёте
        # сальдо месяца - без индекса это перебор всей таблицы на каждую строку.
        Index("ix_fin_transactions_offsets_transaction_id", "offsets_transaction_id"),
        Index("ix_fin_transactions_import_id", "import_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bank: Mapped[str] = mapped_column(String(SHORT))
    # NULL у операции, введённой руками: наличные и переводы с рук на руки
    # ни в одной выписке не появятся, а без них сальдо расходится тихо (§15.1).
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_imports.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[Timestamp] = mapped_column()
    account: Mapped[str] = mapped_column(String(MEDIUM))
    # Пусто у 25 операций из 62 в настоящей выгрузке - поле необязательное,
    # и `fingerprint` обязан переживать его отсутствие.
    card_last4: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    # Деньги только `numeric`. На `float` копейки расходятся в рубли за год,
    # и это инвариант того же уровня, что tz-aware время.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(SHORT))
    # Сумма в валюте счёта. Валютная покупка записывается суммой списания:
    # полноценной мультивалютности в v1 нет (§15.8).
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    merchant: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    # Подсказки банка для разбора, ступени 1-3 порядка §15.4. Хранятся как
    # пришли: по ним объясняется, почему операция попала в свою категорию.
    bank_category: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    own_category: Mapped[str | None] = mapped_column(String(MEDIUM), nullable=True)
    mcc: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Колонка «Учёт в аналитике» из выписки - подсказка для `excluded`,
    # а не сам `excluded`: решение остаётся за owner.
    analytics_hint: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(SHORT), server_default=text("'posted'"))
    kind: Mapped[str] = mapped_column(String(SHORT))
    # Версия категории того месяца, в котором произошла операция (§15.4).
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_categories.id", ondelete="SET NULL"), nullable=True
    )
    # RESTRICT, а не CASCADE: §15.3 запрещает удалять операции вовсе -
    # исчезнувшая из выгрузки помечается `reverted`. Если удаление всё же
    # случится, база не даст оставить гашение без расхода.
    offsets_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("fin_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    # Номер внутри группы одинаковых операций. Единица - не «первая
    # и единственная», а «первая из сколько-нибудь».
    occurrence_no: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    # Исходная строка CSV целиком. Хранится не для истории, а для ответа
    # на вопрос «почему эта операция попала в такую категорию»: без сырья
    # он неотвечаем. Отдельной таблицы не заводим - строка весит сотни байт,
    # в отличие от `capture_blobs` с фотографией.
    source_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    entered_manually: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[CreatedAt] = mapped_column()


class FinSummary(Base):
    """Резюме о расходах после импорта (§15.9).

    **`basis` - снимок чисел, с которыми сравнивали.** Не избыточность:
    привязка гашения задним числом меняет сальдо двух месяцев (§15.5),
    и без снимка прошлый текст «на 30% больше среднего» стал бы противоречить
    текущим цифрам необъяснимо.

    Одно резюме на импорт: повторный расчёт заменяет строку, а не копит
    варианты одного и того же периода.
    """

    __tablename__ = "fin_summaries"
    __table_args__ = (
        UniqueConstraint("import_id", name="uq_fin_summaries_import_id"),
        Index("ix_fin_summaries_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("fin_imports.id", ondelete="CASCADE"))
    # Период, о котором текст: с прошлой загрузки по эту.
    period_start: Mapped[Timestamp] = mapped_column()
    period_end: Mapped[Timestamp] = mapped_column()
    text_ru: Mapped[str] = mapped_column(Text)
    basis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Тот же набор колонок, что в `audit_log`: стоимость нужна и там (инвариант
    # 8, месячный потолок), и здесь - чтобы у резюме было видно, чем оно
    # посчитано, когда назначение моделей сменится.
    provider: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    model: Mapped[str | None] = mapped_column(String(SHORT), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[CreatedAt] = mapped_column()
