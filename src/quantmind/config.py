"""Typed configuration. All runtime knobs live here; secrets come from .env (never committed)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QM_", env_file=".env", extra="ignore")

    account_id: str = ""
    host: str = "127.0.0.1"
    port: int = 4002  # IB Gateway paper-trading default; 4001 live
    client_id: int = 17  # fixed clientId — see Engineering Constraint 1
    benchmark: str = "SPY"
    data_dir: Path = Path("data")
    fred_api_key: str = ""
    n_paths: int = 10_000  # Monte Carlo default, Engineering Constraint 10
    # Free-fallback allowlist (Task A2, Global Constraints: free-first data,
    # single-provenance law): comma-separated symbols synced via yfinance
    # instead of IBKR. Empty by default — never a silent substitute for an
    # IBKR failure, only ever this explicit config-gated list.
    yfinance_symbols: str = ""

    def yfinance_symbol_list(self) -> list[str]:
        return [s.strip() for s in self.yfinance_symbols.split(",") if s.strip()]
