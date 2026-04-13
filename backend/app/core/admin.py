import logging
import secrets

from sqladmin import Admin, ModelView
from app.models.base import User, Market, Outcome, Position, Transaction
from app.core.config import settings
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

logger = logging.getLogger(__name__)


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")

        # 密码校验：必须配置 ADMIN_PASSWORD_HASH (bcrypt)
        if not settings.ADMIN_PASSWORD_HASH:
            logger.error("ADMIN_PASSWORD_HASH 未配置，管理后台登录已禁用。请在 .env 中设置！")
            return False

        try:
            import bcrypt
            if username == settings.ADMIN_USERNAME and bcrypt.checkpw(
                password.encode(), settings.ADMIN_PASSWORD_HASH.encode()
            ):
                token = secrets.token_urlsafe(32)
                request.session.update({"token": token})
                return True
        except Exception as e:
            logger.error(f"Admin 密码校验失败: {e}")

        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = request.session.get("token")
        return bool(token)

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
    authentication_backend = AdminAuth(secret_key=settings.ADMIN_SECRET_KEY)


    admin = Admin(app, engine, authentication_backend=authentication_backend, title="饭纲丸龙赌场后台", base_url="/api/v1/admin")
    admin.add_view(UserAdmin)
    admin.add_view(MarketAdmin)
    admin.add_view(OutcomeAdmin)
    admin.add_view(PositionAdmin)
    admin.add_view(TransactionAdmin)