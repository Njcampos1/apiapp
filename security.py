from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm

from config import settings


@dataclass(frozen=True)
class _JwksCache:
    keys_by_kid: dict[str, Any]
    fetched_at: float


class Auth0JWKSClient:
    def __init__(
        self,
        jwks_url: str,
        ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._jwks_url = jwks_url
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cache: _JwksCache | None = None
        self._lock = asyncio.Lock()

    def _cache_is_valid(self) -> bool:
        if self._cache is None:
            return False
        return (time.time() - self._cache.fetched_at) < self._ttl_seconds

    async def _fetch_jwks(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _parse_keys(jwks_payload: dict[str, Any]) -> dict[str, Any]:
        keys = jwks_payload.get("keys", [])
        parsed: dict[str, Any] = {}
        for key_data in keys:
            kid = key_data.get("kid")
            if not kid:
                continue
            parsed[kid] = RSAAlgorithm.from_jwk(json.dumps(key_data))
        return parsed

    async def get_signing_key(self, kid: str) -> Any:
        if self._cache_is_valid() and self._cache is not None:
            cached_key = self._cache.keys_by_kid.get(kid)
            if cached_key is not None:
                return cached_key

        async with self._lock:
            if self._cache_is_valid() and self._cache is not None:
                cached_key = self._cache.keys_by_kid.get(kid)
                if cached_key is not None:
                    return cached_key

            jwks_payload = await self._fetch_jwks()
            parsed = self._parse_keys(jwks_payload)
            self._cache = _JwksCache(keys_by_kid=parsed, fetched_at=time.time())

            signing_key = parsed.get(kid)
            if signing_key is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token inválido: clave pública no encontrada",
                )
            return signing_key


def _normalize_auth0_domain(domain: str) -> str:
    return domain.replace("https://", "").replace("http://", "").rstrip("/")


def _resolve_issuer() -> str:
    if settings.AUTH0_ISSUER.strip():
        return settings.AUTH0_ISSUER.rstrip("/") + "/"
    normalized_domain = _normalize_auth0_domain(settings.AUTH0_DOMAIN)
    return f"https://{normalized_domain}/" if normalized_domain else ""


def _resolve_jwks_url() -> str:
    normalized_domain = _normalize_auth0_domain(settings.AUTH0_DOMAIN)
    if not normalized_domain:
        return ""
    return f"https://{normalized_domain}/.well-known/jwks.json"


def _parse_unverified_header(token: str) -> dict[str, Any]:
    try:
        return jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido: encabezado malformado",
        ) from exc


http_bearer = HTTPBearer(auto_error=False)
auth0_jwks_client = Auth0JWKSClient(jwks_url=_resolve_jwks_url())


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
) -> dict[str, Any]:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta token Bearer",
        )

    token = credentials.credentials

    if not settings.AUTH0_DOMAIN or not settings.AUTH0_API_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth0 no configurado en variables de entorno",
        )

    issuer = _resolve_issuer()
    if not issuer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Auth0 issuer inválido o no configurado",
        )

    unverified_header = _parse_unverified_header(token)
    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido: faltante 'kid' en header",
        )

    try:
        signing_key = await auth0_jwks_client.get_signing_key(kid)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No fue posible validar el token con JWKS",
        ) from exc

    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=settings.AUTH0_ALGORITHMS,
            audience=settings.AUTH0_API_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
        ) from exc

    return payload


def _extract_roles_or_permissions(claims: dict[str, Any]) -> set[str]:
    values: set[str] = set()

    for claim_name in ("permissions", "roles"):
        claim_value = claims.get(claim_name)
        if isinstance(claim_value, list):
            values.update(str(item) for item in claim_value)
        if isinstance(claim_value, str):
            values.update(item for item in claim_value.split(" ") if item)

    for claim_name, claim_value in claims.items():
        if not (claim_name.endswith("/roles") or claim_name.endswith("/permissions")):
            continue
        if isinstance(claim_value, list):
            values.update(str(item) for item in claim_value)
        if isinstance(claim_value, str):
            values.update(item for item in claim_value.split(" ") if item)

    return values


def require_role(role_name: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def role_dependency(claims: dict[str, Any] = Depends(verify_token)) -> dict[str, Any]:
        permissions_or_roles = _extract_roles_or_permissions(claims)
        if role_name not in permissions_or_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acceso denegado: se requiere el rol/permiso '{role_name}'",
            )
        return claims

    return role_dependency
