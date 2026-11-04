"""Poll Apple's in-store pickup availability and post the result to a Discord webhook.

Every check posts one message: "Nothing available for pickup", or the stores that
have stock (with addresses) and a tag for each configured Discord user.

Each check first tries Apple's fulfillment-messages API (using cookies.txt). If Apple
rejects it, the check falls back to the shopping bag's store search (see bag.py),
which keeps its own session and refreshes it with headless Chrome when needed.

Run from a directory containing:
    .env         DISCORD_WEBHOOK_URL=..., DISCORD_TAG_USER_IDS=..., DISCORD_WARNING_TAG_USER_IDS=...
    cookies.txt  the `cookie` request header copied from your browser (optional; the
                 bag fallback works without it)

Usage:
    apple-checker                 # check every 15 minutes, forever
    apple-checker --once          # single check, then exit
    apple-checker --test-webhook  # send a test message to Discord
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

from apple_checker import bag

FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"

# Part number -> product page linked in alerts (and used to add it to the fallback bag).
# Part numbers are shared across carriers; cppart=UNLOCKED/US selects the unlocked model.
PRODUCTS = {
    "MK1L4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky-unlocked",
    "MK1R4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-2tb-night-sky-unlocked",
}
FALLBACK_PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"

# Apple returns the 12 stores nearest each ZIP. Together these cover the Bay Area,
# South Bay (Oakridge, Los Gatos), Sacramento/Roseville, Modesto and Santa Rosa.
DEFAULT_ZIPS = ["94568", "94108", "95815", "95123"]

# Files the bag fallback manages itself (relative to the working directory).
BAG_SESSION_FILE = Path("bag_session.json")
BROWSER_PROFILE_DIR = Path("chrome-profile")

NOTHING_AVAILABLE_MESSAGE = "Nothing available for pickup"
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")  # all watched stores are in California
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
    """A check failed."""


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


# --- primary source: fulfillment-messages API ---------------------------------

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
        raise CheckError(f"HTTP {resp.status_code}: Apple rejected the session")
    if resp.status_code != 200:
        raise CheckError(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise CheckError("response was not JSON (session likely expired)")

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


# --- checking ----------------------------------------------------------------

def merge_zips(zips, check_zip):
    """Run check_zip(zip) -> (hits, store_ids) for each ZIP. Returns ({(store, part): hit}, store_count)."""
    hits, store_ids = {}, set()
    for i, zip_code in enumerate(zips):
        if i:
            time.sleep(SECONDS_BETWEEN_ZIPS)
        zip_hits, zip_stores = check_zip(zip_code)
        store_ids |= zip_stores
        for h in zip_hits:
            key = (h["store_number"], h["part"])
            # The same store shows up for several ZIPs; keep the one it's closest to.
            if key not in hits or h["miles"] < hits[key]["miles"]:
                hits[key] = h
    return hits, len(store_ids)


def check_fulfillment_api(parts, zips, cookie_path):
    if not cookie_path.exists():
        raise CheckError(f"{cookie_path} not found")
    cookies = load_cookies(cookie_path)  # re-read each check so you can refresh it live

    def check_zip(zip_code):
        data, refreshed = fetch_availability(parts, zip_code, cookies)
        if refreshed and any(cookies.get(k) != v for k, v in refreshed.items()):
            cookies.update(refreshed)
            save_cookies(cookie_path, cookies)
        return find_available(data, parts, zip_code)

    return merge_zips(zips, check_zip)


def check_bag(parts, zips):
    session = bag.ensure_session(BAG_SESSION_FILE, BROWSER_PROFILE_DIR, PRODUCTS, parts)
    try:
        result = merge_zips(zips, lambda z: bag.search_zip(session, z, parts, HEADERS))
    except bag.BagError as e:
        log.info("Bag session stopped working (%s); refreshing it", e)
        session = bag.ensure_session(BAG_SESSION_FILE, BROWSER_PROFILE_DIR, PRODUCTS, parts, force=True)
        result = merge_zips(zips, lambda z: bag.search_zip(session, z, parts, HEADERS))
    bag.save_session(BAG_SESSION_FILE, session)  # keep cookies Apple refreshed
    return result


def check(args):
    """Try the fulfillment API, then the bag search. Returns (hits, store_count, source)."""
    try:
        return (*check_fulfillment_api(args.part, args.zip, args.cookies), "fulfillment API")
    except CheckError as e:
        api_error = e
        log.warning("Fulfillment API failed (%s); falling back to the bag search", e)
    try:
        return (*check_bag(args.part, args.zip), "bag search")
    except bag.BagError as e:
        raise CheckError(f"fulfillment API: {api_error}; bag search: {e}")


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
            f"{tags}⚠️ Apple checker is failing: {message[:1500]}\n"
            "Both the availability API and the bag-search fallback failed. Check the logs with "
            "`journalctl --user -u apple-checker -e`. To fix the API, open the product page in "
            "Chrome, copy the `cookie` request header of the `fulfillment-messages` request from "
            "DevTools > Network, and paste it into cookies.txt; the bot picks it up on the next check."
        ),
    }


# --- main loop ---------------------------------------------------------------

def run(args):
    if not args.webhook:
        log.warning("DISCORD_WEBHOOK_URL not set; messages will only be printed here.")

    failures = 0
    warned = False
    zips = ", ".join(args.zip)
    log.info("Watching %s near %s every ~%ss", ", ".join(args.part), zips, args.interval)

    while True:
        try:
            hits, store_count, source = check(args)

            if warned:
                send_discord(args.webhook, {"content": "✅ Apple checker recovered and is checking again."})
            failures, warned = 0, False

            # Post the result of every check: the stores (with tags) or a short all-clear.
            if hits:
                log.info("AVAILABLE via %s: %s", source,
                         ", ".join(f"{h['store_name']} ({h['title']})" for h in hits.values()))
                send_discord(args.webhook, availability_payload(hits.values(), args.tag))
            else:
                log.info("%s (%d stores near %s, via %s)", NOTHING_AVAILABLE_MESSAGE, store_count, zips, source)
                send_discord(args.webhook, {"content": stamp(NOTHING_AVAILABLE_MESSAGE), "allowed_mentions": {"parse": []}})

        except CheckError as e:
            failures += 1
            log.warning("Check failed (%d in a row): %s", failures, e)
            if not warned:  # warn once per outage; checks are 15 min apart
                warned = send_discord(args.webhook, warning_payload(str(e), args.warn_tag))

        if args.once:
            return
        time.sleep(max(10, args.interval + random.uniform(-15, 15)))


def main():
    load_dotenv(Path.cwd() / ".env")

    def env_ids(name):
        return [t.strip() for t in os.environ.get(name, "").split(",") if t.strip()]

    p = argparse.ArgumentParser(description="Post Apple Store pickup availability to Discord.")
    p.add_argument("--part", action="append", help=f"part number to watch, repeatable (default: {' '.join(PRODUCTS)})")
    p.add_argument("--zip", action="append", help=f"ZIP to search near, repeatable (default: {' '.join(DEFAULT_ZIPS)})")
    p.add_argument("--interval", type=int, default=900, help="seconds between checks (default: 900 = 15 min)")
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
