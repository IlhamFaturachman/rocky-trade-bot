"""
Rocky Trading System - Shared Models
Common data structures used across v1, v2, and executor.
"""

from dataclasses import dataclass, field
from typing import Optional

# Forward references — avoid circular imports
# BtcMarket and BtcSnapshot are imported by the engines, not here.


@dataclass
class TradeSignal:
    """A trade recommendation. Used by both v1 and v2 decision engines."""
    direction: str          # "up", "down", or "skip"
    confidence: float       # 0.0 to 1.0
    reasoning: list[str]    # Reasons for the signal
    market: object          # BtcMarket (typed loosely to avoid circular import)
    snapshot: object        # BtcSnapshot
    token_id: str           # Which token to buy
    expected_price: float   # Expected entry price
    stake_pct: float        # Position size as % of balance
    raw_llm_response: str = ""  # V2 only: full LLM output for journal
    candle_open_price: float = 0.0  # Binance candle open price for resolution
    # Edge Pack metadata
    edge_mode: str = ""
    edge: float = 0.0
    ask_price: float = 0.0
    p_model: float = 0.0
    fee_buffer: float = 0.0
    t_left: float = 0.0
    t_elapsed: float = 0.0
    skip_code: str = ""

    @property
    def should_trade(self) -> bool:
        """Check if direction is tradeable. Confidence threshold is enforced by executor."""
        return self.direction in ("up", "down")

    def summary(self) -> str:
        emoji = {"up": "📈", "down": "📉", "skip": "⏸️"}.get(self.direction, "❓")
        return (
            f"{emoji} BTC {self.direction.upper()} | "
            f"Confidence: {self.confidence:.0%} | "
            f"Stake: {self.stake_pct:.0%} of balance | "
            f"Reasons: {'; '.join(self.reasoning[:3])}"
        )
