#!/usr/bin/env python3
"""
Quick Telegram verification script.
Sends a test message and optionally discovers your chat ID.

Usage:
    python3 setup-telegram-test.py              # Uses .env
    TELEGRAM_BOT_TOKEN=xxx python3 setup-telegram-test.py  # Direct
"""

import os
import sys
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set.")
        print("   Set it in .env or environment:")
        print("   export TELEGRAM_BOT_TOKEN=your-bot-token")
        sys.exit(1)

    base_url = f"https://api.telegram.org/bot{token}"

    # Verify bot token
    print("🔍 Verifying bot token...")
    resp = requests.get(f"{base_url}/getMe", timeout=10)
    if resp.status_code != 200:
        print(f"❌ Invalid bot token: {resp.status_code} {resp.text}")
        sys.exit(1)

    bot_info = resp.json().get("result", {})
    print(f"✅ Bot verified: @{bot_info.get('username', '?')} ({bot_info.get('first_name', '?')})")

    # If no chat ID, try to discover it
    if not chat_id:
        print("\n📱 No TELEGRAM_CHAT_ID set. Trying to discover...")
        print("   Send any message to your bot first, then press Enter.")
        input("   Press Enter when ready...")

        resp = requests.get(f"{base_url}/getUpdates", timeout=10)
        if resp.status_code == 200:
            updates = resp.json().get("result", [])
            if updates:
                # Get the most recent chat ID
                for update in reversed(updates):
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    if chat.get("id"):
                        chat_id = str(chat["id"])
                        chat_name = chat.get("first_name", chat.get("title", "Unknown"))
                        print(f"\n✅ Found chat ID: {chat_id} ({chat_name})")
                        print(f"   Add to .env: TELEGRAM_CHAT_ID={chat_id}")
                        break
            else:
                print("❌ No messages found. Send a message to the bot and try again.")
                sys.exit(1)
        else:
            print(f"❌ Failed to get updates: {resp.status_code}")
            sys.exit(1)

    if not chat_id:
        print("❌ Could not determine chat ID.")
        sys.exit(1)

    # Send test message
    print(f"\n📤 Sending test message to chat {chat_id}...")
    test_msg = "🪨 *Rocky Trading Bot is online!*\n\nReady to trade. This is a test message."

    resp = requests.post(
        f"{base_url}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": test_msg,
            "parse_mode": "Markdown",
        },
        timeout=10,
    )

    if resp.status_code == 200:
        print("✅ Test message sent successfully!")
        print(f"\n📋 Add these to your .env:")
        print(f"   TELEGRAM_BOT_TOKEN={token}")
        print(f"   TELEGRAM_CHAT_ID={chat_id}")
    else:
        print(f"❌ Failed to send: {resp.status_code} {resp.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
