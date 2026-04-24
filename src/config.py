# Rocky Trading System - Configuration

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on env vars directly


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass
class Config:
    # Trading mode
    mode: TradingMode = TradingMode.PAPER

    # Polymarket CLOB API
    clob_api_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    private_key: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    chain_id: int = 137  # Polygon mainnet

    # BTC price data sources
    coingecko_api: str = "https://api.coingecko.com/api/v3"
    binance_api: str = "https://api.binance.com/api/v3"

    # Risk management
    max_risk_pct: float = 0.20        # Max 20% of balance per trade
    min_confidence: float = 0.65      # Min 65% confidence to trade
    max_consecutive_losses: int = 3   # Pause after 3 consecutive losses
    daily_loss_limit_pct: float = 0.40  # Max 40% daily drawdown

    # Position sizing by confidence
    sizing_tiers: dict = field(default_factory=lambda: {
        0.65: 0.10,  # 65-74% confidence → 10% of balance
        0.75: 0.15,  # 75-84% confidence → 15% of balance
        0.85: 0.20,  # 85%+ confidence → 20% of balance (max)
    })

    # Trading loop
    loop_interval_seconds: int = 300  # 5 minutes
    market_scan_keyword: str = "Bitcoin"

    # Paper trading
    paper_starting_balance: float = 5.00

    # Paths
    base_dir: str = ""
    journal_path: str = ""
    state_path: str = ""
    log_path: str = ""

    def __post_init__(self):
        if not self.base_dir:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not self.journal_path:
            self.journal_path = os.path.join(self.base_dir, "logs", "trades.jsonl")
        if not self.state_path:
            self.state_path = os.path.join(self.base_dir, "logs", "state.json")
        if not self.log_path:
            self.log_path = os.path.join(self.base_dir, "logs", "rocky.log")

        # Load from environment
        self.private_key = os.getenv("POLY_PRIVATE_KEY", self.private_key)
        self.api_key = os.getenv("POLY_API_KEY", self.api_key)
        self.api_secret = os.getenv("POLY_API_SECRET", self.api_secret)
        self.api_passphrase = os.getenv("POLY_API_PASSPHRASE", self.api_passphrase)

        mode_env = os.getenv("TRADING_MODE", "").lower()
        if mode_env in ("paper", "live"):
            self.mode = TradingMode(mode_env)

        # Risk parameter overrides from environment
        if os.getenv("ROCKY_MAX_RISK_PCT"):
            self.max_risk_pct = float(os.getenv("ROCKY_MAX_RISK_PCT"))
        if os.getenv("ROCKY_MIN_CONFIDENCE"):
            self.min_confidence = float(os.getenv("ROCKY_MIN_CONFIDENCE"))
        if os.getenv("ROCKY_MAX_CONSECUTIVE_LOSSES"):
            self.max_consecutive_losses = int(os.getenv("ROCKY_MAX_CONSECUTIVE_LOSSES"))
        if os.getenv("ROCKY_DAILY_LOSS_LIMIT"):
            self.daily_loss_limit_pct = float(os.getenv("ROCKY_DAILY_LOSS_LIMIT"))
        if os.getenv("ROCKY_STARTING_BALANCE"):
            self.paper_starting_balance = float(os.getenv("ROCKY_STARTING_BALANCE"))
        if os.getenv("ROCKY_LOOP_INTERVAL"):
            self.loop_interval_seconds = int(os.getenv("ROCKY_LOOP_INTERVAL"))

    def get_position_size_pct(self, confidence: float) -> float:
        """Return position size as fraction of balance based on confidence."""
        if confidence < self.min_confidence:
            return 0.0
        size = 0.0
        for threshold, pct in sorted(self.sizing_tiers.items()):
            if confidence >= threshold:
                size = pct
        return size
