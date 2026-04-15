import logging

import jwt
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.core.config import settings
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position, Transaction

logger = logging.getLogger(__name__)


class AdminAuth(AuthenticationBackend):
    """
    Admin 面板认证：复用本站 JWT。
    登录页输入 access_token（从浏览器 localStorage 复制），
    验证 JWT 有效且用户是 superuser。
    """

    async def login(self, request: Request) -> bool:
        form = await request.form()
        token = form.get("username", "")  # 用 username 字段传 JWT token

        if not token:
            return False

        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
            user_id = int(payload["sub"])
        except (jwt.InvalidTokenError, ValueError, KeyError):
            logger.warning("Admin login: invalid JWT")
            return False

        # 查 DB 确认是 superuser
        async with async_session_maker() as db:
            user = await db.get(User, user_id)
            if not user or not user.is_active or not user.is_superuser:
                logger.warning("Admin login: user_id=%s not superuser or inactive", user_id)
                return False

        # 写入 session
        request.session.update({"user_id": user_id})
        logger.info("Admin login: user_id=%s (%s)", user_id, user.username)
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        user_id = request.session.get("user_id")
        if not user_id:
            return False

        # 每次请求验证用户仍然是 superuser（防止权限被撤销后仍能访问）
        async with async_session_maker() as db:
            user = await db.get(User, user_id)
            if not user or not user.is_active or not user.is_superuser:
                request.session.clear()
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
    authentication_backend = AdminAuth(secret_key=settings.ADMIN_SECRET_KEY)
    admin = Admin(
        app, engine,
        authentication_backend=authentication_backend,
        title="饭纲丸龙赌场后台",
        base_url="/api/v1/admin",
    )
    admin.add_view(UserAdmin)
    admin.add_view(MarketAdmin)
    admin.add_view(OutcomeAdmin)
    admin.add_view(PositionAdmin)
    admin.add_view(TransactionAdmin)
