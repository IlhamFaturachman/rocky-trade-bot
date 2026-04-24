"""
Rocky Trading System - Execution Engine
Handles trade execution in both paper and live modes.
"""

import json
import time
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

from .config import Config, TradingMode
from .models import TradeSignal

logger = logging.getLogger("rocky.executor")


@dataclass
class TradeRecord:
    """A completed trade record."""
    trade_id: int
    timestamp: float
    mode: str               # "paper" or "live"
    direction: str          # "up" or "down"
    confidence: float
    stake_usd: float
    entry_price: float
    token_id: str
    market_question: str
    condition_id: str
    btc_price_at_entry: float
    reasoning: list[str]
    candle_open_price: float = 0.0    # Binance candle open for resolution
    raw_llm_response: str = ""        # V2: full LLM reasoning
    # Filled after resolution
    result: Optional[str] = None      # "win" or "loss"
    payout: float = 0.0
    pnl: float = 0.0
    balance_after: float = 0.0
    resolved_at: Optional[float] = None
    candle_close_price: float = 0.0   # Binance candle close at resolution

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class ExecutionEngine:
    """Executes trades in paper or live mode."""

    def __init__(self, config: Config):
        self.config = config
        self.balance = config.paper_starting_balance
        self.trade_count = 0
        self.consecutive_losses = 0
        self.daily_starting_balance = self.balance
        self.trades: list[TradeRecord] = []
        self._load_state()

    def execute(self, signal: TradeSignal) -> Optional[TradeRecord]:
        """Execute a trade based on the signal."""
        # Pre-trade checks
        if not self._pre_trade_checks(signal):
            return None

        # Calculate stake
        stake = self.balance * signal.stake_pct
        stake = round(stake, 4)

        if stake < 0.01:
            logger.warning(f"Stake too small: ${stake:.4f}, skipping")
            return None

        self.trade_count += 1

        # Deduct stake from balance at trade time to prevent over-leveraging
        self.balance -= stake
        self.balance = round(self.balance, 4)

        if self.config.mode == TradingMode.PAPER:
            record = self._execute_paper(signal, stake)
        else:
            record = self._execute_live(signal, stake)

        if record:
            self.trades.append(record)
            self._save_state()
            self._append_journal(record)
        else:
            # Execution failed — refund the stake
            self.balance += stake
            self.balance = round(self.balance, 4)

        return record

    def resolve_trade(self, record: TradeRecord, won: bool) -> TradeRecord:
        """Resolve a trade after market settlement."""
        record.resolved_at = time.time()

        if won:
            # Payout = stake / entry_price (buying at entry_price, pays $1 if correct)
            record.payout = record.stake_usd / record.entry_price
            record.pnl = record.payout - record.stake_usd
            record.result = "win"
            self.consecutive_losses = 0
        else:
            record.payout = 0.0
            record.pnl = -record.stake_usd
            record.result = "loss"
            self.consecutive_losses += 1

        # Add back payout (stake was already deducted at trade time)
        self.balance += record.payout
        self.balance = round(self.balance, 4)
        record.balance_after = self.balance

        logger.info(
            f"Trade #{record.trade_id} resolved: {record.result.upper()} | "
            f"P&L: ${record.pnl:+.4f} | Balance: ${self.balance:.4f}"
        )

        self._save_state()
        self._append_journal(record, resolved=True)

        return record

    def _pre_trade_checks(self, signal: TradeSignal) -> bool:
        """Run all pre-trade risk checks."""
        # Check confidence threshold
        if signal.confidence < self.config.min_confidence:
            logger.info(
                f"Confidence {signal.confidence:.0%} below minimum "
                f"{self.config.min_confidence:.0%}, no trade"
            )
            return False

        # Check consecutive losses
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            logger.warning(
                f"Hit {self.consecutive_losses} consecutive losses, "
                f"pausing trading. Need manual reset or a win."
            )
            return False

        # Check daily loss limit
        daily_loss = (self.daily_starting_balance - self.balance) / self.daily_starting_balance
        if daily_loss >= self.config.daily_loss_limit_pct:
            logger.warning(
                f"Daily loss limit hit: {daily_loss:.0%} drawdown. "
                f"No more trades today."
            )
            return False

        # Check minimum balance
        if self.balance < 0.10:
            logger.error(f"Balance too low: ${self.balance:.4f}. Cannot trade.")
            return False

        return True

    def _execute_paper(self, signal: TradeSignal, stake: float) -> TradeRecord:
        """Execute a paper trade (simulated)."""
        record = TradeRecord(
            trade_id=self.trade_count,
            timestamp=time.time(),
            mode="paper",
            direction=signal.direction,
            confidence=signal.confidence,
            stake_usd=stake,
            entry_price=signal.expected_price,
            token_id=signal.token_id,
            market_question=signal.market.question,
            condition_id=signal.market.condition_id,
            btc_price_at_entry=signal.snapshot.price_usd,
            reasoning=signal.reasoning,
            candle_open_price=getattr(signal, 'candle_open_price', 0.0),
            raw_llm_response=getattr(signal, 'raw_llm_response', ''),
        )

        logger.info(
            f"📝 PAPER TRADE #{record.trade_id} | "
            f"{'📈 UP' if signal.direction == 'up' else '📉 DOWN'} | "
            f"Confidence: {signal.confidence:.0%} | "
            f"Stake: ${stake:.4f} @ {signal.expected_price:.4f} | "
            f"BTC: ${signal.snapshot.price_usd:,.2f}"
        )

        return record

    def _execute_live(self, signal: TradeSignal, stake: float) -> Optional[TradeRecord]:
        """Execute a live trade via Polymarket CLOB API."""
        # TODO: Implement live trading with py-clob-client
        # This will use:
        # - ClobClient for order placement
        # - Market buy of the selected token
        # - Order monitoring and confirmation
        logger.error("Live trading not yet implemented!")
        return None

    def _load_state(self):
        """Load trading state from disk."""
        try:
            if os.path.exists(self.config.state_path):
                with open(self.config.state_path, "r") as f:
                    state = json.load(f)
                self.balance = state.get("balance", self.config.paper_starting_balance)
                self.trade_count = state.get("trade_count", 0)
                self.consecutive_losses = state.get("consecutive_losses", 0)
                self.daily_starting_balance = state.get(
                    "daily_starting_balance", self.balance
                )
                logger.info(
                    f"Loaded state: balance=${self.balance:.4f}, "
                    f"trades={self.trade_count}"
                )
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        """Persist trading state to disk."""
        try:
            os.makedirs(os.path.dirname(self.config.state_path), exist_ok=True)
            state = {
                "balance": self.balance,
                "trade_count": self.trade_count,
                "consecutive_losses": self.consecutive_losses,
                "daily_starting_balance": self.daily_starting_balance,
                "last_updated": time.time(),
            }
            with open(self.config.state_path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _append_journal(self, record: TradeRecord, resolved: bool = False):
        """Append trade record to the JSONL journal."""
        try:
            os.makedirs(os.path.dirname(self.config.journal_path), exist_ok=True)
            entry = asdict(record)
            entry["_event"] = "resolved" if resolved else "opened"
            with open(self.config.journal_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write journal: {e}")

    def reset_daily(self):
        """Reset daily tracking (call at start of each trading day)."""
        self.daily_starting_balance = self.balance
        self._save_state()
        logger.info(f"Daily reset. Starting balance: ${self.balance:.4f}")

    def get_stats(self) -> dict:
        """Get trading statistics."""
        wins = [t for t in self.trades if t.result == "win"]
        losses = [t for t in self.trades if t.result == "loss"]
        resolved = wins + losses

        total_pnl = sum(t.pnl for t in resolved)
        win_rate = len(wins) / len(resolved) if resolved else 0

        return {
            "balance": self.balance,
            "total_trades": self.trade_count,
            "resolved_trades": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "consecutive_losses": self.consecutive_losses,
            "best_trade": max((t.pnl for t in resolved), default=0),
            "worst_trade": min((t.pnl for t in resolved), default=0),
        }
