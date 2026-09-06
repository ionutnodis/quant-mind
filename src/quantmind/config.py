"""Typed configuration. All runtime knobs live here; secrets come from .env (never committed)."""

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QM_", env_file=".env", extra="ignore")

    account_id: str = ""
    host: str = "127.0.0.1"
    port: int = 4002  # IB Gateway paper-trading default; 4001 live
    client_id: int = 17  # fixed clientId — see Engineering Constraint 1
    benchmark: str = "SPY"
    # Investor/reporting currency. Instrument prices remain in their trading
    # currency and are normalized through dated FX evidence at analysis time.
    base_currency: str = "USD"
    data_dir: Path = Path("data")
    web_dist: Path | None = None
    api_token: str = ""
    api_allowed_origins: str = (
        "http://127.0.0.1:8000,http://localhost:8000,"
        "http://127.0.0.1:5173,http://localhost:5173"
    )
    n_paths: int = 10_000  # Monte Carlo default, Engineering Constraint 10
    # Free-fallback allowlist (Task A2, Global Constraints: free-first data,
    # single-provenance law): comma-separated symbols synced via yfinance
    # instead of IBKR. Empty by default — never a silent substitute for an
    # IBKR failure, only ever this explicit config-gated list.
    yfinance_symbols: str = ""
    # Public-repository safety: terms-sensitive profile retrieval is opt-in.
    # Cached profile data stays local and is never part of the source tree.
    ucits_metadata_enabled: bool = False
    # World feeds run only on an explicit refresh. Social integrations are
    # server-side opt-ins; enabling X acknowledges its metered API charges.
    world_x_enabled: bool = False
    world_x_bearer_token: SecretStr = SecretStr("")
    world_x_query: str = ""
    world_reddit_enabled: bool = False
    world_reddit_client_id: str = ""
    world_reddit_client_secret: SecretStr = SecretStr("")
    world_reddit_refresh_token: SecretStr = SecretStr("")
    world_reddit_user_agent: str = ""
    world_reddit_subreddits: str = "investing,stocks,Economics"
    world_sec_user_agent: str = ""

    def world_config(self):
        from quantmind.world.models import WorldConfig
        return WorldConfig(**{
            name: getattr(self, f"world_{name}")
            for name in WorldConfig.model_fields
        })

    @field_validator("base_currency")
    @classmethod
    def _normalize_base_currency(cls, value: str) -> str:
        currency = str(value or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("base currency must be a three-letter ISO code")
        return currency

    def yfinance_symbol_list(self) -> list[str]:
        return [s.strip() for s in self.yfinance_symbols.split(",") if s.strip()]

    def api_allowed_origin_list(self) -> tuple[str, ...]:
        origins = tuple(
            origin.strip()
            for origin in self.api_allowed_origins.split(",")
            if origin.strip()
        )
        return origins
