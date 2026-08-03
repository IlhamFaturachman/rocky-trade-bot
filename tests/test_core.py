#!/usr/bin/env python3
"""
Quick smoke test for Rocky's core logic (no network dependencies).
Run: python3 tests/test_core.py
"""

import sys
import os
import time
import json
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock requests before importing modules that need it
import types
mock_requests = types.ModuleType("requests")
mock_requests.Session = type("Session", (), {
    "__init__": lambda self: None,
    "headers": property(lambda self: {}),
    "get": lambda self, *a, **kw: None,
})
mock_requests.RequestException = Exception
sys.modules["requests"] = mock_requests

from src.config import Config, TradingMode
from src.scanner import BtcMarket
from src.intelligence import BtcSnapshot
from src.models import TradeSignal
from src.decision import DecisionEngine
from src.executor import ExecutionEngine


def test_config():
    """Test configuration and position sizing."""
    config = Config()
    assert config.mode == TradingMode.PAPER
    assert config.max_risk_pct == 0.10
    assert config.min_confidence == 0.68

    # Position sizing
    assert config.get_position_size_pct(0.50) == 0.0   # Below threshold
    assert config.get_position_size_pct(0.65) == 0.0   # Below 0.68 min confidence
    assert config.get_position_size_pct(0.70) == 0.05  # Tier 1 (68-74%)
    assert config.get_position_size_pct(0.78) == 0.08  # Tier 2 (75-84%)
    assert config.get_position_size_pct(0.85) == 0.10  # Tier 3 (85%+)
    assert config.get_position_size_pct(0.95) == 0.10  # Max

    print("✅ Config tests passed")


def test_decision_engine():
    """Test decision engine with mock data."""
    config = Config()
    engine = DecisionEngine(config)

    market = BtcMarket(
        condition_id="test-123",
        question="Will BTC go up in the next 5 minutes?",
        market_slug="btc-5min-up",
        tokens=[
            {"token_id": "token-yes", "outcome": "Yes"},
            {"token_id": "token-no", "outcome": "No"},
        ],
        end_date="2026-04-24T19:00:00Z",
        active=True,
        yes_price=0.50,
        no_price=0.50,
        volume=1000,
        liquidity=500,
        description="BTC 5-minute up/down market",
    )

    # Bullish snapshot
    bullish = BtcSnapshot(
        timestamp=time.time(),
        price_usd=94000.0,
        price_change_1m=0.15,
        price_change_5m=0.25,
        price_change_15m=0.30,
        price_change_1h=0.50,
        volume_24h=25e9,
        high_24h=94500,
        low_24h=93000,
        momentum="bullish",
        volatility="normal",
        news_sentiment="bullish",
        news_headlines=["Bitcoin surges past $94K on ETF inflows"],
    )

    signal = engine.analyze(market, bullish)
    assert signal is not None
    assert signal.direction == "up"
    assert signal.confidence > 0.5
    print(f"   Bullish → {signal.direction} @ {signal.confidence:.0%} confidence")

    # Bearish snapshot
    bearish = BtcSnapshot(
        timestamp=time.time(),
        price_usd=93000.0,
        price_change_1m=-0.20,
        price_change_5m=-0.30,
        price_change_15m=-0.40,
        price_change_1h=-1.0,
        volume_24h=30e9,
        high_24h=94500,
        low_24h=92500,
        momentum="bearish",
        volatility="high",
        news_sentiment="bearish",
        news_headlines=["Bitcoin crashes on SEC crackdown"],
    )

    signal = engine.analyze(market, bearish)
    assert signal is not None
    assert signal.direction == "down"
    assert signal.confidence > 0.5
    print(f"   Bearish → {signal.direction} @ {signal.confidence:.0%} confidence")

    # Neutral — low confidence
    neutral = BtcSnapshot(
        timestamp=time.time(),
        price_usd=93500.0,
        price_change_1m=0.01,
        price_change_5m=-0.01,
        price_change_15m=0.02,
        price_change_1h=0.05,
        momentum="neutral",
        volatility="low",
        news_sentiment="neutral",
    )

    signal = engine.analyze(market, neutral)
    assert signal is not None
    assert signal.confidence < 0.70
    print(f"   Neutral → {signal.direction} @ {signal.confidence:.0%} confidence")

    print("✅ Decision engine tests passed")


def test_executor_paper():
    """Test paper trade execution and resolution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(mode=TradingMode.PAPER, paper_starting_balance=5.00)
        config.state_path = os.path.join(tmpdir, "state.json")
        config.journal_path = os.path.join(tmpdir, "trades.jsonl")

        executor = ExecutionEngine(config)
        assert executor.balance == 5.00

        market = BtcMarket(
            condition_id="test-123", question="Will BTC go up?",
            market_slug="btc-5min", end_date="2026-04-24T19:00:00Z",
            tokens=[{"token_id": "yes-token", "outcome": "Yes"},
                    {"token_id": "no-token", "outcome": "No"}],
            active=True, yes_price=0.50, no_price=0.50,
            volume=1000, liquidity=500,
        )
        snapshot = BtcSnapshot(timestamp=time.time(), price_usd=94000.0, momentum="bullish")

        signal = TradeSignal(
            direction="up", confidence=0.75, reasoning=["Test trade"],
            market=market, snapshot=snapshot, token_id="yes-token",
            expected_price=0.50, stake_pct=0.15,
        )

        record = executor.execute(signal)
        assert record is not None
        assert record.stake_usd == 0.75  # 15% of $5
        assert record.mode == "paper"
        # Balance should be deducted at trade time
        assert executor.balance == 4.25, f"Balance after trade: {executor.balance} (expected 4.25)"
        print(f"   Executed: ${record.stake_usd:.4f} stake, balance now ${executor.balance:.4f}")

        # Resolve as win. Entry is fee-bumped: 0.50 × (1 + 150bps) = 0.5075,
        # so shares = 0.75 / 0.5075 and payout = shares × $1.
        executor.resolve_trade(record, won=True)
        assert record.result == "win"
        assert record.payout == pytest.approx(0.75 / 0.5075, rel=1e-9), \
            f"Payout: {record.payout} (expected {0.75 / 0.5075})"
        assert record.pnl == pytest.approx(0.75 / 0.5075 - 0.75, rel=1e-9), \
            f"PnL: {record.pnl} (expected {0.75 / 0.5075 - 0.75})"
        assert executor.balance == pytest.approx(round(4.25 + 0.75 / 0.5075, 4), rel=1e-9), \
            f"Balance after win: {executor.balance} (expected {round(4.25 + 0.75 / 0.5075, 4)})"
        print(f"   WIN: payout ${record.payout:.4f}, P&L ${record.pnl:+.4f} → balance ${executor.balance:.4f}")

        # Verify journal
        assert os.path.exists(config.journal_path)
        with open(config.journal_path) as f:
            lines = f.readlines()
        assert len(lines) == 2  # opened + resolved
        print(f"   Journal: {len(lines)} entries written")

        stats = executor.get_stats()
        assert stats["wins"] == 1
        assert stats["win_rate"] == 1.0

        print("✅ Executor tests passed")


def test_risk_management():
    """Test risk management blocks trades correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(mode=TradingMode.PAPER, paper_starting_balance=5.00,
                        max_consecutive_losses=3)
        config.state_path = os.path.join(tmpdir, "state.json")
        config.journal_path = os.path.join(tmpdir, "trades.jsonl")

        executor = ExecutionEngine(config)
        executor.consecutive_losses = 3

        market = BtcMarket(
            condition_id="test", question="Test", market_slug="test",
            tokens=[{"token_id": "t1", "outcome": "Yes"}],
            end_date="2026-04-24T19:00:00Z", active=True,
            yes_price=0.5, no_price=0.5, volume=100, liquidity=100,
        )
        snapshot = BtcSnapshot(timestamp=time.time(), price_usd=94000)

        signal = TradeSignal(
            direction="up", confidence=0.80, reasoning=["Test"],
            market=market, snapshot=snapshot, token_id="t1",
            expected_price=0.5, stake_pct=0.15,
        )

        # Blocked by consecutive loss limit
        record = executor.execute(signal)
        assert record is None
        print("   ✓ Consecutive loss limit blocks trade")

        # Low confidence rejection
        executor.consecutive_losses = 0
        low_signal = TradeSignal(
            direction="up", confidence=0.50, reasoning=["Low conf"],
            market=market, snapshot=snapshot, token_id="t1",
            expected_price=0.5, stake_pct=0.0,
        )
        record = executor.execute(low_signal)
        assert record is None
        print("   ✓ Low confidence blocks trade")

        # Low balance rejection
        executor.balance = 0.05
        record = executor.execute(signal)
        assert record is None
        print("   ✓ Low balance blocks trade")

        print("✅ Risk management tests passed")


if __name__ == "__main__":
    print("\n🪨 Rocky Core Tests\n")
    test_config()
    test_decision_engine()
    test_executor_paper()
    test_risk_management()
    print("\n🎉 All tests passed!\n")
