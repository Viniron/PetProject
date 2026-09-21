"""Схемы книжки - часть контракта, отвечающая вкладке `Finance` (Ф6, §15.7).

Отдельным файлом от `schemas.py`, хотя тот и объявляет «все модели вместе».
Причина та же, по какой книжка - отдельная вкладка: её двадцать моделей
не читаются вперемешку с календарём, и сверять `openapi.json` глазами
пришлось бы по файлу, где две трети строк к делу не относятся. Правила
одни на оба файла: имена классов и полей латиницей, `from_attributes`,
пустое поле приходит как `null`, а не пропадает.

`populate_by_name` вместе с русскими `validation_alias`: одна и та же модель
и собирается из датакласса домена (по русским именам его полей), и строится
роутером по именам полей контракта - например там, где ответ склеен из двух
источников сразу. Без этого флага второй способ отказывал бы на каждом поле
с алиасом, причём только в рантайме.

**Деньги - `Decimal`, и в JSON они уезжают строкой.** `float` для сумм
запрещён `CLAUDE.md`, и не из педантизма: 9 919,81 в двоичной дроби -
это 9919.809999999999, и сумма месяца разошлась бы с выпиской на копейки
ровно там, где owner сверяет книжку с банком.
"""

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis_api.domain.finance_balance import РОЛИ
from jarvis_api.domain.finance_edit import ВИДЫ
from jarvis_api.domain.finance_rules import ВИДЫ as ВИДЫ_ПРАВИЛА
from jarvis_api.domain.finance_rules import ТИПЫ

# --- обзор месяца -----------------------------------------------------------


class MonthOut(BaseModel):
    """Сальдо месяца и статьи «Отложено» и «Осталось» (§15.5)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    month: dt.date = Field(validation_alias="месяц", description="Первое число месяца в зоне owner")
    income: Decimal = Field(validation_alias="пришло")
    expense: Decimal = Field(
        validation_alias="ушло", description="Уменьшен гашениями, не ниже нуля"
    )
    balance: Decimal = Field(validation_alias="сальдо", description="Пришло минус ушло")
    saved: Decimal | None = Field(
        validation_alias="отложено",
        description="Нетто переводов на счета savings. null - счета не размечены, и это не ноль",
    )
    left: Decimal = Field(
        validation_alias="осталось", description="Накопленный итог с начала ведения"
    )
    transactions: int = Field(validation_alias="операций", description="Строк, вошедших в сальдо")
    outside_transfers: int = Field(
        validation_alias="вне_переводов",
        description="Строки на накопительных счетах, не опознанные переводом себе",
    )
    recalculated_later: bool = Field(
        validation_alias="пересчитан_позже",
        description="Расход месяца погашен поступлением более позднего месяца",
    )


class FreshnessOut(BaseModel):
    """Свежесть книжки по самому отстающему банку (§15.6)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    covered_through: dt.date | None = Field(
        validation_alias="покрыто_по",
        description="Последний день, покрытый выписками всех банков. null - покрытия нет",
    )
    lagging_bank: str | None = Field(
        validation_alias="отстающий_банк", description="Банк, по которому книжка отстаёт"
    )
    loaded_at: dt.datetime | None = Field(validation_alias="загружено_в")
    days_ago: int | None = Field(validation_alias="дней_назад")
    stale: bool = Field(
        validation_alias="просрочено", description="Срок из FINANCE_STALE_AFTER_DAYS вышел"
    )


class OverviewOut(BaseModel):
    """Ответ `GET /api/finance/overview`: месяц с оговорками к цифре."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    data: MonthOut
    timezone: str = Field(description="Зона owner, в которой посчитаны границы месяца")
    covered_through: dt.date | None = Field(description="То же, что в freshness: повтор для цифры")
    incomplete: bool = Field(description="Выписки дошли не до конца месяца - цифра промежуточная")
    in_recalc_window: bool = Field(description="Месяц ещё может пересчитаться гашением")
    accounts_marked: bool = Field(description="Все счета книжки получили роль от owner")
    freshness: FreshnessOut


# --- операции ---------------------------------------------------------------


class TransactionOut(BaseModel):
    """Строка ленты и карточки."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    occurred_at: dt.datetime
    bank: str
    account: str
    amount: Decimal = Field(description="Как в выписке: расход отрицателен")
    amount_rub: Decimal
    currency: str
    merchant: str | None
    message: str | None
    kind: str = Field(description="expense, income, transfer или refund")
    kind_source: str = Field(description="sign, default, sender_rule, transfer_pair или manual")
    status: str = Field(description="posted, pending или reverted")
    excluded: bool
    needs_review: bool = Field(description="Стоит в очереди разбора и в сальдо не входит")
    entered_manually: bool
    category_id: int | None
    category_key: str | None = Field(description="Стабильный ключ категории, переживающий месяц")
    category_title: str | None
    category_source: str | None
    offsets_transaction_id: int | None = Field(description="Расход, который гасит это поступление")
    transfer_pair_id: int | None = Field(description="Второй конец перевода себе")
    effective: Decimal | None = Field(
        validation_alias="эффективная",
        description="Чего расход стоил после гашений. null у прихода - гасить нечего",
    )
    offset: Decimal | None = Field(
        validation_alias="погашено", description="Сколько из расхода погашено поступлениями"
    )
    counts: bool = Field(
        validation_alias="в_счёт",
        description="Идёт в счёт: не исключена, не отменена, не в очереди разбора",
    )


class TransactionsOut(BaseModel):
    """Ответ `GET /api/finance/transactions`.

    Объектом, а не голым массивом: массив на верхнем уровне нельзя расширить
    ни одним полем, не сломав клиента.
    """

    month: dt.date | None = Field(description="Месяц фильтра. null - вся книжка")
    needs_review_only: bool
    transactions: list[TransactionOut]


class OffsetPartOut(BaseModel):
    """Один гасящий приход в разбивке расхода (§15.5)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    transaction_id: int = Field(validation_alias="гашение_id")
    applied: Decimal = Field(validation_alias="погасило")
    surplus: Decimal = Field(
        validation_alias="излишек", description="Что не пошло на расход и стало доходом"
    )


class BreakdownOut(BaseModel):
    """Сколько расход стоил на самом деле."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    transaction_id: int = Field(validation_alias="расход_id")
    amount: Decimal = Field(validation_alias="сумма", description="Своя сумма расхода, без гашений")
    offset: Decimal = Field(validation_alias="погашено")
    effective: Decimal = Field(validation_alias="эффективная", description="Не ниже нуля")
    surplus: Decimal = Field(validation_alias="излишек")
    parts: list[OffsetPartOut] = Field(validation_alias="части")
    not_counted: list[int] = Field(
        validation_alias="вне_счёта",
        description="Привязанные, но в счёт не идущие: отменённые, исключённые, в разборе",
    )


class TransactionCardOut(BaseModel):
    """Ответ `GET /api/finance/transactions/{id}` (экран 13 дизайна)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    transaction: TransactionOut = Field(validation_alias="операция")
    breakdown: BreakdownOut | None = Field(
        validation_alias="разбивка", description="Только у расхода"
    )
    offsets: list[TransactionOut] = Field(
        validation_alias="гашения", description="Поступления, гасящие этот расход"
    )
    offsetting: TransactionOut | None = Field(
        validation_alias="гасит", description="Расход, который гасит сама эта операция"
    )
    transfer_pair: TransactionOut | None = Field(
        validation_alias="пара", description="Второй конец перевода себе"
    )


class TransactionPatchIn(BaseModel):
    """Тело `PATCH /api/finance/transactions/{id}` - решение owner (§15.4).

    Переданными считаются только присланные поля: экран правит одно поле
    карточки, и умолчание, затирающее заметку при смене категории, стоило бы
    owner ровно того текста, который он туда вписал. Поэтому у полей нет
    значений по умолчанию в привычном смысле - роутер смотрит
    `model_fields_set`.

    `null` при этом - законное значение: снять категорию или заметку.
    """

    kind: str | None = Field(default=None, description=f"Один из: {', '.join(ВИДЫ)}")
    category_id: int | None = Field(default=None, description="null снимает категорию")
    excluded: bool | None = None
    needs_review: bool | None = Field(default=None, description="false закрывает вопрос разбора")
    note: str | None = None
    remember: bool = Field(
        default=False,
        description="Запомнить решение правилом. По умолчанию правка разовая",
    )

    @field_validator("kind")
    @classmethod
    def _вид_известен(cls, значение: str | None) -> str | None:
        """`null` виду не разрешён: колонка `not null`, снять вид нельзя."""
        if значение is None:
            raise ValueError(f"вид обязателен и не снимается: один из {', '.join(ВИДЫ)}")
        if значение not in ВИДЫ:
            raise ValueError(f"вид {значение!r} не из {', '.join(ВИДЫ)}")
        return значение

    @field_validator("excluded", "needs_review")
    @classmethod
    def _флаг_не_снимается(cls, значение: bool | None) -> bool | None:
        if значение is None:
            raise ValueError("булев флаг принимает true или false, но не null")
        return значение


class EditOut(BaseModel):
    """Ответ на правку: что изменилось и что из этого запомнено правилом."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    changed: bool = Field(validation_alias="изменилось")
    fields_changed: list[str] = Field(validation_alias="поля")
    rules: list[int] = Field(validation_alias="правила", description="Заведённые правила")
    warnings: list[str] = Field(
        validation_alias="предупреждения", description="Читает owner: правка прошла, но с оговоркой"
    )
    transaction: TransactionCardOut = Field(description="Карточка после правки")


# --- гашение расхода --------------------------------------------------------


class OffsetIn(BaseModel):
    """Тело `POST /api/finance/transactions/{id}/offsets`."""

    income_id: int = Field(description="Поступление, которое гасит этот расход")


class OffsetOut(BaseModel):
    """Ответ на привязку и снятие."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    income_id: int = Field(validation_alias="гашение_id")
    expense_id: int | None = Field(validation_alias="расход_id")
    changed: bool = Field(validation_alias="изменилось")
    warnings: list[str] = Field(validation_alias="предупреждения")
    breakdown: BreakdownOut | None = Field(
        default=None, description="Расход после действия: сколько он теперь стоит"
    )


# --- счета, категории, правила ----------------------------------------------


class AccountOut(BaseModel):
    """Счёт книжки. Роль ставит owner, имя приносит импорт (§15.5)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    bank: str
    name: str
    role: str = Field(description=f"Одна из: {', '.join(РОЛИ)}")


class AccountsOut(BaseModel):
    accounts: list[AccountOut]
    all_marked: bool = Field(description="Ни одного счёта с ролью unknown: «Отложено» считается")


class AccountRoleIn(BaseModel):
    """Тело `PUT /api/finance/accounts/{id}/role`."""

    role: str = Field(description=f"Одна из: {', '.join(РОЛИ)}")

    @field_validator("role")
    @classmethod
    def _роль_известна(cls, значение: str) -> str:
        if значение not in РОЛИ:
            raise ValueError(f"роль {значение!r} не из {', '.join(РОЛИ)}")
        return значение


class CategoryOut(BaseModel):
    """Категория месяца (§15.4)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    period_month: dt.date
    key: str = Field(description="Стабильный ключ, переживающий смену месяца")
    title: str
    level: int = Field(description="1 - категория, 2 - подкатегория")
    parent_id: int | None
    origin: str = Field(description="owner или ai")
    status: str = Field(description="active, proposed или rejected")


class CategoriesOut(BaseModel):
    month: dt.date | None = Field(description="Месяц фильтра. null - все месяцы")
    categories: list[CategoryOut]


class RuleOut(BaseModel):
    """Правило разбора (§15.4, §15.5)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    rule_type: str = Field(description=f"Одна из ступеней: {', '.join(ТИПЫ)}")
    pattern: str
    title: str | None = Field(description="Как owner называет отправителя, а не как пишет банк")
    category_key: str | None
    kind: str | None = Field(description=f"Только у правила отправителя: {', '.join(ВИДЫ_ПРАВИЛА)}")


class RulesOut(BaseModel):
    rules: list[RuleOut]


class RuleIn(BaseModel):
    """Тело `POST /api/finance/rules`.

    Форма правила проверяется доменом, а не только здесь: те же проверки
    нужны и флагу «запомнить» на карточке операции, который сюда не заходит.
    """

    rule_type: str = Field(description=f"Одна из: {', '.join(ТИПЫ)}")
    pattern: str = Field(min_length=1, description="Образец: код MCC, мерчант, имя отправителя")
    title: str | None = None
    category_key: str | None = None
    kind: str | None = Field(
        default=None, description=f"Только для sender: {', '.join(ВИДЫ_ПРАВИЛА)}"
    )


# --- разбор и импорт --------------------------------------------------------


class RecategorizeOut(BaseModel):
    """Ответ `POST /api/finance/recategorize` - тот же дифф, что у команды."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    applied: bool = Field(description="false - показан дифф, в базу не записано ничего")
    transactions: int = Field(validation_alias="операций")
    rules: int = Field(validation_alias="правил")
    categories: int = Field(validation_alias="категорий")
    changes: int = Field(validation_alias="изменений")
    transfers: int = Field(validation_alias="переводов", description="Опознано парой концов")
    to_review: int = Field(validation_alias="в_разбор")
    without_category: int = Field(validation_alias="без_категории")
    lines: list[str] = Field(description="Дифф строками, по одной на изменение: их читает owner")


class ImportRowOut(BaseModel):
    """Строка диффа импорта: то, что попадёт в книжку или изменится в ней."""

    id: int | None = Field(description="null у новой строки: её ещё нет в книжке")
    occurred_at: dt.datetime
    amount: Decimal
    merchant: str | None
    account: str | None
    needs_review: bool = Field(description="Уйдёт в очередь разбора: кандидат в дубли")


class ImportFileOut(BaseModel):
    """Что файл сделал бы или сделал (§15.3)."""

    filename: str
    sha256: str
    bank: str | None
    period_start: dt.date | None
    period_end: dt.date | None
    rows_in_file: int
    already_imported: bool = Field(description="Файл с этим sha256 уже загружали: изменений нет")
    added: int
    updated: int
    reverted: int = Field(description="Было в книжке, пропало из выгрузки")
    to_review: int
    unchanged: int
    new_accounts: list[str] = Field(description="Новые имена счетов: роль unknown, ждут owner")
    error: str | None = Field(description="Файл отклонён целиком, с причиной")
    rows_added: list[ImportRowOut]
    rows_updated: list[ImportRowOut]
    rows_reverted: list[ImportRowOut]


class ImportOut(BaseModel):
    """Ответ `POST /api/finance/import`.

    `applied=false` - это дифф: в базу не записано ничего, и повторный
    запрос с `apply=true` тем же файлом запишет ровно показанное.
    """

    applied: bool
    files: list[ImportFileOut]
    failed: int = Field(description="Файлов отклонено. Остальные файлы захода обработаны")
    recategorized: RecategorizeOut | None = Field(
        default=None,
        description="Разбор после записи. null в dry-run: разбирать нечего",
    )
    corrected_weeks: list[dt.date] = Field(
        default_factory=list,
        description=(
            "Распределённые недели бюджета, чьи траты выписка изменила задним числом"
            " (§15.10): разница ушла поправкой вперёд. Пусто в dry-run"
        ),
    )


# --- недельный бюджет (Ф13) -------------------------------------------------


class BudgetDayOut(BaseModel):
    """Один день недели: сколько потрачено и откуда это известно (§15.10)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    day: dt.date = Field(validation_alias="день")
    amount: Decimal | None = Field(
        validation_alias="сумма",
        description="null - сумма неизвестна. Это не ноль: ноль закрывает день",
    )
    source: str = Field(validation_alias="источник", description="statement, manual или unknown")
    manual: Decimal | None = Field(
        validation_alias="ручная", description="Что внёс owner, даже если сумма выше из выписки"
    )
    discrepancy: Decimal | None = Field(
        validation_alias="расхождение",
        description="Выписка минус слова owner. Положительное - потрачено больше, чем он думал",
    )


class LimitOut(BaseModel):
    """Ответ на вопрос «сколько можно потратить» (§15.10)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    amount: Decimal | None = Field(
        validation_alias="сумма",
        description="null - бюджет не задан либо открытых дней не осталось. Не ноль",
    )
    for_day: dt.date | None = Field(
        validation_alias="на_день",
        description="День, к которому относится сумма. Экран обязан его подписать",
    )
    open_days: int = Field(validation_alias="открытых_дней", description="Делитель лимита")
    days_without_amount: int = Field(
        validation_alias="дней_без_суммы",
        description="Прошедшие дни без суммы: в расчёт идут нулём, и экран обязан их назвать",
    )
    budget_set: bool = Field(validation_alias="бюджет_задан")
    spent: Decimal = Field(validation_alias="потрачено", description="Сумма известных дней")
    remainder: Decimal = Field(
        validation_alias="остаток", description="Бюджет с переносом минус траты. Минус - перерасход"
    )


class OutcomeOut(BaseModel):
    """Итог недели, готовой к распределению."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    week_start: dt.date = Field(validation_alias="начало")
    amount: Decimal = Field(
        validation_alias="сумма", description="Плюс - излишек, минус - перерасход"
    )
    spent: Decimal = Field(validation_alias="потрачено")
    settled: bool = Field(validation_alias="распределена")


class SettlementOut(BaseModel):
    """Решение owner о том, куда ушёл итог недели."""

    to_next: Decimal = Field(description="В следующую неделю. Минус - перерасход переносом")
    to_savings: Decimal = Field(description="В копилку. Минус - покрытие перерасхода из неё")
    settled_spend: Decimal = Field(
        description="Снимок трат на момент решения. Расходится с spent - неделю поправили позже"
    )
    settled_at: dt.datetime


class BudgetWeekOut(BaseModel):
    """Неделя целиком: бюджет, перенос, семь дней, лимит и итог."""

    week_start: dt.date
    week_end: dt.date
    budget: Decimal | None = Field(description="null - бюджет не задан. Не ноль и не среднее")
    carry: Decimal = Field(description="Итог предыдущей недели плюс поправки задним числом")
    spent: Decimal = Field(description="Сумма известных дней")
    settled: bool
    days: list[BudgetDayOut]
    limit: LimitOut
    ready_to_settle: bool = Field(description="Неделя прошла целиком либо все семь дней известны")
    outcome: OutcomeOut | None = Field(description="null - неделя ещё не готова к распределению")
    settlement: SettlementOut | None = Field(description="null - owner ещё не распределил итог")


class BudgetOut(BaseModel):
    """Ответ `GET /api/finance/budget` - экран 15 дизайна."""

    week: BudgetWeekOut
    today: dt.date = Field(description="Сегодня в зоне owner: от него считаются открытые дни")
    timezone: str
    covered_through: dt.date | None = Field(
        description="Последний день, покрытый выписками всех банков. null - покрытия нет"
    )
    savings_jar: Decimal = Field(
        description="«Отложено бюджетом»: виртуальная копилка, сумма решений owner"
    )
    saved_real: Decimal | None = Field(
        description=(
            "«Отложено» (§15.5) за всё время: настоящие переводы на счета savings."
            " Разница с savings_jar - напоминание перевести деньги. null - счета не размечены"
        )
    )


class BudgetWeeksOut(BaseModel):
    """Ответ `GET /api/finance/budget/weeks`: недели подряд, для ввода вперёд."""

    from_week: dt.date
    weeks: list[BudgetWeekOut]


class WeekBudgetIn(BaseModel):
    """Тело `PUT /api/finance/budget/weeks/{week_start}`."""

    amount: Decimal = Field(ge=0, description="Бюджет недели. Отрицательного бюджета не бывает")


class DaySpendIn(BaseModel):
    """Тело `PUT /api/finance/budget/days/{day}`.

    Возврат сюда не вносится отрицательной суммой: у него есть исходная
    покупка, и гасит он её привязкой (§15.5).
    """

    amount: Decimal = Field(ge=0, description="Сколько потрачено за день")
    note: str | None = None


class DaySpendOut(BaseModel):
    """Ответ на ввод и снятие суммы дня."""

    day: dt.date
    amount: Decimal | None = Field(description="null после снятия: день снова «неизвестен»")
    previous: Decimal | None = Field(description="Что стояло за день до этого")
    corrected_weeks: list[dt.date] = Field(
        description=(
            "Распределённые недели, чей снимок трат разошёлся с фактом:"
            " поправка ушла в ближайшую нераспределённую"
        )
    )
    week: BudgetWeekOut = Field(description="Неделя после записи: лимит считает сервер, не экран")


class SettleIn(BaseModel):
    """Тело `POST /api/finance/budget/weeks/{week_start}/settle`.

    Два числа, а не выбор из двух: половину излишка в копилку, половину
    в следующую неделю - законное решение owner. Сумма обязана сойтись
    с итогом недели, иначе отказ: доложить недостающее молча значило бы
    решить за него, куда девать его деньги.
    """

    to_next: Decimal = Field(description="В следующую неделю. Минус - перенос перерасхода")
    to_savings: Decimal = Field(description="В копилку. Минус - покрытие перерасхода из неё")
