"""fin_*: схема финансовой книжки (Ф1)

Шесть таблиц по `SPEC.md` §15.2. Имя файла на латинице намеренно: оно
строится из сообщения `-m` средствами консоли, и на Windows русский текст
приезжает в cp1251 - то же решение, что в ревизии e5220d372bfa.

Три ограничения выглядят сложнее остальных, и все три намеренны.

**Два уровня категорий держит база.** `level` плюс `parent_level` плюс
составной внешний ключ на (id, level, period_month) дают сразу два
инварианта: третьего уровня не существует, и подкатегория не может
принадлежать категории другого месяца (ADR-030). Обычным CHECK это не
выразить - он не видит других строк.

**Уникальность категории внутри месяца - двумя частичными индексами.**
В Postgres два NULL не равны друг другу, поэтому UNIQUE по
(period_month, parent_id, key) пропустил бы две основные категории
с одним ключом в одном месяце.

**Счёт операции - составным внешним ключом на `fin_accounts`.** Статья
"Отложено" считается по размеченным счетам (§15.5), и операция на счёт
вне разметки сделала бы расхождение тихим.

Revision ID: f0021f38eb6f
Revises: e5220d372bfa
Create Date: 2026-09-10 20:39:58.558372+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f0021f38eb6f"
down_revision: str | None = "e5220d372bfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fin_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bank", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "role", sa.String(length=64), server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role in ('checking', 'savings', 'unknown')", name=op.f("ck_fin_accounts_role_known")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_accounts")),
        sa.UniqueConstraint("bank", "name", name="uq_fin_accounts_bank_name"),
    )
    op.create_table(
        "fin_categories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_level", sa.SmallInteger(), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=64), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin in ('owner', 'ai')", name=op.f("ck_fin_categories_origin_known")
        ),
        sa.CheckConstraint(
            "status in ('active', 'proposed', 'rejected')",
            name=op.f("ck_fin_categories_status_known"),
        ),
        sa.CheckConstraint(
            "(level = 1 and parent_id is null and parent_level is null)"
            " or (level = 2 and parent_id is not null and parent_level = 1)",
            name=op.f("ck_fin_categories_parent_shape"),
        ),
        sa.CheckConstraint("level in (1, 2)", name=op.f("ck_fin_categories_level_known")),
        sa.ForeignKeyConstraint(
            ["parent_id", "parent_level", "period_month"],
            ["fin_categories.id", "fin_categories.level", "fin_categories.period_month"],
            name="fk_fin_categories_parent",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_categories")),
        sa.UniqueConstraint("id", "level", "period_month", name="uq_fin_categories_id_level_month"),
    )
    op.create_index(
        "ix_fin_categories_period_month", "fin_categories", ["period_month"], unique=False
    )
    op.create_index(
        "uq_fin_categories_month_key_main",
        "fin_categories",
        ["period_month", "key"],
        unique=True,
        postgresql_where=sa.text("parent_id is null"),
    )
    op.create_index(
        "uq_fin_categories_month_key_sub",
        "fin_categories",
        ["period_month", "parent_id", "key"],
        unique=True,
        postgresql_where=sa.text("parent_id is not null"),
    )
    op.create_table(
        "fin_category_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_type", sa.String(length=64), nullable=False),
        sa.Column("pattern", sa.String(length=256), nullable=False),
        sa.Column("category_key", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(rule_type = 'sender' and kind is not null)"
            " or (rule_type <> 'sender' and category_key is not null)",
            name=op.f("ck_fin_category_rules_rule_decides_something"),
        ),
        sa.CheckConstraint(
            "kind is null or kind in ('income', 'refund')",
            name=op.f("ck_fin_category_rules_kind_known"),
        ),
        sa.CheckConstraint(
            "rule_type in ('mcc', 'bank_category', 'merchant', 'sender')",
            name=op.f("ck_fin_category_rules_rule_type_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_category_rules")),
        sa.UniqueConstraint("rule_type", "pattern", name="uq_fin_category_rules_type_pattern"),
    )
    op.create_table(
        "fin_imports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bank", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rows_added", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_updated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_imports")),
        sa.UniqueConstraint("sha256", name="uq_fin_imports_sha256"),
    )
    op.create_index(
        "ix_fin_imports_bank_imported_at", "fin_imports", ["bank", "imported_at"], unique=False
    )
    op.create_table(
        "fin_summaries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text_ru", sa.Text(), nullable=False),
        sa.Column("basis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["fin_imports.id"],
            name=op.f("fk_fin_summaries_import_id_fin_imports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_summaries")),
        sa.UniqueConstraint("import_id", name="uq_fin_summaries_import_id"),
    )
    op.create_index("ix_fin_summaries_created_at", "fin_summaries", ["created_at"], unique=False)
    op.create_table(
        "fin_transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bank", sa.String(length=64), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account", sa.String(length=256), nullable=False),
        sa.Column("card_last4", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=64), nullable=False),
        sa.Column("amount_rub", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("merchant", sa.String(length=256), nullable=True),
        sa.Column("bank_category", sa.String(length=256), nullable=True),
        sa.Column("own_category", sa.String(length=256), nullable=True),
        sa.Column("mcc", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("analytics_hint", sa.Boolean(), nullable=True),
        sa.Column(
            "status", sa.String(length=64), server_default=sa.text("'posted'"), nullable=False
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("offsets_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("occurrence_no", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_row", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "entered_manually", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('expense', 'income', 'transfer', 'refund')",
            name=op.f("ck_fin_transactions_kind_known"),
        ),
        sa.CheckConstraint(
            "status in ('posted', 'pending', 'reverted')",
            name=op.f("ck_fin_transactions_status_known"),
        ),
        sa.CheckConstraint(
            "occurrence_no >= 1", name=op.f("ck_fin_transactions_occurrence_no_positive")
        ),
        sa.CheckConstraint(
            "offsets_transaction_id is null or amount > 0",
            name=op.f("ck_fin_transactions_offsets_only_incoming"),
        ),
        sa.CheckConstraint(
            "offsets_transaction_id is null or offsets_transaction_id <> id",
            name=op.f("ck_fin_transactions_offsets_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["bank", "account"],
            ["fin_accounts.bank", "fin_accounts.name"],
            name="fk_fin_transactions_account",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["fin_categories.id"],
            name=op.f("fk_fin_transactions_category_id_fin_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["fin_imports.id"],
            name=op.f("fk_fin_transactions_import_id_fin_imports"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["offsets_transaction_id"],
            ["fin_transactions.id"],
            name=op.f("fk_fin_transactions_offsets_transaction_id_fin_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fin_transactions")),
        sa.UniqueConstraint(
            "bank",
            "fingerprint",
            "occurrence_no",
            name="uq_fin_transactions_bank_fingerprint_occurrence",
        ),
    )
    op.create_index(
        "ix_fin_transactions_import_id", "fin_transactions", ["import_id"], unique=False
    )
    op.create_index(
        "ix_fin_transactions_occurred_at", "fin_transactions", ["occurred_at"], unique=False
    )
    op.create_index(
        "ix_fin_transactions_offsets_transaction_id",
        "fin_transactions",
        ["offsets_transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fin_transactions_offsets_transaction_id", table_name="fin_transactions")
    op.drop_index("ix_fin_transactions_occurred_at", table_name="fin_transactions")
    op.drop_index("ix_fin_transactions_import_id", table_name="fin_transactions")
    op.drop_table("fin_transactions")
    op.drop_index("ix_fin_summaries_created_at", table_name="fin_summaries")
    op.drop_table("fin_summaries")
    op.drop_index("ix_fin_imports_bank_imported_at", table_name="fin_imports")
    op.drop_table("fin_imports")
    op.drop_table("fin_category_rules")
    op.drop_index(
        "uq_fin_categories_month_key_sub",
        table_name="fin_categories",
        postgresql_where=sa.text("parent_id is not null"),
    )
    op.drop_index(
        "uq_fin_categories_month_key_main",
        table_name="fin_categories",
        postgresql_where=sa.text("parent_id is null"),
    )
    op.drop_index("ix_fin_categories_period_month", table_name="fin_categories")
    op.drop_table("fin_categories")
    op.drop_table("fin_accounts")
