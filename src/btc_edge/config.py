"""Configuration loader for BTC-Edge.

Combines a declarative YAML file (``config/config.yaml``) with environment
variables (``.env``) using Pydantic v2 + pydantic-settings. The resulting
:class:`Settings` object is the single source of truth consumed by every module.

Paper mode has been removed; live routing is gated behind
``ALLOW_LIVE_TRADING == "TRUE"`` (see :meth:`Settings.live_trading_allowed`).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class TradingConfig(BaseModel):
    mode: Literal["live"] = "live"
    base_currency: str = "USD"
    bankroll: float = 20.0
    max_daily_loss_pct: float = 5.0
    # Per-trade cap: max risk on any single order, as % of the (live) bankroll.
    # Raised from 2% to 15% so a small account can actually grow: on a tiny
    # balance the venue minimum (5 shares Polymarket / 1 contract Kalshi) is the
    # real floor, and the *exposure* cap below is what stops the bot stacking
    # orders into the whole bankroll.
    quarter_kelly_cap_pct: float = 15.0
    # Total open notional (per venue) may never exceed this % of the live
    # balance. This is the "don't burn the bankroll" guard: it stops the bot
    # from piling multiple minimum-size orders on top of each other between
    # balance polls.
    max_exposure_pct: float = 35.0
    # Polymarket is more volatile (wider 5-share min relative to a small
    # account, thinner liquidity), so it gets a TIGHTER per-trade cap and a
    # LOWER total-exposure cap than Kalshi. This keeps the PM leg from
    # "drunk-betting" the bankroll while Kalshi trades at full efficiency.
    polymarket_cap_pct: float = 1.0
    polymarket_exposure_pct: float = 20.0
    min_ev_threshold: float = 0.02
    # Don't open a new position on a side that has ALREADY moved this window.
    # ``window_delta_pct`` is in percent; e.g. 0.1 means "skip up-bets once BTC is
    # already +0.1% since open" (chasing). 0 disables the guard.
    chase_guard_pct: float = 0.1
    # Seconds at the start of a window to stay flat (let information develop).
    entry_warmup_seconds: float = 60.0
    # Auto-sell (take profit) when a position's mark-to-market gain reaches this
    # percent above entry, then let the strategies re-enter if there's still edge.
    take_profit_pct: float = 85.0
    # Near-certain all-in: when the late-scalp fires at 93-97c AND the model is
    # >=97% confident (a genuine near-certainty in the final seconds), size up to
    # this % of the live balance instead of the conservative Kelly cap. Deliberately
    # NOT 100% — a single tail loss must never wipe the bankroll.
    certainty_all_in_pct: float = 50.0
    # Polymarket-specific hard price ceiling (a 0..1 fraction). Lower than Kalshi's
    # 0.72 so the PM leg only enters when the contract is genuinely cheap (value),
    # never "throwing in 70-90c" on its first bid of a window.
    polymarket_max_contract_price: float = 0.60
    # LLM veto gates (local Ollama 7B + remote DeepSeek grader). When True every BUY
    # must clear both; when False they are skipped entirely so the fast deterministic
    # gates (risk +4.5% net edge, swarm, time-decay, chase) decide alone. Set False for
    # the fastest autonomous flow (the LLM gates were holding every signal for hours).
    llm_gates: bool = True

    @field_validator(
        "polymarket_max_contract_price",
    )
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("must be between 0 and 1")
        return v

    @field_validator(
        "bankroll",
        "max_daily_loss_pct",
        "quarter_kelly_cap_pct",
        "max_exposure_pct",
        "polymarket_cap_pct",
        "polymarket_exposure_pct",
        "chase_guard_pct",
        "entry_warmup_seconds",
    )
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be positive")
        return v


class PolymarketConfig(BaseModel):
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    min_order_shares: float = 5.0
    taker_fee_scale: float = 0.07
    maker_fee: float = 0.0
    timeframes: list[int] = Field(default_factory=lambda: [300, 900])


class KalshiConfig(BaseModel):
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    demo_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    ws_url: str = "wss://external-feeds.kalshi.com/trade-api/v2/external-feeds"
    rate_limit_balance_per_sec: float = 1.0
    market_ticker: str = "KXBTC15M"
    use_demo: bool = True

    @property
    def active_base_url(self) -> str:
        return self.demo_base_url if self.use_demo else self.base_url


class CoinbaseConfig(BaseModel):
    ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    rolling_window_seconds: int = 60


class BybitConfig(BaseModel):
    ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    symbol: str = "BTCUSDT"


class DeribitConfig(BaseModel):
    rest_url: str = "https://www.deribit.com/api/v2"
    index: str = "btc_usd"


class ConnectorsConfig(BaseModel):
    coinbase: CoinbaseConfig = Field(default_factory=CoinbaseConfig)
    bybit: BybitConfig = Field(default_factory=BybitConfig)
    deribit: DeribitConfig = Field(default_factory=DeribitConfig)


class FeaturesConfig(BaseModel):
    window_delta_weight: float = 6.0
    regime_lookback_seconds: int = 300
    book_imbalance_levels: int = 3
    vol_rv_iv_threshold: float = 1.0
    funding_zscore_threshold: float = 2.0


class StrategyConfig(BaseModel):
    enabled: bool = True


class LateScalpConfig(StrategyConfig):
    min_time_remaining: int = 10
    max_time_remaining: int = 120
    min_prob: float = 0.93
    max_prob: float = 0.98
    min_window_delta_pct: float = 0.05


class MarketMakerConfig(StrategyConfig):
    min_time_remaining: int = 120
    levels: int = 3
    min_spread: float = 0.02
    refresh_seconds: int = 15
    spot_move_pct: float = 0.03
    min_edge: float = 0.02  # directional edge (model prob vs market mid) to enter


class CrowdFollowConfig(StrategyConfig):
    min_time_remaining: int = 180
    max_time_remaining: int = 300
    min_contract_price: float = 0.60


class IvMispricingConfig(StrategyConfig):
    min_divergence: float = 0.02


class StrategiesConfig(BaseModel):
    late_scalp: LateScalpConfig = Field(default_factory=LateScalpConfig)
    market_maker: MarketMakerConfig = Field(default_factory=MarketMakerConfig)
    crowd_follow: CrowdFollowConfig = Field(default_factory=CrowdFollowConfig)
    iv_mispricing: IvMispricingConfig = Field(default_factory=IvMispricingConfig)


class MlConfig(BaseModel):
    trailing_window: int = 200
    lgbm_num_leaves: int = 31
    lgbm_learning_rate: float = 0.05


class NewsConfig(BaseModel):
    enabled: bool = True
    refresh_seconds: int = 300
    min_relevance: float = 0.15
    max_items_per_source: int = 20
    sentiment_backend: str = "lexicon"  # lexicon | transformer
    window: int = 200
    min_signals: int = 5
    downgrade_threshold: float = 0.45
    disable_threshold: float = 0.35
    promote_threshold: float = 0.55


class DatabaseConfig(BaseModel):
    path: str = "data/btc_edge.db"


class AIConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"


class TelegramConfig(BaseModel):
    enabled: bool = False


class SafetyConfig(BaseModel):
    trade_only_keys: bool = True  # hard-block any withdrawal path (must stay True)
    kill_switch_file: str = "data/kill_switch.json"


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class LoggingConfig(BaseModel):
    level: str = "INFO"


class OllamaConfig(BaseModel):
    """Local Ollama execution-gate settings (endpoint, model, inference knobs)."""

    endpoint: str = "http://127.0.0.1:11434/api/chat"
    model: str = "qwen2.5-coder:7b-instruct-q4_K_M"
    num_ctx: int = 512
    num_predict: int = 3
    temperature: float = 0.0
    num_gpu: int = 99  # 99 = offload every layer to GPU (fits a 6GB card for 7B q4)
    # "-1" = keep the model resident forever; "0" = unload after each request;
    # or a duration string like "5m"/"30s".
    keep_alive: str = "-1"
    timeout_seconds: float = 2.0  # hard cap per verdict (7B needs ~0.7-2.0s)


class Settings(BaseSettings):
    """Top-level settings: YAML file + environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    trading: TradingConfig = Field(default_factory=TradingConfig)
    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    ml: MlConfig = Field(default_factory=MlConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)

    # Environment-only overrides (also read from .env).
    allow_live_trading: bool = Field(default=False, alias="ALLOW_LIVE_TRADING")
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    dashboard_api_key: str = Field(default="", alias="DASHBOARD_API_KEY")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    kalshi_api_key_id: str = Field(default="", alias="KALSHI_API_KEY_ID")
    kalshi_private_key: str = Field(default="", alias="KALSHI_PRIVATE_KEY")
    kalshi_access_key_id: str = Field(default="", alias="KALSHI_ACCESS_KEY_ID")
    kalshi_access_key: str = Field(default="", alias="KALSHI_ACCESS_KEY")
    kalshi_use_demo: bool | None = Field(default=None, alias="KALSHI_USE_DEMO")
    polymarket_wallet: str = Field(default="", alias="POLYMARKET_WALLET")
    polymarket_private_key: str = Field(default="", alias="POLYMARKET_PRIVATE_KEY")
    config_path: Path = Field(default=DEFAULT_CONFIG_PATH, alias="BTC_EDGE_CONFIG_PATH")

    # "Gusto" recovery mode (keyword-gated aggressive Polymarket betting).
    # ``gusto_mode_keyword`` is the secret word an operator must type to arm it;
    # empty means the mode can never be armed. It auto-disarms after
    # ``gusto_mode_timeout_seconds`` so it can never leak indefinitely.
    gusto_mode_keyword: str = Field(default="", alias="GUSTO_MODE_KEYWORD")
    gusto_mode_timeout_seconds: int = Field(default=900, alias="GUSTO_MODE_TIMEOUT_SECONDS")

    @field_validator("allow_live_trading", "kalshi_use_demo", mode="before")
    @classmethod
    def _coerce_bool(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower() in {"true", "1", "yes", "on"}
        return v

    def live_trading_allowed(self) -> bool:
        """Single live gate: ALLOW_LIVE_TRADING == 'TRUE' (paper mode removed)."""
        return os.getenv("ALLOW_LIVE_TRADING") == "TRUE"

    @property
    def db_path(self) -> Path:
        return Path(self.database.path)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config YAML at {path}: expected a mapping")
    return data


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Load settings from YAML (and .env), merging env overrides.

    ``config_path`` defaults to ``config/config.yaml`` relative to the current
    working directory, or the ``BTC_EDGE_CONFIG_PATH`` environment variable.
    """
    path = Path(config_path) if config_path else Path(
        os.getenv("BTC_EDGE_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))
    )
    yaml_data = _load_yaml(path) if path.exists() else {}

    kalshi_env = os.getenv("KALSHI_USE_DEMO")
    if kalshi_env is not None:
        yaml_data.setdefault("kalshi", {})["use_demo"] = (
            kalshi_env.strip().lower() in {"true", "1", "yes", "on"}
        )

    settings = Settings(_env_file=os.getenv("BTC_EDGE_ENV_FILE", ".env"), **yaml_data)
    settings.config_path = path
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings instance."""
    return load_settings()


def is_live_allowed(settings: Settings | None = None) -> bool:
    """Convenience wrapper for the live gate."""
    settings = settings or get_settings()
    return settings.live_trading_allowed()
