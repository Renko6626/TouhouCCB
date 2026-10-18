import asyncio
from pathlib import Path
from alembic.config import Config
from alembic import command
from app.core.database import engine

def migrate(conn):
    config=Config(str(Path(__file__).parent/'backend/alembic.ini'))
    config.set_main_option('script_location',str(Path(__file__).parent/'backend/alembic'))
    config.attributes['connection']=conn
    command.upgrade(config,'head')
async def main():
    async with engine.begin() as conn:await conn.run_sync(migrate)
    await engine.dispose()
asyncio.run(main())
