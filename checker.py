#!/usr/bin/env python3
"""
RCB Ticket Availability Checker
Polls the RCB ticket page via headless browser and sends Telegram notifications
when new tickets go live.

Bot commands (send to @RCB_31BOT):
  stop   — Snooze alerts for currently-known matches. New matches still notify.
  start  — Resume alerts immediately.
  status — Show current status.
"""

import asyncio
import json
import os
import urllib.request
from datetime import datetime

from playwright.async_api import async_playwright

# ── Configuration ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
TICKET_PAGE_URL    = "https://shop.royalchallengers.com/ticket"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state.json")
# ───────────────────────────────────────────────────────────────────────────────

# In-memory state (also persisted to STATE_FILE)
_state: dict = {
    "notified": [],   # event_Codes we've already alerted about
    "snoozed": False, # True when user said "stop"
    "update_offset": 0,
}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    global _state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
                _state.update(saved)
        except Exception:
            pass


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(_state, f, indent=2)


# ── Telegram helpers ───────────────────────────────────────────────────────────

def _tg_request(method: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log(f"[NO CREDS] {message}")
        return
    try:
        result = _tg_request("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        if result.get("ok"):
            log("✅ Telegram notification sent.")
        else:
            log(f"❌ Telegram error: {result}")
    except Exception as e:
        log(f"❌ Failed to send Telegram message: {e}")


async def poll_telegram_commands():
    """Check for new bot messages and handle stop/start/status commands."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        result = _tg_request("getUpdates", {
            "offset": _state["update_offset"],
            "timeout": 1,
            "allowed_updates": ["message"],
        })
        updates = result.get("result", [])
        for update in updates:
            _state["update_offset"] = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip().lower()

            # Only respond to the configured chat
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text in ("stop", "/stop"):
                _state["snoozed"] = True
                save_state()
                log("🔕 Snoozed by user command.")
                await send_telegram(
                    "🔕 <b>Notifications paused.</b>\n\n"
                    "I'll stay quiet about current matches.\n"
                    "You'll still get alerted when a <b>new match</b> goes on sale.\n\n"
                    "Send <b>start</b> to resume all alerts."
                )

            elif text in ("start", "/start", "resume", "/resume"):
                _state["snoozed"] = False
                save_state()
                log("🔔 Resumed by user command.")
                await send_telegram("🔔 <b>Notifications resumed!</b> I'll alert you when tickets are available.")

            elif text in ("status", "/status"):
                notified = _state.get("notified", [])
                snoozed  = _state.get("snoozed", False)
                status   = "🔕 Paused (send <b>start</b> to resume)" if snoozed else "🔔 Active"
                await send_telegram(
                    f"📊 <b>RCB Ticket Checker Status</b>\n\n"
                    f"Status: {status}\n"
                    f"Matches tracked: {len(notified)}\n"
                    f"Check interval: every {CHECK_INTERVAL // 60} min"
                )

        if updates:
            save_state()
    except Exception as e:
        log(f"⚠️  Command poll failed: {e}")


# ── Ticket checking ────────────────────────────────────────────────────────────

async def fetch_events() -> list:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        events: list = []

        async def handle_response(response):
            if "ticketgenie.in/ticket/eventlist" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    if data.get("status") == "Success":
                        events.extend(data.get("result", []))
                except Exception as e:
                    log(f"   Failed to parse API response: {e}")

        page.on("response", handle_response)
        try:
            await page.goto(TICKET_PAGE_URL, wait_until="networkidle", timeout=40000)
        except Exception as e:
            log(f"   Page load warning: {e}")
        await browser.close()
        return events


def format_event_message(event: dict) -> str:
    name       = event.get("event_Name", "Unknown Match")
    date       = event.get("event_Display_Date", "")
    venue      = event.get("venue_Name", "")
    city       = event.get("city_Name", "")
    price      = event.get("event_Price_Range", "")
    venue_full = f"{venue}, {city}" if city else venue

    lines = ["🏏 <b>RCB TICKETS ARE LIVE!</b>", ""]
    lines.append(f"🎯 <b>Match:</b> {name}")
    if date:
        lines.append(f"📅 <b>Date:</b> {date}")
    if venue_full:
        lines.append(f"📍 <b>Venue:</b> {venue_full}")
    if price:
        lines.append(f"💰 <b>Price:</b> {price}")
    lines += ["", f"🎟 <b>Book Now:</b> {TICKET_PAGE_URL}", "", "⚡ Grab them fast before they sell out!"]
    lines += ["", "——————————————", "Send <b>stop</b> to pause alerts for this match."]
    return "\n".join(lines)


async def check_once():
    log("🔍 Checking ticket availability...")
    try:
        events = await fetch_events()
    except Exception as e:
        log(f"⚠️  Failed to fetch events: {e}")
        return

    if not events:
        log("   No events listed on the ticket page.")
        return

    log(f"   Found {len(events)} event(s) on the page.")
    notified: set = set(_state.get("notified", []))
    snoozed: bool = _state.get("snoozed", False)

    for event in events:
        code     = str(event.get("event_Code", ""))
        btn_text = event.get("event_Button_Text", "").upper()
        name     = event.get("event_Name", "?")
        date     = event.get("event_Display_Date", "")

        log(f"   [{code}] {name} | {date} | {btn_text}")

        if btn_text != "BUY TICKETS":
            if code in notified:
                log(f"      → Was available, now {btn_text}. Clearing so we re-notify if it reopens.")
                notified.discard(code)
            continue

        if code in notified:
            log(f"      → Already notified. Skipping.")
            continue

        # This is a genuinely new available match — always notify even if snoozed
        if snoozed:
            log(f"      → NEW match (snoozed was on, but new match overrides). Sending notification...")
            _state["snoozed"] = False  # Auto-resume for new matches
        else:
            log(f"      → NEW availability! Sending notification...")

        await send_telegram(format_event_message(event))
        notified.add(code)

    _state["notified"] = list(notified)
    save_state()


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    log("🚀 RCB Ticket Checker started.")
    log(f"   Polling every {CHECK_INTERVAL}s → {TICKET_PAGE_URL}")
    log(f"   Bot commands: stop | start | status")

    load_state()
    await check_once()

    # Run ticker check and command polling concurrently
    async def ticket_loop():
        while True:
            log(f"   Sleeping {CHECK_INTERVAL}s until next check...")
            await asyncio.sleep(CHECK_INTERVAL)
            await check_once()

    async def command_loop():
        while True:
            await poll_telegram_commands()
            await asyncio.sleep(5)  # Poll commands every 5s

    await asyncio.gather(ticket_loop(), command_loop())


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        # Single-shot mode for GitHub Actions / cron
        async def run_once():
            log("🚀 RCB Ticket Checker (single run)")
            load_state()
            await check_once()
            await poll_telegram_commands()
        asyncio.run(run_once())
    else:
        asyncio.run(main())
