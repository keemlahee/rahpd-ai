import uvicorn

from .config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.server:app", host=HOST, port=PORT, log_level="warning")
