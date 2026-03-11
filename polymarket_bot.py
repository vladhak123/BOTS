"""
Polymarket Simulation Bot v2.0 — МАКСИМАЛЬНІ ПОКРАЩЕННЯ
- DeepSeek AI з контекстом попередніх ставок (вчиться на помилках)
- Пошук новин перед аналізом
- Змішана стратегія: 60% лотерея + 40% value
- Kelly Criterion для розміру ставок
- Фільтр застарілих ринків (4 рівні)
- /history — графік балансу
- /top — найкращі та найгірші ставки
- /autostop — зупинити авторежим
- Кожні 3 хвилини автоматично
"""

import json, os, re, random, logging
from datetime import datetime, timezone, date
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─────────────────────────────────────────────
# 🔑  ENV VARS
# ─────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "8647895785:AAESQ2oSwnTNCXW9y9RjgsWvMZjyS_mX3iA")
DS_KEY = os.environ.get("DS_KEY", "sk-7f2b9cc52ff3405baab9824544b129b9"),
MEMORY_FILE      = os.environ.get("MEMORY_FILE", "bot_memory.json")
STARTING_BALANCE = 1000.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 🤖  DEEPSEEK
# ══════════════════════════════════════════════

def call_deepseek(prompt: str, max_tokens: int = 600) -> str:
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {os.environ.get('DS_KEY', '')}",
                 "Content-Type": "application/json"},
        json={"model": "deepseek-chat",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": 0.3},
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
        "peak_balance": STARTING_BALANCE,
        "total_profit": 0.0,
        "bets": [],
        "balance_history": [{"ts": datetime.now(timezone.utc).isoformat(), "balance": STARTING_BALANCE}],
        "stats": {"wins": 0, "losses": 0, "cancelled": 0, "total_wagered": 0.0,
                  "lottery_wins": 0, "lottery_losses": 0, "value_wins": 0, "value_losses": 0},
    }

def save_memory(data: dict):
    # update peak balance
    if data["balance"] > data.get("peak_balance", STARTING_BALANCE):
        data["peak_balance"] = data["balance"]
    # track balance history (max 200 points)
    history = data.get("balance_history", [])
    history.append({"ts": datetime.now(timezone.utc).isoformat(), "balance": round(data["balance"], 2)})
    data["balance_history"] = history[-200:]
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════
# 📡  POLYMARKET API
# ══════════════════════════════════════════════

MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

def is_question_expired(question: str) -> bool:
    q = question.lower()
    pattern = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:[,\s]+(\d{4}))?'
    for mon, day, year in re.findall(pattern, q):
        try:
            month_num = MONTHS.get(mon[:3], 0)
            if not month_num:
                continue
            yr = int(year) if year else datetime.now(timezone.utc).year
            candidate = date(yr, month_num, int(day))
            if candidate < datetime.now(timezone.utc).date():
                return True
        except Exception:
            pass
    return False

def parse_end_date(m: dict):
    for field in ["endDate", "end_date", "expirationTime", "endDateIso", "gameStartTime"]:
        val = m.get(field)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except Exception:
                pass
    return None

def parse_prices(market: dict) -> dict:
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        prices   = json.loads(market.get("outcomePrices", "[]"))
        return {o: float(p) for o, p in zip(outcomes, prices)}
    except Exception:
        return {}

def fetch_markets(limit: int = 100) -> list[dict]:
    now = datetime.now(timezone.utc)
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
            timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        valid = []

        for m in markets:
            question = m.get("question", "")
            if not question or not m.get("outcomePrices"):
                continue
            if is_question_expired(question):
                continue
            if m.get("resolved") or m.get("closed"):
                continue
            end_dt = parse_end_date(m)
            if end_dt:
                hours_left = (end_dt - now).total_seconds() / 3600
                if hours_left < 0 or hours_left > 24:
                    continue
            valid.append(m)

        if not valid:
            log.warning("No valid markets after filtering!")
            return []

        # prefer lottery markets
        lottery = []
        for m in valid:
            prices = parse_prices(m)
            min_price = min(prices.values()) if prices else 1.0
            if min_price <= 0.08:
                m["_min_price"] = min_price
                lottery.append(m)

        lottery.sort(key=lambda x: x.get("_min_price", 1))
        top = lottery[:30]
        random.shuffle(top)
        result = top[:20] if top else valid[:20]
        random.shuffle(result)
        log.info("Fetched %d valid markets (%d lottery)", len(result), len(lottery))
        return result
    except Exception as e:
        log.error("Polymarket fetch error: %s", e)
        return []

def fetch_market(market_id: str) -> dict | None:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Market fetch error: %s", e)
        return None


# ══════════════════════════════════════════════
# 🔍  NEWS SEARCH (DuckDuckGo instant)
# ══════════════════════════════════════════════

def fetch_news(query: str) -> str:
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=8,
        )
        data = r.json()
        snippets = []
        if data.get("AbstractText"):
            snippets.append(data["AbstractText"][:300])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                snippets.append(topic["Text"][:150])
        return " | ".join(snippets) if snippets else "No recent news found."
    except Exception:
        return "News unavailable."


# ══════════════════════════════════════════════
# 🤖  AI ANALYSIS with memory context + news
# ══════════════════════════════════════════════

def build_context(memory: dict) -> str:
    """Build context from last 5 closed bets so AI learns from mistakes."""
    closed = [b for b in memory["bets"] if b["status"] in ("closed", "cancelled")][-5:]
    if not closed:
        return "No previous bets to learn from yet."
    lines = ["Previous bets (learn from these):"]
    for b in closed:
        result = "WIN ✅" if b.get("pnl", 0) > 0 else ("CANCELLED ↩️" if b["status"] == "cancelled" else "LOSS ❌")
        lines.append(f"- {b['question'][:60]} | Picked {b['pick']} | {result} | PnL: ${b.get('pnl', 0):.2f}")
    return "\n".join(lines)

def kelly_bet(edge: float, price: float, balance: float, max_pct: float = 0.05) -> float:
    """Kelly Criterion: f = edge / odds"""
    try:
        odds = (1 / price) - 1
        if odds <= 0:
            return 2.0
        kelly_f = edge / odds
        kelly_f = max(0.01, min(kelly_f, max_pct))
        return round(balance * kelly_f, 2)
    except Exception:
        return 2.0

def ai_analyse(markets: list[dict], memory: dict, skip_ids: set = None) -> dict | None:
    skip_ids = skip_ids or set()
    markets = [m for m in markets if m.get("id") not in skip_ids]
    if not markets:
        return None

    summaries = []
    for m in markets:
        prices = parse_prices(m)
        price_str = ", ".join(f"{o}: {p:.1%}" for o, p in prices.items())
        vol = m.get("volume24hr", 0)
        end_dt = parse_end_date(m)
        hours = f"{(end_dt - datetime.now(timezone.utc)).total_seconds()/3600:.0f}h" if end_dt else "?"
        summaries.append(f"• [id:{m.get('id','?')}] {m['question']} | {price_str} | Vol:${vol} | Closes:{hours}")

    mode = random.choices(["lottery", "value"], weights=[60, 40])[0]
    context = build_context(memory)

    # fetch news for top 3 markets
    news_parts = []
    for m in markets[:3]:
        q = m["question"][:50]
        news = fetch_news(q)
        if "No recent" not in news and "unavailable" not in news:
            news_parts.append(f"News for '{q}': {news[:200]}")
    news_str = "\n".join(news_parts) if news_parts else "No relevant news found."

    if mode == "lottery":
        strategy = "LOTTERY: Find ONE market where a LOW-probability outcome (<8%) is secretly more likely than the crowd thinks. Pick the CHEAP side for maximum multiplier."
        json_template = """{
  "mode": "lottery",
  "market_id": "<id>",
  "question": "<exact question>",
  "pick": "<YES or NO - the cheap side>",
  "market_price": <decimal e.g. 0.03>,
  "true_probability": <your estimate 0.0-1.0>,
  "potential_multiplier": <round(1/market_price)>,
  "reason": "<2-3 sentences>",
  "bet_usd": <1-3>
}"""
    else:
        strategy = "VALUE: Find ONE market where crowd probability is WRONG by 10%+. Pick either side if clearly mispriced."
        json_template = """{
  "mode": "value",
  "market_id": "<id>",
  "question": "<exact question>",
  "pick": "<YES or NO>",
  "market_price": <decimal>,
  "true_probability": <your estimate>,
  "potential_multiplier": <round(true_prob/market_price, 1)>,
  "reason": "<2-3 sentences>",
  "bet_usd": <3-5>
}"""

    prompt = f"""You are an elite prediction-market trader with a track record of finding mispriced markets.

=== RECENT PERFORMANCE (learn from this) ===
{context}

=== CURRENT NEWS ===
{news_str}

=== AVAILABLE MARKETS ===
{chr(10).join(summaries)}

=== YOUR TASK ===
{strategy}

IMPORTANT RULES:
- NEVER pick markets about past dates
- Only pick markets you have genuine insight on
- Be honest about uncertainty

Reply ONLY with valid JSON, no markdown:
{json_template}"""

    try:
        text = call_deepseek(prompt, max_tokens=500)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
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
        if not market:
            continue

        is_resolved = (market.get("resolved") or market.get("closed") or
                      market.get("resolutionTime") or market.get("resolvedAt"))
        if not is_resolved:
            continue

        winner = (market.get("resolvedOutcome") or market.get("resolution") or
                 market.get("winner") or "")

        if not winner and is_resolved:
            bet["status"] = "cancelled"
            bet["pnl"] = 0
            memory["balance"] += bet["wager"]
            memory["stats"]["cancelled"] = memory["stats"].get("cancelled", 0) + 1
            msgs.append(f"↩️ *Повернуто*\n{bet['question']}\nРинок закрився без результату → +${bet['wager']:.2f}")
            continue

        if not winner:
            continue

        bet["status"] = "closed"
        bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
        bet["resolved_outcome"] = winner
        wager = bet["wager"]
        price = bet["price_at_bet"]
        mode = bet.get("mode", "lottery")

        if winner.upper() == bet["pick"].upper():
            payout = wager / price if price > 0 else wager
            profit = payout - wager
            memory["balance"] += payout
            memory["total_profit"] += profit
            memory["stats"]["wins"] += 1
            if mode == "lottery":
                memory["stats"]["lottery_wins"] = memory["stats"].get("lottery_wins", 0) + 1
            else:
                memory["stats"]["value_wins"] = memory["stats"].get("value_wins", 0) + 1
            bet["pnl"] = round(profit, 2)
            msgs.append(
                f"✅ *ВИГРАШ!* {'🎰' if mode=='lottery' else '🎯'}\n"
                f"{bet['question']}\n"
                f"Вибрав {bet['pick']} → {winner}\n"
                f"Ставка: ${wager:.2f} | Прибуток: *+${profit:.2f}*\n"
                f"💼 Баланс: ${memory['balance']:.2f}"
            )
        else:
            memory["total_profit"] -= wager
            memory["stats"]["losses"] += 1
            if mode == "lottery":
                memory["stats"]["lottery_losses"] = memory["stats"].get("lottery_losses", 0) + 1
            else:
                memory["stats"]["value_losses"] = memory["stats"].get("value_losses", 0) + 1
            bet["pnl"] = round(-wager, 2)
            msgs.append(
                f"❌ *ПРОГРАШ* {'🎰' if mode=='lottery' else '🎯'}\n"
                f"{bet['question']}\n"
                f"Вибрав {bet['pick']} → {winner}\n"
                f"Втрачено: -${wager:.2f} | Баланс: ${memory['balance']:.2f}"
            )

    save_memory(memory)
    return msgs


# ══════════════════════════════════════════════
# 📬  TELEGRAM HANDLERS
# ══════════════════════════════════════════════

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Зробити ставку", callback_data="analyse"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📂 Ставки", callback_data="bets"),
         InlineKeyboardButton("🔍 Результати", callback_data="resolve")],
        [InlineKeyboardButton("🏆 Топ ставок", callback_data="top"),
         InlineKeyboardButton("📈 Графік", callback_data="history")],
        [InlineKeyboardButton("⏰ Авторежим ON", callback_data="autostart"),
         InlineKeyboardButton("⏹ Авторежим OFF", callback_data="autostop")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Polymarket Bot v2.0*\n\n"
        "DeepSeek AI + новини + пам'ять помилок\n"
        "Стратегія: 🎰 60% лотерея + 🎯 40% value\n"
        "Частота: кожні 3 хвилини",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fake_update = type('obj', (object,), {'message': query.message, 'effective_chat': query.message.chat})()
    if query.data == "analyse":
        await _do_analyse(query.message)
    elif query.data == "stats":
        await _do_stats(query.message)
    elif query.data == "bets":
        await _do_bets(query.message)
    elif query.data == "resolve":
        await _do_resolve(query.message)
    elif query.data == "top":
        await _do_top(query.message)
    elif query.data == "history":
        await _do_history(query.message)
    elif query.data == "autostart":
        await _do_autostart(query.message, ctx)
    elif query.data == "autostop":
        await _do_autostop(query.message, ctx)

async def _do_analyse(message):
    msg = await message.reply_text("⏳ Сканую ринки + новини…")
    memory = load_memory()
    markets = fetch_markets()
    if not markets:
        await msg.edit_text("❌ Немає актуальних ринків (всі або минули або закриються >24h).")
        return

    await msg.edit_text(f"🧠 DeepSeek аналізує {len(markets)} ринків з новинами…")
    already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
    analysis = ai_analyse(markets, memory, skip_ids=already_bet)
    if not analysis:
        await msg.edit_text("❌ DeepSeek не знайшов хорошої можливості.")
        return

    matched = next((m for m in markets if m.get("id") == analysis.get("market_id")), None)
    if not matched:
        matched = next((m for m in markets if m["question"].strip() == analysis.get("question","").strip()), markets[0])

    pick       = analysis.get("pick", "YES")
    mode       = analysis.get("mode", "lottery")
    prices     = parse_prices(matched)
    price      = prices.get(pick, prices.get("Yes", analysis.get("market_price", 0.5)))
    true_prob  = analysis.get("true_probability", price)
    multiplier = analysis.get("potential_multiplier", round(1/price) if price > 0 else 1)

    # Kelly sizing
    edge = true_prob - price
    wager = kelly_bet(abs(edge), price, memory["balance"])
    wager = max(1.0, min(wager, 5.0))

    if memory["balance"] < wager:
        wager = round(memory["balance"] * 0.02, 2)
    if wager < 0.5:
        await msg.edit_text("❌ Баланс занадто малий.")
        return

    memory["balance"] -= wager
    memory["stats"]["total_wagered"] += wager
    memory["bets"].append({
        "id": f"bet_{len(memory['bets'])+1}",
        "market_id": matched.get("id", "unknown"),
        "question": matched["question"],
        "pick": pick,
        "mode": mode,
        "price_at_bet": price,
        "true_probability": true_prob,
        "potential_multiplier": multiplier,
        "wager": wager,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": analysis.get("reason", ""),
        "pnl": None,
    })
    save_memory(memory)

    emoji = "🎰" if mode == "lottery" else "🎯"
    await msg.edit_text(
        f"{emoji} *Ставка зроблена! [{mode.upper()}]*\n\n"
        f"📋 {matched['question']}\n"
        f"📌 Вибір: *{pick}* @ {price:.1%}\n"
        f"💥 Множник: *x{multiplier}*\n"
        f"💰 Ставка: ${wager:.2f} (Kelly)\n"
        f"🏆 Якщо виграє: ~${wager*multiplier:.0f}\n"
        f"💼 Баланс: ${memory['balance']:.2f}\n\n"
        f"🧠 {analysis.get('reason','—')}",
        parse_mode="Markdown",
    )

async def _do_stats(message):
    memory = load_memory()
    s = memory["stats"]
    total = s["wins"] + s["losses"]
    wr  = (s["wins"] / total * 100) if total else 0
    roi = (memory["total_profit"] / s["total_wagered"] * 100) if s["total_wagered"] else 0
    open_count = sum(1 for b in memory["bets"] if b["status"] == "open")
    drawdown = memory["balance"] - memory.get("peak_balance", STARTING_BALANCE)
    lw = s.get("lottery_wins", 0)
    ll = s.get("lottery_losses", 0)
    vw = s.get("value_wins", 0)
    vl = s.get("value_losses", 0)

    await message.reply_text(
        f"📊 *Статистика v2.0*\n\n"
        f"💼 Баланс:      ${memory['balance']:.2f}\n"
        f"📈 Прибуток:    ${memory['total_profit']:+.2f}\n"
        f"🎯 ROI:         {roi:+.1f}%\n"
        f"📉 Drawdown:    ${drawdown:.2f}\n\n"
        f"✅ Виграші:     {s['wins']} | ❌ Поразки: {s['losses']}\n"
        f"🏆 Win rate:    {wr:.1f}%\n"
        f"↩️ Повернуто:  {s.get('cancelled',0)}\n\n"
        f"🎰 Лотерея:    {lw}W / {ll}L\n"
        f"🎯 Value:       {vw}W / {vl}L\n\n"
        f"💸 Всього ставок: ${s['total_wagered']:.2f}\n"
        f"📂 Відкрито:    {open_count}",
        parse_mode="Markdown",
    )

async def _do_bets(message):
    memory = load_memory()
    open_bets = [b for b in memory["bets"] if b["status"] == "open"]
    if not open_bets:
        await message.reply_text("📭 Немає відкритих ставок.")
        return
    lines = [f"📂 *Відкриті ставки ({len(open_bets)}):*\n"]
    for b in open_bets[-10:]:
        emoji = "🎰" if b.get("mode") == "lottery" else "🎯"
        lines.append(
            f"{emoji} {b['question'][:50]}…\n"
            f"  → *{b['pick']}* | ${b['wager']:.2f} | x{b.get('potential_multiplier','?')} | {b['created_at'][:10]}"
        )
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_resolve(message):
    await message.reply_text("🔍 Перевіряю результати…")
    memory = load_memory()
    results = resolve_bets(memory)
    if not results:
        await message.reply_text("ℹ️ Ринки ще не закрились.")
    else:
        for r in results:
            await message.reply_text(r, parse_mode="Markdown")

async def _do_top(message):
    memory = load_memory()
    closed = [b for b in memory["bets"] if b.get("pnl") is not None]
    if not closed:
        await message.reply_text("📭 Ще немає закритих ставок.")
        return
    by_pnl = sorted(closed, key=lambda x: x.get("pnl", 0), reverse=True)
    top3    = by_pnl[:3]
    worst3  = by_pnl[-3:]

    lines = ["🏆 *Топ 3 найкращих ставок:*\n"]
    for b in top3:
        emoji = "🎰" if b.get("mode") == "lottery" else "🎯"
        lines.append(f"{emoji} {b['question'][:45]}…\n   PnL: *+${b['pnl']:.2f}* | {b['pick']}")

    lines.append("\n💀 *Топ 3 найгірших:*\n")
    for b in worst3:
        lines.append(f"❌ {b['question'][:45]}…\n   PnL: *${b['pnl']:.2f}* | {b['pick']}")

    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_history(message):
    memory = load_memory()
    history = memory.get("balance_history", [])
    if len(history) < 2:
        await message.reply_text("📈 Ще замало даних для графіку. Зачекай трохи!")
        return

    # ASCII chart
    balances = [h["balance"] for h in history[-20:]]
    min_b = min(balances)
    max_b = max(balances)
    height = 8
    width  = len(balances)

    chart_lines = []
    for row in range(height, 0, -1):
        threshold = min_b + (max_b - min_b) * row / height
        line = ""
        for b in balances:
            line += "█" if b >= threshold else "░"
        label = f"${threshold:.0f} |" if row in (height, height//2, 1) else "       |"
        chart_lines.append(f"`{label}{line}`")

    start = history[0]["balance"]
    end   = history[-1]["balance"]
    diff  = end - start
    sign  = "+" if diff >= 0 else ""

    await message.reply_text(
        f"📈 *Графік балансу* (останні {len(balances)} точок)\n\n"
        + "\n".join(chart_lines) +
        f"\n\n💼 Початок: ${start:.2f} → Зараз: ${end:.2f} ({sign}{diff:.2f})",
        parse_mode="Markdown",
    )

async def _do_autostart(message, ctx):
    chat_id = message.chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    ctx.job_queue.run_repeating(
        _auto_job, interval=180, first=30,
        chat_id=chat_id, name=str(chat_id),
    )
    await message.reply_text(
        "⏰ *Авторежим увімкнено!*\n\n"
        "Кожні *3 хвилини:*\n"
        "• Сканую ринки + новини\n"
        "• Вчуся на попередніх ставках\n"
        "• Ставлю якщо є сигнал\n"
        "• Перевіряю результати\n\n"
        "Перший запуск через 30 сек 🚀",
        parse_mode="Markdown",
    )

async def _do_autostop(message, ctx):
    chat_id = message.chat.id
    jobs = ctx.job_queue.get_jobs_by_name(str(chat_id))
    for job in jobs:
        job.schedule_removal()
    await message.reply_text("⏹ *Авторежим зупинено.*", parse_mode="Markdown")

async def _auto_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    memory  = load_memory()

    for r in resolve_bets(memory):
        await ctx.bot.send_message(chat_id=chat_id, text=r, parse_mode="Markdown")

    markets = fetch_markets()
    if not markets:
        return

    already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
    analysis = ai_analyse(markets, memory, skip_ids=already_bet)
    if not analysis:
        return

    mode         = analysis.get("mode", "lottery")
    true_prob    = analysis.get("true_probability", 0)
    market_price = analysis.get("market_price", 0.5)

    # quality filter
    if mode == "value" and (true_prob - market_price) < 0.10:
        return
    if mode == "lottery" and market_price > 0.08:
        return

    matched = next((m for m in markets if m.get("id") == analysis.get("market_id")), markets[0])
    pick       = analysis.get("pick", "YES")
    prices     = parse_prices(matched)
    price      = prices.get(pick, prices.get("Yes", market_price or 0.5))
    multiplier = analysis.get("potential_multiplier", round(1/price) if price > 0 else 1)

    edge  = abs(true_prob - price)
    memory = load_memory()
    wager = kelly_bet(edge, price, memory["balance"])
    wager = max(1.0, min(wager, 5.0))
    if memory["balance"] < wager:
        wager = round(memory["balance"] * 0.02, 2)
    if wager < 0.5:
        return

    memory["balance"] -= wager
    memory["stats"]["total_wagered"] += wager
    memory["bets"].append({
        "id": f"bet_{len(memory['bets'])+1}",
        "market_id": matched.get("id", "unknown"),
        "question": matched["question"],
        "pick": pick,
        "mode": mode,
        "price_at_bet": price,
        "true_probability": true_prob,
        "potential_multiplier": multiplier,
        "wager": wager,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": analysis.get("reason", ""),
        "pnl": None,
    })
    save_memory(memory)

    emoji = "🎰" if mode == "lottery" else "🎯"
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{emoji} *Авто-ставка [{mode.upper()}]*\n\n"
            f"📋 {matched['question']}\n"
            f"📌 *{pick}* @ {price:.1%} | x{multiplier}\n"
            f"💰 ${wager:.2f} → виграш ~${wager*multiplier:.0f}\n"
            f"💼 Баланс: ${memory['balance']:.2f}\n\n"
            f"🧠 {analysis.get('reason','—')}"
        ),
        parse_mode="Markdown",
    )

# Commands (text fallback)
async def cmd_analyse(u, c): await _do_analyse(u.message)
async def cmd_stats(u, c):   await _do_stats(u.message)
async def cmd_bets(u, c):    await _do_bets(u.message)
async def cmd_resolve(u, c): await _do_resolve(u.message)
async def cmd_top(u, c):     await _do_top(u.message)
async def cmd_history(u, c): await _do_history(u.message)
async def cmd_autostart(u, c): await _do_autostart(u.message, c)
async def cmd_autostop(u, c):  await _do_autostop(u.message, c)


# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════

def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("analyse",   cmd_analyse))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("bets",      cmd_bets))
    app.add_handler(CommandHandler("resolve",   cmd_resolve))
    app.add_handler(CommandHandler("top",       cmd_top))
    app.add_handler(CommandHandler("history",   cmd_history))
    app.add_handler(CommandHandler("autostart", cmd_autostart))
    app.add_handler(CommandHandler("autostop",  cmd_autostop))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🚀 Bot v2.0 started!")
    app.run_polling()

if __name__ == "__main__":
    main()
