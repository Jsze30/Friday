from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    livekit_url: str = Field(alias="LIVEKIT_URL")
    livekit_api_key: str = Field(alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(alias="LIVEKIT_API_SECRET")

    agent_name: str = Field(default="friday-agent", alias="FRIDAY_AGENT_NAME")
    room_prefix: str = Field(default="friday", alias="FRIDAY_ROOM_PREFIX")
    token_ttl_seconds: int = Field(default=600, alias="FRIDAY_TOKEN_TTL_SECONDS")

    vosk_model_path: Path = Field(
        default=Path("models/vosk-model-small-en-us-0.15"),
        alias="FRIDAY_VOSK_MODEL_PATH",
    )
    wake_phrase: str = Field(default="friday", alias="FRIDAY_WAKE_PHRASE")
    wake_debounce_ms: int = Field(default=1500, alias="FRIDAY_WAKE_DEBOUNCE_MS")

    def resolved_vosk_model_path(self) -> Path:
        p = self.vosk_model_path
        return p if p.is_absolute() else REPO_ROOT / p


settings = Settings()  # type: ignore[call-arg]
