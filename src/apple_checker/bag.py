"""Fallback availability source: the shopping bag's store search.

Apple can reject the fulfillment-messages API (HTTP 541) while the bag's "check
availability" search keeps working. This module keeps the watched iPhones in a bag
that belongs to a dedicated Chrome profile, saves that bag's session (cookies, request
headers, line items) to a JSON file, and runs the store search with plain HTTP requests.
When the saved session stops working, headless Chrome (SeleniumBase CDP mode) refreshes
it, re-adding the iPhones to the bag if needed.
"""

import json
import logging
import re
import time

import requests

BAG_URL = "https://www.apple.com/shop/bag"
SEARCH_URL = "https://www.apple.com/shop/bagx?_a=search&_m=shoppingCart.items.{item}.delivery.storeLocator"
ITEM_KEY_RE = re.compile(r'"(item-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})":\{')
SKU_RE = re.compile(r'"sku":"([^"]+)"')
CART_HEADERS_RE = re.compile(r'"h":(\{"x-aos-model-page":"cart"[^}]*\})')

# Selects "No AppleCare coverage" on a product page; Add to Bag stays disabled until a choice is made.
NO_APPLECARE_JS = """(() => {
  const input = [...document.querySelectorAll('input[name=applecare-options]')].find(i =>
    /no applecare/i.test(document.querySelector('label[for="' + i.id + '"]')?.innerText || ''));
  if (!input) return false;
  document.querySelector('label[for="' + input.id + '"]').click();
  return true;
})()"""

log = logging.getLogger("apple-checker")


class BagError(Exception):
    pass


# --- session file --------------------------------------------------------------

def load_session(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_session(path, session):
    path.write_text(json.dumps(session, indent=1))
    path.chmod(0o600)


def ensure_session(path, profile_dir, products, parts, force=False):
    """Return a saved bag session holding every part, refreshing it with Chrome if needed."""
    session = None if force else load_session(path)
    if session and set(parts) <= set(session.get("items", {}).values()):
        return session
    missing_pages = [p for p in parts if p not in products]
    if missing_pages:
        raise BagError(f"no product page known for {', '.join(missing_pages)}, so it can't be added to the bag")
    try:
        session = refresh_session(profile_dir, {p: products[p] for p in parts})
    except BagError:
        raise
    except Exception as e:  # Chrome missing, crashed, timed out, page changed...
        raise BagError(f"browser refresh failed: {type(e).__name__}: {e}") from e
    save_session(path, session)
    return session


# --- browser -------------------------------------------------------------------

def parse_bag_page(html):
    """Return ({item_id: part_number}, cart_request_headers) from the bag page HTML."""
    keys = list(ITEM_KEY_RE.finditer(html))
    items = {}
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else m.end() + 8000
        sku = SKU_RE.search(html, m.end(), end)
        if sku and m.group(1) not in items:
            items[m.group(1)] = sku.group(1).upper()
    headers = CART_HEADERS_RE.search(html)
    return items, (json.loads(headers.group(1)) if headers else None)


def refresh_session(profile_dir, products):
    """Open headless Chrome, make sure every product is in the bag, and return the bag session."""
    from seleniumbase import sb_cdp  # heavy import, only needed when refreshing

    log.info("Refreshing the bag session with headless Chrome")
    sb = sb_cdp.Chrome(BAG_URL, headless=True, user_data_dir=str(profile_dir))

    def page_js(expression):
        return sb.loop.run_until_complete(sb.page.evaluate(expression, await_promise=True))

    def read_bag():
        return parse_bag_page(page_js("document.documentElement.outerHTML"))

    try:
        sb.sleep(5)
        items, headers = read_bag()
        for part, url in products.items():
            for _ in range(3):  # the first Add to Bag click occasionally doesn't register
                if part in items.values():
                    break
                log.info("Adding %s to the bag", part)
                sb.open(url)
                sb.sleep(10)
                if not page_js(NO_APPLECARE_JS):
                    raise BagError(f"couldn't find the 'No AppleCare coverage' option on {url}")
                sb.sleep(2)
                sb.click('button[name="add-to-cart"]')
                sb.sleep(8)
                sb.open(BAG_URL)
                sb.sleep(5)
                items, headers = read_bag()
        missing = [p for p in products if p not in items.values()]
        if missing:
            raise BagError(f"couldn't add {', '.join(missing)} to the bag")
        if not headers:
            raise BagError("couldn't find the bag's request headers on the bag page")
        cookies = {c.name: c.value for c in sb.get_all_cookies() if "apple.com" in (c.domain or "")}
    finally:
        sb.driver.stop()

    return {"cookies": cookies, "headers": headers, "items": items, "refreshed_at": int(time.time())}


# --- store search --------------------------------------------------------------

def parse_miles(distance):
    m = re.match(r"\s*([\d.]+)", str(distance or ""))
    return float(m.group(1)) if m else 0.0


def search_zip(session, zip_code, parts, base_headers):
    """Run the bag's store search near one ZIP. Returns (hits, store_ids) like the primary API."""
    item = next(iter(session["items"]))
    try:
        resp = requests.post(
            SEARCH_URL.format(item=item),
            headers={
                **base_headers,
                **session["headers"],
                "content-type": "application/x-www-form-urlencoded",
                "x-requested-with": "Fetch",
                "origin": "https://www.apple.com",
                "referer": BAG_URL,
            },
            data={f"shoppingCart.items.{item}.delivery.storeLocator.searchInput": zip_code},
            cookies=session["cookies"],
            timeout=20,
        )
    except requests.RequestException as e:
        raise BagError(f"network error: {e}") from e
    if resp.status_code != 200:
        raise BagError(f"bag search HTTP {resp.status_code}")
    try:
        locator = resp.json()["body"]["shoppingCart"]["items"][item]["delivery"]["storeLocator"]
        stores = locator["searchResults"]["d"]["retailStores"]
    except (ValueError, KeyError, TypeError):
        raise BagError("bag search returned unexpected data (bag session expired?)")
    session["cookies"].update(resp.cookies.get_dict())

    hits = []
    for store in stores:
        address = store.get("retailAddress") or {}
        for line in (store.get("availability") or {}).get("lineItemAvailability") or []:
            part = session["items"].get(f"item-{line.get('partId')}")
            # availableNowForLine is true for "Available Today/Tomorrow", false for later dates.
            if part not in parts or not line.get("availableNowForLine"):
                continue
            hits.append({
                "part": part,
                "title": line.get("partName", part).replace("\xa0", " "),
                "store_number": store.get("storeId"),
                "store_name": store.get("storeName", "").removeprefix("Apple "),
                "street": address.get("street", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "postal": address.get("postalCode", ""),
                "miles": parse_miles(store.get("distance")),
                "from_zip": zip_code,
                "quote": line.get("availabilityQuote", "Available"),
                "phone": store.get("phoneNumber", ""),
            })
    return hits, {s.get("storeId") for s in stores}
