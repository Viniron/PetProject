"""fin_categories: предложение «убрать» и его причина; месяц предложения (Ф10)

Предложение набора на новый месяц (§15.4, ADR-056) бывает двух видов.
«Добавить» уже выражалось схемой Ф1: строка `status = proposed`. «Убрать» -
нет: убирается унаследованная категория, а вторую строку с тем же ключом
в месяце не пускает уникальный индекс. Поэтому признак ставится на саму
унаследованную строку - она остаётся `active` и работает, пока owner
не решил (§15.4: «до утверждения работает унаследованный набор»).

**Признак только у основной категории.** Модель предлагает набор основных
(§15.4), подкатегория уходит вместе с родителем. CHECK держит это базой,
а не соглашением - как глубину в два уровня.

**Месяц предложения - в `settings`.** Модель могла честно ответить «менять
нечего», и тогда в `fin_categories` не остаётся ни строки, по которой
следующий импорт понял бы, что вопрос уже задан. Это результат работы,
а не конфигурация, - как id календарей Google рядом.

Revision ID: e5a2c7d19f40
Revises: d8f3a91c4b27
Create Date: 2026-10-01 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a2c7d19f40"
down_revision: str | None = "d8f3a91c4b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fin_categories",
        sa.Column("remove_proposed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("fin_categories", sa.Column("proposal_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "remove_main_only",
        "fin_categories",
        "not remove_proposed or level = 1",
    )
    op.add_column("settings", sa.Column("finance_proposed_month", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "finance_proposed_month")
    op.drop_constraint("remove_main_only", "fin_categories", type_="check")
    op.drop_column("fin_categories", "proposal_reason")
    op.drop_column("fin_categories", "remove_proposed")
