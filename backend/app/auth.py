"""Supabase Auth JWT verification.

Every token is verified for signature, issuer, audience, expiry and subject. There is no
code path that decodes a token without verification and no bypass in production. In
development the same verification runs against a locally minted HS256 key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str | None


class KeyResolver(Protocol):
    def key_for(self, token: str, header: dict[str, Any]) -> Any: ...


class ConfiguredKeyResolver:
    """Resolve keys from a JWKS URL (asymmetric, current Supabase default), the legacy
    shared HS256 secret, and/or the development minting secret."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks = (
            jwt.PyJWKClient(settings.auth_jwks_url, cache_keys=True, lifespan=3600)
            if settings.auth_jwks_url
            else None
        )

    def key_for(self, token: str, header: dict[str, Any]) -> Any:
        alg = header.get("alg")
        if alg == "HS256":
            secret = self._settings.auth_jwt_secret
            if not secret and self._settings.dev_token_mint_enabled:
                secret = self._settings.dev_jwt_secret
            if not secret:
                raise jwt.InvalidTokenError("HS256 not configured")
            return secret
        if self._jwks is None:
            raise jwt.InvalidTokenError("asymmetric verification not configured")
        return self._jwks.get_signing_key_from_jwt(token).key


_resolver: KeyResolver | None = None


def get_key_resolver() -> KeyResolver:
    global _resolver
    if _resolver is None:
        _resolver = ConfiguredKeyResolver(get_settings())
    return _resolver


def set_key_resolver(resolver: KeyResolver | None) -> None:
    """Test/DI hook."""
    global _resolver
    _resolver = resolver


def verify_token(token: str, settings: Settings, resolver: KeyResolver) -> AuthUser:
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg not in settings.auth_algorithms or alg == "none":
            raise jwt.InvalidTokenError("algorithm not allowed")
        key = resolver.key_for(token, header)
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=settings.auth_audience,
            issuer=settings.auth_issuer,
            leeway=settings.auth_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        user_id = uuid.UUID(str(claims["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if claims.get("role") not in (None, "authenticated"):
        # Supabase service_role / anon tokens must never act as an end user.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_role")
    email = claims.get("email")
    return AuthUser(id=user_id, email=email if isinstance(email, str) else None)


def current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    resolver: KeyResolver = Depends(get_key_resolver),
) -> AuthUser:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_token(token.strip(), settings, resolver)
