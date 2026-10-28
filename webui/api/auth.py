# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cognito access-token verification and role mapping for the WebUI API."""

from dataclasses import dataclass

import jwt

VIEWER_GROUP = "bedrock-analytics-viewer"
ADMIN_GROUP = "bedrock-analytics-admin"


class AuthError(Exception):
    """Token missing, invalid, or lacking the required group. `status` is 401 or 403."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    username: str
    groups: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return ADMIN_GROUP in self.groups

    @property
    def is_viewer(self) -> bool:
        return self.is_admin or VIEWER_GROUP in self.groups


class TokenVerifier:
    """Verifies Cognito user-pool access tokens: RS256 signature (pool JWKS), issuer, expiry, token_use, client_id."""

    def __init__(self, region: str, user_pool_id: str, client_id: str, jwks_client: jwt.PyJWKClient | None = None):
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self.client_id = client_id
        self._jwks = jwks_client or jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True,
                                                    lifespan=3600, timeout=5)

    def verify(self, authorization: str | None) -> User:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("Missing bearer token")
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            # Cognito access tokens carry client_id instead of aud, so aud is not verified here.
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                options={"require": ["exp", "iss", "token_use", "client_id"], "verify_aud": False},
                                leeway=30)
        except jwt.PyJWTError as e:
            raise AuthError(f"Invalid token: {e}") from e
        if claims.get("token_use") != "access" or claims.get("client_id") != self.client_id:
            raise AuthError("Token is not an access token for this app")
        return User(username=claims.get("username") or claims.get("sub", ""),
                    groups=frozenset(claims.get("cognito:groups") or []))


def require(user: User, admin: bool = False) -> User:
    """Raise 403 unless the user is a viewer (or an admin when admin=True)."""
    if admin and not user.is_admin:
        raise AuthError(f"Requires group {ADMIN_GROUP}", status=403)
    if not user.is_viewer:
        raise AuthError(f"Requires group {VIEWER_GROUP} or {ADMIN_GROUP}", status=403)
    return user
