"""Эндпоинты книжки: обзор, лента, карточка, разбор, импорт (Ф6, §15.7).

Роутер тонкий по тому же правилу, что календарный: разобрать параметры,
позвать домен, перевести отказ в код. Ни сальдо, ни окно гашения, ни план
импорта здесь не считаются - всё это проверяется прямым вызовом без
`TestClient`, потому что ошибка в деньгах owner тихая.

**Действия книжки повторяют команды, а не заменяют их.** `make finance-*`
остаются: плата чинится из терминала, а не из браузера, и единственный
способ импортировать выписку, когда фронт лежит, - командой. Общий у них
не роутер, а домен: второй реализации гашения или разбора в проекте нет.

**Функции объявлены `def`, а не `async def`** (ADR-021): движок синхронный,
и запрос в базу внутри `async def` заблокировал бы цикл событий вместе
с `/health`. Правило ruff `ASYNC` этого не ловит, поэтому оно закреплено
тестом.

**Аудита на чтении нет** (ADR-031 п. 8), но и на записи книжки он не
заводится: у каждого её действия уже есть свой след, и он точнее строки
журнала - импорт оставляет `fin_imports` с sha256 файла, ручная правка
ставит `manual` источником решения, привязка гашения - ссылку с обеих
сторон. Строка `audit_log` добавила бы к ним четвёртую правду о том же.
"""

import datetime as dt
import re
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from jarvis_api.api.deps import Настройки, Сейчас, Сессия
from jarvis_api.api.errors import ОТКАЗЫ, ErrorBody, ОтказAPI
from jarvis_api.api.schemas_finance import (
    AccountOut,
    AccountRoleIn,
    AccountsOut,
    BreakdownOut,
    CategoriesOut,
    CategoryOut,
    EditOut,
    FreshnessOut,
    ImportFileOut,
    ImportOut,
    ImportRowOut,
    MonthOut,
    OffsetIn,
    OffsetOut,
    OverviewOut,
    RecategorizeOut,
    RuleIn,
    RuleOut,
    RulesOut,
    TransactionCardOut,
    TransactionOut,
    TransactionPatchIn,
    TransactionsOut,
)
from jarvis_api.config import Settings
from jarvis_api.db.models import FinAccount, FinTransaction
from jarvis_api.domain import finance_edit, finance_feed, finance_rules
from jarvis_api.domain.finance_balance import (
    ОшибкаРоли,
    назначить_роль,
    размечены,
    роли_счетов,
    собрать,
    счета_списком,
)
from jarvis_api.domain.finance_edit import НЕ_ЗАДАНО, ОшибкаПравки
from jarvis_api.domain.finance_import import ПланИмпорта
from jarvis_api.domain.finance_offsets import ОшибкаПривязки, привязать, разбивка, снять
from jarvis_api.domain.finance_rules import ОшибкаПравила
from jarvis_api.integrations.statements import StatementError
from jarvis_api.jobs.common import owner_timezone
from jarvis_api.jobs.finance_categorize import описать as описать_разбор
from jarvis_api.jobs.finance_categorize import разобрать_книжку
from jarvis_api.jobs.finance_import import импортировать

маршрутизатор = APIRouter(prefix="/api/finance", tags=["finance"])

# Месяц приходит как `2026-08`, а не датой: месяц - это период, и `2026-08-15`
# в параметре означал бы, что клиент вправе спросить середину месяца.
МЕСЯЦ = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ОПИСАНИЕ_МЕСЯЦА = "Месяц в формате YYYY-MM. Не задан - текущий в зоне owner"

НЕ_НАЙДЕНО: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "Операции, счёта или правила с таким id нет"}
}
КОНФЛИКТ: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorBody, "description": "Действие противоречит состоянию книжки"}
}


def _месяц(значение: str | None, момент: dt.datetime, зона: ZoneInfo) -> dt.date:
    """Первое число запрошенного месяца, а без параметра - текущего."""
    if значение is None:
        return момент.astimezone(зона).date().replace(day=1)
    if not МЕСЯЦ.match(значение):
        raise ОтказAPI(
            статус=422,
            code="validation_error",
            message=f"месяц {значение!r} не в формате YYYY-MM",
        )
    год, месяц_ = значение.split("-")
    return dt.date(int(год), int(месяц_), 1)


def _зона(сессия: Session) -> ZoneInfo:
    """Зона owner из `settings`. Испорченную ловит обработчик `OwnerZoneError`."""
    return owner_timezone(сессия)


def _карточка(сессия: Session, tx_id: int) -> TransactionCardOut:
    """Карточка операции или 404. Отдаётся и после каждой правки.

    После правки - не из вежливости: экран обязан показать пересчитанную
    эффективную сумму, а считать её второй раз на клиенте значило бы завести
    вторую арифметику денег (инвариант 1).
    """
    найдена = finance_feed.карточка(сессия, tx_id)
    if найдена is None:
        raise ОтказAPI(статус=404, code="not_found", message=f"операции {tx_id} в книжке нет")
    return TransactionCardOut.model_validate(найдена)


# --- чтение -----------------------------------------------------------------


@маршрутизатор.get("/overview", responses=ОТКАЗЫ, summary="Обзор месяца: сальдо и статьи")
def обзор_месяца(
    сессия: Сессия,
    настройки: Настройки,
    момент: Сейчас,
    month: Annotated[str | None, Query(description=ОПИСАНИЕ_МЕСЯЦА)] = None,
) -> OverviewOut:
    """Сальдо месяца с оговорками, без которых цифру читать нельзя (§15.5).

    Пустой месяц - нули и накопленный итог предыдущих, а не отсутствие:
    книжка, в которой этого месяца ещё нет, отвечает на вопрос «сколько
    потрачено» честным нулём, а не 404.
    """
    зона = _зона(сессия)
    месяц_ = _месяц(month, момент, зона)
    обзор = собрать(сессия, месяц_обзора=месяц_, сейчас=момент, зона=зона)
    свежесть = finance_feed.давность(
        сессия,
        сейчас=момент,
        зона=зона,
        порог_дней=настройки.finance_stale_after_days,
    )
    return OverviewOut(
        data=MonthOut.model_validate(обзор.данные),
        timezone=str(зона),
        covered_through=обзор.покрыт_по,
        incomplete=обзор.неполный,
        in_recalc_window=обзор.в_окне_пересчёта,
        accounts_marked=обзор.данные.отложено is not None,
        freshness=FreshnessOut.model_validate(свежесть),
    )


@маршрутизатор.get("/transactions", responses=ОТКАЗЫ, summary="Лента операций")
def лента_операций(
    сессия: Сессия,
    момент: Сейчас,
    month: Annotated[str | None, Query(description=ОПИСАНИЕ_МЕСЯЦА)] = None,
    all_months: Annotated[
        bool, Query(description="Вся книжка вместо месяца. Осмысленно с needs_review")
    ] = False,
    needs_review: Annotated[bool, Query(description="Только очередь разбора")] = False,
) -> TransactionsOut:
    """Операции месяца или очередь разбора по всей книжке (§15.7).

    `all_months` отдельным флагом, а не пустым `month`: пустой параметр
    означает «текущий месяц», и клиент, забывший его передать, получил бы
    вместо ленты месяца всю книжку целиком.
    """
    зона = _зона(сессия)
    месяц_ = None if all_months else _месяц(month, момент, зона)
    строки = finance_feed.лента(сессия, месяц_=месяц_, только_разбор=needs_review, зона=зона)
    return TransactionsOut(
        month=месяц_,
        needs_review_only=needs_review,
        transactions=[TransactionOut.model_validate(строка) for строка in строки],
    )


@маршрутизатор.get(
    "/transactions/{tx_id}",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО,
    summary="Карточка операции",
)
def карточка_операции(сессия: Сессия, tx_id: int) -> TransactionCardOut:
    """Операция со всем, что к ней привязано: гашения, расход, пара перевода."""
    return _карточка(сессия, tx_id)


@маршрутизатор.get("/accounts", responses=ОТКАЗЫ, summary="Счета книжки и их роли")
def счета(сессия: Сессия) -> AccountsOut:
    """Счета с ролями. `all_marked=false` - «Отложено» пишет слова, а не сумму."""
    строки = счета_списком(сессия)
    return AccountsOut(
        accounts=[AccountOut.model_validate(строка) for строка in строки],
        all_marked=размечены(роли_счетов(сессия)),
    )


@маршрутизатор.get("/categories", responses=ОТКАЗЫ, summary="Категории месяца")
def категории_месяца(
    сессия: Сессия,
    момент: Сейчас,
    month: Annotated[str | None, Query(description=ОПИСАНИЕ_МЕСЯЦА)] = None,
    all_months: Annotated[bool, Query(description="Наборы всех месяцев сразу")] = False,
) -> CategoriesOut:
    """Набор категорий месяца - тот, из которого выбирают на карточке (§15.4)."""
    зона = _зона(сессия)
    месяц_ = None if all_months else _месяц(month, момент, зона)
    строки = finance_rules.категории(сессия, месяц_)
    return CategoriesOut(
        month=месяц_,
        categories=[CategoryOut.model_validate(строка) for строка in строки],
    )


@маршрутизатор.get("/rules", responses=ОТКАЗЫ, summary="Правила разбора")
def правила(сессия: Сессия) -> RulesOut:
    """Все правила: ступени разбора плюс написание owner (`self`)."""
    return RulesOut(
        rules=[RuleOut.model_validate(строка) for строка in finance_rules.перечислить(сессия)]
    )


# --- правка -----------------------------------------------------------------


@маршрутизатор.patch(
    "/transactions/{tx_id}",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО | КОНФЛИКТ,
    summary="Правка операции решением owner",
)
def править_операцию(
    сессия: Сессия,
    tx_id: int,
    тело: TransactionPatchIn,
) -> EditOut:
    """Вид, категория, исключение, заметка и пометка разбора (§15.4).

    Присланными считаются только переданные поля: `PATCH` правит одно поле
    карточки, а не переприсылает её целиком. `remember=true` заводит правило
    и говорит об этом отдельно - книжка по нему не пересчитывается, это
    отдельное действие.
    """
    присланные = тело.model_fields_set
    try:
        правка = finance_edit.править(
            сессия,
            tx_id=tx_id,
            зона=_зона(сессия),
            kind=тело.kind if "kind" in присланные else НЕ_ЗАДАНО,
            category_id=тело.category_id if "category_id" in присланные else НЕ_ЗАДАНО,
            excluded=тело.excluded if "excluded" in присланные else НЕ_ЗАДАНО,
            needs_review=тело.needs_review if "needs_review" in присланные else НЕ_ЗАДАНО,
            note=тело.note if "note" in присланные else НЕ_ЗАДАНО,
            запомнить=тело.remember,
        )
    except ОшибкаПравки as сбой:
        raise _отказ_правки(сбой) from сбой
    except ОшибкаПравила as сбой:
        raise ОтказAPI(статус=409, code=сбой.код, message=str(сбой)) from сбой

    сессия.commit()
    return EditOut(
        id=правка.id,
        changed=правка.изменилось,
        fields_changed=правка.поля,
        rules=правка.правила,
        warnings=правка.предупреждения,
        transaction=_карточка(сессия, tx_id),
    )


def _отказ_правки(сбой: ОшибкаПравки) -> ОтказAPI:
    """Код ответа по коду отказа: «нет такого» - 404, остальное - 409.

    Разделение не косметическое: 404 экран показывает как «строка исчезла,
    обнови ленту», а 409 - как объяснение, которое нужно прочитать.
    """
    статус = 404 if сбой.код in ("нет_операции", "нет_категории") else 409
    return ОтказAPI(статус=статус, code=сбой.код, message=str(сбой))


@маршрутизатор.post(
    "/transactions/{tx_id}/offsets",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО | КОНФЛИКТ,
    summary="Привязать поступление к расходу",
)
def привязать_гашение(сессия: Сессия, момент: Сейчас, tx_id: int, тело: OffsetIn) -> OffsetOut:
    """Гашение конкретного расхода (§15.5).

    Окно - текущий месяц и предыдущий, по обоим концам привязки. Проверяет
    его сервис, а не CHECK: окно зависит от «сейчас».
    """
    try:
        привязка = привязать(
            сессия,
            гашение_id=тело.income_id,
            расход_id=tx_id,
            сейчас=момент,
            зона=_зона(сессия),
        )
    except ОшибкаПривязки as сбой:
        raise _отказ_привязки(сбой) from сбой

    сессия.commit()
    return OffsetOut(
        income_id=привязка.гашение_id,
        expense_id=привязка.расход_id,
        changed=привязка.изменилось,
        warnings=привязка.предупреждения,
        breakdown=BreakdownOut.model_validate(разбивка(сессия, tx_id)),
    )


@маршрутизатор.delete(
    "/transactions/{tx_id}/offsets/{income_id}",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО | КОНФЛИКТ,
    summary="Снять привязку гашения",
)
def снять_гашение(сессия: Сессия, момент: Сейчас, tx_id: int, income_id: int) -> OffsetOut:
    """Снятие ограничено тем же окном, что и привязка (§15.5).

    Расход в пути указан, хотя домену для снятия хватает поступления:
    экран снимает привязку с карточки расхода, и адрес без него читался бы
    как «снять всё, что это поступление гасит».
    """
    гашение = сессия.get(FinTransaction, income_id)
    if гашение is not None and гашение.offsets_transaction_id != tx_id:
        raise ОтказAPI(
            статус=409,
            code="не_та_привязка",
            message=(
                f"поступление {income_id} не гасит расход {tx_id}:"
                f" оно привязано к {гашение.offsets_transaction_id}"
            ),
        )
    try:
        привязка = снять(сессия, гашение_id=income_id, сейчас=момент, зона=_зона(сессия))
    except ОшибкаПривязки as сбой:
        raise _отказ_привязки(сбой) from сбой

    сессия.commit()
    return OffsetOut(
        income_id=привязка.гашение_id,
        expense_id=None,
        changed=привязка.изменилось,
        warnings=привязка.предупреждения,
        breakdown=BreakdownOut.model_validate(разбивка(сессия, tx_id)),
    )


def _отказ_привязки(сбой: ОшибкаПривязки) -> ОтказAPI:
    статус = 404 if сбой.код == "нет_операции" else 409
    return ОтказAPI(статус=статус, code=сбой.код, message=str(сбой))


@маршрутизатор.put(
    "/accounts/{account_id}/role",
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО,
    summary="Роль счёта: расчётный или накопительный",
)
def роль_счёта(сессия: Сессия, account_id: int, тело: AccountRoleIn) -> AccountOut:
    """Разметка счетов owner (§15.5): по ней считается «Отложено».

    Счёт этой ручкой не заводится - он появляется импортом выписки. Поэтому
    адресуется id существующего счёта, а не пара «банк, имя»: разметить
    счёт, которого нет, значит ошибиться в написании и получить двойника.
    """
    счёт = сессия.get(FinAccount, account_id)
    if счёт is None:
        raise ОтказAPI(статус=404, code="not_found", message=f"счёта {account_id} в книжке нет")
    try:
        назначить_роль(сессия, банк=счёт.bank, счёт=счёт.name, роль=тело.role)
    except ОшибкаРоли as сбой:
        raise ОтказAPI(статус=409, code=сбой.код, message=str(сбой)) from сбой
    сессия.commit()
    return AccountOut.model_validate(счёт)


@маршрутизатор.post(
    "/rules",
    status_code=status.HTTP_201_CREATED,
    responses=ОТКАЗЫ | КОНФЛИКТ,
    summary="Завести правило разбора",
)
def завести_правило(сессия: Сессия, тело: RuleIn) -> RuleOut:
    """Правило ступени разбора. Категории этой ручкой не заводятся (§15.4).

    Дубль по типу и образцу - отказ, а не тихая перезапись: правило меняет
    разбор всей книжки, и перезаписанное молча оно поменяло бы сальдо
    закрытых месяцев на следующем же разборе.
    """
    try:
        правило, _ = finance_rules.создать(
            сессия,
            rule_type=тело.rule_type,
            pattern=тело.pattern,
            title=тело.title,
            category_key=тело.category_key,
            kind=тело.kind,
        )
    except ОшибкаПравила as сбой:
        raise ОтказAPI(статус=409, code=сбой.код, message=str(сбой)) from сбой
    сессия.commit()
    return RuleOut.model_validate(правило)


@маршрутизатор.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ОТКАЗЫ | НЕ_НАЙДЕНО,
    summary="Убрать правило разбора",
)
def убрать_правило(сессия: Сессия, rule_id: int) -> None:
    """Удаляет правило. Разобранные им операции не трогаются до разбора."""
    if not finance_rules.удалить(сессия, rule_id):
        raise ОтказAPI(статус=404, code="not_found", message=f"правила {rule_id} нет")
    сессия.commit()


# --- разбор и импорт --------------------------------------------------------


def _разбор(сессия: Session, настройки: Settings, *, apply: bool) -> RecategorizeOut:
    """Разбор книжки целиком: тот же домен и тот же дифф, что у команды."""
    отчёт, решения = разобрать_книжку(сессия, настройки, apply=apply)
    return RecategorizeOut(
        applied=apply,
        transactions=отчёт.операций,
        rules=отчёт.правил,
        categories=отчёт.категорий,
        changes=отчёт.изменений,
        transfers=отчёт.переводов,
        to_review=отчёт.в_разбор,
        without_category=отчёт.без_категории,
        lines=описать_разбор(отчёт, решения, apply=apply),
    )


@маршрутизатор.post(
    "/recategorize",
    responses=ОТКАЗЫ,
    summary="Переразобрать книжку по правилам",
)
def переразобрать(
    сессия: Сессия,
    настройки: Настройки,
    apply: Annotated[bool, Query(description="false - показать дифф и не писать ничего")] = False,
) -> RecategorizeOut:
    """Применяет правила ко всей книжке (§15.4, ADR-041).

    Срез - вся книжка, а не новые строки: перевод себе опознаётся парой
    концов из двух банков, и второй конец приезжает другим файлом в другой
    день. Дифф без записи - умолчание: разбор меняет вид операций, то есть
    сальдо месяцев, и делать это молча по нажатию кнопки нельзя.
    """
    ответ = _разбор(сессия, настройки, apply=apply)
    if apply:
        сессия.commit()
    else:
        # В сессии осталось только чтение, но откат делается явно: молчаливое
        # доверие «тут не могло ничего записаться» - ровно тот случай, когда
        # однажды запишется.
        сессия.rollback()
    return ответ


def _строки_плана(
    сессия: Session, план: ПланИмпорта | None
) -> tuple[list[ImportRowOut], list[ImportRowOut], list[ImportRowOut]]:
    """Дифф импорта строками: что добавится, что изменится, что отменится.

    Экран импорта - единственная точка, где owner видит, что попадёт
    в книжку, **до** того как оно туда попадёт (design/PROGRESS.md, этап 11).
    Счётчиков для этого мало: «обновлено 3» не отвечает на вопрос, какие
    три операции банк переписал.
    """
    if план is None:
        return [], [], []

    добавятся = [
        ImportRowOut(
            id=None,
            occurred_at=новая.строка.occurred_at,
            amount=новая.строка.amount,
            merchant=новая.строка.merchant,
            account=новая.строка.account,
            needs_review=новая.в_разбор,
        )
        for новая in план.новые
    ]
    изменятся = [
        ImportRowOut(
            id=обновление.id,
            occurred_at=обновление.строка.occurred_at,
            amount=обновление.строка.amount,
            merchant=обновление.строка.merchant,
            account=обновление.строка.account,
            needs_review=обновление.id in план.в_разбор,
        )
        for обновление in план.обновления
    ]

    отменятся: list[ImportRowOut] = []
    for номер in план.отменённые:
        строка = сессия.get(FinTransaction, номер)
        if строка is None:
            continue
        отменятся.append(
            ImportRowOut(
                id=строка.id,
                occurred_at=строка.occurred_at,
                amount=строка.amount,
                merchant=строка.merchant,
                account=строка.account,
                needs_review=строка.needs_review,
            )
        )
    return добавятся, изменятся, отменятся


@маршрутизатор.post(
    "/import",
    responses=ОТКАЗЫ,
    summary="Импорт выписок: дифф или запись",
    # Единственный эндпоинт с явным `operation_id`, и причина техническая.
    # Из идентификатора операции FastAPI собирает **имя схемы тела формы**,
    # а имя русской функции превращается в него цепочкой подчёркиваний:
    # в контракте появлялся тип `Body________________api_finance_import_post`,
    # и ровно он уехал бы в клиент Э7 именем типа TypeScript.
    operation_id="finance_import",
)
def импорт_выписок(
    сессия: Сессия,
    настройки: Настройки,
    files: Annotated[list[UploadFile], File(description="Файлы выписок, по одному с банка")],
    apply: Annotated[bool, Query(description="false - показать дифф и не писать ничего")] = False,
) -> ImportOut:
    """Заход книжки: несколько файлов разных банков (§15.3, ADR-030).

    **Дифф и запись - один адрес и два прогона одного файла.** Состояния
    между шагами нет намеренно (решение owner 2026-09-21): черновик плана
    в базе потребовал бы срока жизни и уборки, а повторная отправка файла
    в килобайты ничего не стоит. От двойной записи защищает не память,
    а `fin_imports.sha256`: тот же файл второй раз добавляет ноль строк.

    **Транзакция на файл.** Успешный коммитится сразу, упавший
    откатывается - отказ восьмого файла не уносит семь разобранных.
    Файл, который банк отдал в изменившемся формате, отклоняется целиком
    и с названной причиной (§15.1); остальные файлы захода идут своим ходом.
    """
    зона = _зона(сессия)
    уже_в_заходе: set[str] = set()
    отчёты: list[ImportFileOut] = []

    for файл in files:
        имя = файл.filename or "без имени"
        данные = файл.file.read()
        try:
            отчёт = импортировать(сессия, имя, данные, зона, apply=apply, уже_в_заходе=уже_в_заходе)
        except StatementError as сбой:
            сессия.rollback()
            отчёты.append(_пустой_отчёт(имя, str(сбой)))
            continue

        добавятся, изменятся, отменятся = _строки_плана(сессия, отчёт.план)
        отчёты.append(
            ImportFileOut(
                filename=отчёт.имя,
                sha256=отчёт.sha256,
                bank=отчёт.банк,
                period_start=отчёт.период[0],
                period_end=отчёт.период[1],
                rows_in_file=отчёт.строк_в_файле,
                already_imported=отчёт.уже_загружен,
                added=отчёт.добавлено,
                updated=отчёт.обновлено,
                reverted=отчёт.отменено,
                to_review=отчёт.в_разбор,
                unchanged=отчёт.неизменных,
                new_accounts=отчёт.новых_счетов,
                error=None,
                rows_added=добавятся,
                rows_updated=изменятся,
                rows_reverted=отменятся,
            )
        )
        if apply:
            сессия.commit()
        else:
            сессия.rollback()

    разбор = None
    if apply and any(отчёт.error is None for отчёт in отчёты):
        # Разбор - после всех файлов и один раз: перевод себе опознаётся
        # парой концов из двух банков, и до загрузки второго файла пары
        # ещё нет. В dry-run не запускается вовсе - разбирать нечего.
        разбор = _разбор(сессия, настройки, apply=True)
        сессия.commit()

    return ImportOut(
        applied=apply,
        files=отчёты,
        failed=sum(1 for отчёт in отчёты if отчёт.error is not None),
        recategorized=разбор,
    )


def _пустой_отчёт(имя: str, ошибка: str) -> ImportFileOut:
    """Отклонённый файл: причина названа, чисел нет - разбора не было."""
    return ImportFileOut(
        filename=имя,
        sha256="",
        bank=None,
        period_start=None,
        period_end=None,
        rows_in_file=0,
        already_imported=False,
        added=0,
        updated=0,
        reverted=0,
        to_review=0,
        unchanged=0,
        new_accounts=[],
        error=ошибка,
        rows_added=[],
        rows_updated=[],
        rows_reverted=[],
    )
