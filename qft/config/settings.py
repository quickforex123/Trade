"""Process settings from environment variables (prefix QFT_).

Secrets policy: this class deliberately has NO field for the Groww API secret
or approval key. The execution daemon reads GROWW_API_KEY/GROWW_API_SECRET
directly from its own isolated environment (see qft/brokers/groww_execution.py);
research, backtest and dashboard processes never see them. A read-only access
token (market data) may be provided via QFT_GROWW_READONLY_TOKEN.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from qft.domain.enums import Environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QFT_", env_file=".env", extra="ignore")

    environment: Environment = Environment.BACKTEST
    data_dir: Path = Path("data")
    ledger_path: Path = Path("data/ledger.sqlite")
    risk_config_path: Path = Path("config/risk.yaml")
    strategies_config_path: Path = Path("config/strategies.yaml")

    groww_readonly_token: str = ""  # market-data-scope token only; never the secret

    anthropic_model_deep: str = "claude-fable-5"
    anthropic_model_quick: str = "claude-sonnet-5"
    research_enabled: bool = False

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8600

    log_level: str = "INFO"
