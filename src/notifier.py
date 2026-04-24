"""
Rocky Trading System - Telegram Notifier
Non-blocking Telegram notifications for trades, stats, and alerts.
Silently fails if not configured. Uses HTML parse mode to avoid Markdown escaping issues.
"""

import os
import json
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger("rocky.notifier")

# Lazy import to avoid hard dependency
_requests = None


def _get_requests():
    global _requests
    if _requests is None:
        import requests as req
        _requests = req
    return _requests


def _esc(text: str) -> str:
    """Escape HTML special characters in user-generated content."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramNotifier:
    """Sends trading notifications via Telegram bot API (HTML format)."""

    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.bot_token and self.chat_id)

        if not self.enabled:
            logger.info("Telegram notifier disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")

    def send_startup(self, config, engine_version: str = "v1"):
        """Send bot online notification."""
        msg = (
            f"🪨 <b>Rocky Trading Bot Online</b>\n\n"
            f"Engine: <code>{_esc(engine_version.upper())}</code>\n"
            f"Mode: <code>{_esc(config.mode.value.upper())}</code>\n"
            f"Balance: <code>${config.paper_starting_balance:.2f}</code>\n"
            f"Max risk: <code>{config.max_risk_pct:.0%}</code>\n"
            f"Min confidence: <code>{config.min_confidence:.0%}</code>\n"
            f"Loop: every <code>{config.loop_interval_seconds}s</code>\n\n"
            f"Ready to trade. 🎯"
        )
        self._send(msg)

    def send_trade_opened(self, record):
        """Notify when a trade is placed."""
        emoji = "📈" if record.direction == "up" else "📉"
        msg = (
            f"{emoji} <b>TRADE #{record.trade_id} OPENED</b>\n\n"
            f"Direction: <code>{_esc(record.direction.upper())}</code>\n"
            f"Confidence: <code>{record.confidence:.0%}</code>\n"
            f"Stake: <code>${record.stake_usd:.4f}</code>\n"
            f"Entry price: <code>{record.entry_price:.4f}</code>\n"
            f"BTC: <code>${record.btc_price_at_entry:,.2f}</code>\n"
            f"Mode: <code>{_esc(record.mode)}</code>"
        )
        if record.candle_open_price > 0:
            msg += f"\nCandle open: <code>${record.candle_open_price:,.2f}</code>"
        if record.reasoning:
            reasons = "\n".join(f"• {_esc(r)}" for r in record.reasoning[:3])
            msg += f"\n\n<b>Reasoning:</b>\n{reasons}"
        self._send(msg)

    def send_trade_resolved(self, record):
        """Notify when a trade resolves."""
        if record.result == "win":
            emoji = "✅"
            result = "WIN"
        else:
            emoji = "❌"
            result = "LOSS"

        msg = (
            f"{emoji} <b>TRADE #{record.trade_id} — {result}</b>\n\n"
            f"P&amp;L: <code>${record.pnl:+.4f}</code>\n"
            f"Balance: <code>${record.balance_after:.4f}</code>"
        )
        if record.candle_open_price > 0 and record.candle_close_price > 0:
            change = ((record.candle_close_price - record.candle_open_price) / record.candle_open_price) * 100
            msg += (
                f"\nCandle: <code>${record.candle_open_price:,.2f}</code> → "
                f"<code>${record.candle_close_price:,.2f}</code> ({change:+.4f}%)"
            )
        self._send(msg)

    def send_stats(self, stats: dict):
        """Send hourly stats summary."""
        msg = (
            f"📊 <b>Hourly Stats</b>\n\n"
            f"Balance: <code>${stats['balance']:.4f}</code>\n"
            f"Trades: <code>{stats['total_trades']}</code>\n"
            f"Resolved: <code>{stats['resolved_trades']}</code>\n"
            f"Win rate: <code>{stats['win_rate']:.0%}</code>\n"
            f"Total P&amp;L: <code>${stats['total_pnl']:+.4f}</code>\n"
            f"Best: <code>${stats['best_trade']:+.4f}</code>\n"
            f"Worst: <code>${stats['worst_trade']:+.4f}</code>\n"
            f"Consecutive losses: <code>{stats['consecutive_losses']}</code>"
        )
        self._send(msg)

    def send_warning(self, msg_text: str):
        """Send a warning/alert."""
        self._send(f"⚠️ <b>WARNING</b>\n\n{_esc(msg_text)}")

    def send_shutdown(self):
        """Send bot offline notification."""
        self._send("🛑 <b>Rocky Trading Bot Offline</b>\n\nShutting down gracefully.")

    def send_skip(self, reason: str):
        """Optionally notify on skipped cycles (disabled by default to reduce noise)."""
        if os.environ.get("ROCKY_NOTIFY_SKIPS", "").lower() == "true":
            self._send(f"⏸️ Skipped: {_esc(reason)}")

    def _send(self, text: str):
        """Send a message via Telegram Bot API. Non-blocking."""
        if not self.enabled:
            return
        thread = threading.Thread(target=self._send_sync, args=(text,), daemon=True)
        thread.start()

    def _send_sync(self, text: str):
        """Synchronous send (runs in background thread)."""
        try:
            requests = _get_requests()
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram notification failed: {e}")
