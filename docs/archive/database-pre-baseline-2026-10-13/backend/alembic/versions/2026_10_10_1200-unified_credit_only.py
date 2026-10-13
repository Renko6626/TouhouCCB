"""Validate unified credit parameters and remove obsolete configuration only."""
from decimal import Decimal, InvalidOperation, localcontext
from alembic import op
import sqlalchemy as sa

revision = "unified_credit_only_20261010"
down_revision = "fx_remove_intervention_20261009"
branch_labels = None
depends_on = None

REMOVED = (
    "unified_credit_enabled", "loan_leverage_k", "liquidation_hard_threshold",
    "liquidation_soft_threshold", "liquidation_target_margin", "liquidation_emergency_threshold",
)


def upgrade():
    bind = op.get_bind()
    config = sa.table("siteconfig", sa.column("key", sa.String()), sa.column("value", sa.String()),
                      sa.column("value_type", sa.String()), sa.column("updated_at", sa.DateTime()))
    raw = dict(bind.execute(sa.select(config.c.key, config.c.value)).all())
    values = {}
    for key, legacy, default in (("credit_leverage", "loan_leverage_k", "2"),
                                 ("credit_maintenance_ratio", "liquidation_hard_threshold", "0.2")):
        if key in raw:
            values[key] = raw[key]
        elif legacy in raw:
            try:
                value = Decimal(raw[legacy])
                if not value.is_finite():
                    raise ValueError(f"{legacy} must be finite")
                values[key] = format(value + 1 if key == "credit_leverage" else value, "f")
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Invalid legacy credit configuration: {legacy}") from exc
        elif not raw:
            values[key] = default
        else:
            raise ValueError(f"Missing credit configuration: {key}")
    try:
        leverage = Decimal(values["credit_leverage"])
        maintenance = Decimal(values["credit_maintenance_ratio"])
        if not leverage.is_finite() or not maintenance.is_finite() or not 1 < leverage <= 50 or maintenance <= 0:
            raise ValueError("Invalid unified credit parameters")
        with localcontext() as ctx:
            ctx.prec = 28
            if maintenance >= Decimal(1) / (leverage - 1):
                raise ValueError("Maintenance must be below initial margin")
    except InvalidOperation as exc:
        raise ValueError("Invalid unified credit parameters") from exc
    for key, value in values.items():
        if key not in raw:
            bind.execute(config.insert().values(key=key, value=value, value_type="decimal", updated_at=sa.func.current_timestamp()))
    bind.execute(config.delete().where(config.c.key.in_(REMOVED)))


def downgrade():
    # Removed operator settings cannot be reconstructed safely. Unified values
    # remain intact so rollback never changes risk limits or business gates.
    pass
