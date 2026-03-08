from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.database import engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream

from app.models.base import User, Position, Outcome, Market, Transaction
from sqladmin import Admin, ModelView

from dotenv import load_dotenv

load_dotenv()


app = FastAPI(title="东方炒炒币 (Touhou Exchange)")

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite 开发服务器
        "http://localhost:3000",  # 备用端口
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 启动时初始化数据库
@app.on_event("startup")
async def on_startup():
    await init_db()
    # 初始化管理后台
    setup_admin(app, engine)

# 注册认证模块
# 最终路径示例：
# 注册：POST /api/v1/auth/register
# 登录：POST /api/v1/auth/jwt/login
# 查看自己：GET /api/v1/auth/me
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(user.router, prefix="/api/v1/user", tags=["UserAssets"])
app.include_router(market.router, prefix="/api/v1/market", tags=["Market"])
app.include_router(chart.router, prefix="/api/v1/chart", tags=["Chart"])
app.include_router(stream.router, prefix="/api/v1/stream", tags=["Stream"])

@app.get("/")
async def root():
    return {
        "message": "欢迎来到大天狗交易所",
        "docs": "/docs",
        "admin": "/admin"
    }