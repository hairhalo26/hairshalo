"""Verify a "Sign in with Google" credential (a Google-issued ID token).

The storefront gets the credential from Google Identity Services and posts it
here; nothing about the customer is taken from the browser itself. The token
is checked the way Google documents it: an RS256 signature by one of Google's
published keys, our own client id as the audience, a Google issuer, and an
unexpired timestamp. python-jose (already used for our own tokens) does the
cryptography, so this needs no Google SDK.
"""
import json
import threading
import time
import urllib.request

from jose import jwt
from jose.exceptions import JWTError

from app.config import settings

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_lock = threading.Lock()
_cache = {"keys": [], "expires": 0.0}


class GoogleAuthError(Exception):
    """The credential is not a valid Google sign-in for this shop."""


def _max_age(cache_control: str) -> int:
    for part in (cache_control or "").split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                return max(60, int(part.split("=", 1)[1]))
            except ValueError:
                break
    return 3600


def _fetch_keys():
    req = urllib.request.Request(GOOGLE_CERTS_URL, headers={"User-Agent": "hairshalo-api"})
    with urllib.request.urlopen(req, timeout=5) as resp:        # noqa: S310 (fixed https URL)
        body = json.loads(resp.read().decode("utf-8"))
        return body.get("keys", []), _max_age(resp.headers.get("Cache-Control", ""))


def _keys(force: bool = False):
    """Google's signing keys, cached for as long as Google says they are good."""
    with _lock:
        if force or not _cache["keys"] or time.time() >= _cache["expires"]:
            try:
                keys, ttl = _fetch_keys()
            except Exception as exc:  # noqa: BLE001
                if _cache["keys"] and not force:
                    return _cache["keys"]
                raise GoogleAuthError("Could not reach Google to check the sign-in. Please try again.") from exc
            _cache["keys"], _cache["expires"] = keys, time.time() + ttl
        return _cache["keys"]


def verify_credential(credential: str, client_id: str = None) -> dict:
    """Return the verified claims of a Google ID token, or raise GoogleAuthError."""
    client_id = client_id or settings.GOOGLE_CLIENT_ID
    if not client_id:
        raise GoogleAuthError("Google sign-in is not set up.")
    if not credential or credential.count(".") != 2:
        raise GoogleAuthError("That Google sign-in could not be read.")
    try:
        kid = jwt.get_unverified_header(credential).get("kid")
    except JWTError as exc:
        raise GoogleAuthError("That Google sign-in could not be read.") from exc

    key = next((k for k in _keys() if k.get("kid") == kid), None)
    if key is None:                      # Google rotated its keys since we cached them
        key = next((k for k in _keys(force=True) if k.get("kid") == kid), None)
    if key is None:
        raise GoogleAuthError("That Google sign-in could not be verified.")

    try:
        claims = jwt.decode(credential, key, algorithms=["RS256"], audience=client_id,
                            options={"verify_at_hash": False})
    except JWTError as exc:
        raise GoogleAuthError("That Google sign-in has expired or is not for this shop.") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("That sign-in did not come from Google.")
    return claims
