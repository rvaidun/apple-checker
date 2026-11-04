# apple-checker

Checks Apple Store in-store pickup availability every minute and posts the result to a
Discord channel through a webhook:

- **Nothing in stock:** `Nothing available for pickup`
- **In stock:** tags the configured Discord users and lists each store with its
  address, distance, phone number and a link to order

Out of the box it watches the unlocked **iPhone Duo 1TB Night Sky** (`MK1L4LL/A`) and
**2TB Night Sky** (`MK1R4LL/A`) near ZIPs 94568, 94108, 95815 and 95123. Those four
searches cover the Bay Area, the South Bay, Sacramento/Roseville, Modesto and Santa Rosa.
Change them with `--part` and `--zip`.

## Install on Linux

```bash
# 1. Install uv (installs to ~/.local/bin)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # or open a new shell so `uv` is on PATH

# 2. Clone the repo (private: authenticate with `gh auth login` or an SSH key first)
git clone git@github.com:rvaidun/apple-checker.git ~/apple-checker
cd ~/apple-checker

# 3. Install Python + dependencies into .venv (uv downloads Python if needed)
uv sync

# 4. Configure
cp .env.example .env
nano .env          # set DISCORD_WEBHOOK_URL, DISCORD_TAG_USER_IDS, DISCORD_WARNING_TAG_USER_IDS
nano cookies.txt   # paste your Apple cookie header (see "Cookies" below)
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

Apple rejects requests that don't carry a real browser session (HTTP 541), so the bot sends
the cookies from your browser. To get them:

1. In Chrome, open the product page, e.g.
   <https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky-unlocked>
2. Open DevTools (F12), go to **Network**, and click **Check availability** on the page
3. Click the `fulfillment-messages` request, find **Request Headers > cookie**, and copy its value
4. Paste it as a single line into `cookies.txt`

The bot saves the refreshed cookies Apple sends back, so a session lasts a while. When Apple
does stop accepting it, the bot posts a ⚠️ warning to Discord, tagging the users in
`DISCORD_WARNING_TAG_USER_IDS`. Paste a fresh cookie header
into `cookies.txt` and the running bot picks it up on the next check; no restart needed.

Apple may tie the session to your IP address. If the Linux box is on a different network
than the browser you copied the cookies from, and checks fail right away with HTTP 541,
copy the cookies from a browser on the same network as the box.

## Options

```
uv run apple-checker --help

  --part PART        part number to watch, repeatable (default: MK1L4LL/A MK1R4LL/A)
  --zip ZIP          ZIP to search near, repeatable (default: 94568 94108 95815 95123)
  --interval SECS    seconds between checks (default: 60)
  --cookies PATH     cookie header file (default: ./cookies.txt)
  --webhook URL      Discord webhook (default: $DISCORD_WEBHOOK_URL)
  --tag USER_ID      Discord user to tag when in stock, repeatable (default: $DISCORD_TAG_USER_IDS)
  --warn-tag USER_ID Discord user to tag on failure warnings, repeatable (default: $DISCORD_WARNING_TAG_USER_IDS)
  --once             run a single check and exit
  --test-webhook     send a tagged test message and exit
```

To find a part number for another model, open its product page, then in DevTools > Network
look at the `parts.0=` value in the `fulfillment-messages` request.

## Update

```bash
cd ~/apple-checker && git pull && uv sync && systemctl --user restart apple-checker
```
