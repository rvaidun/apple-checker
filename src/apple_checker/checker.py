"""Poll Apple's in-store pickup availability and post the result to a Discord webhook.

Every check posts one message: "Nothing available for pickup", or the stores that
have stock (with addresses) and a tag for each configured Discord user.

Run from a directory containing:
    .env         DISCORD_WEBHOOK_URL=... and DISCORD_TAG_USER_IDS=id1,id2
    cookies.txt  the `cookie` request header copied from your browser

Usage:
    apple-checker                 # check every 60s, forever
    apple-checker --once          # single check, then exit
    apple-checker --test-webhook  # send a test message to Discord

Apple blocks requests without a browser session, hence cookies.txt. When Apple stops
accepting it, the bot posts a warning to Discord; paste a fresh cookie header into
cookies.txt and the running bot picks it up on the next check (no restart needed).
"""

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"

# Part number -> product page linked in alerts. Part numbers are shared across carriers;
# cppart=UNLOCKED/US in the request is what selects the unlocked model.
PRODUCTS = {
    "MK1L4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky-unlocked",
    "MK1R4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-2tb-night-sky-unlocked",
}
FALLBACK_PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"

# Apple returns the 12 stores nearest each ZIP. Together these cover the Bay Area,
# South Bay (Oakridge, Los Gatos), Sacramento/Roseville, Modesto and Santa Rosa.
DEFAULT_ZIPS = ["94568", "94108", "95815", "95123"]

NOTHING_AVAILABLE_MESSAGE = "Nothing available for pickup"
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")  # all watched stores are in California
FAILURES_BEFORE_ALERT = 3
SECONDS_BETWEEN_ZIPS = 2

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    ),
}

log = logging.getLogger("apple-checker")


class CheckError(Exception):
    """A check failed. `blocked` means Apple rejected the session (refresh cookies)."""

    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


# --- config ------------------------------------------------------------------

def load_dotenv(path):
    """Minimal .env support: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_cookies(path):
    """Parse a raw `Cookie:` header string ("a=1; b=2") from file into a dict."""
    cookies = {}
    for pair in path.read_text().strip().split(";"):
        name, sep, value = pair.strip().partition("=")
        if sep and name:
            cookies[name] = value
    return cookies


def save_cookies(path, cookies):
    path.write_text("; ".join(f"{k}={v}" for k, v in cookies.items()) + "\n")


# --- Apple -------------------------------------------------------------------

def fetch_availability(parts, zip_code, cookies):
    """Return (json, refreshed_cookies) from Apple's fulfillment endpoint."""
    params = {
        "fae": "true",
        "pl": "true",
        "mts.0": "regular",
        "mts.1": "compact",
        "cppart": "UNLOCKED/US",
        "location": zip_code,
    }
    for i, part in enumerate(parts):
        params[f"parts.{i}"] = part

    try:
        resp = requests.get(
            FULFILLMENT_URL,
            params=params,
            headers={**HEADERS, "referer": PRODUCTS.get(parts[0], FALLBACK_PRODUCT_URL)},
            cookies=cookies,
            timeout=20,
        )
    except requests.RequestException as e:
        raise CheckError(f"network error: {e}") from e

    # Apple answers a rejected session with 541 (or 403) and an HTML error page.
    if resp.status_code in (403, 541):
        raise CheckError(f"HTTP {resp.status_code}: Apple rejected the session", blocked=True)
    if resp.status_code != 200:
        raise CheckError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise CheckError("response was not JSON (session likely expired)", blocked=True)

    return data, resp.cookies.get_dict()


def find_available(data, parts, zip_code):
    """Return (hits, store_numbers): every (store, part) available for pickup, and all stores seen."""
    try:
        stores = data["body"]["content"]["pickupMessage"]["stores"]
    except (KeyError, TypeError):
        raise CheckError(f"unexpected response shape: {str(data)[:200]}")

    hits = []
    for store in stores:
        for part in parts:
            info = (store.get("partsAvailability") or {}).get(part)
            if not info or info.get("pickupDisplay") != "available":
                continue
            regular = (info.get("messageTypes") or {}).get("regular") or {}
            address = store.get("address") or {}
            hits.append({
                "part": part,
                "title": regular.get("storePickupProductTitle", part),
                "store_number": store.get("storeNumber"),
                "store_name": store.get("storeName"),
                "street": address.get("address2", ""),
                "city": store.get("city"),
                "state": store.get("state"),
                "postal": address.get("postalCode", ""),
                "miles": float(store.get("storedistance") or 0),
                "from_zip": zip_code,
                "quote": regular.get("storePickupQuote") or info.get("pickupSearchQuote", "Available"),
                "phone": store.get("phoneNumber", ""),
            })
    return hits, {s.get("storeNumber") for s in stores}


def check_all(parts, zips, cookie_path):
    """Query every ZIP and merge results. Returns ({(store, part): hit}, store_count)."""
    cookies = load_cookies(cookie_path)  # re-read each check so you can refresh it live
    hits, store_numbers = {}, set()
    for i, zip_code in enumerate(zips):
        if i:
            time.sleep(SECONDS_BETWEEN_ZIPS)
        data, refreshed = fetch_availability(parts, zip_code, cookies)
        if refreshed and any(cookies.get(k) != v for k, v in refreshed.items()):
            cookies.update(refreshed)
            save_cookies(cookie_path, cookies)
        zip_hits, zip_stores = find_available(data, parts, zip_code)
        store_numbers |= zip_stores
        for h in zip_hits:
            key = (h["store_number"], h["part"])
            # The same store shows up for several ZIPs; keep the one it's closest to.
            if key not in hits or h["miles"] < hits[key]["miles"]:
                hits[key] = h
    return hits, len(store_numbers)


# --- Discord -----------------------------------------------------------------

def send_discord(webhook_url, payload):
    """POST to the webhook. Returns True on success. Prints instead if no webhook."""
    if not webhook_url:
        log.info("[dry run, no webhook set] would send: %s", payload)
        return True
    for _ in range(2):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
        except requests.RequestException as e:
            log.error("Discord request failed: %s", e)
            return False
        if resp.status_code == 429:  # rate limited; Discord says how long to wait
            time.sleep(float(resp.json().get("retry_after", 2)))
            continue
        if resp.ok:
            return True
        log.error("Discord returned HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    return False


def stamp(text):
    """Prefix a message with the current Pacific time, e.g. "[2026-11-03 4:02:10 PM PST] ..."."""
    now = datetime.now(DISPLAY_TZ)
    return f"[{now:%Y-%m-%d} {int(now.strftime('%I'))}:{now:%M:%S %p %Z}] {text}"


def availability_payload(hits, tag_user_ids):
    order = {part: i for i, part in enumerate(PRODUCTS)}  # 1TB first, then 2TB
    by_part = {}
    for h in sorted(hits, key=lambda h: (order.get(h["part"], len(order)), h["miles"])):
        by_part.setdefault(h["part"], []).append(h)

    tags = " ".join(f"<@{uid}>" for uid in tag_user_ids)
    lines = [stamp(f"{tags} **In stock for pickup!**".strip())]
    embeds = []
    for part, part_hits in by_part.items():
        title = part_hits[0]["title"]
        lines.append(f"**{title}**: " + ", ".join(f"{h['store_name']} ({h['city']})" for h in part_hits))
        embeds.append({
            "title": f"{title}: order for pickup",
            "url": PRODUCTS.get(part, FALLBACK_PRODUCT_URL),
            "color": 0x34C759,
            "fields": [
                {
                    "name": f"Apple {h['store_name']} · {h['miles']:g} mi from {h['from_zip']}",
                    "value": f"{h['quote']}\n{h['street']}, {h['city']}, {h['state']} {h['postal']}\n{h['phone']}",
                    "inline": False,
                }
                for h in part_hits[:25]  # Discord caps embeds at 25 fields
            ],
            "footer": {"text": f"Part {part}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    return {
        "content": "\n".join(lines)[:2000],  # Discord's message length limit
        "embeds": embeds[:10],
        # Ping only the listed users, never @everyone/@here or roles.
        "allowed_mentions": {"parse": [], "users": list(tag_user_ids)},
    }


def warning_payload(message, tag_user_ids):
    tags = "".join(f"<@{uid}> " for uid in tag_user_ids)
    return {
        "allowed_mentions": {"parse": [], "users": list(tag_user_ids)},
        "content": (
            f"{tags}⚠️ Apple checker is failing: {message}\n"
            "If Apple rejected the session, open the product page in Chrome, then in "
            "DevTools > Network click the `fulfillment-messages` request, copy its "
            "`cookie` request header, and paste it into cookies.txt. "
            "The running bot picks it up on the next check."
        )
    }


# --- main loop ---------------------------------------------------------------

def run(args):
    if not args.cookies.exists():
        sys.exit(f"Missing {args.cookies}. Paste your browser's cookie header into it.")
    if not args.webhook:
        log.warning("DISCORD_WEBHOOK_URL not set; messages will only be printed here.")

    failures = 0
    warned = False
    zips = ", ".join(args.zip)
    log.info("Watching %s near %s every ~%ss", ", ".join(args.part), zips, args.interval)

    while True:
        try:
            hits, store_count = check_all(args.part, args.zip, args.cookies)

            if warned:
                send_discord(args.webhook, {"content": "✅ Apple checker recovered and is checking again."})
            failures, warned = 0, False

            # Post the result of every check: the stores (with tags) or a short all-clear.
            if hits:
                log.info("AVAILABLE: %s", ", ".join(f"{h['store_name']} ({h['title']})" for h in hits.values()))
                send_discord(args.webhook, availability_payload(hits.values(), args.tag))
            else:
                log.info("%s (%d stores near %s)", NOTHING_AVAILABLE_MESSAGE, store_count, zips)
                send_discord(args.webhook, {"content": stamp(NOTHING_AVAILABLE_MESSAGE), "allowed_mentions": {"parse": []}})

        except CheckError as e:
            failures += 1
            log.warning("Check failed (%d in a row): %s", failures, e)
            if (e.blocked or failures >= FAILURES_BEFORE_ALERT) and not warned:
                warned = send_discord(args.webhook, warning_payload(str(e), args.warn_tag))

        if args.once:
            return
        time.sleep(max(10, args.interval + random.uniform(-5, 5)))


def main():
    load_dotenv(Path.cwd() / ".env")

    def env_ids(name):
        return [t.strip() for t in os.environ.get(name, "").split(",") if t.strip()]

    p = argparse.ArgumentParser(description="Post Apple Store pickup availability to Discord.")
    p.add_argument("--part", action="append", help=f"part number to watch, repeatable (default: {' '.join(PRODUCTS)})")
    p.add_argument("--zip", action="append", help=f"ZIP to search near, repeatable (default: {' '.join(DEFAULT_ZIPS)})")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks (default: 60)")
    p.add_argument("--cookies", type=Path, default=Path(os.environ.get("APPLE_COOKIES_FILE", "cookies.txt")),
                   help="file holding the browser cookie header (default: ./cookies.txt)")
    p.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"), help="Discord webhook URL (or set DISCORD_WEBHOOK_URL)")
    p.add_argument("--tag", action="append", help="Discord user ID to tag when in stock, repeatable (or set DISCORD_TAG_USER_IDS)")
    p.add_argument("--warn-tag", action="append", help="Discord user ID to tag on failure warnings, repeatable (or set DISCORD_WARNING_TAG_USER_IDS)")
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument("--test-webhook", action="store_true", help="send a test message to Discord and exit")
    args = p.parse_args()
    args.part = args.part or list(PRODUCTS)
    args.zip = args.zip or DEFAULT_ZIPS
    args.tag = args.tag or env_ids("DISCORD_TAG_USER_IDS")
    args.warn_tag = args.warn_tag or env_ids("DISCORD_WARNING_TAG_USER_IDS")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.test_webhook:
        if not args.webhook:
            sys.exit("Set DISCORD_WEBHOOK_URL or pass --webhook first.")
        tags = " ".join(f"<@{uid}>" for uid in args.tag)
        ok = send_discord(args.webhook, {
            "content": f"{tags} 👋 Apple checker test message: the webhook and tags work.".strip(),
            "allowed_mentions": {"parse": [], "users": list(args.tag)},
        })
        log.info("Test message %s (tagged: %s)", "sent" if ok else "FAILED", ", ".join(args.tag) or "nobody")
        sys.exit(0 if ok else 1)

    try:
        run(args)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
