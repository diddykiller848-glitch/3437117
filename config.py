import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

COLOR_PRIMARY = 0x5865F2
COLOR_SUCCESS = 0x57F287
COLOR_WARNING = 0xFEE75C
COLOR_ERROR = 0xED4245
COLOR_INFO = 0x3498DB


def _id_list(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    owner_id: int
    admin_role_ids: frozenset[int]
    prefix: str
    database_path: str
    key_pepper: bytes

    @classmethod
    def from_env(cls) -> "Config":
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"Missing required environment variable: {name}")
            return value

        pepper = required("KEY_PEPPER")
        if len(pepper) < 32:
            raise RuntimeError("KEY_PEPPER must be at least 32 characters long.")
        return cls(
            token=required("DISCORD_TOKEN"),
            guild_id=int(required("GUILD_ID")),
            owner_id=int(required("OWNER_ID")),
            admin_role_ids=_id_list(os.getenv("ADMIN_ROLE_IDS", "")),
            prefix=os.getenv("COMMAND_PREFIX", ".").strip() or ".",
            database_path=os.getenv("DATABASE_PATH", "data/slots.db"),
            key_pepper=pepper.encode(),
        )
