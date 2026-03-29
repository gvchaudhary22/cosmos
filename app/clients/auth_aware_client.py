"""
Auth-Aware API Client — Routes API calls through the correct token based on user context.

When a MARS user logs in (SSO → email → users table lookup):
  - Email's company_id == 1 → ICRM admin → dual token (admin + seller for target)
  - Email's company_id != 1 → seller → single token (their own seller only)

Tokens are generated ONCE at login (or on first API call) and cached.
If a token gets 401, it's invalidated and regenerated on next call.

This client wraps MCAPIClient and injects the correct Authorization header
automatically based on whether the API is ICRM-internal or seller-facing.
"""

from typing import Any, Dict, Optional

import structlog

from app.clients.mcapi import MCAPIClient, MCAPIResponse, MCAPIError
from app.clients.sso_auth import SSOAuthClient, AuthContext

logger = structlog.get_logger()


# API paths that require ICRM admin token (internal APIs)
_ICRM_API_PREFIXES = [
    "/internal/",
    "/icrm/",
    "/admin/",
    "/v1/admin/",
    "/v1/internal/",
    "/multichannel/",
]

# API paths that require seller token (seller-facing APIs)
_SELLER_API_PREFIXES = [
    "/v1/orders",
    "/v1/shipments",
    "/v1/courier",
    "/v1/ndr",
    "/v1/billing",
    "/v1/wallet",
    "/v1/returns",
    "/v1/settings",
    "/v1/tracking",
    "/v1/manifest",
    "/v1/pickup",
]


class AuthAwareClient:
    """
    Wraps MCAPIClient with automatic token injection based on:
    1. The chat user's identity (ICRM admin vs seller)
    2. The API being called (internal vs seller-facing)

    Usage by tools:
        # In tool execution, auth_context is set per-request from MarsContext
        result = await auth_client.get(
            "/v1/orders/12345",
            auth_context=auth_ctx,  # has icrm_token + seller_token
        )
        # Automatically picks seller_token for /v1/orders/*
    """

    def __init__(self, mcapi: MCAPIClient, sso: SSOAuthClient):
        self.mcapi = mcapi
        self.sso = sso

    def _select_token(self, path: str, auth_context: AuthContext) -> Optional[str]:
        """Select the correct token for the API path."""
        # Check if this is an ICRM-internal API
        for prefix in _ICRM_API_PREFIXES:
            if path.startswith(prefix):
                if not auth_context.is_icrm_user:
                    logger.warning(
                        "auth.blocked_icrm_api",
                        path=path,
                        company_id=auth_context.user_company_id,
                    )
                    return None  # Seller cannot access ICRM APIs
                return auth_context.icrm_token

        # Default: use seller token
        return auth_context.seller_token

    async def get(
        self,
        path: str,
        auth_context: AuthContext,
        params: Dict = None,
        extra_headers: Dict = None,
    ) -> MCAPIResponse:
        """GET with automatic token selection."""
        token = self._select_token(path, auth_context)
        if token is None:
            raise MCAPIError(
                f"Access denied: company_id={auth_context.user_company_id} "
                f"cannot access {path}",
                status_code=403,
            )

        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)

        try:
            return await self.mcapi.get(path, params=params, headers=headers)
        except MCAPIError as e:
            if e.status_code == 401:
                # Token expired — invalidate cache and retry once
                refreshed_token = await self._refresh_token(path, auth_context)
                if refreshed_token and refreshed_token != token:
                    headers["Authorization"] = f"Bearer {refreshed_token}"
                    return await self.mcapi.get(path, params=params, headers=headers)
            raise

    async def post(
        self,
        path: str,
        auth_context: AuthContext,
        json: Dict = None,
        extra_headers: Dict = None,
    ) -> MCAPIResponse:
        """POST with automatic token selection."""
        token = self._select_token(path, auth_context)
        if token is None:
            raise MCAPIError(
                f"Access denied: company_id={auth_context.user_company_id} "
                f"cannot access {path}",
                status_code=403,
            )

        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)

        try:
            return await self.mcapi.post(path, json=json, headers=headers)
        except MCAPIError as e:
            if e.status_code == 401:
                refreshed_token = await self._refresh_token(path, auth_context)
                if refreshed_token and refreshed_token != token:
                    headers["Authorization"] = f"Bearer {refreshed_token}"
                    return await self.mcapi.post(path, json=json, headers=headers)
            raise

    async def _refresh_token(self, path: str, auth_context: AuthContext) -> Optional[str]:
        """Invalidate and refresh the appropriate token."""
        is_icrm_path = any(path.startswith(p) for p in _ICRM_API_PREFIXES)

        if is_icrm_path:
            self.sso._icrm_cache = None  # Invalidate
            new_ctx = await self.sso.get_auth_context(
                user_company_id=auth_context.user_company_id,
                target_company_id=auth_context.target_company_id,
            )
            return new_ctx.icrm_token
        else:
            cid = auth_context.target_company_id or auth_context.user_company_id
            self.sso._seller_cache.pop(cid, None)  # Invalidate
            new_ctx = await self.sso.get_auth_context(
                user_company_id=auth_context.user_company_id,
                target_company_id=auth_context.target_company_id,
            )
            return new_ctx.seller_token
