"""Frozen full business schema, 2026-10-13.

This revision owns DDL; it does not import runtime model definitions.
SQLite integer primary keys use AUTOINCREMENT so reset can restore old ID watermarks.
"""
from alembic import op
import sqlalchemy as sa

revision = 'business_baseline_20261013'
down_revision = None
branch_labels = None
depends_on = None

# Frozen minimum schema contract used by read-only deployment preflight.
SCHEMA_TABLE_COLUMNS = {'audit_event': ('id',
                 'ts',
                 'event_type',
                 'user_id',
                 'market_id',
                 'outcome_id',
                 'operator_user_id',
                 'ref_table',
                 'ref_id',
                 'payload',
                 'user_after',
                 'position_after',
                 'market_after'),
 'bot_profile': ('id',
                 'user_id',
                 'template',
                 'params',
                 'status',
                 'market_scope',
                 'created_at',
                 'last_trade_at'),
 'bot_suspicion': ('id',
                   'user_id',
                   'triggered_at',
                   'signals',
                   'metrics',
                   'window_start',
                   'window_end',
                   'review_status',
                   'reviewed_by',
                   'reviewed_at',
                   'review_note'),
 'danmuku_exchange': ('id',
                      'user_id',
                      'qq_user_id',
                      'room_id',
                      'yuan',
                      'huo',
                      'amount',
                      'code_string',
                      'timestamp'),
 'fx_candle': ('pair_id',
               'interval',
               'bucket_start',
               'open_price',
               'high_price',
               'low_price',
               'close_price',
               'gold_volume',
               'n_trades',
               'first_event_at',
               'first_event_id',
               'last_event_at',
               'last_event_id',
               'n_events',
               'first_trade_at',
               'first_trade_id',
               'last_trade_at',
               'last_trade_id',
               'updated_at'),
 'fx_execution': ('id',
                  'user_id',
                  'pair_id',
                  'idempotency_key',
                  'request_payload',
                  'response_version',
                  'response_payload',
                  'created_at'),
 'fx_market_data_state': ('pair_id',
                          'last_event_id',
                          'history_version',
                          'history_ready',
                          'updated_at'),
 'fx_pair': ('id',
             'currency_code',
             'currency_name',
             'status',
             'archived',
             'gold_reserve',
             'foreign_reserve',
             'initial_price',
             'buy_fee_rate',
             'sell_fee_rate',
             'short_lending_limit_foreign',
             'pool_version',
             'reduce_only',
             'created_at',
             'updated_at'),
 'fx_pool_event': ('id',
                   'pair_id',
                   'created_at',
                   'kind',
                   'pre_gold_reserve',
                   'pre_foreign_reserve',
                   'post_gold_reserve',
                   'post_foreign_reserve',
                   'gold_volume',
                   'trade_id',
                   'audit_id'),
 'fx_short_position': ('id',
                       'user_id',
                       'pair_id',
                       'principal_foreign',
                       'interest_foreign',
                       'interest_last_accrued_at',
                       'restricted_gold',
                       'proceeds_basis_gold',
                       'created_at',
                       'updated_at'),
 'fx_trade': ('id',
              'execution_id',
              'execution_leg',
              'pair_id',
              'user_id',
              'purpose',
              'requested_foreign_amount',
              'borrow_amount',
              'max_gold_in',
              'cover_all',
              'side',
              'input_amount',
              'output_amount',
              'min_out',
              'fee_amount',
              'pre_gold_reserve',
              'pre_foreign_reserve',
              'post_gold_reserve',
              'post_foreign_reserve',
              'post_price',
              'source',
              'idempotency_key',
              'created_at'),
 'fx_treasury': ('id', 'pair_id', 'gold_balance', 'foreign_balance', 'updated_at'),
 'fx_wallet': ('id', 'user_id', 'pair_id', 'foreign_amount', 'cost_basis', 'updated_at'),
 'ledger_entry': ('id',
                  'user_id',
                  'entry_type',
                  'cash_delta',
                  'debt_delta',
                  'cash_after',
                  'debt_after',
                  'debt_last_accrued_at_after',
                  'daily_rate_at_event',
                  'operator_user_id',
                  'reason',
                  'created_at'),
 'liquidation_action': ('id',
                        'run_id',
                        'user_id',
                        'round_no',
                        'kind',
                        'product',
                        'group_id',
                        'mode',
                        'requested',
                        'executed',
                        'proceeds',
                        'gold_spent',
                        'foreign_repaid',
                        'short_after',
                        'fee',
                        'fee_currency',
                        'repaid',
                        'debt_after',
                        'cash_after',
                        'economic_version_after',
                        'blocked_reason',
                        'created_at'),
 'liquidation_events': ('id',
                        'user_id',
                        'triggered_at',
                        'pre_cash',
                        'pre_debt',
                        'pre_holdings_value',
                        'pre_net_worth',
                        'pre_margin_ratio',
                        'sold_positions_count',
                        'total_proceeds',
                        'repaid_amount',
                        'remaining_debt',
                        'post_cash',
                        'trigger_source',
                        'mode',
                        'run_id',
                        'product'),
 'liquidation_run': ('id',
                     'user_id',
                     'status',
                     'trigger_source',
                     'started_at',
                     'updated_at',
                     'next_round',
                     'rounds',
                     'last_group_product',
                     'last_group_id',
                     'last_blocked_reason',
                     'closed_at',
                     'pre_cash',
                     'pre_debt',
                     'pre_liquidation_equity',
                     'pre_short_positions',
                     'pre_risk_basis',
                     'pre_equity_to_risk_basis',
                     'margin_version',
                     'total_proceeds',
                     'total_repaid',
                     'total_fee'),
 'market': ('id',
            'title',
            'description',
            'liquidity_b',
            'status',
            'created_at',
            'closes_at',
            'tags',
            'winning_outcome_id',
            'settled_at',
            'settled_by_user_id'),
 'market_required_title': ('id', 'market_id', 'title_id'),
 'outcome': ('id', 'market_id', 'label', 'total_shares', 'initial_shares', 'payout'),
 'outcome_candle': ('outcome_id',
                    'interval',
                    'bucket_start',
                    'open_price',
                    'high_price',
                    'low_price',
                    'close_price',
                    'volume_shares',
                    'n_trades',
                    'updated_at'),
 'position': ('id', 'user_id', 'outcome_id', 'amount', 'cost_basis'),
 'redemption_batch': ('id',
                      'partner_id',
                      'name',
                      'description',
                      'unit_price',
                      'status',
                      'created_at',
                      'created_by_admin_id'),
 'redemption_code': ('id',
                     'batch_id',
                     'code_string',
                     'status',
                     'bought_by_user_id',
                     'bought_at',
                     'marked_used_by_user_at',
                     'redeemed_at',
                     'redeemed_by_admin_id',
                     'redemption_note'),
 'redemption_partner': ('id',
                        'name',
                        'description',
                        'website_url',
                        'logo_url',
                        'is_active',
                        'created_at'),
 'redemption_transaction': ('id',
                            'user_id',
                            'code_id',
                            'batch_id',
                            'partner_id',
                            'batch_name_snapshot',
                            'partner_name_snapshot',
                            'amount',
                            'timestamp'),
 'siteconfig': ('id', 'key', 'value', 'value_type', 'updated_at', 'updated_by'),
 'title': ('id', 'name', 'description', 'color', 'icon', 'sort_order', 'is_active', 'created_at'),
 'title_code': ('id', 'batch_id', 'code_string', 'status', 'used_by_user_id', 'used_at'),
 'title_code_batch': ('id', 'title_id', 'name', 'description', 'created_at', 'created_by_admin_id'),
 'transaction': ('id',
                 'user_id',
                 'outcome_id',
                 'type',
                 'shares',
                 'cost',
                 'fee',
                 'gross',
                 'price',
                 'pre_market_price',
                 'post_market_price',
                 'market_prices_post',
                 'timestamp'),
 'user': ('id',
          'casdoor_id',
          'username',
          'email',
          'is_active',
          'is_superuser',
          'is_bot',
          'cash',
          'debt',
          'debt_last_accrued_at',
          'tos_accepted_at',
          'last_liquidated_at',
          'economic_version',
          'credit_frozen',
          'equipped_title_id'),
 'user_title': ('id', 'user_id', 'title_id', 'granted_at', 'granted_by_admin_id', 'source')}


def upgrade():
    op.create_table('audit_event',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('market_id', sa.Integer(), nullable=True),
    sa.Column('outcome_id', sa.Integer(), nullable=True),
    sa.Column('operator_user_id', sa.Integer(), nullable=True),
    sa.Column('ref_table', sa.String(length=32), nullable=True),
    sa.Column('ref_id', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('user_after', sa.JSON(), nullable=True),
    sa.Column('position_after', sa.JSON(), nullable=True),
    sa.Column('market_after', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_audit_event_event_type'), 'audit_event', ['event_type'], unique=False)
    op.create_index(op.f('ix_audit_event_market_id'), 'audit_event', ['market_id'], unique=False)
    op.create_index('ix_audit_event_market_id_id', 'audit_event', ['market_id', 'id'], unique=False)
    op.create_index('ix_audit_event_ref_type_id', 'audit_event', ['ref_table', 'ref_id', 'event_type', 'id'], unique=False)
    op.create_index(op.f('ix_audit_event_ts'), 'audit_event', ['ts'], unique=False)
    op.create_index('ix_audit_event_type_id', 'audit_event', ['event_type', 'id'], unique=False)
    op.create_index(op.f('ix_audit_event_user_id'), 'audit_event', ['user_id'], unique=False)
    op.create_index('ix_audit_event_user_id_id', 'audit_event', ['user_id', 'id'], unique=False)
    op.create_table('fx_pair',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=16), nullable=False),
    sa.Column('currency_name', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('gold_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('foreign_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('initial_price', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('buy_fee_rate', sa.Numeric(precision=10, scale=8), nullable=False),
    sa.Column('sell_fee_rate', sa.Numeric(precision=10, scale=8), nullable=False),
    sa.Column('short_lending_limit_foreign', sa.Numeric(precision=24, scale=6), server_default=sa.text('0'), nullable=False),
    sa.Column('pool_version', sa.Integer(), nullable=False),
    sa.Column('reduce_only', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('draft','trading','paused','closed')", name='ck_fx_pair_status'),
    sa.CheckConstraint('buy_fee_rate >= 0 AND buy_fee_rate <= 1 AND sell_fee_rate >= 0 AND sell_fee_rate <= 1', name='ck_fx_pair_fee_rate'),
    sa.CheckConstraint('gold_reserve > 0 AND foreign_reserve > 0', name='ck_fx_pair_reserves_positive'),
    sa.CheckConstraint('short_lending_limit_foreign >= 0', name='ck_fx_pair_short_limit_non_negative'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('currency_code'),
    sqlite_autoincrement=True
    )
    op.create_index('ix_fx_pair_status', 'fx_pair', ['status'], unique=False)
    op.create_table('redemption_partner',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=False),
    sa.Column('website_url', sa.String(), nullable=False),
    sa.Column('logo_url', sa.String(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_redemption_partner_name'), 'redemption_partner', ['name'], unique=False)
    op.create_table('title',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('color', sa.String(length=16), nullable=False),
    sa.Column('icon', sa.String(length=16), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', name='uq_title_name'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_title_name'), 'title', ['name'], unique=False)
    op.create_index(op.f('ix_title_sort_order'), 'title', ['sort_order'], unique=False)
    op.create_table('fx_candle',
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('interval', sa.String(length=8), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('open_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('high_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('low_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('close_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('gold_volume', sa.Numeric(precision=30, scale=6), nullable=False),
    sa.Column('n_trades', sa.Integer(), nullable=False),
    sa.Column('first_event_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_event_id', sa.BigInteger(), nullable=True),
    sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_event_id', sa.BigInteger(), nullable=True),
    sa.Column('n_events', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('first_trade_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_trade_id', sa.Integer(), nullable=True),
    sa.Column('last_trade_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_trade_id', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("interval IN ('10s','1m','15m','1h')", name='ck_fx_candle_interval_supported'),
    sa.CheckConstraint('gold_volume >= 0', name='ck_fx_candle_volume_non_negative'),
    sa.CheckConstraint('high_price >= low_price', name='ck_fx_candle_h_ge_l'),
    sa.CheckConstraint('n_events >= 0', name='ck_fx_candle_events_non_negative'),
    sa.CheckConstraint('n_trades >= 0', name='ck_fx_candle_n_non_negative'),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.PrimaryKeyConstraint('pair_id', 'interval', 'bucket_start')
    )
    op.create_table('fx_market_data_state',
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('last_event_id', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('history_version', sa.String(length=36), nullable=False),
    sa.Column('history_ready', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('last_event_id >= 0', name='ck_fx_market_data_state_event_cursor_non_negative'),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.PrimaryKeyConstraint('pair_id'),
    sqlite_autoincrement=True
    )
    op.create_table('fx_treasury',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('gold_balance', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('foreign_balance', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('gold_balance >= 0 AND foreign_balance >= 0', name='ck_fx_treasury_balances_non_negative'),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('pair_id', name='uq_fx_treasury_pair'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_fx_treasury_pair_id'), 'fx_treasury', ['pair_id'], unique=False)
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('casdoor_id', sa.String(), nullable=True),
    sa.Column('username', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_superuser', sa.Boolean(), nullable=False),
    sa.Column('is_bot', sa.Boolean(), nullable=False),
    sa.Column('cash', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt_last_accrued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tos_accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_liquidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('economic_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('credit_frozen', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('equipped_title_id', sa.Integer(), nullable=True),
    sa.CheckConstraint('cash >= 0', name='ck_user_cash_non_negative'),
    sa.CheckConstraint('debt >= 0', name='ck_user_debt_non_negative'),
    sa.ForeignKeyConstraint(['equipped_title_id'], ['title.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_user_casdoor_id'), 'user', ['casdoor_id'], unique=True)
    op.create_index(op.f('ix_user_email'), 'user', ['email'], unique=True)
    op.create_index(op.f('ix_user_is_bot'), 'user', ['is_bot'], unique=False)
    op.create_index(op.f('ix_user_username'), 'user', ['username'], unique=True)
    op.create_table('bot_profile',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('template', sa.String(length=32), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('market_scope', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_trade_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_bot_profile_status'), 'bot_profile', ['status'], unique=False)
    op.create_index(op.f('ix_bot_profile_user_id'), 'bot_profile', ['user_id'], unique=True)
    op.create_table('bot_suspicion',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('signals', sa.String(length=200), nullable=False),
    sa.Column('metrics', sa.String(length=500), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('review_status', sa.String(length=20), nullable=False),
    sa.Column('reviewed_by', sa.Integer(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('review_note', sa.String(length=500), nullable=True),
    sa.ForeignKeyConstraint(['reviewed_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_bot_suspicion_triggered_at'), 'bot_suspicion', ['triggered_at'], unique=False)
    op.create_index(op.f('ix_bot_suspicion_user_id'), 'bot_suspicion', ['user_id'], unique=False)
    op.create_table('danmuku_exchange',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('qq_user_id', sa.String(length=32), nullable=False),
    sa.Column('room_id', sa.String(length=64), nullable=False),
    sa.Column('yuan', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('huo', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('code_string', sa.String(length=512), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_danmuku_exchange_timestamp'), 'danmuku_exchange', ['timestamp'], unique=False)
    op.create_index(op.f('ix_danmuku_exchange_user_id'), 'danmuku_exchange', ['user_id'], unique=False)
    op.create_table('fx_execution',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('request_payload', sa.JSON(), nullable=False),
    sa.Column('response_version', sa.Integer(), nullable=False),
    sa.Column('response_payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_fx_execution_user_key'),
    sqlite_autoincrement=True
    )
    op.create_table('fx_short_position',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('principal_foreign', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('interest_foreign', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('interest_last_accrued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('restricted_gold', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('proceeds_basis_gold', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('(principal_foreign + interest_foreign > 0 AND interest_last_accrued_at IS NOT NULL) OR (principal_foreign = 0 AND interest_foreign = 0 AND restricted_gold = 0 AND proceeds_basis_gold = 0 AND interest_last_accrued_at IS NULL)', name='ck_fx_short_position_state'),
    sa.CheckConstraint('principal_foreign >= 0 AND interest_foreign >= 0 AND restricted_gold >= 0 AND proceeds_basis_gold >= 0', name='ck_fx_short_position_non_negative'),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'pair_id', name='uq_fx_short_position_user_pair'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_fx_short_position_pair_id'), 'fx_short_position', ['pair_id'], unique=False)
    op.create_index(op.f('ix_fx_short_position_user_id'), 'fx_short_position', ['user_id'], unique=False)
    op.create_table('fx_wallet',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('foreign_amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('cost_basis', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('foreign_amount >= 0 AND cost_basis >= 0', name='ck_fx_wallet_non_negative'),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'pair_id', name='uq_fx_wallet_user_pair'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_fx_wallet_pair_id'), 'fx_wallet', ['pair_id'], unique=False)
    op.create_index(op.f('ix_fx_wallet_user_id'), 'fx_wallet', ['user_id'], unique=False)
    op.create_table('ledger_entry',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('entry_type', sa.String(length=32), nullable=False),
    sa.Column('cash_delta', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt_delta', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('cash_after', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt_after', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt_last_accrued_at_after', sa.DateTime(timezone=True), nullable=True),
    sa.Column('daily_rate_at_event', sa.Numeric(precision=16, scale=8), nullable=True),
    sa.Column('operator_user_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['operator_user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_ledger_entry_created_at'), 'ledger_entry', ['created_at'], unique=False)
    op.create_index('ix_ledger_entry_user_created', 'ledger_entry', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ledger_entry_user_id'), 'ledger_entry', ['user_id'], unique=False)
    op.create_table('liquidation_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('trigger_source', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('next_round', sa.Integer(), nullable=False),
    sa.Column('rounds', sa.Integer(), nullable=False),
    sa.Column('last_group_product', sa.String(length=8), nullable=True),
    sa.Column('last_group_id', sa.Integer(), nullable=True),
    sa.Column('last_blocked_reason', sa.String(length=255), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pre_cash', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_debt', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_liquidation_equity', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('pre_short_positions', sa.JSON(), nullable=True),
    sa.Column('pre_risk_basis', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('pre_equity_to_risk_basis', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('margin_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('total_proceeds', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('total_repaid', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('total_fee', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.CheckConstraint("status IN ('active','recovered','insolvent','blocked','stopped')", name='ck_liquidation_run_status'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index('ix_liquidation_run_status_updated', 'liquidation_run', ['status', 'updated_at'], unique=False)
    op.create_index(op.f('ix_liquidation_run_user_id'), 'liquidation_run', ['user_id'], unique=False)
    op.create_index('uq_liquidation_run_active_user', 'liquidation_run', ['user_id'], unique=True, postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"))
    op.create_table('market',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=False),
    sa.Column('liquidity_b', sa.Float(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closes_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tags', sa.String(), nullable=False),
    sa.Column('winning_outcome_id', sa.Integer(), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settled_by_user_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['settled_by_user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['winning_outcome_id'], ['outcome.id'], name='fk_market_winning_outcome', use_alter=True),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_market_closes_at'), 'market', ['closes_at'], unique=False)
    op.create_index(op.f('ix_market_settled_at'), 'market', ['settled_at'], unique=False)
    op.create_index(op.f('ix_market_settled_by_user_id'), 'market', ['settled_by_user_id'], unique=False)
    op.create_index(op.f('ix_market_title'), 'market', ['title'], unique=False)
    op.create_index(op.f('ix_market_winning_outcome_id'), 'market', ['winning_outcome_id'], unique=False)
    op.create_table('redemption_batch',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('partner_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_admin_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['partner_id'], ['redemption_partner.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_redemption_batch_partner_id'), 'redemption_batch', ['partner_id'], unique=False)
    op.create_index(op.f('ix_redemption_batch_status'), 'redemption_batch', ['status'], unique=False)
    op.create_table('siteconfig',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.Column('value_type', sa.String(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['updated_by'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', name='uq_siteconfig_key'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_siteconfig_key'), 'siteconfig', ['key'], unique=False)
    op.create_table('title_code_batch',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('description', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_admin_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['title_id'], ['title.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_title_code_batch_title_id'), 'title_code_batch', ['title_id'], unique=False)
    op.create_table('user_title',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('title_id', sa.Integer(), nullable=False),
    sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('granted_by_admin_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.CheckConstraint("source IN ('admin','code')", name='ck_user_title_source'),
    sa.ForeignKeyConstraint(['granted_by_admin_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['title_id'], ['title.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'title_id', name='uq_user_title'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_user_title_title_id'), 'user_title', ['title_id'], unique=False)
    op.create_index(op.f('ix_user_title_user_id'), 'user_title', ['user_id'], unique=False)
    op.create_table('fx_trade',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('execution_id', sa.Integer(), nullable=True),
    sa.Column('execution_leg', sa.Integer(), nullable=True),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('purpose', sa.String(length=16), server_default=sa.text("'spot'"), nullable=False),
    sa.Column('requested_foreign_amount', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('borrow_amount', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('max_gold_in', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('cover_all', sa.Boolean(), nullable=True),
    sa.Column('side', sa.String(length=8), nullable=False),
    sa.Column('input_amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('output_amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('min_out', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('fee_amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_gold_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_foreign_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('post_gold_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('post_foreign_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('post_price', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(purpose = 'borrow_buy' AND side = 'buy' AND borrow_amount IS NOT NULL AND borrow_amount > 0 AND borrow_amount <= input_amount) OR (purpose <> 'borrow_buy' AND borrow_amount IS NULL)", name='ck_fx_trade_borrow_amount'),
    sa.CheckConstraint("purpose IN ('spot','short_open','short_cover','borrow_buy')", name='ck_fx_trade_purpose'),
    sa.CheckConstraint("side IN ('buy','sell')", name='ck_fx_trade_side'),
    sa.CheckConstraint('(execution_id IS NULL AND execution_leg IS NULL) OR (execution_id IS NOT NULL AND execution_leg IS NOT NULL AND execution_leg IN (1,2) AND idempotency_key IS NULL)', name='ck_fx_trade_execution_link'),
    sa.CheckConstraint('input_amount > 0 AND output_amount > 0', name='ck_fx_trade_amounts_positive'),
    sa.CheckConstraint('pre_gold_reserve > 0 AND pre_foreign_reserve > 0 AND post_gold_reserve > 0 AND post_foreign_reserve > 0', name='ck_fx_trade_reserves_positive'),
    sa.ForeignKeyConstraint(['execution_id'], ['fx_execution.id'], ),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('execution_id', 'execution_leg', name='uq_fx_trade_execution_leg'),
    sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_fx_trade_user_idempotency'),
    sqlite_autoincrement=True
    )
    op.create_index('ix_fx_trade_created_at', 'fx_trade', ['created_at'], unique=False)
    op.create_index(op.f('ix_fx_trade_execution_id'), 'fx_trade', ['execution_id'], unique=False)
    op.create_index('ix_fx_trade_pair_created', 'fx_trade', ['pair_id', 'created_at'], unique=False)
    op.create_index('ix_fx_trade_pair_id', 'fx_trade', ['pair_id'], unique=False)
    op.create_index('ix_fx_trade_pair_id_id', 'fx_trade', ['pair_id', 'id'], unique=False)
    op.create_index('ix_fx_trade_user_id', 'fx_trade', ['user_id'], unique=False)
    op.create_table('liquidation_action',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('round_no', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('product', sa.String(length=8), nullable=True),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('mode', sa.String(length=8), nullable=True),
    sa.Column('requested', sa.JSON(), nullable=True),
    sa.Column('executed', sa.JSON(), nullable=True),
    sa.Column('proceeds', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('gold_spent', sa.Numeric(precision=16, scale=6), server_default=sa.text('0'), nullable=False),
    sa.Column('foreign_repaid', sa.Numeric(precision=24, scale=6), server_default=sa.text('0'), nullable=False),
    sa.Column('short_after', sa.JSON(), nullable=True),
    sa.Column('fee', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('fee_currency', sa.String(length=8), nullable=True),
    sa.Column('repaid', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('debt_after', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('cash_after', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('economic_version_after', sa.Integer(), nullable=False),
    sa.Column('blocked_reason', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("fee_currency IS NULL OR fee_currency IN ('gold','foreign')", name='ck_liquidation_action_fee_currency'),
    sa.CheckConstraint("kind IN ('sell_group','cover_group','repay_cash','repay_only','blocked','stopped')", name='ck_liquidation_action_kind'),
    sa.ForeignKeyConstraint(['run_id'], ['liquidation_run.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'round_no', name='uq_liquidation_action_run_round'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_liquidation_action_run_id'), 'liquidation_action', ['run_id'], unique=False)
    op.create_index('ix_liquidation_action_user_created', 'liquidation_action', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_liquidation_action_user_id'), 'liquidation_action', ['user_id'], unique=False)
    op.create_table('liquidation_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('pre_cash', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_debt', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('pre_holdings_value', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('pre_net_worth', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('pre_margin_ratio', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('sold_positions_count', sa.Integer(), nullable=False),
    sa.Column('total_proceeds', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('repaid_amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('remaining_debt', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('post_cash', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('trigger_source', sa.String(), nullable=False),
    sa.Column('mode', sa.String(length=20), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('product', sa.String(length=8), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['liquidation_run.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_liquidation_events_run_id'), 'liquidation_events', ['run_id'], unique=False)
    op.create_index(op.f('ix_liquidation_events_triggered_at'), 'liquidation_events', ['triggered_at'], unique=False)
    op.create_index(op.f('ix_liquidation_events_user_id'), 'liquidation_events', ['user_id'], unique=False)
    op.create_table('market_required_title',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('title_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['market_id'], ['market.id'], ),
    sa.ForeignKeyConstraint(['title_id'], ['title.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('market_id', 'title_id', name='uq_market_required_title'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_market_required_title_market_id'), 'market_required_title', ['market_id'], unique=False)
    op.create_index(op.f('ix_market_required_title_title_id'), 'market_required_title', ['title_id'], unique=False)
    op.create_table('outcome',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(), nullable=False),
    sa.Column('total_shares', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('initial_shares', sa.Numeric(precision=16, scale=6), server_default='0', nullable=False),
    sa.Column('payout', sa.Numeric(precision=16, scale=8), nullable=True),
    sa.ForeignKeyConstraint(['market_id'], ['market.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_outcome_market_id'), 'outcome', ['market_id'], unique=False)
    op.create_table('redemption_code',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('code_string', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('bought_by_user_id', sa.Integer(), nullable=True),
    sa.Column('bought_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('marked_used_by_user_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('redeemed_by_admin_id', sa.Integer(), nullable=True),
    sa.Column('redemption_note', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['redemption_batch.id'], ),
    sa.ForeignKeyConstraint(['bought_by_user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['redeemed_by_admin_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_string', name='uq_redemption_code_string'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_redemption_code_batch_id'), 'redemption_code', ['batch_id'], unique=False)
    op.create_index('ix_redemption_code_batch_status', 'redemption_code', ['batch_id', 'status'], unique=False)
    op.create_index(op.f('ix_redemption_code_bought_by_user_id'), 'redemption_code', ['bought_by_user_id'], unique=False)
    op.create_index(op.f('ix_redemption_code_redeemed_at'), 'redemption_code', ['redeemed_at'], unique=False)
    op.create_table('title_code',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('code_string', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('used_by_user_id', sa.Integer(), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('available','used')", name='ck_title_code_status'),
    sa.ForeignKeyConstraint(['batch_id'], ['title_code_batch.id'], ),
    sa.ForeignKeyConstraint(['used_by_user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_string', name='uq_title_code_string'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_title_code_batch_id'), 'title_code', ['batch_id'], unique=False)
    op.create_index('ix_title_code_batch_status', 'title_code', ['batch_id', 'status'], unique=False)
    op.create_index(op.f('ix_title_code_used_by_user_id'), 'title_code', ['used_by_user_id'], unique=False)
    op.create_table('fx_pool_event',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('pre_gold_reserve', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('pre_foreign_reserve', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('post_gold_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('post_foreign_reserve', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('gold_volume', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('trade_id', sa.Integer(), nullable=True),
    sa.Column('audit_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.CheckConstraint("(kind = 'initialize' AND pre_gold_reserve IS NULL AND pre_foreign_reserve IS NULL) OR (kind <> 'initialize' AND pre_gold_reserve IS NOT NULL AND pre_foreign_reserve IS NOT NULL AND pre_gold_reserve > 0 AND pre_foreign_reserve > 0)", name='ck_fx_pool_event_pre_positive'),
    sa.CheckConstraint("(kind = 'trade' AND trade_id IS NOT NULL AND audit_id IS NULL) OR (kind <> 'trade' AND trade_id IS NULL AND audit_id IS NOT NULL)", name='ck_fx_pool_event_source'),
    sa.CheckConstraint("gold_volume >= 0 AND (kind = 'trade' OR gold_volume = 0)", name='ck_fx_pool_event_volume'),
    sa.CheckConstraint("kind IN ('initialize','trade','fund','withdraw')", name='ck_fx_pool_event_kind'),
    sa.CheckConstraint('post_gold_reserve > 0 AND post_foreign_reserve > 0', name='ck_fx_pool_event_post_positive'),
    sa.ForeignKeyConstraint(['audit_id'], ['audit_event.id'], ),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['trade_id'], ['fx_trade.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('audit_id', name='uq_fx_pool_event_audit'),
    sa.UniqueConstraint('trade_id', name='uq_fx_pool_event_trade'),
    sqlite_autoincrement=True
    )
    op.create_index('ix_fx_pool_event_pair_created_id', 'fx_pool_event', ['pair_id', 'created_at', 'id'], unique=False)
    op.create_index('ix_fx_pool_event_pair_id', 'fx_pool_event', ['pair_id', 'id'], unique=False)
    op.create_table('outcome_candle',
    sa.Column('outcome_id', sa.Integer(), nullable=False),
    sa.Column('interval', sa.String(length=8), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('open_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('high_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('low_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('close_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('volume_shares', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('n_trades', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("interval IN ('10s', '1m', '15m', '1h')", name='ck_candle_interval_supported'),
    sa.CheckConstraint('high_price >= low_price', name='ck_candle_h_ge_l'),
    sa.CheckConstraint('n_trades >= 0', name='ck_candle_n_non_negative'),
    sa.CheckConstraint('volume_shares >= 0', name='ck_candle_volume_non_negative'),
    sa.ForeignKeyConstraint(['outcome_id'], ['outcome.id'], ),
    sa.PrimaryKeyConstraint('outcome_id', 'interval', 'bucket_start')
    )
    op.create_table('position',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('outcome_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('cost_basis', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.CheckConstraint('amount >= 0', name='ck_position_amount_non_negative'),
    sa.ForeignKeyConstraint(['outcome_id'], ['outcome.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'outcome_id', name='uq_position_user_outcome'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_position_outcome_id'), 'position', ['outcome_id'], unique=False)
    op.create_index(op.f('ix_position_user_id'), 'position', ['user_id'], unique=False)
    op.create_table('redemption_transaction',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('code_id', sa.Integer(), nullable=True),
    sa.Column('batch_id', sa.Integer(), nullable=True),
    sa.Column('partner_id', sa.Integer(), nullable=True),
    sa.Column('batch_name_snapshot', sa.String(), nullable=False),
    sa.Column('partner_name_snapshot', sa.String(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['redemption_batch.id'], ),
    sa.ForeignKeyConstraint(['code_id'], ['redemption_code.id'], ),
    sa.ForeignKeyConstraint(['partner_id'], ['redemption_partner.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_redemption_transaction_batch_id'), 'redemption_transaction', ['batch_id'], unique=False)
    op.create_index(op.f('ix_redemption_transaction_code_id'), 'redemption_transaction', ['code_id'], unique=False)
    op.create_index(op.f('ix_redemption_transaction_partner_id'), 'redemption_transaction', ['partner_id'], unique=False)
    op.create_index(op.f('ix_redemption_transaction_timestamp'), 'redemption_transaction', ['timestamp'], unique=False)
    op.create_index(op.f('ix_redemption_transaction_user_id'), 'redemption_transaction', ['user_id'], unique=False)
    op.create_table('transaction',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('outcome_id', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('shares', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('cost', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('fee', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('gross', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('pre_market_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('post_market_price', sa.Numeric(precision=16, scale=8), nullable=False),
    sa.Column('market_prices_post', sa.JSON(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['outcome_id'], ['outcome.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_transaction_outcome_id'), 'transaction', ['outcome_id'], unique=False)
    op.create_index('ix_transaction_outcome_timestamp', 'transaction', ['outcome_id', 'timestamp'], unique=False)
    op.create_index(op.f('ix_transaction_timestamp'), 'transaction', ['timestamp'], unique=False)
    op.create_index(op.f('ix_transaction_user_id'), 'transaction', ['user_id'], unique=False)
    op.create_index('ix_transaction_user_timestamp', 'transaction', ['user_id', 'timestamp'], unique=False)
    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key('fk_market_winning_outcome', 'market', 'outcome', ['winning_outcome_id'], ['id'])


def downgrade():
    connection = op.get_bind()
    # Refuse data loss before executing any DDL.
    for name in ['audit_event', 'fx_pair', 'redemption_partner', 'title', 'fx_candle', 'fx_market_data_state', 'fx_treasury', 'user', 'bot_profile', 'bot_suspicion', 'danmuku_exchange', 'fx_execution', 'fx_short_position', 'fx_wallet', 'ledger_entry', 'liquidation_run', 'market', 'redemption_batch', 'siteconfig', 'title_code_batch', 'user_title', 'fx_trade', 'liquidation_action', 'liquidation_events', 'market_required_title', 'outcome', 'redemption_code', 'title_code', 'fx_pool_event', 'outcome_candle', 'position', 'redemption_transaction', 'transaction']:
        table = sa.table(name)
        if connection.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first():
            raise RuntimeError('Refusing populated business baseline downgrade')
    if connection.dialect.name != 'sqlite':
        op.drop_constraint('fk_market_winning_outcome', 'market', type_='foreignkey')
    op.drop_table('transaction')
    op.drop_table('redemption_transaction')
    op.drop_table('position')
    op.drop_table('outcome_candle')
    op.drop_table('fx_pool_event')
    op.drop_table('title_code')
    op.drop_table('redemption_code')
    op.drop_table('outcome')
    op.drop_table('market_required_title')
    op.drop_table('liquidation_events')
    op.drop_table('liquidation_action')
    op.drop_table('fx_trade')
    op.drop_table('user_title')
    op.drop_table('title_code_batch')
    op.drop_table('siteconfig')
    op.drop_table('redemption_batch')
    op.drop_table('market')
    op.drop_table('liquidation_run')
    op.drop_table('ledger_entry')
    op.drop_table('fx_wallet')
    op.drop_table('fx_short_position')
    op.drop_table('fx_execution')
    op.drop_table('danmuku_exchange')
    op.drop_table('bot_suspicion')
    op.drop_table('bot_profile')
    op.drop_table('user')
    op.drop_table('fx_treasury')
    op.drop_table('fx_market_data_state')
    op.drop_table('fx_candle')
    op.drop_table('title')
    op.drop_table('redemption_partner')
    op.drop_table('fx_pair')
    op.drop_table('audit_event')
