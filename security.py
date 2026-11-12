import hashlib
import hmac
import secrets

# 32 symbols without look-alikes (0/O, 1/I)
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_key() -> str:
    """SLOT-XXXX-XXXX-XXXX: 60 bits of randomness from the OS CSPRNG."""
    groups = ("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))
    return "SLOT-" + "-".join(groups)


def normalize_key(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


def hash_key(key: str, pepper: bytes) -> str:
    """Keyed hash. Only this digest is stored; the key itself never touches the database."""
    return hmac.new(pepper, normalize_key(key).encode(), hashlib.sha256).hexdigest()
