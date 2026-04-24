#!/usr/bin/env python3
"""
Rocky Trading System - Main Loop
Autonomous 5-minute BTC prediction market trading agent.

Usage:
    python3 src/main.py --mode paper    # Paper trading (default)
    python3 src/main.py --mode live     # Live trading (requires API keys)
"""

import sys
import os
import time
import signal
import logging
import argparse
import requests
from datetime import datetime, timezone, timedelta

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config, TradingMode
from src.scanner import MarketScanner
from src.intelligence import IntelligenceEngine
from src.decision import DecisionEngine
from src.decision_v2 import DecisionEngineV2
from src.executor import ExecutionEngine
from src.notifier import TelegramNotifier

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(config: Config):
    os.makedirs(os.path.dirname(config.log_path), exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(config.log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # Console handler
    console_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)


logger = logging.getLogger("rocky.main")


# ── Main Trading Loop ────────────────────────────────────────────────────────

class Rocky:
    """The autonomous trading agent."""

    def __init__(self, config: Config, engine: str = "v1"):
        self.config = config
        self.engine_version = engine
        self.scanner = MarketScanner(config)
        self.intel = IntelligenceEngine(config)
        if engine == "v2":
            self.decision = DecisionEngineV2(config)
        else:
            self.decision = DecisionEngine(config)
        self.executor = ExecutionEngine(config)
        self.notifier = TelegramNotifier()
        self.running = True
        self.cycle_count = 0
        self.pending_trades = []  # Trades awaiting resolution

    def run(self):
        """Main trading loop."""
        logger.info("=" * 60)
        logger.info(f"🪨 Rocky PolyClaw Trader starting up")
        logger.info(f"   Engine: {self.engine_version.upper()} {'(rule-based)' if self.engine_version == 'v1' else '(Opus 4.6 LLM)'}")
        logger.info(f"   Mode: {self.config.mode.value.upper()}")
        logger.info(f"   Balance: ${self.executor.balance:.4f}")
        logger.info(f"   Max risk per trade: {self.config.max_risk_pct:.0%}")
        logger.info(f"   Min confidence: {self.config.min_confidence:.0%}")
        logger.info(f"   Loop interval: {self.config.loop_interval_seconds}s")
        logger.info("=" * 60)

        # Telegram startup notification
        self.notifier.send_startup(self.config, self.engine_version)

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        last_daily_reset = None

        while self.running:
            try:
                tz_offset = int(os.environ.get("ROCKY_TIMEZONE_OFFSET", "0"))
                now = datetime.now(timezone(timedelta(hours=tz_offset)))

                # Daily reset at midnight
                today = now.date()
                if last_daily_reset != today:
                    self.executor.reset_daily()
                    last_daily_reset = today
                    logger.info(f"📅 New trading day: {today}")

                # Run one trading cycle
                self.cycle_count += 1
                self._run_cycle()

                # Print periodic stats
                if self.cycle_count % 12 == 0:  # Every hour
                    self._print_stats()

                # Wait for next cycle
                logger.info(
                    f"💤 Sleeping {self.config.loop_interval_seconds}s "
                    f"until next cycle..."
                )
                time.sleep(self.config.loop_interval_seconds)

            except KeyboardInterrupt:
                self._shutdown(None, None)
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                logger.info("Sleeping 60s before retry...")
                time.sleep(60)

    def _run_cycle(self):
        """Execute one trading cycle."""
        logger.info(f"\n{'─' * 40}")
        logger.info(f"🔄 Cycle #{self.cycle_count} | Balance: ${self.executor.balance:.4f}")
        logger.info(f"{'─' * 40}")

        # Step 1: Resolve any pending trades
        self._resolve_pending_trades()

        # Step 2: Gather intelligence
        logger.info("📊 Gathering BTC intelligence...")
        snapshot = self.intel.get_snapshot()

        if snapshot.price_usd <= 0:
            logger.warning("Could not get BTC price, skipping cycle")
            return

        # Step 3: Enrich with news
        # V2 (LLM): every cycle — SearXNG is self-hosted, no rate limits
        # V1 (rules): every 3rd cycle to avoid external API rate limits
        if self.engine_version == "v2" or self.cycle_count % 3 == 1:
            snapshot = self.intel.enrich_with_news(snapshot)

        # Step 4: Scan for markets
        logger.info("🔍 Scanning Polymarket for BTC 5-min markets...")
        markets = self.scanner.fetch_btc_markets()

        if not markets:
            logger.info("No active BTC 5-min markets found. Waiting...")
            return

        # Step 5: Pick best market
        market = self.scanner.get_best_market(markets)
        if not market:
            logger.info("No suitable market found")
            return

        logger.info(f"🎯 Target market: {market.question}")
        logger.info(f"   YES: {market.yes_price:.4f} | NO: {market.no_price:.4f}")

        # Step 6: Analyze and decide
        logger.info(f"🧠 Analyzing ({self.engine_version})...")

        if self.engine_version == "v2":
            # V2: fetch orderbook and pass everything to the LLM
            orderbook = {}
            if market.tokens:
                token_id = market.tokens[0].get("token_id", "")
                if token_id:
                    orderbook = self.scanner.fetch_market_orderbook(token_id)
            signal = self.decision.analyze(market, snapshot, orderbook=orderbook)
        else:
            signal = self.decision.analyze(market, snapshot)

        if not signal:
            logger.info("No signal generated")
            return

        if not signal.should_trade:
            logger.info(
                f"⏸️  Signal direction is '{signal.direction}' — not tradeable. Sitting out."
            )
            return

        # Step 7: Execute trade
        logger.info("🚀 Executing trade...")
        record = self.executor.execute(signal)

        if record:
            self.pending_trades.append(record)
            logger.info(f"✅ Trade #{record.trade_id} placed successfully")
            self.notifier.send_trade_opened(record)
        else:
            logger.info("Trade execution blocked by risk checks")

    def _resolve_pending_trades(self):
        """
        Resolve pending paper trades using Polymarket resolution logic:
        UP wins if Binance BTC/USDT 5-min candle CLOSE >= OPEN.
        DOWN wins if CLOSE < OPEN.
        """
        if not self.pending_trades:
            return

        resolved = []

        for trade in self.pending_trades:
            # Only resolve trades older than 5 minutes
            age = time.time() - trade.timestamp
            if age < 300:  # 5 minutes
                continue

            if self.config.mode == TradingMode.PAPER:
                # Get the candle open price (price to beat)
                candle_open = trade.candle_open_price

                # If we don't have a candle open from the market metadata,
                # fetch the 5-min candle from Binance that covers the trade time
                if candle_open <= 0:
                    candle_open = self._fetch_candle_open_at(trade.timestamp)

                if candle_open <= 0:
                    # Last resort: use BTC price at entry
                    candle_open = trade.btc_price_at_entry

                if candle_open <= 0:
                    logger.warning(f"Trade #{trade.trade_id}: no candle open price, skipping")
                    continue

                # Fetch the candle close price (current price as proxy,
                # or the actual 5-min candle close from Binance)
                candle_close = self._fetch_candle_close_at(trade.timestamp)

                if candle_close <= 0:
                    # Fallback: use current price
                    snapshot = self.intel.get_snapshot()
                    candle_close = snapshot.price_usd

                if candle_close <= 0:
                    continue

                # Polymarket resolution: UP wins if close >= open
                if trade.direction == "up":
                    won = candle_close >= candle_open
                else:  # down
                    won = candle_close < candle_open

                trade.candle_close_price = candle_close
                self.executor.resolve_trade(trade, won)

                price_change = ((candle_close - candle_open) / candle_open) * 100
                emoji = "✅" if won else "❌"
                logger.info(
                    f"{emoji} Trade #{trade.trade_id} resolved: "
                    f"{'WIN' if won else 'LOSS'} | "
                    f"Candle: open ${candle_open:,.2f} → close ${candle_close:,.2f} "
                    f"({price_change:+.4f}%)"
                )

                resolved.append(trade)
                self.notifier.send_trade_resolved(trade)

                # Warn on consecutive losses
                if self.executor.consecutive_losses >= 2:
                    self.notifier.send_warning(
                        f"{self.executor.consecutive_losses} consecutive losses. "
                        f"Balance: ${self.executor.balance:.4f}"
                    )

        # Remove resolved trades from pending
        for trade in resolved:
            self.pending_trades.remove(trade)

    def _fetch_candle_open_at(self, trade_timestamp: float) -> float:
        """Fetch the Binance 5-min candle open price that covers the trade time."""
        try:
            # Get the 5-min candle that started at or before trade_timestamp
            start_ms = int(trade_timestamp * 1000)
            resp = requests.get(
                f"{self.config.binance_api}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "5m",
                    "startTime": start_ms - 300000,  # 5 min before
                    "limit": 2,
                },
                timeout=10,
            )
            resp.raise_for_status()
            klines = resp.json()
            if klines:
                # Return the open of the candle that contains our trade time
                for k in klines:
                    if k[0] <= start_ms <= k[6]:  # open_time <= trade <= close_time
                        return float(k[1])  # open price
                # Fallback: last candle's open
                return float(klines[-1][1])
        except Exception as e:
            logger.warning(f"Failed to fetch candle open: {e}")
        return 0.0

    def _fetch_candle_close_at(self, trade_timestamp: float) -> float:
        """Fetch the Binance 5-min candle close price for resolution."""
        try:
            start_ms = int(trade_timestamp * 1000)
            resp = requests.get(
                f"{self.config.binance_api}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "5m",
                    "startTime": start_ms - 300000,
                    "limit": 2,
                },
                timeout=10,
            )
            resp.raise_for_status()
            klines = resp.json()
            if klines:
                for k in klines:
                    if k[0] <= start_ms <= k[6]:
                        return float(k[4])  # close price
                return float(klines[-1][4])
        except Exception as e:
            logger.warning(f"Failed to fetch candle close: {e}")
        return 0.0

    def _print_stats(self):
        """Print trading statistics."""
        stats = self.executor.get_stats()
        logger.info("\n" + "=" * 40)
        logger.info("📊 TRADING STATS")
        logger.info(f"   Balance: ${stats['balance']:.4f}")
        logger.info(f"   Total trades: {stats['total_trades']}")
        logger.info(f"   Win rate: {stats['win_rate']:.0%}")
        logger.info(f"   Total P&L: ${stats['total_pnl']:+.4f}")
        logger.info(f"   Best trade: ${stats['best_trade']:+.4f}")
        logger.info(f"   Worst trade: ${stats['worst_trade']:+.4f}")
        logger.info(f"   Consecutive losses: {stats['consecutive_losses']}")
        logger.info("=" * 40 + "\n")
        self.notifier.send_stats(stats)

    def _shutdown(self, signum, frame):
        """Graceful shutdown."""
        logger.info("\n🛑 Rocky shutting down gracefully...")
        self.running = False
        self._print_stats()
        self.notifier.send_shutdown()
        logger.info("Goodbye. 🪨")
        time.sleep(1)  # Let notification thread finish
        sys.exit(0)


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Rocky PolyClaw Trader")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--engine",
        choices=["v1", "v2"],
        default=os.environ.get("ROCKY_ENGINE", "v1"),
        help="Decision engine: v1 (rule-based) or v2 (Opus 4.6 LLM)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Loop interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=5.00,
        help="Starting paper balance (default: 5.00)",
    )
    args = parser.parse_args()

    config = Config(
        mode=TradingMode(args.mode),
        loop_interval_seconds=args.interval,
        paper_starting_balance=args.balance,
    )

    setup_logging(config)

    rocky = Rocky(config, engine=args.engine)
    rocky.run()


if __name__ == "__main__":
    main()
