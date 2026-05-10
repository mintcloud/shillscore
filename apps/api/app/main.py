"""Drop at: apps/api/app/main.py — full replacement (1 import + 1 include_router added)."""
import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.auth import twitter as twitter_auth
from app.config import get_settings
from app.routers import admin, health, leaderboard

settings = get_settings()

logging.basicConfig(level=settings.log_level)

app = FastAPI(title="shillscore", version="0.1.0")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "dev-only-not-for-prod",
    https_only=True,
    same_site="lax",
)

app.include_router(health.router)
app.include_router(twitter_auth.router)
app.include_router(leaderboard.router, prefix="/api")
app.include_router(admin.router, prefix="/api/admin")
