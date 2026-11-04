# apple-checker

Checks Apple Store in-store pickup availability every 15 minutes and posts the result to a
Discord channel through a webhook:

- **Nothing in stock:** `[time] Nothing available for pickup`
- **In stock:** `[time]`, tags the configured Discord users, and lists each store with its
  address, distance, phone number and a link to order

Out of the box it watches the unlocked **iPhone Duo 1TB Night Sky** (`MK1L4LL/A`) and
**2TB Night Sky** (`MK1R4LL/A`) near ZIPs 94568, 94108, 95815 and 95123. Those four
searches cover the Bay Area, the South Bay, Sacramento/Roseville, Modesto and Santa Rosa.
Change them with `--part` and `--zip`.

## How it checks

1. **Availability API** (`/shop/fulfillment-messages`), using the browser cookies in
   `cookies.txt`. Apple often rejects this with HTTP 541.
2. **Bag search fallback** (what the bag page's "check availability" uses). The bot keeps
   both iPhones in a bag that belongs to its own Chrome profile (`chrome-profile/`), saves
   that bag's session to `bag_session.json`, and searches stores with plain HTTP requests.
   When the saved session stops working, headless Chrome (SeleniumBase CDP mode) refreshes
   it automatically, re-adding the iPhones to the bag if needed. Nothing is ever ordered.

If both fail, it posts a ⚠️ warning that tags the users in `DISCORD_WARNING_TAG_USER_IDS`.

## Install on Linux

```bash
# 1. Install uv (installs to ~/.local/bin) and Google Chrome (for the bag fallback)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # or open a new shell so `uv` is on PATH
wget -qO /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y /tmp/chrome.deb

# 2. Clone the repo (private: authenticate with `gh auth login` or an SSH key first)
git clone git@github.com:rvaidun/apple-checker.git ~/apple-checker
cd ~/apple-checker

# 3. Install Python + dependencies into .venv (uv downloads Python if needed)
uv sync

# 4. Configure
cp .env.example .env
nano .env          # set DISCORD_WEBHOOK_URL, DISCORD_TAG_USER_IDS, DISCORD_WARNING_TAG_USER_IDS
nano cookies.txt   # optional: your Apple cookie header (see "Cookies" below)
chmod 600 .env cookies.txt

# 5. Test
uv run apple-checker --test-webhook   # sends a tagged test message to Discord
uv run apple-checker --once           # one real check, posts the result
```

## Run it 24/7 with systemd

```bash
mkdir -p ~/.config/systemd/user
cp deploy/apple-checker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now apple-checker
sudo loginctl enable-linger "$USER"   # keep it running after logout and across reboots

journalctl --user -u apple-checker -f   # live logs
systemctl --user restart apple-checker  # after changing .env or pulling updates
```

The service file assumes the repo is at `~/apple-checker` and uv is at `~/.local/bin/uv`.
Edit `WorkingDirectory` / `ExecStart` if yours differ.

## Cookies

`cookies.txt` is only used by the availability API; the bag fallback manages its own session.
To get them:

1. In Chrome, open the product page, e.g.
   <https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky-unlocked>
2. Open DevTools (F12), go to **Network**, and click **Check availability** on the page
3. Click the `fulfillment-messages` request, find **Request Headers > cookie**, and copy its value
4. Paste it as a single line into `cookies.txt`

The bot saves the refreshed cookies Apple sends back. A running bot picks up a new
`cookies.txt` on the next check; no restart needed.

## Options

```
uv run apple-checker --help

  --part PART        part number to watch, repeatable (default: MK1L4LL/A MK1R4LL/A)
  --zip ZIP          ZIP to search near, repeatable (default: 94568 94108 95815 95123)
  --interval SECS    seconds between checks (default: 900 = 15 minutes)
  --cookies PATH     cookie header file (default: ./cookies.txt)
  --webhook URL      Discord webhook (default: $DISCORD_WEBHOOK_URL)
  --tag USER_ID      Discord user to tag when in stock, repeatable (default: $DISCORD_TAG_USER_IDS)
  --warn-tag USER_ID Discord user to tag on failure warnings, repeatable (default: $DISCORD_WARNING_TAG_USER_IDS)
  --once             run a single check and exit
  --test-webhook     send a tagged test message and exit
```

To watch another model, add its part number and product page to `PRODUCTS` in
`src/apple_checker/checker.py` (the bag fallback needs the page to add it to the bag).
You can find a part number in DevTools > Network as the `parts.0=` value of the
`fulfillment-messages` request on its product page.

## Update

```bash
cd ~/apple-checker && git pull && uv sync && systemctl --user restart apple-checker
```
