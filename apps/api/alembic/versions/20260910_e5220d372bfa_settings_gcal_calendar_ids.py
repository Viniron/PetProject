"""settings: идентификаторы календарей Google (Э4)

Три колонки в единственной строке настроек. Здесь, а не в env (решение
owner, ADR-026): id календарей - результат работы `make gcal-setup-apply`,
а не конфигурация, и он обязан вернуться вместе с базой, восстановленной
из дампа. Иначе первый же прогон на восстановленной плате завёл бы вторые
три календаря рядом с живыми.

Nullable - потому что пустое значение рабочее: до настройки календарей
их нет, и джоб записи отказывается работать с внятным текстом.

Заголовок ревизии на латинице намеренно: русское `-m` даёт нечитаемое имя
файла - имя строится из сообщения средствами консоли, и на Windows оно
приезжает в cp1251.

Revision ID: e5220d372bfa
Revises: be67a3dd2486
Create Date: 2026-09-10 10:54:55.472259+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5220d372bfa"
down_revision: str | None = "be67a3dd2486"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("gcal_itmo_id", sa.String(length=256), nullable=True))
    op.add_column("settings", sa.Column("gcal_study_id", sa.String(length=256), nullable=True))
    op.add_column("settings", sa.Column("gcal_events_id", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "gcal_events_id")
    op.drop_column("settings", "gcal_study_id")
    op.drop_column("settings", "gcal_itmo_id")
