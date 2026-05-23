# Title 系统设计

> **Status**: design approved, awaiting plan
> **Date**: 2026-05-23
> **Owner**: Renko6626

## 一、目标

给 TouhouCCB 加入**永久的用户称号（title）系统**：

1. 用户可拥有**多个** title（多对多关系），但一次只能**佩戴一个**（装备制）
2. title 获取来源两条路：
   - **管理员手动授予**（admin UI 操作）
   - **激活码兑换**（管理员预先 CSV 导入唯一码，用过即废）
3. 部分市场可设置**称号门槛**："仅持有 XX 称号的用户可下单买入" — 多个 title 时 ANY-of 语义（任一即可）
4. 顺手把"单用户管理"散落各处的 admin 操作整合到一个新的 `/admin/users` 页

## 二、非目标

- title 之间不做包含关系 / rarity 数值 / 等级 / 经验槽
- 不做 title 转赠系统（自己用掉就是自己的）
- 不做 title 自动颁发（基于行为/数据触发） — 全部走 admin / code
- 不做硬删 title（is_active=false 软删即可）
- 不做 title 历史/审计页 — log 写入即可，前端不专门做视图
- 卖出 / 结算 / 派彩 **不** 受称号门槛限制（保留用户对持仓的清算/止损权利）

---

## 三、Schema

### 3.1 新增 5 张表 + User 加 1 列

```python
# backend/app/models/base.py (红线文件，走 alembic autogenerate)

class Title(SQLModel, table=True):
    __tablename__ = "title"
    __table_args__ = (UniqueConstraint("name", name="uq_title_name"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=32, index=True)
    description: str = Field(default="", max_length=200)
    color: str = Field(default="#000000", max_length=16)
    icon: str = Field(default="", max_length=16)
    sort_order: int = Field(default=100, index=True)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


class UserTitle(SQLModel, table=True):
    __tablename__ = "user_title"
    __table_args__ = (UniqueConstraint("user_id", "title_id", name="uq_user_title"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    granted_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")
    source: str = Field(max_length=16)  # 'admin' | 'code'


class TitleCodeBatch(SQLModel, table=True):
    __tablename__ = "title_code_batch"
    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    name: str = Field(nullable=False, max_length=64)
    description: str = Field(default="", max_length=200)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    created_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")


class TitleCode(SQLModel, table=True):
    __tablename__ = "title_code"
    __table_args__ = (
        UniqueConstraint("code_string", name="uq_title_code_string"),
        Index("ix_title_code_batch_status", "batch_id", "status"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="title_code_batch.id", index=True, nullable=False)
    code_string: str = Field(nullable=False, max_length=64)
    status: str = Field(default="available", nullable=False)  # 'available' | 'used'
    used_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    used_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class MarketRequiredTitle(SQLModel, table=True):
    """市场 → 允许交易的 title 名单（ANY-of 语义）。空 = 任何人可交易。"""
    __tablename__ = "market_required_title"
    __table_args__ = (
        UniqueConstraint("market_id", "title_id", name="uq_market_required_title"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: int = Field(foreign_key="market.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)


# User 表加列
class User(SQLModel, table=True):
    # ... 既有字段不动 ...
    equipped_title_id: Optional[int] = Field(
        default=None,
        sa_column=Column(ForeignKey("title.id", ondelete="SET NULL"), nullable=True),
    )
```

### 3.2 关系映射策略

所有新表的 `List[...]` 反向关系**一律** `lazy="raise_on_sql"`（守 CLAUDE.md hot path 性能护栏）。
读 user.user_titles / market.required_titles 时必须显式 `selectinload(...)`。

---

## 四、字段语义细则

- `title.color`: hex 字符串（如 `#FFD700`），前端 chip 背景；contrast 文字色前端 auto 选黑/白
- `title.icon`: emoji 或 1-2 个 ASCII 字符（如 `★`），可空字符串 = 无 icon
- `title.sort_order`: ASC 排序（小者优先），用于 leaderboard chip 排序、`my-titles` 展示顺序
- `title.is_active`: 软删开关
  - `false` 时：禁止新发激活码、禁止新手动授予、禁止激活码兑换
  - **不** 影响：现有持有者继续佩戴、market_required_title 关系继续 gate 市场
- `user_title.source`: `'admin'` 或 `'code'`（CHECK 约束守护），便于审计
- `title_code.status`: `'available'` 或 `'used'`；`used` 时 `used_by_user_id` + `used_at` 必非空
- `user.equipped_title_id`: nullable FK → title.id，`ondelete="SET NULL"` 兜底（title 硬删时 DB 自动清）
- `market_required_title`: 空集合 = 该市场无门槛；非空 = ANY-of（用户需持有任一）

---

## 五、API surface

### 5.1 用户自助

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/title/me` | 我的全部 title + equipped 状态 |
| POST | `/api/v1/title/me/equip` | `{title_id: int \| null}` 切换佩戴 / null=取下 |
| POST | `/api/v1/title/redeem` | `{code: str}` 兑换激活码 |
| GET | `/api/v1/title/catalog` | 公开 catalog（含 is_active=true 的全部 title） |
| GET | `/api/v1/title/users/{user_id}/equipped` | 别人佩戴的 title（leaderboard / 翻车墙渲染用） |

### 5.2 Admin: title catalog

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/titles` | 全部 title（含 is_active=false） |
| POST | `/api/v1/admin/titles` | 创建 title |
| PATCH | `/api/v1/admin/titles/{title_id}` | 改 name/description/color/icon/sort_order/is_active |

> 不开硬删 endpoint。

### 5.3 Admin: 激活码 batch

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/title-batches` | 全部 batch 含 used/total 计数 |
| POST | `/api/v1/admin/title-batches` | 创建 batch（指定 title_id + name + description） |
| POST | `/api/v1/admin/title-batches/{batch_id}/import-codes` | CSV multipart 上传，整批 reject 校验 |
| GET | `/api/v1/admin/title-batches/{batch_id}/codes` | 看 batch 内 code 列表（分页） |

### 5.4 Admin: 单用户 title 管理 + 资产快照

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/users/{user_id}/summary` | 资产快照（cash/debt/net_worth/equipped_title/...），复用 `/user/summary` 计算逻辑但允许传 user_id |
| GET | `/api/v1/admin/users/{user_id}/titles` | 列出该用户 title |
| POST | `/api/v1/admin/users/{user_id}/titles` | `{title_id}` 授予（幂等） |
| DELETE | `/api/v1/admin/users/{user_id}/titles/{title_id}` | 撤销（同事务清 equipped_title_id 若匹配） |

### 5.5 Admin: 市场门槛

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/markets/{market_id}/required-titles` | 当前列表 |
| PUT | `/api/v1/admin/markets/{market_id}/required-titles` | `{title_ids: [int]}` 整体覆写（单事务 delete-all + insert-all） |

### 5.6 改动的现有 endpoint

```python
# backend/app/api/v1/market.py
# buy endpoint 在进入 LMSR 计价前调用 gate
async def _check_user_can_trade_market(db, user, market_id) -> None:
    """
    ANY-of 语义。
    若 market 无 required_titles 直接返回；否则 user_title 与 required 集合
    至少有一交集，否则抛 403 detail=MARKET_TITLE_REQUIRED + payload。
    单条 SQL 完成（不破坏 hot path 性能）。
    """

# GET /market/{id} 详情响应加字段：
{
  "required_titles": [{"id":..,"name":..,"color":..,"icon":..}],
  "user_can_trade": bool,  # 后端代算，前端不再算
  # ... 既有字段不动 ...
}

# GET /market 列表响应加字段：
{
  "required_titles": [...],  # 简略
  # ... 既有字段不动 ...
}

# GET /user/summary 响应加字段：
{
  "equipped_title": {"id":..,"name":..,"color":..,"icon":..} | null,
  "all_titles": [{...}],  # 按 sort_order asc
  # ... 既有字段不动 ...
}

# leaderboard / 翻车墙等已返回 user_id+username 的 endpoint：
# 在每条记录加 equipped_title: {id,name,color,icon} | null
```

### 5.7 限速与权限

- `/title/redeem` 走 `/auth` 同档限速 5r/s（防爆破）
- 所有 `/api/v1/admin/*` 走 `current_superuser` Depends
- CSV multipart 大小限制 1MB
- CSV 内容 hardcap 5000 行

---

## 六、边界 case 总表

| 编号 | 场景 | 策略 |
|---|---|---|
| a | 兑已使用的 code | 403 "激活码无效"（**与不存在同措辞，防探测**） |
| b | 兑不存在 code | 403 "激活码无效" |
| c | 兑自己已持有 title 的另一 code | 403 "你已拥有此称号"，**code 不消耗** |
| d | 兑成功不自动佩戴 | toast "获得新称号：XX，可在个人页佩戴"；equipped_title_id 保持原值 |
| e | admin 给已持有 title 的用户再加 | 幂等 200，不重复 insert |
| f | admin 撤销用户未持有的 title | 404 "该用户未持有此称号" |
| g | title 设为 is_active=false | 已持有用户保留 + 仍可佩戴 + market 门槛仍生效；新发码 / 新授予 / 兑换被拒 |
| h | CSV 含重复（与库或与文件） | 整批 reject："第 N 行 code XYZ 与已有冲突" |
| i | CSV 超 5000 行 | 400 hardcap |
| j | code_string 格式 | 长度 4-64，`[A-Za-z0-9\-_]` |
| k | title.name UNIQUE | DB 层 UNIQUE，违反返 409 |
| l | market 删除 | market_required_title CASCADE 自动清 |
| m | title 硬删 | 不开 UI；若 SQL 强删，user_title / market_required_title / title_code / title_code_batch CASCADE，user.equipped_title_id 因 FK SET NULL 自动清 |
| n | admin 撤销用户当前佩戴的 title | 同事务清 equipped_title_id → NULL（B1 语义） |
| o | market_required_title PUT 覆写 | 单事务内 DELETE old + INSERT new，避免中间态有人趁机交易 |

---

## 七、前端

### 7.1 新页面

```
src/pages/admin/UserManage.vue          /admin/users        用户管理总枢纽
src/pages/admin/TitleCatalog.vue        /admin/titles       title 目录 CRUD
src/pages/admin/TitleCodeBatches.vue    /admin/title-codes  batch 列表 + CSV 导入
src/pages/redeem/RedeemTitle.vue        /redeem-title       用户兑换（一级菜单）
```

### 7.2 改动的现有页面

| 文件 | 变更 |
|---|---|
| `src/pages/profile/Profile.vue` | 加"我的称号"区（列表 + equip 切换） |
| `src/pages/admin/MarketManage.vue` | 编辑市场表单加 multi-select "需要的称号" |
| `src/components/leaderboard/*.vue` | username 旁加 TitleChip |
| `src/components/layout/AppNavbar.vue` | 自己用户名旁加 TitleChip |
| `src/pages/market/Market.vue`（或同等） | 不达标用户：顶部黄条 + buy 按钮 disabled；卡片右上角 🔒 chip |
| `src/pages/liquidation/*` 翻车墙 | 用户列旁加 TitleChip |

### 7.3 新组件

```
src/components/title/TitleChip.vue              单 title 渲染（bg=color, contrast 文字色，icon+name，2px 黑边）
src/components/title/MyTitlesPanel.vue          "我的称号" 列表 + equip 切换（profile 用）
src/components/title/RequiredTitlesBadge.vue    市场卡片右上角 🔒 chip
```

### 7.4 新 API client / store

```
src/api/title.ts                                包装 5.1-5.5 全部 endpoint
src/api/admin.ts                                追加 title catalog / batch / user-title
src/stores/title.ts                             catalog 公开缓存（chip 渲染共享）
```

### 7.5 侧边栏

- 用户侧：新增"称号兑换" → `/redeem-title`
- Admin 侧：新增 3 项：用户管理 / 称号目录 / 激活码批次

### 7.6 错误识别

前端 axios interceptor 识别 `MARKET_TITLE_REQUIRED` marker，弹 toast：
"此市场仅限以下称号交易：{required_titles[].name 拼接}"

---

## 八、`/admin/users` 整合范围

| 操作 | 复用的现有 API | UI 来源 |
|---|---|---|
| 用户列表 + 搜索（username/id） | `GET /user/list` | 新做 |
| 资产快照（cash/debt/net_worth/title） | `GET /admin/users/{user_id}/summary`（新增，见 §5.4） | 新做 |
| 单用户调现金 | `POST /user/{id}/adjust-cash` | 已有 API，**前端无 UI**，现在补上 |
| 强制放贷 | `POST /user/{id}/force-loan` | 已有 API，前端 UI 散在他处 / 现整合 |
| 免债 | `POST /user/{id}/forgive-debt` | 已有 API |
| 封号 / 解封 | `PATCH /user/{id}/ban` `/unban` | 复用 BotReviewBan 调用，本页作为主入口 |
| 提 / 撤管理员 | `PATCH /user/{id}/admin` | 已有 API，前端无 UI，现在补上 |
| Title 授予 / 撤销 | 5.4 新增 endpoints | 本次新做 |
| 持仓 / 交易历史 | — | **跳转链接**，不内嵌 |

**老页处理**：
- `BotReviewBan` 保留作"Bot 嫌疑视角"专用页，封号入口仍可用但不再是主入口
- `BatchAdjustCash` 保留（批量场景独立，有 dry_run 流程）

---

## 九、Alembic 迁移

加固版 — 详细 upgrade/downgrade 代码见 §9.2。

### 9.1 关键要点

1. **PG DDL 默认事务化** — 任一步失败整体回滚
2. **不直接合 autogen 输出**：手抄成"全显式命名"版（约束名、索引名、FK 名都写死），downgrade 才能精确 drop
3. **server_default 给所有 NOT NULL 列**：迁移瞬间 INSERT 不报 NULL violation
4. **建表顺序严格 parent → child**；downgrade 严格反向
5. **FK 显式 ondelete**：
   - `user.equipped_title_id` → SET NULL（title 硬删不爆 user）
   - `user_title` / `market_required_title` → CASCADE
   - `title_code.used_by_user_id` → 无 ondelete（业务约束禁删用户）
6. **CHECK constraint** 守 `user_title.source IN ('admin','code')`
7. **本地预演**：`upgrade head → downgrade -1 → upgrade head` 三连，确认幂等
8. **生产无须停机**：全新表 + nullable column + FK，不锁现有交易表
9. **迁移文件不 INSERT 任何 row**，避免数据耦合

### 9.2 迁移文件结构

```python
# backend/alembic/versions/2026_05_23_XXXX-add_title_system.py
"""add title system: catalog/codes/user-title/market-gating"""
from alembic import op
import sqlalchemy as sa

revision = "<generated_by_alembic>"
down_revision = "679d34cb5986"  # 接 partial_liq mode 之后
branch_labels = None
depends_on = None


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

    # 6. user.equipped_title_id (最后加列，FK 依赖 title 已建)
    op.add_column(
        "user",
        sa.Column("equipped_title_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_equipped_title", "user", "title",
        ["equipped_title_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_user_equipped_title", "user", type_="foreignkey")
    op.drop_column("user", "equipped_title_id")

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
```

---

## 十、测试

### 10.1 后端 pytest

```
backend/tests/test_title_catalog.py
  - admin 创建 / 改 title
  - 重名 title → 409
  - 软删 is_active=false：旧持有保留 + 仍可佩戴 + market gating 生效；新发码 / 新授予被拒
  - 非超管访问 admin endpoint → 403

backend/tests/test_title_code.py
  - admin 建 batch → 上传 CSV → 落库 available
  - CSV 含已有 code → 整批 reject
  - CSV 自身重复 → 整批 reject
  - CSV 格式不合法（空格/非 ascii） → 整批 reject
  - CSV 超 5000 行 → 400
  - 用户兑 available → user_title + 1 + code.status=used + used_by/used_at 写入
  - 兑已用 → 403 invalid（措辞 a）
  - 兑不存在 → 403 invalid（同措辞）
  - 兑自己已持有的 title 另一 code → 403 own，code 不消耗
  - 兑成功不自动佩戴

backend/tests/test_title_equip.py
  - equip own title → 写入 equipped_title_id
  - equip 未持有 title → 403
  - equip null → 取下
  - admin 撤销当前 equipped title → equipped_title_id 同事务清空

backend/tests/test_title_admin_user.py
  - admin 加已持有 title → 幂等 200
  - admin 撤未持有 title → 404
  - admin endpoint 全 superuser gate

backend/tests/test_market_gating.py
  - 无 required → 任何人买入通过
  - 有 required，用户无任一 → buy 403 detail=MARKET_TITLE_REQUIRED
  - 有 required，用户有任一 → buy 通过（ANY-of）
  - sell / quote / settle 不受 gate
  - 用户持不达标 title 仍可 settle 派彩
  - PUT required-titles 整体覆写（单事务）
  - market 删除时 market_required_title CASCADE

backend/tests/test_title_lazy_load.py
  - User 反向集合 user_titles 配 lazy="raise_on_sql"
  - 未 selectinload 访问抛错
  - selectinload 后正常返回

backend/tests/test_title_migration.py
  - upgrade head → 5 张表 + column 存在
  - downgrade -1 → 干净回滚
  - upgrade head 再次幂等
```

### 10.2 前端

`npm run type-check && npm run lint && npm run build`

### 10.3 手动 smoke（部署后必跑）

- admin 链路：建 title → 建 batch → 上传 CSV → 看 used/total
- 用户链路：登录 → `/redeem-title` → 输 code → 个人页看 chip → equip → leaderboard chip 渲染
- 市场门槛：admin 加 required title → 不达标用户 buy 403 toast → 达标用户 buy 通过 → 卖出始终可用
- 移动端 + 未登录态 + 空状态 + 错误 toast 全过

### 10.4 性能护栏

- buy 接口 gate check 单条 SQL 完成（不破坏 hot path）
- leaderboard / 翻车墙带 equipped_title 走 selectinload，不 N+1

---

## 十一、部署节奏

按 CLAUDE.md，schema 改动 + 红线 base.py = "非小修补"：

1. 本地分支：`feat/2026-05-23-title-system`
2. 完成后跑：
   - `python -m py_compile $(find app -name '*.py')`
   - `python -c "import app.main"`
   - `pytest -x`
   - 前端 `npm run type-check && npm run lint && npm run build`
3. 合 main → push（自动部署）
4. 生产 deploy.sh 走 `alembic upgrade head`
5. 部署后 smoke（§10.3）

回滚：`alembic downgrade -1` 干净回 partial_liq mode 状态。
