"""Hash de contraseñas.

* Nuevos hashes: **Argon2id** (``argon2-cffi``), ganador de la Password Hashing Competition.
* Hashes heredados de la app Flask (``werkzeug.security.generate_password_hash``:
  ``pbkdf2:sha256:N$salt$hex`` o ``scrypt:n:r:p$salt$hex``) se verifican con ``hashlib``
  (sin depender de Werkzeug) y se **re-hashean a Argon2id en el primer login exitoso**.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # parámetros por defecto RFC 9106 (perfil de baja memoria)
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


@dataclass(frozen=True, slots=True)
class VerifyResult:
    valid: bool
    needs_rehash: bool = False


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> VerifyResult:
    """Verifica en tiempo aproximadamente constante (incluso si no hay hash)."""
    if not stored_hash:
        dummy_verify()
        return VerifyResult(valid=False)
    if stored_hash.startswith("$argon2"):
        try:
            _hasher.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return VerifyResult(valid=False)
        return VerifyResult(valid=True, needs_rehash=_hasher.check_needs_rehash(stored_hash))
    if stored_hash.startswith(("pbkdf2:", "scrypt:")):
        return VerifyResult(valid=_verify_werkzeug(stored_hash, password), needs_rehash=True)
    dummy_verify()
    return VerifyResult(valid=False)


def dummy_verify() -> None:
    """Gasta el mismo tiempo que una verificación real (mitiga enumeración de usuarios)."""
    try:
        _hasher.verify(_DUMMY_HASH, "wrong-password")
    except VerifyMismatchError:
        pass


def _verify_werkzeug(stored_hash: str, password: str) -> bool:
    """Réplica de ``werkzeug.security._hash_internal`` para los formatos pbkdf2/scrypt."""
    try:
        method, salt, expected = stored_hash.split("$", 2)
    except ValueError:
        return False
    name, *args = method.split(":")
    password_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("utf-8")
    try:
        if name == "scrypt":
            n, r, p = (int(x) for x in args) if args else (2**15, 8, 1)
            digest = hashlib.scrypt(password_bytes, salt=salt_bytes, n=n, r=r, p=p, maxmem=132 * n * r * p).hex()
        elif name == "pbkdf2":
            hash_name = args[0] if args else "sha256"
            iterations = int(args[1]) if len(args) > 1 else 600_000
            digest = hashlib.pbkdf2_hmac(hash_name, password_bytes, salt_bytes, iterations).hex()
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)
