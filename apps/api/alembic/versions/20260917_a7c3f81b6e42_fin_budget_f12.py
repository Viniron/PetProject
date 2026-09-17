"""fin_week_budgets и fin_day_spend: недельный бюджет (Ф12)

Две таблицы по `SPEC.md` §15.10 (ADR-038). Имя файла на латинице намеренно:
оно строится из сообщения `-m` средствами консоли, и на Windows русский текст
приезжает в cp1251 - то же решение, что в ревизиях e5220d372bfa и f0021f38eb6f.

Две, а не одна: бюджет вносится **вперёд** (на месяц), а факт траты приходит
**назад** (вечером за сегодня либо выпиской через неделю). В одной таблице
"неделя, за которую бюджет задан" и "день, за который сумма известна" были бы
одним состоянием строки, и неделя без единой траты стала бы неотличима
от недели без бюджета.

Три решения, которые выглядят сложнее, чем могли бы, и все три намеренны.

**Понедельник держит база, а не сервис.** `extract(isodow) = 1` - ровно тот
случай, когда CHECK выразим: он не зависит ни от "сейчас", ни от других строк.
Неделя, случайно заведённая со среды, дала бы пересекающиеся недели и двойной
учёт одного дня - ошибку, которая не падает, а тихо искажает лимит.

**Перенос излишка не хранится.** `carry_in` следующей недели - это `to_next`
предыдущей, одна ссылка назад, и вторая копия разошлась бы с первой.
Хранится только `carry_adjust` - поправка задним числом, потому что это
событие, а не производная (см. ниже).

**`amount` допускает NULL.** Строка недели может существовать ради одной лишь
поправки `carry_adjust`, когда бюджет на эту неделю owner ещё не внёс.
"Бюджет не задан" - это отсутствие строки **или** NULL в `amount`; обе формы
на экране дают одно и то же "бюджет не задан", а не ноль (§10).

Revision ID: a7c3f81b6e42
Revises: f0021f38eb6f
Create Date: 2026-09-17 09:12:44.106318+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3f81b6e42"
down_revision: str | None = "f0021f38eb6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fin_week_budgets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "carry_adjust",
            sa.Numeric(precision=12, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("to_next", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("to_savings", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("settled_spend", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "extract(isodow from week_start) = 1",
            name=op.f("ck_fin_week_budgets_week_starts_on_monday"),
        ),
        sa.CheckConstraint(
            "amount is null or amount >= 0",
            name=op.f("ck_fin_week_budgets_amount_not_negative"),
        ),
        # Распределение недели - одно событие, а не три поля, которые можно
        # заполнить по одному. Половина решения (есть `to_next`, нет
        # `settled_at`) читалась бы как "неделя ещё открыта", и перенос
        # попал бы в следующую неделю дважды: один раз как перенос,
        # второй раз при настоящем распределении.
        sa.CheckConstraint(
            "(settled_at is null) = (to_next is null)"
            " and (settled_at is null) = (to_savings is null)"
            " and (settled_at is null) = (settled_spend is null)",
            name=op.f("ck_fin_week_budgets_settled_all_or_nothing"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_week_budgets")),
        sa.UniqueConstraint("week_start", name="uq_fin_week_budgets_week_start"),
    )
    op.create_table(
        "fin_day_spend",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Внесённая руками трата дня неотрицательна. Возврат руками не
        # вносится: у него есть исходная покупка, и гасит он её через
        # `offsets_transaction_id` (§15.5), а не отрицательным днём.
        sa.CheckConstraint("amount >= 0", name=op.f("ck_fin_day_spend_amount_not_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_day_spend")),
        sa.UniqueConstraint("day", name="uq_fin_day_spend_day"),
    )


def downgrade() -> None:
    op.drop_table("fin_day_spend")
    op.drop_table("fin_week_budgets")
