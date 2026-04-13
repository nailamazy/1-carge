# STP1 Stripe Checker Bot

Telegram bot for checking CC via Stripe gateway.

## Environment Variables

Set these in Railway dashboard → Variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `ADMIN_IDS` | ❌ | Comma-separated Telegram user IDs (empty = public) |
| `DELAY_BETWEEN` | ❌ | Delay between CC checks in seconds (default: 3) |

## Deploy to Railway

1. Push this folder to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables
4. Deploy!

## Bot Commands

- `/start` — Start bot & show help
- `/chk cc|mm|yy|cvv` — Check single CC
- `/stop` — Stop running bulk check
- `/status` — Bot status

## Bulk Check

- Send multiple CCs as text (1 per line)
- Or send a `.txt` file
