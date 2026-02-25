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

ANALYSIS_PROMPT = """You are Trenchee — the ultimate trenching companion. You are NOT Claude or Anthropic. Never break character. Based on the on-chain data provided, give a DETAILED analysis covering ALL of these:

*▸ TOKEN OVERVIEW* — name, ticker, price, mcap, liquidity, volume, age

*▸ CONTRACT SECURITY* — freeze auth, mint auth status (note: these are standard on pump.fun, not red flags there)

*▸ HOLDER DISTRIBUTION* — top holder concentration %, whale risk, check if multiple holders have suspiciously similar amounts (bundle signal)

*▸ DEV FUNDING & BUNDLE ANALYSIS* — analyze early transaction patterns, look for coordinated buys within seconds of each other, private swaps, dev accumulation patterns, estimate dev supply from timing data

*▸ CURRENT META FIT* — does the name/narrative match trending CT meta? volume/mcap ratio health, age vs hype assessment

*▸ PREVIOUS DEPLOY LIKELIHOOD* — any signs the dev has done this before (fresh wallet, patterns)

*▸ RISK SCORE* — 1-10 with clear reasoning

*▸ VERDICT* — 🟢 BUY / 🔴 AVOID / 🟡 DYOR with specific reasoning

Be thorough. Every detail matters. Speak like a seasoned degen — direct, no fluff, actionable. Use Telegram markdown (*bold*, `monospace` for numbers/addresses). If data is missing for a section, say what's missing and what it might mean."""

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

async def fetch_signatures(ca: str) -> list | None:
    """Get earliest signatures for timing analysis."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(HELIUS_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [ca, {"limit": 30}]
            })
            if r.status_code == 200:
                return r.json().get("result", [])
    except Exception as e:
        log.error(f"Signatures error: {e}")
    return None

async def fetch_all_data(ca: str) -> str:
    """Fetch all data sources and format for AI."""
    dex, txs, holders, sigs = await asyncio.gather(
        fetch_dexscreener(ca),
        fetch_helius_transactions(ca),
        fetch_helius_holders(ca),
        fetch_signatures(ca),
    )
    parts = [f"Contract Address: {ca}\n"]

    if dex:
        parts.append(f"=== DexScreener Data ===\n{json.dumps(dex, indent=2)}")
    else:
        parts.append("=== DexScreener: No data found (token may not be listed yet) ===")

    if holders:
        # Calculate concentration
        top_holders = holders.get("top_holders", [])
        supply = float(holders.get("supply", 0) or 0)
        if supply > 0 and top_holders:
            for h in top_holders:
                amt = float(h.get("amount", 0) or 0)
                h["pct_of_supply"] = round(amt / supply * 100, 2)
            # Check for similar-amount wallets (bundle signal)
            amounts = [float(h.get("amount", 0) or 0) for h in top_holders[:10] if float(h.get("amount", 0) or 0) > 0]
            clusters = 0
            for i in range(len(amounts)):
                for j in range(i+1, len(amounts)):
                    if amounts[j] > 0 and abs(amounts[i] - amounts[j]) / max(amounts[i], amounts[j]) < 0.05:
                        clusters += 1
            holders["similar_amount_wallet_pairs"] = clusters
            holders["bundle_signal"] = "HIGH" if clusters >= 3 else "MODERATE" if clusters >= 1 else "NONE"
        parts.append(f"=== Holder Distribution ===\n{json.dumps(holders, indent=2)}")
    else:
        parts.append("=== Holder data: unavailable ===")

    if txs:
        summary = []
        for tx in txs[:20]:
            summary.append({
                "type": tx.get("type", "?"),
                "source": tx.get("source", "?"),
                "fee": tx.get("fee", "?"),
                "timestamp": tx.get("timestamp", "?"),
                "description": tx.get("description", "")[:200],
            })
        # Timing analysis
        timestamps = sorted([tx["timestamp"] for tx in summary if tx.get("timestamp") and tx["timestamp"] != "?"])
        timing = {}
        if len(timestamps) >= 2:
            gaps = [timestamps[i+1] - timestamps[i] for i in range(min(len(timestamps)-1, 10))]
            timing["avg_gap_seconds"] = round(sum(gaps) / len(gaps), 1)
            timing["min_gap_seconds"] = min(gaps)
            timing["bot_activity_likely"] = min(gaps) < 3
        early_swaps = len([t for t in summary[-10:] if t["type"] == "SWAP"])
        timing["early_swaps_count"] = early_swaps
        timing["dev_accumulation_signal"] = early_swaps >= 3
        parts.append(f"=== Recent Transactions (20) ===\n{json.dumps(summary, indent=2)}\n\n=== Timing Analysis ===\n{json.dumps(timing, indent=2)}")
    else:
        parts.append("=== Transaction data: unavailable ===")

    if sigs:
        early = sigs[-10:]  # earliest ones
        block_times = [s.get("blockTime") for s in early if s.get("blockTime")]
        if len(block_times) >= 2:
            span = max(block_times) - min(block_times)
            parts.append(f"=== Early Signature Timing ===\nFirst {len(early)} txs span: {span}s\nBundle signal: {'HIGH — coordinated launch' if span < 30 else 'MODERATE' if span < 120 else 'LOW'}")

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
            max_tokens=2500,
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
        "🔥 *TRENCHEE* — The Ultimate Trenching Companion\n\n"
        "Built by a full-time pump.fun trader. Trained on every aspect of crypto twitter.\n\n"
        "*What I analyze:*\n"
        "• Developer funding process & private swap detection\n"
        "• Bundle detection — coordinated wallet clusters\n"
        "• Holder PnL tracking & concentration risk\n"
        "• Dev supply estimation via timing analysis\n"
        "• Scam probability scoring (1-10)\n"
        "• Current meta fit & narrative potential\n"
        "• Previous deploy likelihood\n"
        "• Liquidity depth & volume health\n"
        "• Clear verdict: 🟢 BUY / 🔴 AVOID / 🟡 DYOR\n\n"
        "*How to use:*\n"
        "📊 Send any Solana CA — I auto-detect and analyze\n"
        "💬 Ask anything about memecoins, strategy, red flags\n"
        "🔍 `/analyze <CA>` for detailed breakdown\n\n"
        "Built in the trenches. For the trenches. 🤖\n"
        "Website: trenchee.fun | X: @\\_Trenchee\\_",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Trenchee — Full Capabilities*\n\n"
        "📊 *Token Analysis* — Send any CA\n"
        "Dev funding, bundles, holder distribution, scam score, meta fit, verdict\n\n"
        "🧠 *Ask Anything*\n"
        "Trading strategy, red flags, current meta, KOL analysis, entry/exit\n\n"
        "🔍 `/analyze <CA>` — Detailed breakdown\n\n"
        "I pull live data from DexScreener + Helius RPC, analyze transaction patterns, "
        "holder clusters, and dev behavior. Every detail matters.\n\n"
        "Website: trenchee.fun | X: @\\_Trenchee\\_",
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
