"""
Polymarket Simulation Bot v3.0
- Claude Haiku для аналізу
- Батчевий аналіз 100 ринків
- Тільки ринки що закриються протягом 12 годин
- Змішана стратегія: 60% лотерея + 40% value
- Кожні 30 хвилин автоматично
- Пам'ять + новини + Kelly Criterion
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
CLAUDE_KEY       = os.environ.get("CLAUDE_KEY", "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")
MEMORY_FILE      = os.environ.get("MEMORY_FILE", "bot_memory.json")
STARTING_BALANCE = 100.0

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 🤖  CLAUDE HAIKU
# ══════════════════════════════════════════════

def call_claude(prompt: str, max_tokens: int = 600) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ.get("ANTHROPIC_KEY", ""),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


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
    if data["balance"] > data.get("peak_balance", STARTING_BALANCE):
        data["peak_balance"] = data["balance"]
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

def fetch_markets() -> list[dict]:
    now = datetime.now(timezone.utc)
    all_markets = []
    for offset in [0, 100]:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 100,
                        "order": "volume24hr", "ascending": "false", "offset": offset},
                timeout=10,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            all_markets.extend(batch)
        except Exception:
            pass

    seen = set()
    unique = []
    for m in all_markets:
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)

    valid = []
    topic_count = {}
    TOPICS = ["bitcoin", "btc", "ethereum", "eth", "trump", "fed", "war",
              "iran", "russia", "china", "nvidia", "apple", "taylor"]

    for m in unique:
        question = m.get("question", "")
        if not question or not m.get("outcomePrices"):
            continue
        if is_question_expired(question):
            continue
        if m.get("resolved") or m.get("closed"):
            continue
        end_dt = parse_end_date(m)
        if not end_dt:
            continue
        hours_left = (end_dt - now).total_seconds() / 3600
        if hours_left < 0 or hours_left > 12:
            continue

        # diversity filter: max 2 per topic
        q = question.lower()
        topic = next((kw for kw in TOPICS if kw in q), "other")
        if topic_count.get(topic, 0) >= 2:
            continue
        topic_count[topic] = topic_count.get(topic, 0) + 1
        valid.append(m)

    # prefer lottery markets (price < 8%)
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

def fetch_market(market_id: str) -> dict | None:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Market fetch error: %s", e)
        return None


# ══════════════════════════════════════════════
# 🔍  NEWS
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
        for topic in data.get("RelatedTopics", [])[:2]:
            if isinstance(topic, dict) and topic.get("Text"):
                snippets.append(topic["Text"][:150])
        return " | ".join(snippets) if snippets else ""
    except Exception:
        return ""


# ══════════════════════════════════════════════
# 🧠  AI ANALYSIS
# ══════════════════════════════════════════════

def build_context(memory: dict) -> str:
    closed = [b for b in memory["bets"] if b["status"] in ("closed", "cancelled")][-5:]
    if not closed:
        return "Нет предыдущих ставок."
    lines = ["Предыдущие ставки (учись на них):"]
    for b in closed:
        result = "ВЫИГРЫШ ✅" if (b.get("pnl") or 0) > 0 else "ПРОИГРЫШ ❌"
        lines.append(f"- {b['question'][:60]} | {b['pick']} | {result} | ${b.get('pnl', 0):.2f}")
    return "\n".join(lines)

def kelly_bet(edge: float, price: float, balance: float) -> float:
    try:
        odds = (1 / price) - 1
        if odds <= 0:
            return 2.0
        kelly_f = min(abs(edge) / odds, 0.05)
        return max(1.0, min(round(balance * kelly_f, 2), 5.0))
    except Exception:
        return 2.0

def ai_analyse_batch(batch: list, mode: str, context: str, news_str: str) -> dict | None:
    summaries = []
    now = datetime.now(timezone.utc)
    for m in batch:
        prices = parse_prices(m)
        price_str = ", ".join(f"{o}: {p:.1%}" for o, p in prices.items())
        end_dt = parse_end_date(m)
        hours = f"{(end_dt - now).total_seconds()/3600:.0f}h" if end_dt else "?"
        summaries.append(f"[id:{m.get('id','?')}] {m['question']} | {price_str} | Closes:{hours}")

    if mode == "lottery":
        strategy = "ЛОТЕРЕЯ: Найди рынок где низковероятный исход (<8%) реально более вероятен чем думает толпа."
        bet_field = '"bet_usd": <1-3>,'
    else:
        strategy = "VALUE: Найди рынок где толпа ошибается на 10%+ в вероятности."
        bet_field = '"bet_usd": <3-5>,'

    prompt = f"""Ты эксперт по prediction markets.

ИСТОРИЯ СТАВОК:
{context}

НОВОСТИ:
{news_str}

РЫНКИ:
{chr(10).join(summaries)}

ЗАДАЧА: {strategy}

ПРАВИЛА:
- Никогда не выбирай рынки с прошедшими датами
- Пиши reason на РУССКОМ языке
- Переведи question на русский
- Если нет хорошей возможности — верни score: 0

Ответь ТОЛЬКО валидным JSON без markdown:
{{
  "mode": "{mode}",
  "market_id": "<id>",
  "question": "<перевод на русский>",
  "pick": "<YES или NO>",
  "market_price": <число>,
  "true_probability": <твоя оценка>,
  "potential_multiplier": <целое число>,
  "reason": "<2-3 предложения на русском>",
  {bet_field}
  "score": <0-100>
}}"""

    try:
        text = call_claude(prompt, max_tokens=400)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        result.setdefault("bet_pct", 2)
        return result
    except Exception as e:
        log.error("Claude batch error: %s", e)
        return None

def ai_analyse(markets: list, memory: dict, skip_ids: set = None) -> dict | None:
    skip_ids = skip_ids or set()
    markets = [m for m in markets if m.get("id") not in skip_ids]
    if not markets:
        return None

    mode    = random.choices(["lottery", "value"], weights=[60, 40])[0]
    context = build_context(memory)

    news_parts = []
    for m in markets[:3]:
        news = fetch_news(m["question"][:50])
        if news:
            news_parts.append(f"{m['question'][:40]}: {news[:150]}")
    news_str = "\n".join(news_parts) if news_parts else "Нет новостей."

    batches = [markets[i:i+25] for i in range(0, len(markets), 25)]
    log.info("Analysing %d markets in %d batches (mode=%s)", len(markets), len(batches), mode)

    candidates = []
    for i, batch in enumerate(batches):
        result = ai_analyse_batch(batch, mode, context, news_str)
        if result and result.get("score", 0) > 30 and result.get("market_id"):
            candidates.append(result)

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x.get("score", 0))
    log.info("Best score=%d: %s", best.get("score", 0), best.get("question", "")[:50])
    return best


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

        if not winner:
            bet["status"] = "cancelled"
            bet["pnl"] = 0
            memory["balance"] += bet["wager"]
            memory["stats"]["cancelled"] = memory["stats"].get("cancelled", 0) + 1
            msgs.append(f"↩️ *Возврат*\n{bet['question']}\nРынок закрылся без результата → +${bet['wager']:.2f}")
            continue

        bet["status"] = "closed"
        bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
        bet["resolved_outcome"] = winner
        wager = bet["wager"]
        price = bet["price_at_bet"]
        mode  = bet.get("mode", "lottery")

        if winner.upper() == bet["pick"].upper():
            payout = wager / price if price > 0 else wager
            profit = payout - wager
            memory["balance"] += payout
            memory["total_profit"] += profit
            memory["stats"]["wins"] += 1
            memory["stats"][f"{mode}_wins"] = memory["stats"].get(f"{mode}_wins", 0) + 1
            bet["pnl"] = round(profit, 2)
            msgs.append(
                f"✅ *ВЫИГРЫШ!* {'🎰' if mode=='lottery' else '🎯'}\n"
                f"{bet['question']}\n"
                f"Выбрал {bet['pick']} → {winner}\n"
                f"Ставка: ${wager:.2f} | Прибыль: *+${profit:.2f}*\n"
                f"💼 Баланс: ${memory['balance']:.2f}"
            )
        else:
            memory["total_profit"] -= wager
            memory["stats"]["losses"] += 1
            memory["stats"][f"{mode}_losses"] = memory["stats"].get(f"{mode}_losses", 0) + 1
            bet["pnl"] = round(-wager, 2)
            msgs.append(
                f"❌ *ПРОИГРЫШ* {'🎰' if mode=='lottery' else '🎯'}\n"
                f"{bet['question']}\n"
                f"Выбрал {bet['pick']} → {winner}\n"
                f"Потеряно: -${wager:.2f} | Баланс: ${memory['balance']:.2f}"
            )

    save_memory(memory)
    return msgs


# ══════════════════════════════════════════════
# 🎮  SHARED BET LOGIC
# ══════════════════════════════════════════════

async def place_bet(message, memory: dict, analysis: dict, markets: list):
    matched = next((m for m in markets if m.get("id") == analysis.get("market_id")), None)
    if not matched:
        matched = next((m for m in markets if m["question"].strip() == analysis.get("question","").strip()), None)
    if not matched:
        return False

    pick       = analysis.get("pick", "YES")
    mode       = analysis.get("mode", "lottery")
    prices     = parse_prices(matched)
    price      = prices.get(pick, prices.get("Yes", analysis.get("market_price", 0.5)))
    true_prob  = analysis.get("true_probability", price)
    multiplier = analysis.get("potential_multiplier", max(1, round(1/price)) if price > 0 else 1)

    edge  = abs(true_prob - price)
    wager = kelly_bet(edge, price, memory["balance"])
    wager = float(analysis.get("bet_usd", wager))
    wager = max(1.0, min(wager, 5.0))

    if memory["balance"] < wager:
        wager = round(memory["balance"] * 0.02, 2)
    if wager < 0.5:
        return False

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
    market_url = f"https://polymarket.com/market/{matched.get('id', '')}"
    await message.reply_text(
        f"{emoji} *Ставка! [{mode.upper()}]*\n\n"
        f"📋 {analysis.get('question', matched['question'])}\n"
        f"📌 Выбор: *{pick}* @ {price:.1%}\n"
        f"💥 Множитель: *x{multiplier}*\n"
        f"💰 Ставка: ${wager:.2f}\n"
        f"🏆 Если выиграет: ~${wager*multiplier:.0f}\n"
        f"💼 Баланс: ${memory['balance']:.2f}\n\n"
        f"🧠 {analysis.get('reason','—')}\n\n"
        f"🔗 [Открыть рынок]({market_url})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    return True


# ══════════════════════════════════════════════
# 📬  TELEGRAM HANDLERS
# ══════════════════════════════════════════════

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎰 Сделать ставку", callback_data="analyse"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📂 Ставки", callback_data="bets"),
         InlineKeyboardButton("🔍 Результаты", callback_data="resolve")],
        [InlineKeyboardButton("🏆 Топ ставок", callback_data="top"),
         InlineKeyboardButton("📈 График", callback_data="history")],
        [InlineKeyboardButton("⏰ Авторежим ON", callback_data="autostart"),
         InlineKeyboardButton("⏹ Авторежим OFF", callback_data="autostop")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Polymarket Bot v3.0*\n\n"
        "Claude Haiku AI + новости + память ошибок\n"
        "Стратегия: 🎰 60% лотерея + 🎯 40% value\n"
        "Частота: каждые 30 минут\n"
        "Рынки: только закрываются в течение 12ч",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = query.message
    if query.data == "analyse":   await _do_analyse(msg)
    elif query.data == "stats":   await _do_stats(msg)
    elif query.data == "bets":    await _do_bets(msg)
    elif query.data == "resolve": await _do_resolve(msg)
    elif query.data == "top":     await _do_top(msg)
    elif query.data == "history": await _do_history(msg)
    elif query.data == "autostart": await _do_autostart(msg, ctx)
    elif query.data == "autostop":  await _do_autostop(msg, ctx)

async def _do_analyse(message):
    msg = await message.reply_text("⏳ Сканирую рынки...")
    memory  = load_memory()
    markets = fetch_markets()
    if not markets:
        await msg.edit_text("❌ Нет актуальных рынков (все закрываются >12ч или уже прошли).")
        return
    await msg.edit_text(f"🧠 Claude анализирует {len(markets)} рынков...")
    already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
    analysis = ai_analyse(markets, memory, skip_ids=already_bet)
    if not analysis:
        await msg.edit_text("❌ Claude не нашёл хорошей возможности сейчас.")
        return
    await msg.delete()
    await place_bet(message, memory, analysis, markets)

async def _do_stats(message):
    memory = load_memory()
    s = memory["stats"]
    total = s["wins"] + s["losses"]
    wr  = (s["wins"] / total * 100) if total else 0
    roi = (memory["total_profit"] / s["total_wagered"] * 100) if s["total_wagered"] else 0
    open_count = sum(1 for b in memory["bets"] if b["status"] == "open")
    drawdown = memory["balance"] - memory.get("peak_balance", STARTING_BALANCE)

    await message.reply_text(
        f"📊 *Статистика*\n\n"
        f"💼 Баланс:      ${memory['balance']:.2f}\n"
        f"📈 Прибыль:     ${memory['total_profit']:+.2f}\n"
        f"🎯 ROI:         {roi:+.1f}%\n"
        f"📉 Drawdown:    ${drawdown:.2f}\n\n"
        f"✅ Выигрыши:    {s['wins']} | ❌ Проигрыши: {s['losses']}\n"
        f"🏆 Win rate:    {wr:.1f}%\n"
        f"↩️ Возвраты:   {s.get('cancelled', 0)}\n\n"
        f"🎰 Лотерея:    {s.get('lottery_wins',0)}W / {s.get('lottery_losses',0)}L\n"
        f"🎯 Value:       {s.get('value_wins',0)}W / {s.get('value_losses',0)}L\n\n"
        f"💸 Всего ставок: ${s['total_wagered']:.2f}\n"
        f"📂 Открыто:     {open_count}",
        parse_mode="Markdown",
    )

async def _do_bets(message):
    memory = load_memory()
    open_bets = [b for b in memory["bets"] if b["status"] == "open"]
    if not open_bets:
        await message.reply_text("📭 Нет открытых ставок.")
        return
    lines = [f"📂 *Открытые ставки ({len(open_bets)}):*\n"]
    for b in open_bets[-10:]:
        emoji = "🎰" if b.get("mode") == "lottery" else "🎯"
        lines.append(
            f"{emoji} {b['question'][:50]}…\n"
            f"  → *{b['pick']}* | ${b['wager']:.2f} | x{b.get('potential_multiplier','?')} | {b['created_at'][:10]}"
        )
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_resolve(message):
    await message.reply_text("🔍 Проверяю результаты...")
    memory  = load_memory()
    results = resolve_bets(memory)
    if not results:
        await message.reply_text("ℹ️ Рынки ещё не закрылись.")
    else:
        for r in results:
            await message.reply_text(r, parse_mode="Markdown")

async def _do_top(message):
    memory = load_memory()
    closed = [b for b in memory["bets"] if b.get("pnl") is not None]
    if not closed:
        await message.reply_text("📭 Ещё нет закрытых ставок.")
        return
    by_pnl = sorted(closed, key=lambda x: x.get("pnl", 0), reverse=True)
    lines  = ["🏆 *Топ 3 лучших:*\n"]
    for b in by_pnl[:3]:
        emoji = "🎰" if b.get("mode") == "lottery" else "🎯"
        lines.append(f"{emoji} {b['question'][:45]}…\n   *+${b['pnl']:.2f}* | {b['pick']}")
    lines.append("\n💀 *Топ 3 худших:*\n")
    for b in by_pnl[-3:]:
        lines.append(f"❌ {b['question'][:45]}…\n   *${b['pnl']:.2f}* | {b['pick']}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_history(message):
    memory  = load_memory()
    history = memory.get("balance_history", [])
    if len(history) < 2:
        await message.reply_text("📈 Мало данных. Подожди немного!")
        return
    balances = [h["balance"] for h in history[-20:]]
    min_b    = min(balances)
    max_b    = max(balances)
    height   = 8
    lines    = []
    for row in range(height, 0, -1):
        threshold = min_b + (max_b - min_b) * row / height
        bar   = "".join("█" if b >= threshold else "░" for b in balances)
        label = f"${threshold:.0f}|" if row in (height, height//2, 1) else "      |"
        lines.append(f"`{label}{bar}`")
    diff = balances[-1] - balances[0]
    sign = "+" if diff >= 0 else ""
    await message.reply_text(
        f"📈 *График баланса* (последние {len(balances)} точек)\n\n" +
        "\n".join(lines) +
        f"\n\n💼 Начало: ${balances[0]:.2f} → Сейчас: ${balances[-1]:.2f} ({sign}{diff:.2f})",
        parse_mode="Markdown",
    )

async def _do_autostart(message, ctx):
    chat_id = message.chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    ctx.job_queue.run_repeating(
        _auto_job, interval=1800, first=60,
        chat_id=chat_id, name=str(chat_id),
    )
    await message.reply_text(
        "⏰ *Авторежим включён!*\n\n"
        "Каждые *30 минут:*\n"
        "• Сканирую 100 рынков\n"
        "• Анализирую через Claude Haiku\n"
        "• Ставлю если есть сигнал\n"
        "• Проверяю результаты\n\n"
        "Первый запуск через 1 минуту 🚀",
        parse_mode="Markdown",
    )

async def _do_autostop(message, ctx):
    chat_id = message.chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await message.reply_text("⏹ *Авторежим остановлен.*", parse_mode="Markdown")

async def _auto_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    memory  = load_memory()

    for r in resolve_bets(memory):
        await ctx.bot.send_message(chat_id=chat_id, text=r, parse_mode="Markdown")

    markets = fetch_markets()
    if not markets:
        return

    already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
    analysis    = ai_analyse(markets, memory, skip_ids=already_bet)
    if not analysis:
        return

    mode         = analysis.get("mode", "lottery")
    true_prob    = analysis.get("true_probability", 0)
    market_price = analysis.get("market_price", 0.5)

    if mode == "value" and (true_prob - market_price) < 0.10:
        return
    if mode == "lottery" and market_price > 0.08:
        return

    class FakeMessage:
        def __init__(self, bot, chat_id):
            self.bot = bot
            self.chat_id = chat_id
        async def reply_text(self, text, **kwargs):
            await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    fake_msg = FakeMessage(ctx.bot, chat_id)
    memory   = load_memory()
    await place_bet(fake_msg, memory, analysis, markets)


# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════

async def cmd_analyse(u, c):   await _do_analyse(u.message)
async def cmd_stats(u, c):     await _do_stats(u.message)
async def cmd_bets(u, c):      await _do_bets(u.message)
async def cmd_resolve(u, c):   await _do_resolve(u.message)
async def cmd_top(u, c):       await _do_top(u.message)
async def cmd_history(u, c):   await _do_history(u.message)
async def cmd_autostart(u, c): await _do_autostart(u.message, c)
async def cmd_autostop(u, c):  await _do_autostop(u.message, c)

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
    log.info("🚀 Bot v3.0 started!")
    app.run_polling()

if __name__ == "__main__":
    main()
