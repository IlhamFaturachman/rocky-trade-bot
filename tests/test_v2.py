#!/usr/bin/env python3
"""
Tests for the V2 LLM Decision Engine (OpenAI-compatible API).
Tests prompt building, response parsing, and signal generation.
No actual LLM calls — mocks the API.
"""

import sys
import os
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock requests
import types
mock_requests = types.ModuleType("requests")


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = json.dumps(json_data)
        self.response = self  # for HTTPError.response

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class MockSession:
    """Mock session that returns predefined LLM responses in OpenAI format."""
    _next_response = None

    def __init__(self):
        self.headers = {}
        self._last_request = None

    def update(self, *a, **kw):
        pass

    def post(self, url, **kwargs):
        self._last_request = {"url": url, "kwargs": kwargs}
        if MockSession._next_response:
            return MockSession._next_response
        # Default: bullish response in OpenAI format
        return MockResponse({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "direction": "up",
                        "confidence": 78,
                        "reasoning": [
                            "Strong 5m uptrend at +0.25%",
                            "Bullish news sentiment",
                            "Market underpricing UP at 50% vs our 65% estimate"
                        ]
                    })
                }
            }]
        })

    def get(self, *a, **kw):
        return MockResponse({})


mock_requests.Session = MockSession
mock_requests.exceptions = types.ModuleType("requests.exceptions")
mock_requests.exceptions.Timeout = TimeoutError
mock_requests.exceptions.HTTPError = Exception
mock_requests.RequestException = Exception
sys.modules["requests"] = mock_requests
sys.modules["requests.exceptions"] = mock_requests.exceptions

from src.config import Config
from src.scanner import BtcMarket
from src.intelligence import BtcSnapshot
from src.models import TradeSignal
from src.decision_v2 import DecisionEngineV2, build_analysis_prompt


def make_market(**overrides):
    defaults = dict(
        condition_id="test-123",
        question="Will BTC go up in the next 5 minutes?",
        market_slug="btc-5min-up",
        tokens=[
            {"token_id": "token-yes", "outcome": "Up"},
            {"token_id": "token-no", "outcome": "Down"},
        ],
        end_date="2026-04-24T19:00:00Z",
        active=True,
        yes_price=0.50,
        no_price=0.50,
        volume=1000,
        liquidity=500,
        series_slug="btc-up-or-down-5m",
        price_to_beat=93950.0,
        event_id="evt-123",
    )
    defaults.update(overrides)
    return BtcMarket(**defaults)


def make_snapshot(**overrides):
    defaults = dict(
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
        news_headlines=["Bitcoin surges past $94K"],
        raw_klines=[
            {"open_time": 0, "open": 93800, "high": 93850, "low": 93780,
             "close": 93820, "volume": 100, "close_time": 0}
        ] * 15,
    )
    defaults.update(overrides)
    return BtcSnapshot(**defaults)


def test_prompt_building():
    """Test that the analysis prompt includes all data including price_to_beat."""
    market = make_market()
    snapshot = make_snapshot()
    orderbook = {
        "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "200"}],
        "asks": [{"price": "0.52", "size": "150"}, {"price": "0.53", "size": "250"}],
    }

    prompt = build_analysis_prompt(market, snapshot, orderbook, snapshot.news_headlines)

    # Verify all key data is in the prompt
    assert "$94,000.00" in prompt, "BTC price missing"
    assert "bullish" in prompt.lower(), "Momentum missing"
    assert "Will BTC go up" in prompt, "Market question missing"
    assert "0.48" in prompt, "Orderbook bids missing"
    assert "0.52" in prompt, "Orderbook asks missing"
    assert "Bitcoin surges" in prompt, "News missing"
    assert "| 1m |" in prompt, "Klines missing"
    assert "btc-up-or-down-5m" in prompt, "Series slug missing"
    assert "93,950.00" in prompt, "Price to beat missing"
    assert "Price to beat" in prompt or "price to beat" in prompt, "Price to beat label missing"

    print(f"   Prompt length: {len(prompt)} chars")
    print("✅ Prompt building test passed")


def test_openai_api_format():
    """Test that the LLM call uses OpenAI-compatible format."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    os.environ["LLM_API_URL"] = "http://127.0.0.1:8080/v1/chat/completions"
    os.environ["LLM_MODEL"] = "grok-4.5"
    engine = DecisionEngineV2(config)

    # Verify defaults (local g2a proxy / farm endpoint)
    assert "127.0.0.1:8080" in engine.api_url, f"Wrong API URL: {engine.api_url}"
    assert "chat/completions" in engine.api_url, f"Not chat completions: {engine.api_url}"
    assert engine.model == "grok-4.5", f"Wrong model: {engine.model}"

    # Make a call and inspect what was sent
    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "up", "confidence": 75, "reasoning": ["test"]
                })
            }
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal is not None

    # Check the request format
    last_req = engine.session._last_request
    assert last_req is not None, "No request was made"

    payload = last_req["kwargs"]["json"]
    headers = last_req["kwargs"]["headers"]

    # OpenAI format checks
    assert "Authorization" in headers, "Missing Authorization header"
    assert headers["Authorization"] == "Bearer test-key", f"Wrong auth: {headers['Authorization']}"
    assert "x-api-key" not in headers, "Should not have Anthropic x-api-key header"
    assert "anthropic-version" not in headers, "Should not have anthropic-version header"

    # Payload format
    assert payload["model"] == "grok-4.5"
    assert len(payload["messages"]) == 2, "Should have system + user messages"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert "system" not in payload or payload.get("system") is None, \
        "Should not have top-level 'system' key (Anthropic format)"

    print("✅ OpenAI API format test passed")


def test_llm_bullish_response():
    """Test parsing a bullish LLM response (OpenAI format)."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "up",
                    "confidence": 82,
                    "p_up": 0.62,
                    "reasoning": [
                        "Strong momentum across all timeframes",
                        "News catalyst: ETF inflows",
                        "Market underpricing UP"
                    ]
                })
            }
        }]
    })

    market = make_market()
    snapshot = make_snapshot()

    signal = engine.analyze(market, snapshot, orderbook={})
    assert signal is not None
    assert signal.direction == "up"
    assert signal.confidence == 0.82
    assert signal.should_trade is True
    assert len(signal.reasoning) == 4  # 3 LLM reasons + code edge check
    assert signal.stake_pct == 0.08  # 75-84% tier
    assert signal.token_id == "token-yes"
    assert signal.candle_open_price == 93950.0, f"Wrong candle open: {signal.candle_open_price}"
    print(f"   Bullish: {signal.direction} @ {signal.confidence:.0%}, stake {signal.stake_pct:.0%}")
    print("✅ Bullish LLM response test passed")


def test_llm_bearish_response():
    """Test parsing a bearish LLM response."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "down",
                    "confidence": 71,
                    "p_up": 0.35,
                    "reasoning": ["Bearish divergence", "SEC news"]
                })
            }
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.direction == "down"
    assert signal.confidence == 0.71
    assert signal.should_trade is True
    assert signal.token_id == "token-no"
    assert signal.stake_pct == 0.05  # 68-74% tier
    print(f"   Bearish: {signal.direction} @ {signal.confidence:.0%}, stake {signal.stake_pct:.0%}")
    print("✅ Bearish LLM response test passed")


def test_llm_skip_response():
    """Test that SKIP signals don't trigger trades."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "skip",
                    "confidence": 45,
                    "reasoning": ["No clear edge", "Choppy price action"]
                })
            }
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.direction == "skip"
    assert signal.should_trade is False
    print(f"   Skip: {signal.direction} @ {signal.confidence:.0%}")
    print("✅ Skip response test passed")


def test_llm_low_confidence():
    """Test that low confidence UP/DOWN still doesn't trade."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "up",
                    "confidence": 55,
                    "p_up": 0.57,
                    "reasoning": ["Slight bullish lean but uncertain"]
                })
            }
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.direction == "up"
    assert signal.confidence == 0.55
    # should_trade is True (direction is valid), but executor will reject
    # because confidence < min_confidence (0.65). Confidence gating is in executor.
    assert signal.should_trade is True
    assert signal.stake_pct == 0.0  # Below min_confidence → 0% sizing
    print(f"   Low confidence: {signal.direction} @ {signal.confidence:.0%} → executor will reject")
    print("✅ Low confidence test passed")


def test_markdown_fence_parsing():
    """Test that JSON wrapped in markdown fences is parsed correctly."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    fenced_json = '```json\n{"direction": "up", "confidence": 88, "p_up": 0.62, "reasoning": ["test"]}\n```'
    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {"content": fenced_json}
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal is not None
    assert signal.direction == "up"
    assert signal.confidence == 0.88
    print("✅ Markdown fence parsing test passed")


def test_confidence_clamping():
    """Test that confidence is clamped to 0-100 range."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    # Confidence > 100
    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "up", "confidence": 150, "reasoning": ["over-confident"]
                })
            }
        }]
    })
    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.confidence == 1.0

    # Confidence < 0
    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "direction": "down", "confidence": -10, "reasoning": ["negative"]
                })
            }
        }]
    })
    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.confidence == 0.0

    print("✅ Confidence clamping test passed")


def test_no_api_key():
    """Test that missing API key returns None gracefully."""
    config = Config()
    os.environ["LLM_API_KEY"] = ""
    engine = DecisionEngineV2(config)

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal is None
    print("✅ No API key test passed")


def test_raw_llm_response_stored():
    """Test that the full LLM response is stored in the signal."""
    config = Config()
    os.environ["LLM_API_KEY"] = "test-key"
    engine = DecisionEngineV2(config)

    raw_content = json.dumps({
        "direction": "up", "confidence": 80, "reasoning": ["stored test"]
    })
    MockSession._next_response = MockResponse({
        "choices": [{
            "message": {"content": raw_content}
        }]
    })

    signal = engine.analyze(make_market(), make_snapshot(), orderbook={})
    assert signal.raw_llm_response == raw_content
    print("✅ Raw LLM response storage test passed")


if __name__ == "__main__":
    print("\n🪨 Rocky V2 (LLM Engine — OpenAI-compatible) Tests\n")
    test_prompt_building()
    test_openai_api_format()
    test_llm_bullish_response()
    test_llm_bearish_response()
    test_llm_skip_response()
    test_llm_low_confidence()
    test_markdown_fence_parsing()
    test_confidence_clamping()
    test_no_api_key()
    test_raw_llm_response_stored()
    print(f"\n🎉 All V2 tests passed! (10 tests)\n")
