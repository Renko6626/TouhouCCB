"""add title system catalog codes user-title market-gating

Revision ID: e1f8319139a1
Revises: 679d34cb5986
Create Date: 2026-05-23 22:31:07.305237

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f8319139a1'
down_revision: Union[str, Sequence[str], None] = '679d34cb5986'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. title (parent，最先建)
    op.create_table(
        "title",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("color", sa.String(16), nullable=False, server_default="#000000"),
        sa.Column("icon", sa.String(16), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("name", name="uq_title_name"),
    )
    op.create_index("ix_title_name", "title", ["name"])
    op.create_index("ix_title_sort_order", "title", ["sort_order"])

    # 2. title_code_batch
    op.create_table(
        "title_code_batch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["title_id"], ["title.id"],
                                name="fk_title_code_batch_title"),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["user.id"],
                                name="fk_title_code_batch_admin"),
    )
    op.create_index("ix_title_code_batch_title_id", "title_code_batch", ["title_id"])

    # 3. title_code
    op.create_table(
        "title_code",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("code_string", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="available"),
        sa.Column("used_by_user_id", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["title_code_batch.id"],
                                name="fk_title_code_batch"),
        sa.ForeignKeyConstraint(["used_by_user_id"], ["user.id"],
                                name="fk_title_code_user"),
        sa.UniqueConstraint("code_string", name="uq_title_code_string"),
        sa.CheckConstraint("status IN ('available','used')", name="ck_title_code_status"),
    )
    op.create_index("ix_title_code_batch_status", "title_code", ["batch_id", "status"])
    op.create_index("ix_title_code_used_by_user_id", "title_code", ["used_by_user_id"])

    # 4. user_title
    op.create_table(
        "user_title",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("granted_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"],
                                name="fk_user_title_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["title_id"], ["title.id"],
                                name="fk_user_title_title", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_admin_id"], ["user.id"],
                                name="fk_user_title_admin"),
        sa.UniqueConstraint("user_id", "title_id", name="uq_user_title"),
        sa.CheckConstraint("source IN ('admin','code')", name="ck_user_title_source"),
    )
    op.create_index("ix_user_title_user_id", "user_title", ["user_id"])
    op.create_index("ix_user_title_title_id", "user_title", ["title_id"])

    # 5. market_required_title
    op.create_table(
        "market_required_title",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["market.id"],
                                name="fk_mrt_market", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["title_id"], ["title.id"],
                                name="fk_mrt_title", ondelete="CASCADE"),
        sa.UniqueConstraint("market_id", "title_id", name="uq_market_required_title"),
    )
    op.create_index("ix_mrt_market_id", "market_required_title", ["market_id"])
    op.create_index("ix_mrt_title_id", "market_required_title", ["title_id"])

    # 6. user.equipped_title_id (FK 依赖 title 已建)
    # 用 batch_alter_table 兼容 sqlite (ALTER constraint 在 sqlite 走 copy-and-move)；
    # postgres 下等价于普通 ALTER。
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column("equipped_title_id", sa.Integer(), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_user_equipped_title", "title",
            ["equipped_title_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint("fk_user_equipped_title", type_="foreignkey")
        batch_op.drop_column("equipped_title_id")

    op.drop_index("ix_mrt_title_id", table_name="market_required_title")
    op.drop_index("ix_mrt_market_id", table_name="market_required_title")
    op.drop_table("market_required_title")

    op.drop_index("ix_user_title_title_id", table_name="user_title")
    op.drop_index("ix_user_title_user_id", table_name="user_title")
    op.drop_table("user_title")

    op.drop_index("ix_title_code_used_by_user_id", table_name="title_code")
    op.drop_index("ix_title_code_batch_status", table_name="title_code")
    op.drop_table("title_code")

    op.drop_index("ix_title_code_batch_title_id", table_name="title_code_batch")
    op.drop_table("title_code_batch")

    op.drop_index("ix_title_sort_order", table_name="title")
    op.drop_index("ix_title_name", table_name="title")
    op.drop_table("title")
