from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import engine, init_db
from app.core.admin import setup_admin
from app.api.v1 import auth, user, market, chart, stream

from dotenv import load_dotenv

load_dotenv()


app = FastAPI(title="东方炒炒币 (Touhou Exchange)")

# CORS — 从配置读取允许的源
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
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
    return {"message": "欢迎来到大天狗交易所", "docs": "/docs"}