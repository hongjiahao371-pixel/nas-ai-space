from __future__ import annotations

import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"
    iterations = 310000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        values = encoded.split("$")
        if values[0] == "scrypt" and len(values) == 6 and hasattr(hashlib, "scrypt"):
            _, n, r, p, salt, expected = values
            digest = hashlib.scrypt(
                password.encode("utf-8"),
                salt=bytes.fromhex(salt),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(expected)),
            )
        elif values[0] == "pbkdf2_sha256" and len(values) == 4:
            _, iterations, salt, expected = values
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations), dklen=len(bytes.fromhex(expected))
            )
        else:
            return False
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def session_token() -> str:
    return secrets.token_urlsafe(40)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
