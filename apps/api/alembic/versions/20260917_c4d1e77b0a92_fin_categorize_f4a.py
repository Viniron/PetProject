"""Признаки разбора и пара перевода себе (Ф4а)

Четыре колонки по `SPEC.md` §15.4 и §15.5. Имя файла на латинице намеренно:
оно строится из сообщения `-m` средствами консоли, и на Windows русский текст
приезжает в cp1251 - то же решение, что в ревизиях e5220d372bfa, f0021f38eb6f,
a7c3f81b6e42 и 0233258da315.

**Зачем признак ступени, если есть категория.** §15.4 требует, чтобы ручная
правка owner была сильнее любого правила и переживала переимпорт. Отличить
её от проставленного правилом нечем: в колонке `category_id` и то, и другое
выглядит одинаково. Без `category_source` первый же переразбор - а он
неизбежен, потому что правила приезжают позже данных - молча вернул бы
операции категорию, которую owner руками снял. Ошибка не падает и не видна:
ровно тот класс, ради которого §15.2 хранит и сырьё строки.

**`kind_source` по той же причине, но про другое.** Импорт Ф3 пишет `kind`
по знаку суммы и честно называет это провизорным (§15.3): колонка `not null`,
«неизвестно» в ней не выражается. С появлением правил `expense` по знаку
и `expense` по правилу отправителя - разные степени уверенности, и переразбор
вправе переписать первое, но не решение owner. Значение по умолчанию `sign`
проставляется существующим строкам не из удобства: они действительно
разобраны знаком и ничем больше.

**`transfer_pair_id` хранится, а не пересчитывается** (ADR-041). Перевод себе
опознаётся парой концов - списание в одном банке и зачисление в другом, - и
эта пара обязана быть видна в книжке строкой. Пересчёт по сумме и дню дал бы
тот же ответ ровно до первой правки owner: снял бы он пометку с пары, следующий
разбор нашёл бы её заново и поставил снова. Ссылка симметрична (A знает B,
B знает A), но симметрию держит сервис: CHECK другой строки не видит.

**`fin_category_rules.title` - кого owner имел в виду.** Т-Банк пишет
отправителя как «Евгений В.», а список родных owner прислал в виде ФИО.
Сопоставлять надо с написанием банка, показывать - ФИО, и без второй колонки
список родных в книжке читался бы инициалами. Однофамилец - известная цена
правила по отправителю (ADR-041), и разглядеть его в списке «Евгений В.»
невозможно по построению.

Индексов ни на одну колонку нет намеренно, по причине ревизии 0233258da315:
`transfer_pair_id` заполнен у редкого исключения, `category_source` читается
вместе со строкой, а не ищется по нему.

Revision ID: c4d1e77b0a92
Revises: 0233258da315
Create Date: 2026-09-17 19:12:04.118722+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d1e77b0a92"
down_revision: str | None = "0233258da315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fin_category_rules",
        sa.Column("title", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "fin_transactions",
        sa.Column(
            "kind_source",
            sa.String(length=64),
            server_default=sa.text("'sign'"),
            nullable=False,
        ),
    )
    op.add_column(
        "fin_transactions",
        sa.Column("category_source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "fin_transactions",
        sa.Column("transfer_pair_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fin_transactions_transfer_pair",
        "fin_transactions",
        "fin_transactions",
        ["transfer_pair_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "category_source_known",
        "fin_transactions",
        "category_source is null or category_source in"
        " ('mcc', 'bank_category', 'own_category', 'merchant', 'model', 'manual')",
    )
    # Категория без источника запрещена; источник без категории разрешён
    # только `manual` - это «owner снял категорию руками», и переразбор
    # не вправе вернуть её.
    op.create_check_constraint(
        "category_source_shape",
        "fin_transactions",
        "(category_id is not null and category_source is not null)"
        " or (category_id is null and (category_source is null or category_source = 'manual'))",
    )
    op.create_check_constraint(
        "kind_source_known",
        "fin_transactions",
        "kind_source in ('sign', 'default', 'sender_rule', 'transfer_pair', 'manual')",
    )
    # Тип правила `self` - как банк пишет самого owner (ADR-041). Оно не
    # определяет ни категории, ни вида: им опознаётся непарный конец перевода
    # себе, который обязан уйти в разбор, а не разнестись умолчанием §15.5.
    op.drop_constraint("rule_type_known", "fin_category_rules", type_="check")
    op.create_check_constraint(
        "rule_type_known",
        "fin_category_rules",
        "rule_type in ('mcc', 'bank_category', 'merchant', 'sender', 'self')",
    )
    op.drop_constraint("rule_decides_something", "fin_category_rules", type_="check")
    op.create_check_constraint(
        "rule_decides_something",
        "fin_category_rules",
        "(rule_type = 'sender' and kind is not null)"
        " or (rule_type = 'self' and kind is null and category_key is null)"
        " or (rule_type not in ('sender', 'self') and category_key is not null)",
    )
    op.create_check_constraint(
        "transfer_pair_not_self",
        "fin_transactions",
        "transfer_pair_id is null or transfer_pair_id <> id",
    )


def downgrade() -> None:
    # Правила типа `self` не переживают отката: без них CHECK прежней формы
    # не создастся. Удаляются, а не переписываются в другой тип - выдумывать
    # за owner, чем стало его имя, книжка не вправе.
    op.execute("delete from fin_category_rules where rule_type = 'self'")
    op.drop_constraint("rule_decides_something", "fin_category_rules", type_="check")
    op.create_check_constraint(
        "rule_decides_something",
        "fin_category_rules",
        "(rule_type = 'sender' and kind is not null)"
        " or (rule_type <> 'sender' and category_key is not null)",
    )
    op.drop_constraint("rule_type_known", "fin_category_rules", type_="check")
    op.create_check_constraint(
        "rule_type_known",
        "fin_category_rules",
        "rule_type in ('mcc', 'bank_category', 'merchant', 'sender')",
    )
    op.drop_constraint("transfer_pair_not_self", "fin_transactions", type_="check")
    op.drop_constraint("kind_source_known", "fin_transactions", type_="check")
    op.drop_constraint("category_source_shape", "fin_transactions", type_="check")
    op.drop_constraint("category_source_known", "fin_transactions", type_="check")
    op.drop_constraint("fk_fin_transactions_transfer_pair", "fin_transactions", type_="foreignkey")
    op.drop_column("fin_transactions", "transfer_pair_id")
    op.drop_column("fin_transactions", "category_source")
    op.drop_column("fin_transactions", "kind_source")
    op.drop_column("fin_category_rules", "title")
