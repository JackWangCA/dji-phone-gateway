"""Verify Cloudflare Access application JWTs; never trust identity headers alone."""
import os
import re
from functools import lru_cache
from urllib.parse import urlsplit


def configuration():
    issuer = os.getenv("CLOUDFLARE_ACCESS_ISSUER", "").rstrip("/")
    audience = os.getenv("CLOUDFLARE_ACCESS_AUD", "")
    hostname = os.getenv("CLOUDFLARE_ACCESS_HOSTNAME", "").lower()
    if not any((issuer, audience, hostname)):
        return None
    if (not re.fullmatch(r"https://[a-z0-9-]+\.cloudflareaccess\.com", issuer)
            or not re.fullmatch(r"[a-f0-9]{64}", audience)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname)):
        raise ValueError("Configure a Cloudflare team URL, application AUD, and public hostname.")
    return issuer, audience, hostname


def required(headers):
    config = configuration()
    if not config:
        return False
    try:
        host = urlsplit("//" + headers.get("Host", "")).hostname
    except ValueError:
        return True
    return host == config[2] or "Cf-Access-Jwt-Assertion" in headers


@lru_cache(maxsize=4)
def key_client(issuer):
    import jwt
    return jwt.PyJWKClient(issuer + "/cdn-cgi/access/certs", lifespan=300, timeout=5)


def identity(headers):
    config = configuration()
    if not config:
        return None
    token = headers.get("Cf-Access-Jwt-Assertion", "")
    if not token or len(token) > 16384:
        return None
    try:
        import jwt
        issuer, audience, _ = config
        key = key_client(issuer).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=audience,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]})
        if claims.get("type") != "app" or not isinstance(claims["email"], str) or not claims["email"]:
            return None
        return claims["email"]
    except Exception:
        # Includes key-server outages: fail closed. Never log bearer tokens.
        return None
