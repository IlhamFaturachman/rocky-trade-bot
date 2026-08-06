"""
Rocky Trading System - Execution Engine

Paper / live / shadow share ONE fill model (walk CLOB asks → VWAP + fee/slip).
Mode switch is env-only: TRADING_MODE=paper|live + ROCKY_LIVE_ENABLED / DRY_RUN.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .config import Config, TradingMode
from .fill_model import FillConfig, FillResult, walk_buy_asks
from .models import TradeSignal

logger = logging.getLogger("rocky.executor")


@dataclass
class TradeRecord:
    """A completed trade record."""

    trade_id: int
    timestamp: float
    mode: str  # paper | live | live_dry
    direction: str  # up | down
    confidence: float
    stake_usd: float
    entry_price: float
    token_id: str
    market_question: str
    condition_id: str
    btc_price_at_entry: float
    reasoning: list[str]
    candle_open_price: float = 0.0
    raw_llm_response: str = ""
    edge_mode: str = ""
    edge: float = 0.0
    ask_price: float = 0.0
    p_model: float = 0.0
    fee_buffer: float = 0.0
    t_left: float = 0.0
    t_elapsed: float = 0.0
    # Data-collection / shadow
    is_shadow: bool = False
    flag: str = ""
    flag_reason: str = ""
    # Fill quality (paper ≈ live)
    expected_ask: float = 0.0
    vwap: float = 0.0
    best_ask: float = 0.0
    slip_vs_best: float = 0.0
    filled_shares: float = 0.0
    filled_usd: float = 0.0
    fill_ok: bool = True
    fill_reason: str = ""
    order_id: str = ""
    order_status: str = ""
    # Resolution
    result: Optional[str] = None
    payout: float = 0.0
    pnl: float = 0.0
    balance_after: float = 0.0
    resolved_at: Optional[float] = None
    candle_close_price: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class ExecutionEngine:
    """Executes trades in paper or live mode with unified fill realism."""

    def __init__(self, config: Config):
        self.config = config
        self.fills = FillConfig.from_env()
        self.balance = config.paper_starting_balance
        self.trade_count = 0
        self.consecutive_losses = 0
        self._paused_cycles = 0  # auto-resume counter for consecutive-loss pause
        self.daily_starting_balance = self.balance
        self.trades: list[TradeRecord] = []
        self._clob = None
        self._load_state()
        self._recover_pending_posts()
        # Wallet sync: if POLY_PRIVATE_KEY set + ROCKY_WALLET_SYNC=true,
        # read real USDC balance from Polymarket and use as authoritative balance.
        # This makes paper = live behavior (real wallet balance drives sizing).
        self._wallet_sync_enabled = os.getenv("ROCKY_WALLET_SYNC", "false").lower() in (
            "1", "true", "yes",
        )
        if self._wallet_sync_enabled:
            self._sync_balance_to_wallet()

    # ── public API ──────────────────────────────────────────────────────────

    def execute(
        self,
        signal: TradeSignal,
        orderbook: Optional[dict] = None,
    ) -> Optional[TradeRecord]:
        """Real paper/live trade (affects balance). Same fill model both modes."""
        if not self._pre_trade_checks(signal):
            return None

        stake = round(self.balance * signal.stake_pct, 2)
        # Polymarket minimum order = $1. If stake below floor, boost to floor.
        # Compounding zone (balance < $20): $1 floor is the user-accepted risk
        # (20% of $5 = $1). Floor is EXEMPT from max_risk_pct — it IS the sizing
        # policy until balance grows past $20, then tier percentages take over.
        min_stake = self.config.min_stake_usd
        compounding_zone = self.balance < 20.0
        if stake < min_stake:
            stake = round(min_stake, 2)
            if compounding_zone:
                logger.info(
                    f"Stake boosted to ${stake:.2f} (Polymarket min order, "
                    f"compounding zone — {stake/self.balance:.1%} of balance)"
                )
            else:
                # Balance >= $20 but tier pct gave < $1 — shouldn't happen
                # (5% of $20 = $1), but guard anyway
                risk_check = stake / self.balance if self.balance > 0 else 1.0
                if risk_check > self.config.max_risk_pct:
                    logger.warning(
                        f"Stake ${stake:.2f} exceeds max_risk "
                        f"{self.config.max_risk_pct:.0%} — skipping"
                    )
                    return None
        if stake < 0.01:
            logger.warning(f"Stake too small: ${stake:.4f}, skipping")
            return None

        self.trade_count += 1
        self.balance -= stake
        self.balance = round(self.balance, 4)

        mode = self.config.mode
        if mode == TradingMode.PAPER:
            record = self._execute_unified(
                signal,
                stake,
                orderbook=orderbook,
                is_shadow=False,
                flag="real",
                flag_reason="",
                mode_label="paper",
                post_live=False,
            )
        else:
            # LIVE path: same fill sim first; post only if enabled & not dry-run
            post = bool(self.fills.live_enabled and not self.fills.live_dry_run)
            label = "live" if post else "live_dry"
            if not self.fills.live_enabled:
                logger.warning(
                    "TRADING_MODE=live but ROCKY_LIVE_ENABLED=false — "
                    "simulating fill only (live_dry). Set ROCKY_LIVE_ENABLED=true "
                    "and ROCKY_LIVE_DRY_RUN=false to post real CLOB orders."
                )
            elif self.fills.live_dry_run:
                logger.warning(
                    "ROCKY_LIVE_DRY_RUN=true — fill sim only, no CLOB post."
                )
            # Crash-safe: persist deducted balance + trade_count BEFORE post,
            # and journal a pending_post entry so a crash between place_market_order
            # and the opened journal leaves a recoverable trail.
            if post:
                self._save_state()
                self._journal_pending_post(self.trade_count, signal, stake)
            record = self._execute_unified(
                signal,
                stake,
                orderbook=orderbook,
                is_shadow=False,
                flag="real",
                flag_reason="",
                mode_label=label,
                post_live=post,
            )

        if record:
            self.trades.append(record)
            self._save_state()
            self._append_journal(record)
        else:
            self.balance += stake
            self.balance = round(self.balance, 4)
            self._save_state()
        return record

    def open_shadow(
        self,
        signal: TradeSignal,
        flag: str,
        flag_reason: str = "",
        stake_pct: float = 0.05,
        orderbook: Optional[dict] = None,
    ) -> Optional[TradeRecord]:
        """
        Shadow trade: same fill model + journal/resolution as paper,
        does NOT touch balance / consecutive losses / risk counters.
        """
        if not signal or signal.direction not in ("up", "down"):
            return None

        pct = stake_pct if stake_pct > 0 else 0.05
        if signal.stake_pct and signal.stake_pct > 0:
            pct = signal.stake_pct
        stake = round(max(self.balance, 1.0) * pct, 4)
        if stake < 0.01:
            stake = 0.05

        self.trade_count += 1
        record = self._execute_unified(
            signal,
            stake,
            orderbook=orderbook,
            is_shadow=True,
            flag=flag or "shadow",
            flag_reason=flag_reason or "",
            mode_label="paper",
            post_live=False,
            allow_fallback_entry=True,
        )
        if record:
            self.trades.append(record)
            self._save_state()
            self._append_journal(record)
            logger.info(
                f"👻 SHADOW #{record.trade_id} [{record.flag}] "
                f"{record.direction.upper()} @ {record.entry_price:.3f} | "
                f"vwap={record.vwap:.3f} slip={record.slip_vs_best:.4f} | "
                f"{(flag_reason or '')[:80]}"
            )
        return record

    def resolve_trade(self, record: TradeRecord, won: bool) -> TradeRecord:
        """Resolve a trade after market settlement."""
        record.resolved_at = time.time()

        if getattr(record, "result", None) == "void":
            # Refund stake for real trades — it was deducted at execute() time.
            # Shadow trades never touched balance, so no refund needed.
            record.payout = 0.0
            record.pnl = 0.0
            if not getattr(record, "is_shadow", False):
                self.balance = round(self.balance + record.stake_usd, 4)
                logger.info(
                    f"Trade #{record.trade_id} VOID — stake ${record.stake_usd:.4f} refunded, "
                    f"balance ${self.balance:.4f}"
                )
            else:
                logger.info(
                    f"Trade #{record.trade_id} VOID (shadow) — no resolution data, balance untouched"
                )
            self._save_state()
            self._append_journal(record, resolved=True)
            return record

        if won:
            record.payout = (
                record.stake_usd / record.entry_price if record.entry_price else 0.0
            )
            record.pnl = record.payout - record.stake_usd
            record.result = "win"
            if not record.is_shadow:
                self.consecutive_losses = 0
                self._paused_cycles = 0
        else:
            record.payout = 0.0
            record.pnl = -record.stake_usd
            record.result = "loss"
            if not record.is_shadow:
                self.consecutive_losses += 1

        if record.is_shadow:
            record.balance_after = self.balance
            logger.info(
                f"👻 SHADOW #{record.trade_id} [{record.flag}] resolved: "
                f"{record.result.upper()} | would-be P&L: ${record.pnl:+.4f} | "
                f"(balance untouched ${self.balance:.4f})"
            )
        else:
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

    # ── risk ────────────────────────────────────────────────────────────────

    def _pre_trade_checks(self, signal: TradeSignal) -> bool:
        if signal.confidence < self.config.min_confidence:
            logger.info(
                f"Confidence {signal.confidence:.0%} below minimum "
                f"{self.config.min_confidence:.0%}, no trade"
            )
            return False

        # Compounding zone (balance < $20): $1 stake = 5-20% of balance.
        # Default limits (3 consecutive, 25% daily) stall bot after 2 losses
        # from $5. Loosen here so bot can trade through normal losing streaks.
        compounding_zone = self.balance < 20.0
        effective_max_losses = 10 if compounding_zone else self.config.max_consecutive_losses
        effective_daily_limit = 1.0 if compounding_zone else self.config.daily_loss_limit_pct

        if self.consecutive_losses >= effective_max_losses:
            self._paused_cycles += 1
            # Auto-resume after 50 paused cycles (~50min) — keeps collecting
            # real-trade performance data during 30-day unattended run.
            # A 10-loss streak is normal over a month; permanent pause kills
            # the "lihat performa trade real" data the user wants.
            if self._paused_cycles >= 50:
                logger.warning(
                    f"Auto-resuming after {self.consecutive_losses} consecutive losses "
                    f"and {self._paused_cycles} paused cycles — resetting loss counter "
                    f"to continue real-trade data collection"
                )
                self.consecutive_losses = 0
                self._paused_cycles = 0
            else:
                logger.warning(
                    f"Hit {self.consecutive_losses} consecutive losses, "
                    f"pausing real trades ({self._paused_cycles}/50 paused cycles) — "
                    f"shadow trades continue"
                )
                return False

        daily_loss = (
            (self.daily_starting_balance - self.balance) / self.daily_starting_balance
            if self.daily_starting_balance > 0
            else 0.0
        )
        if daily_loss >= effective_daily_limit:
            logger.warning(
                f"Daily loss limit hit: {daily_loss:.0%} drawdown. No more trades today."
            )
            return False

        if self.balance < 0.10:
            logger.error(f"Balance too low: ${self.balance:.4f}. Cannot trade.")
            return False

        return True

    # ── unified fill + record ────────────────────────────────────────────────

    def _execute_unified(
        self,
        signal: TradeSignal,
        stake: float,
        *,
        orderbook: Optional[dict],
        is_shadow: bool,
        flag: str,
        flag_reason: str,
        mode_label: str,
        post_live: bool,
        allow_fallback_entry: bool = False,
    ) -> Optional[TradeRecord]:
        """
        Shared path for paper / live_dry / live / shadow:
        1) walk book for VWAP
        2) apply fee bump + slip/max_entry gates
        3) optionally post CLOB market order (live only)
        """
        expected_ask = float(getattr(signal, "ask_price", 0) or 0) or float(
            signal.expected_price or 0
        )
        fill = walk_buy_asks(
            orderbook,
            stake,
            self.fills,
            fallback_ask=expected_ask if (expected_ask > 0 or allow_fallback_entry) else 0.0,
        )

        # Shadow: if book walk fails, still open at quoted ask so research continues
        # (real paper/live hard-reject — no fantasy mid fills)
        if not fill.ok and is_shadow and allow_fallback_entry:
            fb = expected_ask if expected_ask > 0 else 0.5
            fb = min(0.99, max(0.01, float(fb)))
            fee_bps = self.fills.fee_bps if self.fills.apply_fee_to_entry else 0.0
            # Dynamic fee: same formula as fill_model (rate * price * (1-price))
            if fee_bps:
                rate = fee_bps / 10_000.0
                fee_frac = rate * fb * (1 - fb)
                entry_fb = min(0.99, max(0.01, fb * (1.0 + fee_frac)))
                fee_bps = fee_frac * 10_000
            else:
                entry_fb = fb
            fill = FillResult(
                ok=True,
                entry_price=entry_fb,
                vwap=fb,
                best_ask=fb,
                filled_usd=stake,
                filled_shares=stake / fb if fb else 0,
                slip_vs_best=0.0,
                fee_bps_applied=fee_bps,
                reason=f"shadow_fallback_ask ({fill.reason})",
            )

        if not fill.ok:
            logger.info(
                f"FILL REJECT [{mode_label}] {fill.reason} | "
                f"stake=${stake:.2f} ask≈{expected_ask:.3f}"
            )
            return None

        order_id = ""
        order_status = "simulated"
        entry = fill.entry_price

        if post_live:
            live = self._post_live_order(signal, stake, fill)
            if live is None:
                return None
            order_id = str(live.get("order_id") or "")
            order_status = str(live.get("status") or "posted")
            if live.get("entry_price"):
                try:
                    entry = float(live["entry_price"])
                except (TypeError, ValueError):
                    pass

        record = TradeRecord(
            trade_id=self.trade_count,
            timestamp=time.time(),
            mode=mode_label,
            direction=signal.direction,
            confidence=signal.confidence,
            stake_usd=stake,
            entry_price=entry,
            token_id=signal.token_id,
            market_question=getattr(signal.market, "question", "") if signal.market else "",
            condition_id=getattr(signal.market, "condition_id", "") if signal.market else "",
            btc_price_at_entry=getattr(signal.snapshot, "price_usd", 0) if signal.snapshot else 0,
            reasoning=list(signal.reasoning or []),
            candle_open_price=getattr(signal, "candle_open_price", 0.0)
            or (
                getattr(signal.market, "price_to_beat", 0.0) if signal.market else 0.0
            )
            or 0.0,
            raw_llm_response=getattr(signal, "raw_llm_response", ""),
            edge_mode=getattr(signal, "edge_mode", "") or "",
            edge=float(getattr(signal, "edge", 0) or 0),
            ask_price=expected_ask or fill.best_ask,
            p_model=float(getattr(signal, "p_model", 0) or 0),
            fee_buffer=float(getattr(signal, "fee_buffer", 0) or 0),
            t_left=float(getattr(signal, "t_left", 0) or 0),
            t_elapsed=float(getattr(signal, "t_elapsed", 0) or 0),
            is_shadow=is_shadow,
            flag=flag or ("shadow" if is_shadow else "real"),
            flag_reason=flag_reason or "",
            expected_ask=expected_ask or fill.best_ask,
            vwap=fill.vwap,
            best_ask=fill.best_ask,
            slip_vs_best=fill.slip_vs_best,
            filled_shares=fill.filled_shares,
            filled_usd=fill.filled_usd or stake,
            fill_ok=True,
            fill_reason=fill.reason,
            order_id=order_id,
            order_status=order_status,
        )

        if is_shadow:
            tag = "👻 SHADOW"
        elif mode_label == "live":
            tag = "💰 LIVE"
        elif mode_label == "live_dry":
            tag = "🧪 LIVE_DRY"
        else:
            tag = "📝 PAPER"

        logger.info(
            f"{tag} TRADE #{record.trade_id} | "
            f"{'📈 UP' if signal.direction == 'up' else '📉 DOWN'} | "
            f"Conf {signal.confidence:.0%} | Stake ${stake:.4f} @ {entry:.4f} | "
            f"vwap={fill.vwap:.4f} best_ask={fill.best_ask:.4f} "
            f"slip={fill.slip_vs_best:.4f} fee_bps={fill.fee_bps_applied:.0f} | "
            f"flag={record.flag} edge={record.edge:.3f} | "
            f"BTC ${record.btc_price_at_entry:,.2f}"
        )
        return record

    def _post_live_order(
        self,
        signal: TradeSignal,
        stake: float,
        fill: FillResult,
    ) -> Optional[dict[str, Any]]:
        """Post FOK market buy via polymarket-client (py-sdk).

        Uses SecureClient.place_market_order + wait_for_order_fill_settlement.
        Includes: balance pre-flight, settlement tracking, crash-safe.
        """
        client = self._get_clob_client()
        if client is None:
            logger.error("Live post aborted: no SecureClient (missing keys?)")
            return None

        # ── Pre-flight: check wallet has enough collateral ──
        if not self._check_wallet_balance(stake):
            logger.error(
                f"Live post aborted: wallet balance < stake ${stake:.2f} + fee buffer"
            )
            return None

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                # py-sdk: place_market_order (handles signing + posting internally)
                order = client.place_market_order(
                    token_id=str(signal.token_id),
                    side="BUY",
                    amount=float(stake),
                )

                # ── Wait for settlement (CONFIRMED/FAILED) ──
                # This polls trade status until on-chain confirmation.
                # Prevents ghost positions (order accepted but killed).
                try:
                    tx_hashes = client.wait_for_order_fill_settlement(
                        order, timeout_s=30.0
                    )
                    logger.info(
                        f"CLOB fill settled: order_id={getattr(order, 'order_id', '?')} "
                        f"tx_hashes={tx_hashes}"
                    )
                except Exception as settle_err:
                    logger.warning(
                        f"Order fill settlement timeout/error: {settle_err} — "
                        f"order may still settle async"
                    )
                    # Don't fail — order was placed, settlement may complete later

                # Extract order metadata
                order_id = str(getattr(order, "order_id", "") or "")
                status = str(getattr(order, "status", "posted") or "posted")
                actual_entry = fill.entry_price

                # Try to get actual fill price from order response
                for attr in ("average_price", "avg_price", "price", "making_amount"):
                    val = getattr(order, attr, None)
                    if val is not None:
                        try:
                            v = float(val)
                            if 0 < v < 1:
                                actual_entry = v
                                break
                        except (TypeError, ValueError):
                            pass

                logger.info(
                    f"CLOB py-sdk post ok order_id={order_id} status={status} "
                    f"entry≈{actual_entry:.4f}"
                )
                return {
                    "order_id": order_id,
                    "status": status,
                    "entry_price": actual_entry,
                    "raw": str(order)[:500],
                }
            except Exception as e:
                err_msg = str(e)[:300]
                if attempt < max_retries:
                    logger.warning(
                        f"Live CLOB order failed (attempt {attempt+1}/{max_retries+1}): {err_msg} — retrying"
                    )
                    time.sleep(0.5)
                    continue
                logger.error(
                    f"Live CLOB order FAILED after {max_retries+1} attempts: {err_msg}"
                )
                return None

    def _check_wallet_balance(self, stake: float) -> bool:
        """Pre-flight: query wallet collateral balance, reject if < stake + fee buffer."""
        try:
            client = self._get_clob_client()
            if client is None:
                return True  # paper/dry-run: skip check (no client)
            bal = client.get_balance_allowance(asset_type="COLLATERAL")
            wallet_balance = 0.0
            for attr in ("balance", "usdc_balance", "collateral_balance"):
                val = getattr(bal, attr, None)
                if val is not None:
                    try:
                        wallet_balance = float(val)
                        break
                    except (TypeError, ValueError):
                        pass
            fee_buffer = stake * 0.02  # 2% buffer for fee + slip
            if wallet_balance < stake + fee_buffer:
                logger.warning(
                    f"Wallet balance ${wallet_balance:.2f} < stake ${stake:.2f} + buffer ${fee_buffer:.2f}"
                )
                return False
            logger.info(f"Wallet balance OK: ${wallet_balance:.2f} >= ${stake + fee_buffer:.2f}")
            return True
        except Exception as e:
            logger.warning(f"Wallet balance check failed (allowing): {e}")
            return True  # fail-open: don't block trades on check error

    def _sync_balance_to_wallet(self) -> None:
        """Read real USDC balance from Polymarket wallet → set as authoritative balance.

        Makes paper = live: sizing uses real wallet balance, not synthetic number.
        Called at startup (if ROCKY_WALLET_SYNC=true) and periodically during runs.
        """
        try:
            client = self._get_clob_client()
            if client is None:
                logger.info("Wallet sync: no client (paper mode) — keeping synthetic balance")
                return
            bal = client.get_balance_allowance(asset_type="COLLATERAL")
            wallet_balance = 0.0
            for attr in ("balance", "usdc_balance", "collateral_balance"):
                val = getattr(bal, attr, None)
                if val is not None:
                    try:
                        wallet_balance = float(val)
                        break
                    except (TypeError, ValueError):
                        pass
            if wallet_balance > 0:
                drift = abs(wallet_balance - self.balance)
                if drift > 0.01:
                    logger.info(
                        f"Wallet sync: ${wallet_balance:.2f} (real) vs ${self.balance:.2f} (tracked) "
                        f"— drift ${drift:.2f}, syncing to real wallet"
                    )
                self.balance = round(wallet_balance, 4)
                self.daily_starting_balance = self.balance
                self._save_state()
                logger.info(f"Wallet sync complete: balance = ${self.balance:.4f}")
        except Exception as e:
            logger.warning(f"Wallet sync failed (keeping tracked balance): {e}")

    def _get_clob_client(self):
        if self._clob is not None:
            return self._clob
        pk = self.config.private_key
        if not pk:
            logger.error("POLY_PRIVATE_KEY not set — cannot init live SecureClient")
            return None
        try:
            from polymarket import SecureClient
        except ImportError as e:
            logger.error(f"polymarket-client not installed: {e}")
            return None

        try:
            # py-sdk factory: SecureClient.create handles env, creds, transport
            client = SecureClient.create(private_key=pk)
            self._clob = client
            logger.info("SecureClient (py-sdk) ready (live path)")
            return client
        except Exception as e:
            logger.error(f"SecureClient init failed: {e}")
            return None

    # ── persistence ─────────────────────────────────────────────────────────

    def _load_state(self):
        try:
            if os.path.exists(self.config.state_path):
                with open(self.config.state_path, "r") as f:
                    state = json.load(f)
                self.balance = state.get("balance", self.config.paper_starting_balance)
                self.trade_count = state.get("trade_count", 0)
                # Recover from journal if state was lost (prevents ID collision)
                journal_max = self._max_trade_id_from_journal()
                if journal_max >= self.trade_count:
                    logger.warning(
                        f"State trade_count={self.trade_count} < journal max={journal_max} "
                        f"— recovering to {journal_max} to prevent ID collision"
                    )
                    self.trade_count = journal_max
                self.daily_starting_balance = state.get(
                    "daily_starting_balance", self.balance
                )
                logger.info(
                    f"Loaded state: balance=${self.balance:.4f}, "
                    f"trades={self.trade_count}"
                )
        except Exception as e:
            logger.warning(f"Could not load state: {e} — recovering trade_count from journal")
            self.trade_count = self._max_trade_id_from_journal()

    def _max_trade_id_from_journal(self) -> int:
        """Scan journal for max trade_id — prevents ID collision if state lost."""
        mx = 0
        try:
            if os.path.exists(self.config.journal_path):
                with open(self.config.journal_path, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            tid = int(d.get("trade_id", 0) or 0)
                            if tid > mx:
                                mx = tid
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
        except Exception:
            pass
        return mx

    def _save_state(self):
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
        try:
            os.makedirs(os.path.dirname(self.config.journal_path), exist_ok=True)
            entry = asdict(record)
            entry["_event"] = "resolved" if resolved else "opened"
            with open(self.config.journal_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write journal: {e}")

    def _journal_pending_post(
        self, trade_id: int, signal: TradeSignal, stake: float
    ) -> None:
        """Write a pending_post journal entry BEFORE place_market_order.

        If the process dies before the matching 'opened' entry is written,
        _recover_pending_posts can detect the orphan and re-sync balance /
        flag it for manual review.
        """
        try:
            os.makedirs(os.path.dirname(self.config.journal_path), exist_ok=True)
            entry = {
                "_event": "pending_post",
                "trade_id": trade_id,
                "timestamp": time.time(),
                "mode": "live",
                "direction": signal.direction,
                "stake_usd": stake,
                "token_id": str(getattr(signal, "token_id", "") or ""),
                "condition_id": getattr(signal.market, "condition_id", "")
                if signal.market
                else "",
            }
            with open(self.config.journal_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write pending_post journal: {e}")

    def _recover_pending_posts(self) -> None:
        """On startup, scan journal for pending_post entries with no matching 'opened'.

        An orphaned pending_post means we debited balance + incremented trade_count,
        then crashed before the order fully settled / opened journal entry was written.
        We can't know if the CLOB order succeeded, so we log a loud WARNING for manual
        review and refund the balance (safer to refund + miss than to double-spend).
        The trade is NOT counted as a real trade.
        """
        try:
            if not os.path.exists(self.config.journal_path):
                return
            pending: dict[int, dict] = {}
            opened: set[int] = set()
            with open(self.config.journal_path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev = entry.get("_event")
                    tid = entry.get("trade_id")
                    if ev == "pending_post" and isinstance(tid, int):
                        pending[tid] = entry
                    elif ev == "opened" and isinstance(tid, int):
                        opened.add(tid)
            orphans = [tid for tid in pending if tid not in opened]
            if not orphans:
                return
            for tid in orphans:
                stake = float(pending[tid].get("stake_usd", 0) or 0)
                logger.warning(
                    f"RECOVER orphan pending_post trade #{tid}: refunding ${stake:.2f} "
                    f"— verify on Polymarket UI whether order actually filled. "
                    f"If yes, manually reconcile balance."
                )
                self.balance = round(self.balance + stake, 4)
                self.trade_count = max(self.trade_count, tid)
            self._save_state()
            logger.warning(
                f"Recovery complete: {len(orphans)} orphaned pending_post(s) refunded."
            )
        except Exception as e:
            logger.error(f"Failed to scan pending_post recovery: {e}")

    def reset_daily(self):
        # Paper mode: top-up balance if depleted so 30-day real-trade data
        # collection doesn't die after a losing streak. Live mode: never touch.
        if self.config.mode == TradingMode.PAPER and self.balance < 1.0:
            old = self.balance
            self.balance = round(self.config.paper_starting_balance, 4)
            logger.info(
                f"📅 Paper balance topped up: ${old:.4f} → ${self.balance:.4f} "
                f"(daily reset keeps real-trade data flowing)"
            )
        self.daily_starting_balance = self.balance
        self._save_state()
        logger.info(f"Daily reset. Starting balance: ${self.balance:.4f}")

    def get_stats(self) -> dict:
        real = [t for t in self.trades if not getattr(t, "is_shadow", False)]
        shadow = [t for t in self.trades if getattr(t, "is_shadow", False)]
        wins = [t for t in real if t.result == "win"]
        losses = [t for t in real if t.result == "loss"]
        resolved = wins + losses
        sh_wins = [t for t in shadow if t.result == "win"]
        sh_losses = [t for t in shadow if t.result == "loss"]
        sh_resolved = sh_wins + sh_losses

        total_pnl = sum(t.pnl for t in resolved)
        win_rate = len(wins) / len(resolved) if resolved else 0
        sh_pnl = sum(t.pnl for t in sh_resolved)
        sh_wr = len(sh_wins) / len(sh_resolved) if sh_resolved else 0

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
            "shadow_resolved": len(sh_resolved),
            "shadow_wins": len(sh_wins),
            "shadow_losses": len(sh_losses),
            "shadow_win_rate": sh_wr,
            "shadow_pnl": sh_pnl,
        }
