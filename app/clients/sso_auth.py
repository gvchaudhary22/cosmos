"""
SSO Auth Client — Generates seller and ICRM tokens for COSMOS tool execution.

Mirrors the MARS Go ssoauth package logic:
  - SSO login with email/password + TOTP 2FA
  - Seller token: SSO token → exchange with company_id → seller JWT
  - ICRM token: SSO token → exchange with is_web=1 → admin JWT (company_id=1)

Token caching:
  - ICRM admin token cached for 23 hours (SSO tokens expire in 24h)
  - Seller tokens cached per company_id for 23 hours
  - Cache prevents re-login on every tool call

Access model:
  - Chat user with company_id=1 (ICRM) → gets admin token + seller token for target company
  - Chat user with company_id!=1 (seller) → gets ONLY their own seller token, NO ICRM access
"""

import asyncio
import hashlib
import hmac
import math
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import httpx
import structlog

logger = structlog.get_logger()

# Token cache TTL: 23 hours (SSO tokens expire in ~24h)
TOKEN_TTL_SECONDS = 23 * 3600


@dataclass
class TokenEntry:
    """Cached token with expiry."""
    token: str
    created_at: float
    company_id: str
    token_type: str  # "seller" or "icrm"
    user_id: Optional[int] = None
    email: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > TOKEN_TTL_SECONDS


@dataclass
class AuthContext:
    """Auth context for a chat user — determines what APIs they can call."""
    user_company_id: str        # The chat user's own company_id
    is_icrm_user: bool          # True if company_id == "1"
    icrm_token: Optional[str]   # Admin token (only if is_icrm_user)
    seller_token: Optional[str]  # Seller token for the target company
    target_company_id: Optional[str]  # The company being queried about


class SSOAuthClient:
    """
    Authenticates with Shiprocket SSO and generates tokens for COSMOS tools.

    Usage:
        client = SSOAuthClient(config)
        auth_ctx = await client.get_auth_context(
            user_company_id="1",          # chat user's company
            target_company_id="789",      # company they're asking about
        )
        # auth_ctx.icrm_token → for ICRM APIs
        # auth_ctx.seller_token → for seller APIs scoped to company 789
    """

    def __init__(
        self,
        username: str,
        password: str,
        domain: str = "@shiprocket.com",
        totp_secret: str = "",
        base_url: str = "https://apiv2.shiprocket.co",
    ):
        self.username = username
        self.password = password
        self.domain = domain
        self.totp_secret = totp_secret
        self.base_url = base_url.rstrip("/")

        # Token caches
        self._icrm_cache: Optional[TokenEntry] = None
        self._seller_cache: Dict[str, TokenEntry] = {}
        self._lock = asyncio.Lock()

    async def get_auth_context(
        self,
        user_company_id: str,
        target_company_id: Optional[str] = None,
    ) -> AuthContext:
        """
        Build auth context based on the chat user's company_id.

        Rules:
          - company_id == "1" → ICRM user → gets admin token + seller token for target
          - company_id != "1" → seller → gets ONLY their own seller token
          - seller CANNOT get ICRM token (blocked, not just skipped)
        """
        is_icrm = str(user_company_id) == "1"

        icrm_token = None
        seller_token = None
        effective_target = target_company_id or user_company_id

        if is_icrm:
            # ICRM user: get admin token + seller token for target company
            icrm_token = await self._get_icrm_token()
            if effective_target and effective_target != "1":
                seller_token = await self._get_seller_token(effective_target)
        else:
            # Seller user: ONLY their own seller token
            # Force target to their own company (prevent impersonation)
            effective_target = user_company_id
            seller_token = await self._get_seller_token(user_company_id)

        return AuthContext(
            user_company_id=user_company_id,
            is_icrm_user=is_icrm,
            icrm_token=icrm_token,
            seller_token=seller_token,
            target_company_id=effective_target,
        )

    # -------------------------------------------------------------------
    # Token generation (with caching)
    # -------------------------------------------------------------------

    async def _get_icrm_token(self) -> Optional[str]:
        """Get ICRM admin token (cached)."""
        if self._icrm_cache and not self._icrm_cache.is_expired:
            return self._icrm_cache.token

        async with self._lock:
            # Double-check after acquiring lock
            if self._icrm_cache and not self._icrm_cache.is_expired:
                return self._icrm_cache.token

            try:
                result = await self._sso_login_and_get_icrm_token()
                if result:
                    self._icrm_cache = TokenEntry(
                        token=result["token"],
                        created_at=time.time(),
                        company_id="1",
                        token_type="icrm",
                        user_id=result.get("user_id"),
                        email=result.get("email"),
                    )
                    logger.info("sso_auth.icrm_token_refreshed")
                    return result["token"]
            except Exception as e:
                logger.error("sso_auth.icrm_token_failed", error=str(e))
                return None

    async def _get_seller_token(self, company_id: str) -> Optional[str]:
        """Get seller token for a specific company_id (cached)."""
        cached = self._seller_cache.get(company_id)
        if cached and not cached.is_expired:
            return cached.token

        async with self._lock:
            cached = self._seller_cache.get(company_id)
            if cached and not cached.is_expired:
                return cached.token

            try:
                result = await self._sso_login_and_get_seller_token(company_id)
                if result:
                    self._seller_cache[company_id] = TokenEntry(
                        token=result["seller_token"],
                        created_at=time.time(),
                        company_id=company_id,
                        token_type="seller",
                    )
                    logger.info("sso_auth.seller_token_refreshed", company_id=company_id)
                    return result["seller_token"]
            except Exception as e:
                logger.error("sso_auth.seller_token_failed", company_id=company_id, error=str(e))
                return None

    # -------------------------------------------------------------------
    # SSO login flow (mirrors MARS Go ssoauth/client.go)
    # -------------------------------------------------------------------

    async def _sso_login_and_get_icrm_token(self) -> Optional[Dict]:
        """Full SSO flow: login → 2FA → get ICRM SSO token → exchange for ICRM JWT."""
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Step 1: SSO Login
            sso_cookies = await self._sso_login(client)
            if not sso_cookies:
                return None

            # Step 2: 2FA
            if not await self._handle_2fa(client, sso_cookies):
                return None

            # Step 3: Get ICRM SSO token
            sso_token = await self._get_icrm_sso_token(client, sso_cookies)
            if not sso_token:
                return None

            # Step 4: Exchange for ICRM panel token (is_web=1, no company_id)
            return await self._exchange_icrm_token(sso_token)

    async def _sso_login_and_get_seller_token(self, company_id: str) -> Optional[Dict]:
        """Full SSO flow: login → 2FA → get seller SSO token → exchange with company_id."""
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Step 1: SSO Login
            sso_cookies = await self._sso_login(client)
            if not sso_cookies:
                return None

            # Step 2: 2FA
            if not await self._handle_2fa(client, sso_cookies):
                return None

            # Step 3: Get Seller SSO token
            sso_token = await self._get_seller_sso_token(client, sso_cookies)
            if not sso_token:
                return None

            # Step 4: Exchange for seller panel token with company_id
            return await self._exchange_seller_token(sso_token, company_id)

    async def _sso_login(self, client: httpx.AsyncClient) -> Optional[Dict]:
        """Step 1: Login to Shiprocket SSO portal."""
        email = f"{self.username}{self.domain}"
        try:
            resp = await client.post(
                "https://sso.shiprocket.co/api/login",
                json={"email": email, "password": self.password},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return dict(resp.cookies)
            logger.warning("sso_auth.login_failed", status=resp.status_code)
            return None
        except Exception as e:
            logger.error("sso_auth.login_error", error=str(e))
            return None

    async def _handle_2fa(self, client: httpx.AsyncClient, cookies: Dict) -> bool:
        """Step 2: Submit TOTP 2FA code."""
        if not self.totp_secret:
            return True  # No 2FA configured

        totp_code = self._generate_totp(self.totp_secret)
        try:
            resp = await client.post(
                "https://sso.shiprocket.co/api/verify-2fa",
                json={"otp": totp_code},
                headers={"Content-Type": "application/json"},
                cookies=cookies,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error("sso_auth.2fa_error", error=str(e))
            return False

    async def _get_seller_sso_token(self, client: httpx.AsyncClient, cookies: Dict) -> Optional[str]:
        """Step 3a: Get SSO token for Shiprocket Seller panel."""
        try:
            resp = await client.get(
                "https://sso.shiprocket.co/api/apps/shiprocket-seller/token",
                cookies=cookies,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token") or data.get("sso_token")
            return None
        except Exception as e:
            logger.error("sso_auth.seller_sso_token_error", error=str(e))
            return None

    async def _get_icrm_sso_token(self, client: httpx.AsyncClient, cookies: Dict) -> Optional[str]:
        """Step 3b: Get SSO token for Shiprocket ICRM panel."""
        try:
            resp = await client.get(
                "https://sso.shiprocket.co/api/apps/shiprocket-icrm/token",
                cookies=cookies,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token") or data.get("sso_token")
            return None
        except Exception as e:
            logger.error("sso_auth.icrm_sso_token_error", error=str(e))
            return None

    async def _exchange_seller_token(self, sso_token: str, company_id: str) -> Optional[Dict]:
        """Step 4a: Exchange SSO token for seller panel JWT with company_id."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Validate token first
            await client.get(
                f"{self.base_url}/v1/auth/login/token_login_validity",
                params={"token": sso_token},
                headers={
                    "Authorization": f"Bearer {sso_token}",
                    "No-Auth": "True",
                    "Origin": "https://app.shiprocket.in",
                    "Referer": "https://app.shiprocket.in/",
                },
            )

            # Exchange for seller token
            resp = await client.get(
                f"{self.base_url}/v1/auth/login/user",
                params={"token": sso_token, "company_id": company_id},
                headers={
                    "Authorization": f"Bearer {sso_token}",
                    "No-Auth": "True",
                    "Origin": "https://app.shiprocket.in",
                    "Referer": "https://app.shiprocket.in/",
                },
            )

            if resp.status_code == 200:
                body = resp.text
                token_match = re.search(r'"token"\s*:\s*"([^"]+)"', body)
                if token_match:
                    return {
                        "seller_token": token_match.group(1),
                        "sso_token": sso_token,
                        "company_id": company_id,
                    }
            logger.warning("sso_auth.seller_exchange_failed", status=resp.status_code)
            return None

    async def _exchange_icrm_token(self, sso_token: str) -> Optional[Dict]:
        """Step 4b: Exchange SSO token for ICRM panel JWT (is_web=1, company_id=1 auto)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/v1/auth/login/user",
                params={"is_web": "1", "token": sso_token},
                headers={
                    "Authorization": f"Bearer {sso_token}",
                    "Origin": "https://app.shiprocket.in",
                    "Referer": "https://app.shiprocket.in/",
                },
            )

            if resp.status_code == 200:
                body = resp.text
                token_match = re.search(r'"token"\s*:\s*"([^"]+)"', body)
                id_match = re.search(r'"id"\s*:\s*(\d+)', body)
                email_match = re.search(r'"email"\s*:\s*"([^"]+)"', body)
                cid_match = re.search(r'"company_id"\s*:\s*(\d+)', body)

                if token_match:
                    return {
                        "token": token_match.group(1),
                        "sso_token": sso_token,
                        "user_id": int(id_match.group(1)) if id_match else None,
                        "email": email_match.group(1) if email_match else None,
                        "company_id": int(cid_match.group(1)) if cid_match else 1,
                    }
            logger.warning("sso_auth.icrm_exchange_failed", status=resp.status_code)
            return None

    # -------------------------------------------------------------------
    # TOTP generation (matches MARS Go crypto/otp logic)
    # -------------------------------------------------------------------

    @staticmethod
    def _generate_totp(secret_b32: str, period: int = 30) -> str:
        """Generate a 6-digit TOTP code from a base32 secret."""
        import base64
        key = base64.b32decode(secret_b32.upper().replace(" ", ""), casefold=True)
        counter = int(time.time()) // period
        counter_bytes = struct.pack(">Q", counter)
        hmac_hash = hmac.new(key, counter_bytes, hashlib.sha1).digest()
        offset = hmac_hash[-1] & 0x0F
        code = struct.unpack(">I", hmac_hash[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(code % 10**6).zfill(6)

    # -------------------------------------------------------------------
    # Cache management
    # -------------------------------------------------------------------

    def clear_cache(self):
        """Clear all cached tokens."""
        self._icrm_cache = None
        self._seller_cache.clear()

    def cache_stats(self) -> Dict:
        """Return cache status."""
        return {
            "icrm_cached": self._icrm_cache is not None and not self._icrm_cache.is_expired,
            "seller_cached_companies": [
                cid for cid, entry in self._seller_cache.items()
                if not entry.is_expired
            ],
            "total_cached": (
                (1 if self._icrm_cache and not self._icrm_cache.is_expired else 0)
                + sum(1 for e in self._seller_cache.values() if not e.is_expired)
            ),
        }
