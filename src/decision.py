"""
Rocky Trading System - Decision Engine
Analyzes market data and generates trade signals with confidence scores.
"""

import logging
from typing import Optional

from .intelligence import BtcSnapshot
from .scanner import BtcMarket
from .models import TradeSignal

logger = logging.getLogger("rocky.decision")


class DecisionEngine:
    """Analyzes all signals and produces trade decisions."""

    def __init__(self, config):
        self.config = config

    def analyze(self, market: BtcMarket, snapshot: BtcSnapshot) -> Optional[TradeSignal]:
        """
        Analyze market + intelligence and produce a trade signal.
        
        The core logic: combine momentum, volatility, news, and market pricing
        to determine if there's an edge.
        """
        if snapshot.price_usd <= 0:
            logger.warning("No price data available, skipping analysis")
            return None

        # Score each factor
        momentum_score, momentum_reasons = self._score_momentum(snapshot)
        volatility_score, volatility_reasons = self._score_volatility(snapshot)
        news_score, news_reasons = self._score_news(snapshot)
        market_score, market_reasons = self._score_market_pricing(market, snapshot)

        # Determine direction
        direction = self._determine_direction(
            momentum_score, news_score, market_score, snapshot
        )

        # Calculate overall confidence
        # Base: 50% (coin flip). Signals push it up or down.
        # Strong aligned signals should reach 75-90%.
        base = 0.50

        # Directional boost from momentum (strongest signal for 5-min markets)
        momentum_boost = abs(momentum_score) * 0.25  # up to +0.25
        market_boost = abs(market_score) * 0.15       # up to +0.15
        news_boost = abs(news_score) * 0.08            # up to +0.08
        vol_boost = volatility_score * 0.07            # up to +0.056

        # Alignment bonus: when all signals agree, extra confidence
        directional_signals = [momentum_score, news_score, market_score]
        same_dir = all(s >= 0 for s in directional_signals) or all(s <= 0 for s in directional_signals)
        alignment_bonus = 0.08 if same_dir and any(abs(s) > 0.2 for s in directional_signals) else 0.0

        raw_confidence = base + momentum_boost + market_boost + news_boost + vol_boost + alignment_bonus

        # Normalize to 0.0-1.0 range
        confidence = min(max(raw_confidence, 0.0), 1.0)

        # Penalty for conflicting signals
        signals = [momentum_score, news_score, market_score]
        positive = sum(1 for s in signals if s > 0)
        negative = sum(1 for s in signals if s < 0)
        if positive > 0 and negative > 0:
            conflict_penalty = 0.10 * min(positive, negative)
            confidence -= conflict_penalty
            momentum_reasons.append(f"Conflicting signals penalty: -{conflict_penalty:.0%}")

        confidence = max(confidence, 0.0)

        # Collect all reasoning
        all_reasons = momentum_reasons + volatility_reasons + news_reasons + market_reasons

        # Determine which token to buy
        token_id, expected_price = self._select_token(market, direction)

        # Position sizing
        stake_pct = self.config.get_position_size_pct(confidence)

        signal = TradeSignal(
            direction=direction,
            confidence=confidence,
            reasoning=all_reasons,
            market=market,
            snapshot=snapshot,
            token_id=token_id,
            expected_price=expected_price,
            stake_pct=stake_pct,
        )

        logger.info(signal.summary())
        return signal

    def _score_momentum(self, snapshot: BtcSnapshot) -> tuple[float, list[str]]:
        """
        Score momentum from -1.0 (strong bearish) to +1.0 (strong bullish).
        """
        score = 0.0
        reasons = []

        # 1-minute momentum (most relevant for 5-min markets)
        if snapshot.price_change_1m > 0.1:
            score += 0.3
            reasons.append(f"Strong 1m momentum: {snapshot.price_change_1m:+.3f}%")
        elif snapshot.price_change_1m > 0.03:
            score += 0.15
            reasons.append(f"Mild 1m bullish: {snapshot.price_change_1m:+.3f}%")
        elif snapshot.price_change_1m < -0.1:
            score -= 0.3
            reasons.append(f"Strong 1m sell-off: {snapshot.price_change_1m:+.3f}%")
        elif snapshot.price_change_1m < -0.03:
            score -= 0.15
            reasons.append(f"Mild 1m bearish: {snapshot.price_change_1m:+.3f}%")

        # 5-minute trend
        if snapshot.price_change_5m > 0.15:
            score += 0.25
            reasons.append(f"Strong 5m uptrend: {snapshot.price_change_5m:+.3f}%")
        elif snapshot.price_change_5m > 0.05:
            score += 0.1
            reasons.append(f"5m uptrend: {snapshot.price_change_5m:+.3f}%")
        elif snapshot.price_change_5m < -0.15:
            score -= 0.25
            reasons.append(f"Strong 5m downtrend: {snapshot.price_change_5m:+.3f}%")
        elif snapshot.price_change_5m < -0.05:
            score -= 0.1
            reasons.append(f"5m downtrend: {snapshot.price_change_5m:+.3f}%")

        # 15-minute trend (context)
        if snapshot.price_change_15m > 0.2:
            score += 0.15
            reasons.append(f"15m trend supports up: {snapshot.price_change_15m:+.3f}%")
        elif snapshot.price_change_15m < -0.2:
            score -= 0.15
            reasons.append(f"15m trend supports down: {snapshot.price_change_15m:+.3f}%")

        # Trend alignment bonus
        if snapshot.trend_direction == "bullish":
            score += 0.1
            reasons.append("Multi-timeframe trend: bullish alignment")
        elif snapshot.trend_direction == "bearish":
            score -= 0.1
            reasons.append("Multi-timeframe trend: bearish alignment")

        return (max(min(score, 1.0), -1.0), reasons)

    def _score_volatility(self, snapshot: BtcSnapshot) -> tuple[float, list[str]]:
        """
        Score volatility. Higher volatility = more opportunity but also more risk.
        Returns 0.0-1.0 (higher = more tradeable).
        """
        reasons = []

        if snapshot.volatility == "high":
            score = 0.8
            reasons.append("High volatility: strong moves likely")
        elif snapshot.volatility == "normal":
            score = 0.5
            reasons.append("Normal volatility")
        else:
            score = 0.2
            reasons.append("Low volatility: choppy/flat, harder to predict")

        return (score, reasons)

    def _score_news(self, snapshot: BtcSnapshot) -> tuple[float, list[str]]:
        """
        Score news sentiment from -1.0 to +1.0.
        """
        reasons = []

        if snapshot.news_sentiment == "bullish":
            score = 0.5
            reasons.append("News sentiment: bullish")
        elif snapshot.news_sentiment == "bearish":
            score = -0.5
            reasons.append("News sentiment: bearish")
        else:
            score = 0.0
            reasons.append("News sentiment: neutral")

        if snapshot.news_headlines:
            reasons.append(f"Top headline: {snapshot.news_headlines[0][:80]}")

        return (score, reasons)

    def _score_market_pricing(self, market: BtcMarket, snapshot: BtcSnapshot) -> tuple[float, list[str]]:
        """
        Score market mispricing. If our analysis disagrees with market odds,
        that's where the edge is.
        Returns -1.0 to +1.0 (positive = market underprices UP, negative = underprices DOWN).
        """
        reasons = []
        score = 0.0

        # Market's implied probability of UP
        market_up_prob = market.yes_price

        # Our estimated probability based on momentum
        if snapshot.momentum == "bullish":
            our_up_estimate = 0.65
        elif snapshot.momentum == "bearish":
            our_up_estimate = 0.35
        else:
            our_up_estimate = 0.50

        # Adjust based on trend strength
        if snapshot.price_change_5m > 0.1:
            our_up_estimate += 0.10
        elif snapshot.price_change_5m < -0.1:
            our_up_estimate -= 0.10

        our_up_estimate = max(0.1, min(0.9, our_up_estimate))

        # Edge = difference between our estimate and market price
        edge = our_up_estimate - market_up_prob

        if abs(edge) > 0.05:
            score = edge  # Positive = we think UP is underpriced
            reasons.append(
                f"Market prices UP at {market_up_prob:.0%}, "
                f"we estimate {our_up_estimate:.0%} "
                f"(edge: {edge:+.0%})"
            )
        else:
            reasons.append(
                f"Market fairly priced: UP at {market_up_prob:.0%} "
                f"vs our {our_up_estimate:.0%}"
            )

        return (max(min(score, 1.0), -1.0), reasons)

    def _determine_direction(
        self, momentum_score: float, news_score: float,
        market_score: float, snapshot: BtcSnapshot
    ) -> str:
        """Determine trade direction based on combined signals."""
        combined = momentum_score * 0.5 + market_score * 0.3 + news_score * 0.2

        if combined >= 0:
            return "up"
        return "down"

    def _select_token(self, market: BtcMarket, direction: str) -> tuple[str, float]:
        """Select which token to buy based on direction."""
        if not market.tokens:
            return ("", 0.5)

        if direction == "up":
            # Buy YES token (first token is typically Yes/Up)
            token_id = market.tokens[0]["token_id"] if market.tokens else ""
            price = market.yes_price
        else:
            # Buy NO token (second token is typically No/Down)
            token_id = market.tokens[1]["token_id"] if len(market.tokens) > 1 else ""
            price = market.no_price

        return (token_id, price)
