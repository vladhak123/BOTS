"""
Polymarket Simulation Bot
- DeepSeek AI для аналізу
- Polymarket публічний API
- Пам'ять у JSON
- Telegram інтерфейс
"""

import json
import os
import logging
from datetime import datetime, timezone, time as dtime
from pathlib import Path

import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────
# 🔑  ENV VARS — встав на Railway
# ─────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "8647895785:AAESQ2oSwnTNCXW9y9RjgsWvMZjyS_mX3iA")
DS_KEY = os.environ.get("DS_KEY", "sk-7f2b9cc52ff3405baab9824544b129b9")
MEMORY_FILE      = os.environ.get("MEMORY_FILE", "bot_memory.json")
STARTING_BALANCE = 1000.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

def call_deepseek(prompt: str) -> str:
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {os.environ.get('DS_KEY', '')}",
                 "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 400, "temperature": 0.3},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ══════════════════════════════════════════════
# 💾  MEMORY
# ══════════════════════════════════════════════

def load_memory() -> dict:
    Path(MEMORY_FILE).parent.mkdir(parents=True, exist_ok=True)
    if Path(MEMORY_FILE).exists():
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "balance": STARTING_BALANCE,
        "total_profit": 0.0,
        "bets": [],
        "stats": {"wins": 0, "losses": 0, "total_wagered": 0.0},
    }

def save_memory(data: dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════
# 📡  POLYMARKET API
# ══════════════════════════════════════════════

def fetch_markets(limit: int = 100) -> list[dict]:
    import random
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
            timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        valid = [m for m in markets if m.get("question") and m.get("outcomePrices")]

        # LOTTERY STRATEGY: find markets with at least one outcome priced <8%
        lottery = []
        for m in valid:
            prices = parse_prices(m)
            min_price = min(prices.values()) if prices else 1.0
            if min_price <= 0.08:  # at least one outcome < 8% = potential x12+
                m["_min_price"] = min_price
                lottery.append(m)

        # sort by min_price ascending (cheapest = biggest multiplier)
        lottery.sort(key=lambda x: x.get("_min_price", 1))

        # take top-30 cheapest, shuffle to avoid always same market
        top = lottery[:30]
        random.shuffle(top)
        return top[:20] if top else valid[:20]
    except Exception as e:
        log.error("Polymarket error: %s", e)
        return []

def fetch_market(market_id: str) -> dict | None:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Market fetch error: %s", e)
        return None

def parse_prices(market: dict) -> dict:
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        prices   = json.loads(market.get("outcomePrices", "[]"))
        return {o: float(p) for o, p in zip(outcomes, prices)}
    except Exception:
        return {}


# ══════════════════════════════════════════════
# 🤖  DEEPSEEK ANALYSIS
# ══════════════════════════════════════════════

def ai_analyse(markets: list[dict], skip_ids: set = None) -> dict | None:
    skip_ids = skip_ids or set()
    # filter out already-bet markets
    markets = [m for m in markets if m.get("id") not in skip_ids]
    if not markets:
        return None
    summaries = []
    for m in markets:
        prices = parse_prices(m)
        price_str = ", ".join(f"{o}: {p:.0%}" for o, p in prices.items())
        vol = m.get("volume24hr", 0)
        summaries.append(f"• [id:{m.get('id','?')}] {m['question']} | {price_str} | Vol: ${vol}")

    import random
    mode = random.choices(["lottery", "value"], weights=[60, 40])[0]

    if mode == "lottery":
        strategy_desc = "LOTTERY MODE: Find ONE market where a LOW-probability outcome (<8%) is secretly more likely than crowd thinks. Look for black swans, surprise events, underdogs. Pick the CHEAP side."
        json_fields = """{
  "mode": "lottery",
  "market_id": "<id>",
  "question": "<exact question>",
  "pick": "<YES or NO - the cheap side>",
  "market_price": <price as decimal e.g. 0.03>,
  "potential_multiplier": <round(1/price)>,
  "reason": "<why this underdog has real chance>",
  "bet_usd": 2
}"""
    else:
        strategy_desc = "VALUE MODE: Find ONE market where the crowd probability is clearly WRONG based on recent news or logic. Pick whichever side (YES or NO) seems mispriced by at least 10-20%."
        json_fields = """{
  "mode": "value",
  "market_id": "<id>",
  "question": "<exact question>",
  "pick": "<YES or NO>",
  "market_price": <price as decimal>,
  "true_probability": <your estimate>,
  "potential_multiplier": <round(true_prob/market_price, 1)>,
  "reason": "<why crowd is wrong>",
  "bet_usd": 5
}"""

    prompt = f"""You are an elite prediction-market trader. Here are active Polymarket markets:

{chr(10).join(summaries)}

{strategy_desc}

Reply ONLY with valid JSON, no markdown:
{json_fields}"""

    try:
        text = call_deepseek(prompt)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        # fallback bet_pct if not present
        if "bet_pct" not in result:
            result["bet_pct"] = 2
        return result
    except Exception as e:
        log.error("DeepSeek error: %s", e)
        return None


# ══════════════════════════════════════════════
# 📊  RESOLVE BETS
# ══════════════════════════════════════════════

def resolve_bets(memory: dict) -> list[str]:
    msgs = []
    for bet in memory["bets"]:
        if bet["status"] != "open":
            continue
        market = fetch_market(bet["market_id"])
        if not market or not market.get("resolved"):
            continue
        winner = market.get("resolvedOutcome", "")
        if not winner:
            continue

        bet["status"] = "closed"
        bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
        bet["resolved_outcome"] = winner
        wager = bet["wager"]
        price = bet["price_at_bet"]

        if winner.upper() == bet["pick"].upper():
            payout = wager / price
            profit = payout - wager
            memory["balance"] += payout
            memory["total_profit"] += profit
            memory["stats"]["wins"] += 1
            bet["pnl"] = round(profit, 2)
            msgs.append(
                f"✅ *ВИГРАШ!*\n{bet['question']}\n"
                f"Вибрав {bet['pick']} → {winner}\n"
                f"Ставка: ${wager:.2f} | Прибуток: +${profit:.2f}\n"
                f"💼 Баланс: ${memory['balance']:.2f}"
            )
        else:
            memory["total_profit"] -= wager
            memory["stats"]["losses"] += 1
            bet["pnl"] = round(-wager, 2)
            msgs.append(
                f"❌ *ПРОГРАШ*\n{bet['question']}\n"
                f"Вибрав {bet['pick']} → {winner}\n"
                f"Втрачено: -${wager:.2f}\n"
                f"💼 Баланс: ${memory['balance']:.2f}"
            )

    save_memory(memory)
    return msgs


# ══════════════════════════════════════════════
# 📬  TELEGRAM HANDLERS
# ══════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Polymarket Simulation Bot*\n\n"
        "Аналізую ринки через DeepSeek AI і роблю *віртуальні* ставки.\n\n"
        "Команди:\n"
        "/analyse — аналіз і нова ставка\n"
        "/bets — відкриті ставки\n"
        "/resolve — перевірити результати\n"
        "/stats — баланс і статистика\n"
        "/autostart — щоденний авторежим о 09:00 UTC",
        parse_mode="Markdown",
    )

async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Завантажую ринки Polymarket…")
    markets = fetch_markets()
    if not markets:
        await msg.edit_text("❌ Не вдалося отримати ринки.")
        return

    await msg.edit_text(f"🧠 DeepSeek аналізує {len(markets)} ринків…")
    already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
    analysis = ai_analyse(markets, skip_ids=already_bet)
    if not analysis:
        await msg.edit_text("❌ Всі ринки вже в ставках або DeepSeek не відповів.")
        return

    matched = next((m for m in markets if m.get("id") == analysis.get("market_id")), None)
    if not matched:
        matched = next((m for m in markets if m["question"].strip() == analysis.get("question", "").strip()), markets[0])

    pick    = analysis.get("pick", "YES")
    prices  = parse_prices(matched)
    price   = prices.get(pick, prices.get("Yes", 0.5))
    bet_pct = min(analysis.get("bet_pct", 3), 8)
    wager  = round(memory["balance"] * bet_pct / 100, 2)
    if wager < 0.01:
        await msg.edit_text("❌ Баланс занадто малий.")
        return

    memory["balance"] -= wager
    memory["stats"]["total_wagered"] += wager
    memory["bets"].append({
        "id": f"bet_{len(memory['bets'])+1}",
        "market_id": matched.get("id", "unknown"),
        "question": matched["question"],
        "pick": pick,
        "confidence": analysis.get("confidence", 70),
        "price_at_bet": price,
        "wager": wager,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": analysis.get("reason", ""),
        "pnl": None,
    })
    save_memory(memory)

    await msg.edit_text(
        f"🎯 *Ставка зроблена!*\n\n"
        f"📋 {matched['question']}\n"
        f"📌 Вибір: *{pick}* ({analysis.get('confidence')}% впевненості)\n"
        f"💰 Ставка: ${wager:.2f} ({bet_pct}% балансу)\n"
        f"📊 Ціна ринку: {price:.0%}\n"
        f"💼 Баланс: ${memory['balance']:.2f}\n\n"
        f"🧠 *Причина:* {analysis.get('reason', '—')}",
        parse_mode="Markdown",
    )

async def cmd_bets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    open_bets = [b for b in memory["bets"] if b["status"] == "open"]
    if not open_bets:
        await update.message.reply_text("📭 Немає відкритих ставок. Спробуй /analyse")
        return
    lines = [f"📂 *Відкриті ставки ({len(open_bets)}):*\n"]
    for b in open_bets[-10:]:
        lines.append(
            f"• {b['question'][:55]}…\n"
            f"  → *{b['pick']}* | ${b['wager']:.2f} | {b['created_at'][:10]}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Перевіряю результати…")
    memory = load_memory()
    results = resolve_bets(memory)
    if not results:
        await update.message.reply_text("ℹ️ Немає нових результатів — ринки ще не закрились.")
    else:
        for r in results:
            await update.message.reply_text(r, parse_mode="Markdown")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    s = memory["stats"]
    total = s["wins"] + s["losses"]
    wr  = (s["wins"] / total * 100) if total else 0
    roi = (memory["total_profit"] / s["total_wagered"] * 100) if s["total_wagered"] else 0
    open_count = sum(1 for b in memory["bets"] if b["status"] == "open")
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"💼 Баланс:        ${memory['balance']:.2f}\n"
        f"📈 Прибуток:      ${memory['total_profit']:+.2f}\n"
        f"🎯 ROI:           {roi:+.1f}%\n\n"
        f"✅ Виграші:       {s['wins']}\n"
        f"❌ Поразки:       {s['losses']}\n"
        f"🏆 Win rate:      {wr:.1f}%\n"
        f"💸 Всього ставок: ${s['total_wagered']:.2f}\n"
        f"📂 Відкрито:      {open_count}",
        parse_mode="Markdown",
    )

async def _daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    memory = load_memory()
    for r in resolve_bets(memory):
        await ctx.bot.send_message(chat_id=chat_id, text=r, parse_mode="Markdown")

    markets = fetch_markets()
    if not markets:
        return
    analysis = ai_analyse(markets)
    if not analysis:
        return

    matched = next((m for m in markets if m.get("id") == analysis.get("market_id")), markets[0])
    pick    = analysis.get("pick", "YES")
    prices  = parse_prices(matched)
    price   = prices.get(pick, 0.5)
    bet_pct = min(analysis.get("bet_pct", 3), 8)

    memory = load_memory()
    wager  = round(memory["balance"] * bet_pct / 100, 2)
    if wager < 0.01:
        return

    memory["balance"] -= wager
    memory["stats"]["total_wagered"] += wager
    memory["bets"].append({
        "id": f"bet_{len(memory['bets'])+1}",
        "market_id": matched.get("id", "unknown"),
        "question": matched["question"],
        "pick": pick,
        "confidence": analysis.get("confidence", 70),
        "price_at_bet": price,
        "wager": wager,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": analysis.get("reason", ""),
        "pnl": None,
    })
    save_memory(memory)

    mode = analysis.get("mode", "lottery")
    emoji = "🎰" if mode == "lottery" else "🎯"
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{emoji} *Авто-ставка [{mode.upper()}]*\n\n"
            f"📋 {matched['question']}\n"
            f"📌 *{pick}* @ {price:.1%} | x{multiplier}\n"
            f"💰 ${wager:.2f} → якщо виграє ~${wager*multiplier:.0f}\n"
            f"💼 Баланс: ${memory['balance']:.2f}\n\n"
            f"🧠 {analysis.get('reason', '—')}"
        ),
        parse_mode="Markdown",
    )

async def cmd_autostart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # remove old jobs
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    # run every 1 hour
    ctx.job_queue.run_repeating(
        _daily_job,
        interval=3600,
        first=60,
        chat_id=chat_id,
        name=str(chat_id),
    )
    await update.message.reply_text(
        "⏰ *Авторежим увімкнено!*\n\n"
        "Кожну *годину* бот буде:\n"
        "1. Перевіряти результати ставок\n"
        "2. Шукати лотерейні ринки (<8% ціна)\n"
        "3. Ставити $1-5 на найкращий варіант\n"
        "4. Звітувати тут\n\n"
        "Стратегія: багато дрібних ставок, чекаємо x100-x1000 🎰\n"
        "Перший запуск через 1 хвилину!",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════

def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("analyse",   cmd_analyse))
    app.add_handler(CommandHandler("bets",      cmd_bets))
    app.add_handler(CommandHandler("resolve",   cmd_resolve))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("autostart", cmd_autostart))
    log.info("🚀 Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()


