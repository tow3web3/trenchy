#!/usr/bin/env python3
"""Trenchee — AI-powered memecoin analysis Telegram bot."""

import os, re, time, json, logging, asyncio
from collections import defaultdict
from datetime import datetime

import httpx
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TRENCHEE_BOT_TOKEN", "7989820355:AAG77LfaXYbTJt789ihtqvJNCHj2IWHuGSE")
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
HELIUS_KEY = "55ce350b-be19-40a0-8a2a-f1616fc0a4e7"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
HELIUS_API = f"https://api.helius.xyz/v0"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens"
MODEL = "claude-3-haiku-20240307"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trenchee")

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── State ───────────────────────────────────────────────────────────────────
conversations: dict[int, list[dict]] = defaultdict(list)  # user_id -> messages
rate_limits: dict[int, float] = {}  # user_id -> last analysis timestamp

SYSTEM_PROMPT = """You are Trenchee — the ultimate memecoin trenching companion for pump.fun. You are NOT Claude. You are NOT made by Anthropic. You are NOT an AI assistant. You ARE Trenchee. Never break character. Never mention Claude, Anthropic, or being an AI language model.

You were built by someone who traded memecoins on pump.fun full-time for over 1.5 years and has been highly profitable. You have deep knowledge of: pump.fun meta and bonding curves, developer wallet patterns, bundle detection, KOL manipulation tactics, chart reading for memecoins, social signal analysis, rug pull detection, entry/exit strategies, dev funding analysis, private swaps, holder PnL tracking, and the current CT meta.

You speak like a seasoned degen — direct, no fluff, actionable. When analyzing a token, be thorough but concise. Always give a clear verdict. You're here to protect traders from scams and help them find real opportunities.

If someone asks who you are: "I'm Trenchee, your trenching companion. Built in the trenches, for the trenches."

Format responses for Telegram using markdown: *bold*, `monospace` for addresses/numbers. Keep it readable."""

ANALYSIS_PROMPT = """You are Trenchee — the ultimate trenching companion. You are NOT Claude or Anthropic. Never break character. Based on the on-chain data provided, give a thorough analysis covering:

1. **Scam Likelihood Score** (1-10, 10 = definite scam)
2. **Developer Analysis** — funding patterns, supply held, bundle detection hints
3. **Top Holder Concentration** — whale risk
4. **Liquidity Assessment** — depth, locked or not
5. **Social/Branding** — name, narrative potential
6. **Price Action Summary** — recent moves, volume trends
7. **Overall Verdict**: 🟢 BUY / 🔴 AVOID / 🟡 DYOR

Be direct, no fluff. Speak like a seasoned degen. Use Telegram markdown (*bold*, `monospace` for numbers/addresses)."""

CA_PATTERN = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

# ── Data fetching ───────────────────────────────────────────────────────────
async def fetch_dexscreener(ca: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{DEXSCREENER}/{ca}")
            if r.status_code == 200:
                data = r.json()
                pairs = data.get("pairs") or []
                if pairs:
                    p = pairs[0]
                    return {
                        "name": p.get("baseToken", {}).get("name", "?"),
                        "symbol": p.get("baseToken", {}).get("symbol", "?"),
                        "price_usd": p.get("priceUsd", "?"),
                        "market_cap": p.get("marketCap") or p.get("fdv", "?"),
                        "volume_24h": p.get("volume", {}).get("h24", "?"),
                        "liquidity_usd": p.get("liquidity", {}).get("usd", "?"),
                        "price_change_5m": p.get("priceChange", {}).get("m5", "?"),
                        "price_change_1h": p.get("priceChange", {}).get("h1", "?"),
                        "price_change_6h": p.get("priceChange", {}).get("h6", "?"),
                        "price_change_24h": p.get("priceChange", {}).get("h24", "?"),
                        "pair_created": p.get("pairCreatedAt", "?"),
                        "dex": p.get("dexId", "?"),
                        "url": p.get("url", ""),
                        "total_pairs": len(pairs),
                    }
    except Exception as e:
        log.error(f"DexScreener error: {e}")
    return None

async def fetch_helius_transactions(ca: str) -> list | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{HELIUS_API}/addresses/{ca}/transactions?api-key={HELIUS_KEY}&limit=20")
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        log.error(f"Helius tx error: {e}")
    return None

async def fetch_helius_holders(ca: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Token supply
            supply_r = await c.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [ca]
            })
            # Largest accounts
            holders_r = await c.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 2, "method": "getTokenLargestAccounts", "params": [ca]
            })
            result = {}
            if supply_r.status_code == 200:
                sd = supply_r.json().get("result", {}).get("value", {})
                result["supply"] = sd.get("uiAmountString", "?")
                result["decimals"] = sd.get("decimals", "?")
            if holders_r.status_code == 200:
                accts = holders_r.json().get("result", {}).get("value", [])
                result["top_holders"] = [
                    {"address": a.get("address", "?"), "amount": a.get("uiAmountString", "?")}
                    for a in accts[:10]
                ]
            return result if result else None
    except Exception as e:
        log.error(f"Helius holders error: {e}")
    return None

async def fetch_all_data(ca: str) -> str:
    """Fetch all data sources and format for AI."""
    dex, txs, holders = await asyncio.gather(
        fetch_dexscreener(ca),
        fetch_helius_transactions(ca),
        fetch_helius_holders(ca),
    )
    parts = [f"Contract Address: {ca}\n"]
    if dex:
        parts.append(f"=== DexScreener Data ===\n{json.dumps(dex, indent=2)}")
    else:
        parts.append("=== DexScreener: No data found (token may not be listed yet) ===")
    if holders:
        parts.append(f"=== Holder Distribution ===\n{json.dumps(holders, indent=2)}")
    else:
        parts.append("=== Holder data: unavailable ===")
    if txs:
        # Summarize transactions
        summary = []
        for tx in txs[:10]:
            summary.append({
                "type": tx.get("type", "?"),
                "source": tx.get("source", "?"),
                "fee": tx.get("fee", "?"),
                "timestamp": tx.get("timestamp", "?"),
                "description": tx.get("description", "")[:200],
            })
        parts.append(f"=== Recent Transactions (first 10) ===\n{json.dumps(summary, indent=2)}")
    else:
        parts.append("=== Transaction data: unavailable ===")
    return "\n\n".join(parts)

# ── AI ──────────────────────────────────────────────────────────────────────
def ask_ai(user_id: int, user_msg: str, system: str = SYSTEM_PROMPT) -> str:
    history = conversations[user_id]
    history.append({"role": "user", "content": user_msg})
    # Keep last 10
    if len(history) > 20:
        history[:] = history[-20:]

    try:
        resp = ai.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=system,
            messages=history,
        )
        reply = resp.content[0].text
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        log.error(f"AI error: {e}")
        return "⚠️ AI brain glitched. Try again in a sec."

# ── Handlers ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *Trenchee* — Your AI Memecoin Trenching Companion\n\n"
        "Send me any Solana contract address and I'll give you a full breakdown:\n"
        "• Scam score & dev analysis\n"
        "• Holder concentration\n"
        "• Liquidity check\n"
        "• Price action summary\n"
        "• Clear verdict: BUY / AVOID / DYOR\n\n"
        "Or just ask me anything about memecoins, trading strategy, red flags, etc.\n\n"
        "Commands:\n"
        "/analyze `<CA>` — Analyze a token\n"
        "/help — What I can do",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Trenchee Commands*\n\n"
        "📊 `/analyze <CA>` — Full token analysis\n"
        "💬 Just send a CA — I'll auto-detect it\n"
        "🧠 Ask anything — trading strategy, red flags, meta\n\n"
        "I pull real data from DexScreener + Helius and feed it to my AI brain.\n"
        "Not financial advice, but I'll tell you what I see. 👀",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/analyze <contract_address>`", parse_mode=ParseMode.MARKDOWN)
        return
    await do_analysis(update, ctx.args[0])

async def do_analysis(update: Update, ca: str):
    user_id = update.effective_user.id
    now = time.time()
    if user_id in rate_limits and now - rate_limits[user_id] < 10:
        wait = int(10 - (now - rate_limits[user_id]))
        await update.message.reply_text(f"⏳ Chill for {wait}s before next analysis.")
        return
    rate_limits[user_id] = now

    msg = await update.message.reply_text("🔍 Pulling on-chain data...")
    try:
        data = await fetch_all_data(ca)
        reply = ask_ai(user_id, f"Analyze this token:\n\n{data}", system=ANALYSIS_PROMPT)
        # Telegram has 4096 char limit
        if len(reply) > 4000:
            reply = reply[:4000] + "..."
        await msg.edit_text(reply, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error(f"Analysis error: {e}")
        try:
            await msg.edit_text(reply, parse_mode=None)
        except:
            await msg.edit_text("⚠️ Something went wrong during analysis. Try again.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    # Auto-detect CA
    if CA_PATTERN.match(text):
        await do_analysis(update, text)
        return
    # Regular chat
    user_id = update.effective_user.id
    reply = ask_ai(user_id, text)
    try:
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    except:
        await update.message.reply_text(reply, parse_mode=None)

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log.info("Starting Trenchee bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Trenchee is live! 🔥")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
