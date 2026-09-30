"""calendar_events.source: третий источник - напоминание книжки (Ф8)

Воскресное напоминание о выписках (§15.7, ADR-030 п. 6, ADR-053) - первая
запись в календарь, которую ставит не расписание и не owner, а книжка.
Имя файла на латинице по той же причине, что у предыдущих ревизий.

**Отдельное значение, а не `capture`.** Строки захвата разбирает
`push_capture` по `source = capture`, и напоминание под чужим источником
уехало бы в его очередь: два джоба писали бы одно событие, и отказ одного
выглядел бы отказом другого.

**Откат удаляет строки напоминаний.** Иначе вернуть старое ограничение
нельзя: оно не пропустит уже лежащие `finance`. События в самом Google при
этом остаются - откат схемы не ходит наружу, а напоминание прошедшего
воскресенья вреда не несёт.

Revision ID: d8f3a91c4b27
Revises: c4d1e77b0a92
Create Date: 2026-09-30 10:00:00.000000+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f3a91c4b27"
down_revision: str | None = "c4d1e77b0a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("source_known", "calendar_events", type_="check")
    op.create_check_constraint(
        "source_known",
        "calendar_events",
        "source in ('itmo', 'capture', 'finance')",
    )


def downgrade() -> None:
    op.execute("delete from calendar_events where source = 'finance'")
    op.drop_constraint("source_known", "calendar_events", type_="check")
    op.create_check_constraint(
        "source_known",
        "calendar_events",
        "source in ('itmo', 'capture')",
    )
