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
import json
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
from src.edge import EdgeGate
from src.features import build_cycle_features, tape_fair_p_up, best_bid_ask
from src.twap_source import TwapSource

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
        self.twap = TwapSource()
        self.scanner = MarketScanner(config)
        self.intel = IntelligenceEngine(config, rtds=self.twap)
        if engine == "v2":
            self.decision = DecisionEngineV2(config)
        else:
            self.decision = DecisionEngine(config)
        self.executor = ExecutionEngine(config)
        self.notifier = TelegramNotifier()
        self.edge = EdgeGate()
        self.running = True
        self.cycle_count = 0
        self.pending_trades = []  # Trades awaiting resolution
        self.skip_stats: dict[str, int] = {}
        self.last_cycle_meta: dict = {}
        # Data-collection: avoid duplicate shadows per market window+flag
        self._shadow_keys: set[str] = set()
        self.data_collect = os.environ.get("ROCKY_DATA_COLLECT", "true").lower() in (
            "1", "true", "yes",
        )

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
        logger.info(
            f"   Fill model: walk-asks + fee_bps={self.executor.fills.fee_bps:.0f} "
            f"slip_bps={self.executor.fills.slip_bps:.0f} "
            f"apply_fee={self.executor.fills.apply_fee_to_entry} "
            f"live_enabled={self.executor.fills.live_enabled} "
            f"live_dry={self.executor.fills.live_dry_run}"
        )
        logger.info(f"   Data collect (shadow): {self.data_collect}")
        logger.info("=" * 60)

        # Start Polymarket RTDS TWAP source (Chainlink 30s BTC/USD settlement feed)
        self.twap.start()
        logger.info(
            f"   TWAP source: Polymarket RTDS Chainlink 30s "
            f"({'connecting...' if not self.twap.connected else 'connected'})"
        )

        # Reload unresolved opens from journal (survives restarts — fixes stuck pending)
        reloaded = self._reload_pending_from_journal()
        if reloaded:
            logger.info(f"♻️  Reloaded {reloaded} unresolved trade(s) from journal into pending")
            # Immediately try to settle anything already past the 5m window
            self._resolve_pending_trades()

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

        # Heartbeat early so watchdog does not treat long LLM cycles as dead.
        # last_cycle is otherwise only written at skip/edge/trade end.
        self.last_cycle_meta = {
            **(self.last_cycle_meta or {}),
            "ts": time.time(),
            "event": "cycle_start",
            "cycle": self.cycle_count,
        }
        self._write_cycle_meta()

        # Step 1: Resolve any pending trades
        self._resolve_pending_trades()

        # Step 2: Gather intelligence
        logger.info("📊 Gathering BTC intelligence...")
        snapshot = self.intel.get_snapshot()

        if snapshot.price_usd <= 0:
            logger.warning("Could not get BTC price, skipping cycle")
            return

        # Staleness guard: if RTDS spot is >60s old, skip — trading on stale
        # prices is worse than skipping. RTDS reconnects automatically (5s),
        # so this only triggers during genuine outages.
        spot_age = self.twap.get_spot_age_seconds()
        if spot_age > 60:
            logger.warning(
                f"RTDS spot stale ({spot_age:.0f}s old), skipping cycle — "
                f"RTDS connected={self.twap.connected}"
            )
            self.last_cycle_meta = {"event": "skip", "code": "stale_price",
                                   "reason": f"RTDS spot {spot_age:.0f}s old"}
            self._write_cycle_meta()
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

        # Fill price-to-beat from RTDS Chainlink spot (Binance fallback if RTDS miss)
        if getattr(market, "price_to_beat", 0) <= 0:
            ptb = self._fetch_candle_open_at(time.time())
            if ptb <= 0 and snapshot.raw_klines:
                # Approximate: open of the current 5m bucket from 1m klines
                try:
                    # last closed 1m open of the 5m window
                    ptb = float(snapshot.raw_klines[-(int(time.time()) % 300 // 60 or 5)]["open"])
                except Exception:
                    ptb = 0.0
            if ptb > 0:
                market.price_to_beat = ptb
                logger.info(f"   Price to beat (RTDS Chainlink spot): ${ptb:,.2f}")

        # Step 6: Analyze and decide
        logger.info(f"🧠 Analyzing ({self.engine_version})...")

        orderbook = {}
        if market.tokens:
            # Prefer UP token book for mid/ask; edge gate will use side-specific ask
            token_id = market.tokens[0].get("token_id", "")
            if token_id:
                orderbook = self.scanner.fetch_market_orderbook(token_id)

        llm_failed = False
        if self.engine_version == "v2":
            signal = self.decision.analyze(market, snapshot, orderbook=orderbook)
            # Fallback to v1 if LLM path fails (502 / timeout)
            if signal is None:
                llm_failed = True
                logger.warning("V2 LLM failed — falling back to v1 rules for this cycle")
                signal = DecisionEngine(self.config).analyze(market, snapshot)
        else:
            signal = self.decision.analyze(market, snapshot)

        # Always log rich cycle features (research gold)
        edge_preview = None
        if signal and signal.direction in ("up", "down"):
            side_book_preview = orderbook
            if signal.token_id:
                side_book_preview = self.scanner.fetch_market_orderbook(signal.token_id) or orderbook
            edge_preview = self.edge.evaluate(
                market=market,
                snapshot=snapshot,
                direction=signal.direction,
                confidence=signal.confidence,
                orderbook=side_book_preview,
            )

        feats = build_cycle_features(
            market=market,
            snapshot=snapshot,
            orderbook=orderbook,
            signal=signal,
            edge=edge_preview,
            engine=self.engine_version,
            cycle=self.cycle_count,
            extra={"llm_failed": llm_failed, "data_collect": self.data_collect},
        )
        self._append_cycle_features(feats)

        if not signal:
            logger.info("No signal generated")
            self._record_skip("no_signal", "No signal generated", market, snapshot)
            if self.data_collect:
                self._open_shadow_from_tape(
                    market, snapshot, orderbook, flag="no_signal", reason="No signal generated"
                )
                self._maybe_open_late_and_dual_shadows(market, snapshot, orderbook, base_flag="no_signal")
            return

        if not signal.should_trade:
            logger.info(
                f"⏸️  Signal direction is '{signal.direction}' — not tradeable. Sitting out."
            )
            self._record_skip(
                "model_skip",
                f"model said {signal.direction}",
                market,
                snapshot,
                signal=signal,
            )
            if self.data_collect:
                self._open_shadow_from_tape(
                    market, snapshot, orderbook,
                    flag="model_skip",
                    reason=f"model said {signal.direction}",
                    confidence=getattr(signal, "confidence", 0.55) or 0.55,
                    raw_llm=getattr(signal, "raw_llm_response", ""),
                    reasoning=list(getattr(signal, "reasoning", []) or []),
                )
                self._maybe_open_late_and_dual_shadows(
                    market, snapshot, orderbook, base_flag="model_skip", signal=signal
                )
            return

        # Step 6b: Edge Pack hard gates (timing / chop / EV / flip)
        side_book = orderbook
        if signal.token_id:
            side_book = self.scanner.fetch_market_orderbook(signal.token_id) or orderbook

        edge = edge_preview or self.edge.evaluate(
            market=market,
            snapshot=snapshot,
            direction=signal.direction,
            confidence=signal.confidence,
            orderbook=side_book,
        )
        self.last_cycle_meta = {
            "ts": time.time(),
            "market": market.question,
            "direction": signal.direction,
            "confidence": signal.confidence,
            "edge_allow": edge.allow,
            "edge_mode": edge.mode,
            "edge_reason": edge.reason,
            "skip_code": edge.skip_code,
            "t_left": edge.t_left,
            "t_elapsed": edge.t_elapsed,
            "ask": edge.ask_price,
            "edge": edge.edge,
            "p_model": edge.p_model,
            "ptb_bps": edge.ptb_bps,
            "yes": market.yes_price,
            "no": market.no_price,
            "btc": snapshot.price_usd,
            "ptb": getattr(market, "price_to_beat", 0),
            "phase": feats.get("phase"),
            "fair_p_up_tape": feats.get("fair_p_up_tape"),
        }
        self._write_cycle_meta()

        if not edge.allow:
            logger.info(f"🛡️ EDGE BLOCK [{edge.skip_code}] {edge.reason}")
            self._record_skip(edge.skip_code or "edge", edge.reason, market, snapshot, signal=signal, edge=edge)
            self.notifier.send_skip(f"[{edge.skip_code}] {edge.reason}")
            if self.data_collect:
                if edge.ask_price > 0:
                    signal.expected_price = edge.ask_price
                    signal.ask_price = edge.ask_price
                signal.edge_mode = edge.mode
                signal.edge = edge.edge
                signal.p_model = edge.p_model
                signal.fee_buffer = edge.fee_buffer
                signal.t_left = edge.t_left
                signal.t_elapsed = edge.t_elapsed
                self._open_shadow_dedup(
                    signal,
                    flag=edge.skip_code or "edge_block",
                    flag_reason=edge.reason,
                    market=market,
                    orderbook=side_book,
                )
                self._maybe_open_late_and_dual_shadows(
                    market, snapshot, side_book, base_flag=edge.skip_code or "edge_block", signal=signal
                )
            return

        # Apply realistic paper entry = ask
        if edge.ask_price > 0:
            signal.expected_price = edge.ask_price
            signal.ask_price = edge.ask_price
        signal.edge_mode = edge.mode
        signal.edge = edge.edge
        signal.p_model = edge.p_model
        signal.fee_buffer = edge.fee_buffer
        signal.t_left = edge.t_left
        signal.t_elapsed = edge.t_elapsed
        if edge.details:
            signal.reasoning = list(signal.reasoning or []) + edge.details[:3]

        # Step 7: Execute REAL paper trade
        logger.info(
            f"🚀 Executing trade ({edge.mode}) edge={edge.edge:.3f} ask={edge.ask_price:.3f}..."
        )
        record = self.executor.execute(signal, orderbook=side_book)

        if record:
            self.pending_trades.append(record)
            logger.info(f"✅ Trade #{record.trade_id} placed successfully")
            self.notifier.send_trade_opened(record)
            # Also log a shadow twin for counterfactual opposite? optional dual already covers research
        else:
            logger.info("Trade execution blocked by risk checks")
            self._record_skip("risk", "executor risk checks blocked", market, snapshot, signal=signal, edge=edge)
            if self.data_collect:
                self._open_shadow_dedup(
                    signal,
                    flag="risk",
                    flag_reason="executor risk checks blocked",
                    market=market,
                    orderbook=side_book,
                )

    def _shadow_key(self, market, flag: str, direction: str) -> str:
        cid = getattr(market, "condition_id", "") or getattr(market, "market_slug", "") or getattr(market, "question", "")
        end = getattr(market, "end_date", "") or ""
        return f"{cid}|{end}|{flag}|{direction}"

    def _open_shadow_dedup(self, signal, flag: str, flag_reason: str, market=None, orderbook=None) -> None:
        m = market or getattr(signal, "market", None)
        if m is None or signal is None:
            return
        key = self._shadow_key(m, flag, signal.direction)
        if key in self._shadow_keys:
            logger.debug(f"shadow dedup skip {key}")
            return
        shadow = self.executor.open_shadow(
            signal, flag=flag, flag_reason=flag_reason, stake_pct=0.05, orderbook=orderbook
        )
        if shadow:
            if shadow.candle_open_price <= 0:
                shadow.candle_open_price = getattr(m, "price_to_beat", 0) or 0
            self.pending_trades.append(shadow)
            self._shadow_keys.add(key)
            # bound memory
            if len(self._shadow_keys) > 5000:
                self._shadow_keys = set(list(self._shadow_keys)[-2000:])

    def _maybe_open_late_and_dual_shadows(self, market, snapshot, orderbook, base_flag: str, signal=None):
        """
        Maximal collection:
        - dual: opposite direction shadow (counterfactual)
        - late: if in late window, extra late_sniper-tagged shadow on tape side
        """
        if not self.data_collect:
            return
        t_left = float(getattr(market, "seconds_to_end", 0) or 0)
        # Dual opposite of tape direction
        price = float(getattr(snapshot, "price_usd", 0) or 0)
        ptb = float(getattr(market, "price_to_beat", 0) or 0)
        tape_dir = "up" if (ptb > 0 and price >= ptb) else ("down" if ptb > 0 else "up")
        opp = "down" if tape_dir == "up" else "up"
        self._open_shadow_from_tape(
            market, snapshot, orderbook,
            flag=f"dual_{base_flag}",
            reason=f"counterfactual opposite of tape ({opp})",
            confidence=0.55,
            force_direction=opp,
        )
        # Late-window specialist sample
        if 30 < t_left <= 90:
            self._open_shadow_from_tape(
                market, snapshot, orderbook,
                flag="late_window",
                reason=f"late window sample t_left={t_left:.0f}s",
                confidence=0.60,
                force_direction=tape_dir,
            )

    def _append_cycle_features(self, feats: dict):
        try:
            path = os.path.join(self.config.base_dir, "logs", "cycles.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                import json as _json
                f.write(_json.dumps(feats, default=str) + "\n")
        except Exception as e:
            logger.debug(f"cycle features write failed: {e}")
        # keep last_cycle rich
        try:
            self.last_cycle_meta = {**(self.last_cycle_meta or {}), **{
                k: feats.get(k) for k in (
                    "phase", "fair_p_up_tape", "edge_up_vs_ask", "edge_down_vs_ask",
                    "book_spread", "ptb_bps", "signed_ptb_bps", "t_left", "t_elapsed",
                    "signal_dir", "signal_conf", "edge_allow", "edge_skip_code", "llm_failed",
                ) if k in feats
            }}
            self._write_cycle_meta()
        except Exception:
            pass

    def _open_shadow_from_tape(
        self,
        market,
        snapshot,
        orderbook,
        flag: str,
        reason: str,
        confidence: float = 0.55,
        raw_llm: str = "",
        reasoning: list | None = None,
        force_direction: str | None = None,
    ):
        """When model skips, still open a hypothetical trade from tape (PTB side)."""
        from src.models import TradeSignal
        from src.edge import _ask_for_direction

        price = float(getattr(snapshot, "price_usd", 0) or 0)
        ptb = float(getattr(market, "price_to_beat", 0) or 0)
        if force_direction in ("up", "down"):
            direction = force_direction
        elif ptb > 0 and price > 0:
            direction = "up" if price >= ptb else "down"
        else:
            mom = str(getattr(snapshot, "momentum", "neutral") or "neutral")
            direction = "up" if mom == "bullish" else ("down" if mom == "bearish" else "up")

        # token selection
        token_id = ""
        if market.tokens:
            for t in market.tokens:
                oc = str(t.get("outcome", "")).lower()
                if direction == "up" and oc in ("up", "yes"):
                    token_id = str(t.get("token_id", ""))
                if direction == "down" and oc in ("down", "no"):
                    token_id = str(t.get("token_id", ""))
            if not token_id:
                idx = 0 if direction == "up" else min(1, len(market.tokens) - 1)
                token_id = str(market.tokens[idx].get("token_id", ""))

        side_book = orderbook
        if token_id:
            side_book = self.scanner.fetch_market_orderbook(token_id) or orderbook
        ask = _ask_for_direction(market, direction, side_book)

        edge = self.edge.evaluate(
            market=market,
            snapshot=snapshot,
            direction=direction,
            confidence=confidence,
            orderbook=side_book,
        )
        fair = tape_fair_p_up(snapshot, market)
        p_model = fair if direction == "up" else (1.0 - fair)

        signal = TradeSignal(
            direction=direction,
            confidence=float(confidence or 0.55),
            reasoning=list(reasoning or []) + [f"shadow tape dir={direction}", reason],
            market=market,
            snapshot=snapshot,
            token_id=token_id,
            expected_price=ask,
            stake_pct=0.05,
            raw_llm_response=raw_llm or "",
            candle_open_price=ptb,
            edge_mode=edge.mode or "shadow",
            edge=edge.edge,
            ask_price=ask if ask > 0 else edge.ask_price,
            p_model=p_model,
            fee_buffer=edge.fee_buffer,
            t_left=edge.t_left or getattr(market, "seconds_to_end", 0) or 0,
            t_elapsed=edge.t_elapsed,
            skip_code=flag,
        )
        self._open_shadow_dedup(
            signal, flag=flag, flag_reason=reason, market=market, orderbook=side_book
        )
        return signal

    def _record_skip(self, code: str, reason: str, market=None, snapshot=None, signal=None, edge=None):
        code = code or "other"
        self.skip_stats[code] = self.skip_stats.get(code, 0) + 1
        meta = {
            "ts": time.time(),
            "event": "skip",
            "code": code,
            "reason": reason,
            "skip_stats": dict(self.skip_stats),
        }
        if market is not None:
            meta["market"] = getattr(market, "question", "")
            meta["t_left"] = getattr(market, "seconds_to_end", 0)
            meta["yes"] = getattr(market, "yes_price", 0)
            meta["no"] = getattr(market, "no_price", 0)
            meta["ptb"] = getattr(market, "price_to_beat", 0)
        if snapshot is not None:
            meta["btc"] = getattr(snapshot, "price_usd", 0)
            meta["mom"] = getattr(snapshot, "momentum", "")
            meta["vol"] = getattr(snapshot, "volatility", "")
        if signal is not None:
            meta["direction"] = getattr(signal, "direction", "")
            meta["confidence"] = getattr(signal, "confidence", 0)
        if edge is not None:
            meta["edge_mode"] = edge.mode
            meta["edge"] = edge.edge
            meta["ask"] = edge.ask_price
            meta["p_model"] = edge.p_model
        self.last_cycle_meta = meta
        self._write_cycle_meta()
        # Append skip journal line for dashboard
        try:
            path = os.path.join(self.config.base_dir, "logs", "skips.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                import json as _json
                f.write(_json.dumps(meta, default=str) + "\n")
        except Exception as e:
            logger.debug(f"skip journal write failed: {e}")

    def _write_cycle_meta(self):
        try:
            import json as _json
            path = os.path.join(self.config.base_dir, "logs", "last_cycle.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = dict(self.last_cycle_meta or {})
            payload["balance"] = self.executor.balance
            payload["trade_count"] = self.executor.trade_count
            payload["pending"] = len(self.pending_trades)
            payload["skip_stats"] = dict(self.skip_stats)
            payload["cycle"] = self.cycle_count
            with open(path, "w") as f:
                _json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"last_cycle write failed: {e}")

    def _reload_pending_from_journal(self) -> int:
        """
        Rebuild pending_trades from trades.jsonl after restart.

        Dashboard treats opened-without-resolved as pending. Pending only lived
        in memory before — process death left ghost opens forever. Reload them
        so resolve can close them.
        """
        path = getattr(self.config, "journal_path", "") or ""
        if not path or not os.path.exists(path):
            return 0

        opened: dict[int, dict] = {}
        resolved_ids: set[int] = set()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid = row.get("trade_id")
                    if tid is None:
                        continue
                    try:
                        tid = int(tid)
                    except (TypeError, ValueError):
                        continue
                    ev = row.get("_event")
                    res = row.get("result")
                    if ev == "resolved" or res in ("win", "loss"):
                        resolved_ids.add(tid)
                        continue
                    if ev == "opened" or res in (None, "", "pending", "open"):
                        opened[tid] = row
        except Exception as e:
            logger.warning(f"journal reload failed: {e}")
            return 0

        # Already in memory (same process re-entry)
        already = {int(t.trade_id) for t in self.pending_trades if getattr(t, "trade_id", None) is not None}
        from src.executor import TradeRecord

        n = 0
        for tid, row in opened.items():
            if tid in resolved_ids or tid in already:
                continue
            try:
                rec = TradeRecord(
                    trade_id=tid,
                    timestamp=float(row.get("timestamp") or time.time()),
                    mode=str(row.get("mode") or "paper"),
                    direction=str(row.get("direction") or "up"),
                    confidence=float(row.get("confidence") or 0),
                    stake_usd=float(row.get("stake_usd") or 0),
                    entry_price=float(row.get("entry_price") or 0.5),
                    token_id=str(row.get("token_id") or ""),
                    market_question=str(row.get("market_question") or ""),
                    condition_id=str(row.get("condition_id") or ""),
                    btc_price_at_entry=float(row.get("btc_price_at_entry") or 0),
                    reasoning=list(row.get("reasoning") or []),
                    candle_open_price=float(row.get("candle_open_price") or 0),
                    raw_llm_response=str(row.get("raw_llm_response") or ""),
                    edge_mode=str(row.get("edge_mode") or ""),
                    edge=float(row.get("edge") or 0),
                    ask_price=float(row.get("ask_price") or row.get("entry_price") or 0),
                    p_model=float(row.get("p_model") or 0),
                    fee_buffer=float(row.get("fee_buffer") or 0),
                    t_left=float(row.get("t_left") or 0),
                    t_elapsed=float(row.get("t_elapsed") or 0),
                    is_shadow=bool(row.get("is_shadow")),
                    flag=str(row.get("flag") or ("shadow" if row.get("is_shadow") else "real")),
                    flag_reason=str(row.get("flag_reason") or ""),
                    expected_ask=float(row.get("expected_ask") or row.get("ask_price") or 0),
                    vwap=float(row.get("vwap") or 0),
                    best_ask=float(row.get("best_ask") or 0),
                    slip_vs_best=float(row.get("slip_vs_best") or 0),
                    filled_shares=float(row.get("filled_shares") or 0),
                    filled_usd=float(row.get("filled_usd") or 0),
                    fill_ok=bool(row.get("fill_ok", True)),
                    fill_reason=str(row.get("fill_reason") or ""),
                    order_id=str(row.get("order_id") or ""),
                    order_status=str(row.get("order_status") or ""),
                )
            except Exception as e:
                logger.debug(f"skip bad journal row #{tid}: {e}")
                continue
            self.pending_trades.append(rec)
            already.add(tid)
            n += 1
            # restore shadow dedup keys so we don't re-open same window
            try:
                key = f"{rec.condition_id}|{rec.market_question}|{rec.flag}|{rec.direction}"
                self._shadow_keys.add(key)
            except Exception:
                pass
        return n

    def _resolve_pending_trades(self):
        """
        Resolve pending paper trades using Polymarket resolution logic:
        UP wins if Chainlink 30s TWAP at window end >= Chainlink spot at open.
        DOWN wins if TWAP < spot at open.
        """
        if not self.pending_trades:
            return

        resolved = []
        # 5m window + small buffer; allow env override for research
        min_age = float(os.environ.get("ROCKY_RESOLVE_MIN_AGE_SEC", "300"))

        for trade in self.pending_trades:
            # Only resolve trades older than the market window
            age = time.time() - trade.timestamp
            if age < min_age:
                continue

            if self.config.mode in (TradingMode.PAPER, TradingMode.LIVE):
                # Get the candle open price (price to beat)
                candle_open = trade.candle_open_price

                # If we don't have a candle open from the market metadata,
                # fetch the Chainlink spot at window open via RTDS
                if candle_open <= 0:
                    candle_open = self._fetch_candle_open_at(trade.timestamp)

                if candle_open <= 0:
                    # Last resort: use BTC price at entry
                    candle_open = trade.btc_price_at_entry

                if candle_open <= 0:
                    # Genuinely unrecoverable — no price data anywhere.
                    # Mark void so it stops retried every cycle.
                    age = time.time() - trade.timestamp
                    logger.error(
                        f"Trade #{trade.trade_id}: no candle open price after all fallbacks "
                        f"(age {age/60:.0f}min) — marking VOID"
                    )
                    trade.result = "void"
                    trade.candle_close_price = 0.0
                    self.executor.resolve_trade(trade, won=False)
                    resolved.append(trade)
                    continue

                # Fetch the candle close price (actual 5-min candle close from Binance)
                candle_close = self._fetch_candle_close_at(trade.timestamp)

                if candle_close <= 0:
                    # Unrecoverable close price after 600s — VOID instead of guessing.
                    # Using current spot (10+ min post-window) or entry price (~5 min pre-close)
                    # fabricates fake win/loss. VOID keeps the 7-day dataset clean.
                    age = time.time() - trade.timestamp
                    if age > 600:
                        logger.warning(
                            f"Trade #{trade.trade_id}: no candle close after {age/60:.0f}min "
                            f"(RTDS TWAP miss) — marking VOID (unrecovered)"
                        )
                        trade.result = "void"
                        trade.candle_close_price = 0.0
                        self.executor.resolve_trade(trade, won=False)
                        resolved.append(trade)
                        continue
                    logger.warning(f"Trade #{trade.trade_id}: no candle close, skipping (will retry)")
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
                # Telegram only for real trades (shadow = data collection noise)
                if not getattr(trade, "is_shadow", False):
                    self.notifier.send_trade_resolved(trade)
                    if self.executor.consecutive_losses >= 2:
                        self.notifier.send_warning(
                            f"{self.executor.consecutive_losses} consecutive losses. "
                            f"Balance: ${self.executor.balance:.4f}"
                        )

        # Remove resolved trades from pending
        for trade in resolved:
            self.pending_trades.remove(trade)

    def _fetch_candle_open_at(self, trade_timestamp: float) -> float:
        """Fetch the 5-min window open price. RTDS Chainlink spot first, Binance fallback."""
        # 1. RTDS: Chainlink spot at window start (exact Polymarket source)
        window_start = int((trade_timestamp // 300) * 300)
        rtds_price = self.twap.get_price_at(window_start, tolerance=10)
        if rtds_price and rtds_price > 0:
            logger.info(f"Candle open from RTDS: ${rtds_price:,.2f}")
            return rtds_price
        # 1b. RTDS klines fallback: open of 1m candle at window start
        klines = self.twap.get_klines_1m()
        if klines:
            for k in klines:
                if k["open_time"] == window_start:
                    logger.info(f"Candle open from RTDS klines: ${k['open']:,.2f}")
                    return k["open"]
        # 2. Binance 5m kline fallback
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
                        return float(k[1])
                return float(klines[-1][1])
        except Exception as e:
            logger.debug(f"Failed to fetch candle open (Binance, expected on DE VPS): {e}")
        return 0.0

    def _fetch_candle_close_at(self, trade_timestamp: float) -> float:
        """Fetch the settlement price for resolution.

        As of Aug 7 2026, Polymarket 5-min BTC markets settle via 30-second TWAP
        (Time-Weighted Average Price) of the Chainlink feed, NOT a single candle close.

        Primary source: Polymarket RTDS WebSocket (Chainlink TWAP 30s) — the EXACT
        settlement feed Polymarket uses. Cached in TwapSource background thread.
        Fallback: Binance 1s klines averaged over the last 30s of the window.
        """
        window_start = int((trade_timestamp // 300) * 300)
        window_end = window_start + 300

        # ── Primary: Polymarket RTDS Chainlink TWAP ──
        # Broad tolerance: TWAP ticks arrive ~1s but window_end is at :00 boundary.
        # Use 120s to catch the nearest tick even if RTDS had a brief gap.
        twap = self.twap.get_twap_at(window_end, tolerance=120)
        if twap and twap > 0:
            logger.debug(f"TWAP from RTDS: ${twap:,.2f} (window_end={window_end})")
            return twap

        # ── Fallback 1: RTDS Chainlink spot at window_end (always cached) ──
        spot = self.twap.get_price_at(window_end, tolerance=60)
        if spot and spot > 0:
            logger.debug(f"TWAP miss → spot from RTDS: ${spot:,.2f} (window_end={window_end})")
            return spot

        # ── Fallback 2: RTDS 1m kline close nearest to window_end ──
        klines = self.twap.get_klines_1m()
        if klines:
            closest = None
            best_diff = 999
            for k in klines:
                diff = abs(k["open_time"] - window_end)
                if diff < best_diff:
                    best_diff = diff
                    closest = k
            if closest and best_diff <= 120:
                logger.debug(f"TWAP+spot miss → kline close: ${closest['close']:,.2f} (diff={best_diff}s)")
                return closest["close"]

        # ── Fallback 3: Binance 1s klines (unreachable on DE VPS, kept for completeness) ──
        try:
            twap_start = window_end - 30
            resp = requests.get(
                f"{self.config.binance_api}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1s",
                    "startTime": int(twap_start * 1000),
                    "endTime": int(window_end * 1000),
                    "limit": 60,
                },
                timeout=10,
            )
            resp.raise_for_status()
            klines = resp.json()
            if klines:
                closes = [float(k[4]) for k in klines]
                if closes:
                    twap = sum(closes) / len(closes)
                    logger.debug(
                        f"TWAP from Binance fallback: ${twap:,.2f} "
                        f"({len(closes)} samples)"
                    )
                    return twap
        except Exception as e:
            logger.warning(f"TWAP fetch failed (all RTDS fallbacks missed + Binance error): {e}")
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
