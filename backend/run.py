
import os

import uvicorn
from app.main import app

if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8004"))
    uvicorn.run(app, host=host, port=port)