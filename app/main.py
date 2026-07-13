from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routes import admin, survey
from .settings import settings
from .storage import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="EPV RPPS", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


app.include_router(survey.router)
app.include_router(admin.router)


@app.get("/")
def home():
    return {"status": "ok", "admin": "/admin"}
