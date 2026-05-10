"""Twitter OAuth 2.0 PKCE for shillscore.

Two routes:
- GET /auth/twitter           — kicks off the auth flow (redirects to x.com)
- GET /auth/twitter/callback  — exchange code for tokens, upsert user

Tokens stored on the `users` row. Refresh handled by the worker on use.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import User

router = APIRouter(prefix="/auth/twitter", tags=["auth"])

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_ME_URL = "https://api.x.com/2/users/me"

SCOPES = ["tweet.read", "users.read", "follows.read", "offline.access"]


def _gen_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


@router.get("")
async def login(request: Request) -> RedirectResponse:
    settings = get_settings()
    if not settings.twitter_client_id or not settings.twitter_redirect_uri:
        raise HTTPException(500, "twitter oauth not configured")

    state = secrets.token_urlsafe(32)
    verifier, challenge = _gen_pkce()
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = verifier

    params = {
        "response_type": "code",
        "client_id": settings.twitter_client_id,
        "redirect_uri": settings.twitter_redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"{X_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None
) -> RedirectResponse:
    settings = get_settings()
    expected_state = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)

    if not code or not state or state != expected_state or not verifier:
        raise HTTPException(400, "invalid oauth state")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
            X_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.twitter_redirect_uri,
                "client_id": settings.twitter_client_id,
                "code_verifier": verifier,
            },
            auth=(settings.twitter_client_id, settings.twitter_client_secret)
            if settings.twitter_client_secret
            else None,
        )
        if token_resp.status_code != 200:
            raise HTTPException(400, f"token exchange failed: {token_resp.text}")
        tokens = token_resp.json()

        me_resp = await client.get(
            X_ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        me_resp.raise_for_status()
        me = me_resp.json()["data"]

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 7200))

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.twitter_id == me["id"]))
        ).scalar_one_or_none()

        if existing:
            existing.handle = me["username"]
            existing.twitter_access_token = tokens["access_token"]
            existing.twitter_refresh_token = tokens.get("refresh_token") or existing.twitter_refresh_token
            existing.twitter_token_expires_at = expires_at
            user_id = existing.id
        else:
            new_user = User(
                twitter_id=me["id"],
                handle=me["username"],
                twitter_access_token=tokens["access_token"],
                twitter_refresh_token=tokens.get("refresh_token"),
                twitter_token_expires_at=expires_at,
            )
            session.add(new_user)
            await session.flush()
            user_id = new_user.id

        await session.commit()

    request.session["user_id"] = user_id
    return RedirectResponse("/?authed=1")
