from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT.parent / ".env.local")
load_dotenv(REPO_ROOT / ".env", override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT.parent / ".env.local", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    livekit_url: str = Field(alias="LIVEKIT_URL")
    livekit_api_key: str = Field(alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(alias="LIVEKIT_API_SECRET")

    agent_name: str = Field(default="friday-agent", alias="FRIDAY_AGENT_NAME")
    room_prefix: str = Field(default="friday", alias="FRIDAY_ROOM_PREFIX")
    token_ttl_seconds: int = Field(default=600, alias="FRIDAY_TOKEN_TTL_SECONDS")
    spotify_client_id: str | None = Field(default=None, alias="SPOTIFY_CLIENT_ID")
    spotify_redirect_uri: str = Field(
        default="http://127.0.0.1:43821/spotify/callback",
        alias="SPOTIFY_REDIRECT_URI",
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    vision_model: str = Field(
        default="gpt-4.1-mini",
        alias="FRIDAY_VISION_MODEL",
    )
    cloud_visual_analysis: bool = Field(
        default=True,
        alias="FRIDAY_CLOUD_VISUAL_ANALYSIS",
    )

    # openWakeWord accepts a pretrained model name or a custom ONNX/TFLite path.
    wake_model: str = Field(
        default="models/hey_friday.onnx",
        alias="FRIDAY_WAKE_MODEL",
    )
    wake_threshold: float = Field(default=0.5, alias="FRIDAY_WAKE_THRESHOLD")
    wake_debounce_ms: int = Field(default=1500, alias="FRIDAY_WAKE_DEBOUNCE_MS")

    def resolved_wake_model(self) -> str:
        """Pretrained model name as-is, or a custom model path made absolute."""
        v = self.wake_model.strip()
        if v.endswith((".onnx", ".tflite")) or "/" in v:
            p = Path(v)
            return str(p if p.is_absolute() else REPO_ROOT / p)
        return v


settings = Settings()  # type: ignore[call-arg]
