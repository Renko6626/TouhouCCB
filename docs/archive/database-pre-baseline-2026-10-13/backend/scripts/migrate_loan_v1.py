"""Run the current idempotent loan configuration migration."""
import asyncio
from app.services.loan_migrate import auto_migrate

if __name__ == "__main__":
    asyncio.run(auto_migrate())
