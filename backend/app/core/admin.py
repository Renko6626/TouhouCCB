from sqladmin import Admin, ModelView
from app.models.base import User, Market, Outcome, Position, Transaction
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        # ⚠️ 这里为了最小化演示，直接硬编码校验
        # 实际项目中建议把密码放在环境变量，或者查数据库校验 hash
        if username == "admin@secret-sealing.club" and password == "nailoong":
            # 登录成功，设置 session token
            request.session.update({"token": "admin_token"})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = request.session.get("token")
        if not token:
            return False
        return True

class UserAdmin(ModelView, model=User):
    name = "交易员"
    column_list = [User.id, User.username, User.cash, User.debt, User.is_superuser]
    column_searchable_list = [User.username, User.email]
    icon = "fa-solid fa-user"

class MarketAdmin(ModelView, model=Market):
    name = "市场"
    column_list = [Market.id, Market.title, Market.status, Market.liquidity_b]
    column_details_list = "__all__"
    icon = "fa-solid fa-chess"

class OutcomeAdmin(ModelView, model=Outcome):
    name = "选项"
    column_list = [Outcome.id, Outcome.market_id, Outcome.label, Outcome.total_shares]
    icon = "fa-solid fa-tags"

class PositionAdmin(ModelView, model=Position):
    name = "持仓"
    column_list = [Position.id, Position.user_id, Position.outcome_id, Position.amount]
    icon = "fa-solid fa-vault"

class TransactionAdmin(ModelView, model=Transaction):
    name = "成交记录"
    column_list = [Transaction.id, Transaction.user_id, Transaction.type, Transaction.shares, Transaction.cost, Transaction.timestamp]
    column_sortable_list = [Transaction.timestamp]
    icon = "fa-solid fa-receipt"

def setup_admin(app, engine):
    # 这里可以添加之前提到的 AdminAuth 身份认证

    # AdminAuth机制
    authentication_backend = AdminAuth(secret_key="super_secret_key_123")


    admin = Admin(app, engine, authentication_backend=authentication_backend, title="饭纲丸龙赌场后台", base_url="/api/v1/admin")
    admin.add_view(UserAdmin)
    admin.add_view(MarketAdmin)
    admin.add_view(OutcomeAdmin)
    admin.add_view(PositionAdmin)
    admin.add_view(TransactionAdmin)