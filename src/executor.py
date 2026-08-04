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
        self.daily_starting_balance = self.balance
        self.trades: list[TradeRecord] = []
        self._clob = None
        self._load_state()

    # ── public API ──────────────────────────────────────────────────────────

    def execute(
        self,
        signal: TradeSignal,
        orderbook: Optional[dict] = None,
    ) -> Optional[TradeRecord]:
        """Real paper/live trade (affects balance). Same fill model both modes."""
        if not self._pre_trade_checks(signal):
            return None

        stake = round(self.balance * signal.stake_pct, 4)
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
            # Pre-marked void (no price data) — journal only, no balance/PnL impact.
            record.payout = 0.0
            record.pnl = 0.0
            logger.info(
                f"Trade #{record.trade_id} VOID — no resolution data, balance unchanged"
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

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            logger.warning(
                f"Hit {self.consecutive_losses} consecutive losses, "
                f"pausing trading. Need manual reset or a win."
            )
            return False

        daily_loss = (
            (self.daily_starting_balance - self.balance) / self.daily_starting_balance
            if self.daily_starting_balance > 0
            else 0.0
        )
        if daily_loss >= self.config.daily_loss_limit_pct:
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
            entry_fb = min(0.99, max(0.01, fb * (1.0 + fee_bps / 10_000.0))) if fee_bps else fb
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
        """Post FOK-style market buy via py-clob-client. Returns meta or None."""
        client = self._get_clob_client()
        if client is None:
            logger.error("Live post aborted: no CLOB client (missing keys?)")
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
        except ImportError as e:
            logger.error(f"py-clob-client import failed: {e}")
            return None

        try:
            ot = OrderType.FOK if self.fills.order_type == "FOK" else OrderType.GTC
            # Cap price near simulated entry so we don't chase worse than paper model
            price_cap = min(0.99, max(0.01, float(fill.entry_price)))
            mo = MarketOrderArgs(
                token_id=str(signal.token_id),
                amount=float(stake),
                side=BUY,
                price=price_cap,
                order_type=ot,
            )
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, ot)
            order_id = ""
            status = "posted"
            actual_entry = fill.entry_price
            if isinstance(resp, dict):
                order_id = str(
                    resp.get("orderID")
                    or resp.get("order_id")
                    or resp.get("id")
                    or ""
                )
                status = str(resp.get("status") or resp.get("orderStatus") or status)
                for k in ("average_price", "avgPrice", "price", "takingAmount"):
                    if resp.get(k) is not None:
                        try:
                            v = float(resp[k])
                            if 0 < v < 1:
                                actual_entry = v
                                break
                        except (TypeError, ValueError):
                            pass
            logger.info(
                f"CLOB post ok order_id={order_id} status={status} "
                f"entry≈{actual_entry:.4f} resp_type={type(resp).__name__}"
            )
            return {
                "order_id": order_id,
                "status": status,
                "entry_price": actual_entry,
                "raw": resp if isinstance(resp, dict) else {"raw": str(resp)[:500]},
            }
        except Exception as e:
            logger.error(f"Live CLOB order failed: {e}")
            return None

    def _get_clob_client(self):
        if self._clob is not None:
            return self._clob
        pk = self.config.private_key
        if not pk:
            logger.error("POLY_PRIVATE_KEY not set — cannot init live CLOB client")
            return None
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            logger.error(f"py-clob-client not installed: {e}")
            return None

        host = self.config.clob_api_url or "https://clob.polymarket.com"
        chain_id = int(self.config.chain_id or 137)
        try:
            if self.config.api_key and self.config.api_secret and self.config.api_passphrase:
                creds = ApiCreds(
                    api_key=self.config.api_key,
                    api_secret=self.config.api_secret,
                    api_passphrase=self.config.api_passphrase,
                )
                client = ClobClient(
                    host,
                    key=pk,
                    chain_id=chain_id,
                    creds=creds,
                )
            else:
                client = ClobClient(host, key=pk, chain_id=chain_id)
                derived = client.create_or_derive_api_creds()
                client.set_api_creds(derived)
                logger.info("Derived CLOB API creds from private key")
            self._clob = client
            logger.info("CLOB client ready (live path)")
            return client
        except Exception as e:
            logger.error(f"CLOB client init failed: {e}")
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

    def reset_daily(self):
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
