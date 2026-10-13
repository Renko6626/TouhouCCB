"""unified credit risk foundation

Revision ID: credit_foundation_20260930
Revises: fx_tables_20260928

WP1 基座（计划 §3.3）——**additive**，旧镜像可在升级后的库上继续运行：

- 新表 ``liquidation_run`` / ``liquidation_action``（每用户最多一个 active run）
- ``user.economic_version`` / ``user.credit_frozen``
- ``liquidation_events.run_id``（FK，SET NULL）/ ``liquidation_events.product``
- ``fx_pair.reduce_only``（F9：paused 默认仍是全停，迁移不改变既有语义）

autogenerate 方式：本机 SQLite 上用 `compare_metadata` 得到完整 diff 后人工渲染；
`alembic revision --autogenerate` 的渲染阶段在 SQLite 上会因批处理重建
``liquidation_events`` 时找不到尚未创建的 ``liquidation_run`` 而崩溃（环境限制，
见 wp-1-report.md），因此用 `alembic check` 验证"升级后 metadata 与库零差异"。

downgrade() 删新表 / 新列：**丢 run/action 运行记录，不影响资金**
（现金与债务始终只在 user/ledger_entry；删掉执行记录不会改变余额）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "credit_foundation_20260930"
down_revision: Union[str, Sequence[str], None] = "fx_tables_20260928"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# F12 冻结：每用户最多一个 active run。
_ACTIVE_RUN_WHERE = sa.text("status = 'active'")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "liquidation_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger_source", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_round", sa.Integer(), nullable=False),
        sa.Column("rounds", sa.Integer(), nullable=False),
        sa.Column("last_group_product", sa.String(length=8), nullable=True),
        sa.Column("last_group_id", sa.Integer(), nullable=True),
        sa.Column("last_blocked_reason", sa.String(length=255), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pre_cash", sa.Numeric(16, 6), nullable=False),
        sa.Column("pre_debt", sa.Numeric(16, 6), nullable=False),
        sa.Column("pre_liquidation_equity", sa.Numeric(16, 6), nullable=False),
        sa.Column("total_proceeds", sa.Numeric(16, 6), nullable=False),
        sa.Column("total_repaid", sa.Numeric(16, 6), nullable=False),
        sa.Column("total_fee", sa.Numeric(16, 6), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','recovered','insolvent','blocked','stopped')",
            name="ck_liquidation_run_status",
        ),
    )
    op.create_index("ix_liquidation_run_user_id", "liquidation_run", ["user_id"], unique=False)
    op.create_index(
        "ix_liquidation_run_status_updated", "liquidation_run", ["status", "updated_at"], unique=False
    )
    # 部分唯一索引：PostgreSQL 与 SQLite 都支持；MySQL 不支持（本项目不跑 MySQL 迁移）。
    op.create_index(
        "uq_liquidation_run_active_user",
        "liquidation_run",
        ["user_id"],
        unique=True,
        postgresql_where=_ACTIVE_RUN_WHERE,
        sqlite_where=_ACTIVE_RUN_WHERE,
    )

    op.create_table(
        "liquidation_action",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("liquidation_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("product", sa.String(length=8), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=True),
        sa.Column("requested", sa.JSON(), nullable=True),
        sa.Column("executed", sa.JSON(), nullable=True),
        sa.Column("proceeds", sa.Numeric(16, 6), nullable=False),
        sa.Column("fee", sa.Numeric(16, 6), nullable=False),
        sa.Column("fee_currency", sa.String(length=8), nullable=True),
        sa.Column("repaid", sa.Numeric(16, 6), nullable=False),
        sa.Column("debt_after", sa.Numeric(16, 6), nullable=False),
        sa.Column("cash_after", sa.Numeric(16, 6), nullable=False),
        sa.Column("economic_version_after", sa.Integer(), nullable=False),
        sa.Column("blocked_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "round_no", name="uq_liquidation_action_run_round"),
        sa.CheckConstraint(
            "kind IN ('sell_group','repay_cash','repay_only','blocked','stopped')",
            name="ck_liquidation_action_kind",
        ),
        sa.CheckConstraint(
            "fee_currency IS NULL OR fee_currency IN ('gold','foreign')",
            name="ck_liquidation_action_fee_currency",
        ),
    )
    op.create_index("ix_liquidation_action_run_id", "liquidation_action", ["run_id"], unique=False)
    op.create_index("ix_liquidation_action_user_id", "liquidation_action", ["user_id"], unique=False)
    op.create_index(
        "ix_liquidation_action_user_created",
        "liquidation_action",
        ["user_id", "created_at"],
        unique=False,
    )

    # SQLite 不支持 ALTER 加约束，统一走 batch（PG 下 batch 退化为普通 ALTER）。
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("economic_version", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("credit_frozen", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )

    with op.batch_alter_table("fx_pair", schema=None) as batch_op:
        # F9：新列默认 false —— 已存在的 paused pair 仍是"全停"，不会被无提示变成可卖。
        batch_op.add_column(
            sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )

    with op.batch_alter_table("liquidation_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("product", sa.String(length=8), nullable=True))
        batch_op.create_index("ix_liquidation_events_run_id", ["run_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_liquidation_events_run_id_liquidation_run",
            "liquidation_run",
            ["run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 丢失：``liquidation_run`` / ``liquidation_action`` 全部运行记录，以及新列自身的
    取值（``user.economic_version`` / ``user.credit_frozen`` / ``fx_pair.reduce_only`` /
    ``liquidation_events.run_id|product``）。**不改变任何资金**：``user.cash`` /
    ``user.debt`` / ``ledger_entry`` / 既有 liquidation_events 摘要行都不动。
    """
    # 先摘掉指向 liquidation_run 的外键列（batch 在 SQLite 上重建表，在 PG 上
    # DROP COLUMN 会自动带走 FK 与索引）。SQLite 批处理必须显式先 drop_index：
    # 否则重建表后会拿着已删除列的旧索引再次 CREATE INDEX 而报 no such column。
    with op.batch_alter_table("liquidation_events", schema=None) as batch_op:
        batch_op.drop_index("ix_liquidation_events_run_id")
        batch_op.drop_column("run_id")
        batch_op.drop_column("product")

    with op.batch_alter_table("fx_pair", schema=None) as batch_op:
        batch_op.drop_column("reduce_only")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("economic_version")
        batch_op.drop_column("credit_frozen")

    op.drop_index("ix_liquidation_action_user_created", table_name="liquidation_action")
    op.drop_index("ix_liquidation_action_user_id", table_name="liquidation_action")
    op.drop_index("ix_liquidation_action_run_id", table_name="liquidation_action")
    op.drop_table("liquidation_action")

    op.drop_index("uq_liquidation_run_active_user", table_name="liquidation_run")
    op.drop_index("ix_liquidation_run_status_updated", table_name="liquidation_run")
    op.drop_index("ix_liquidation_run_user_id", table_name="liquidation_run")
    op.drop_table("liquidation_run")
