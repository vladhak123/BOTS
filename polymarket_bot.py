"""
Polymarket Simulation Bot v4.0
- async httpx: завантаження 2000 ринків паралельно (в 10-20 разів швидше)
- NewsAPI: реальні новини перед кожною ставкою
- Claude Haiku з веб-пошуком
- Фільтр ринків 48 годин + фільтр дат в назві
- 60% лотерея + 40% value
- Кожні 30 хвилин автоматично
- Стартовий баланс $100 + Kelly Criterion
"""

import json, os, re, random, logging, asyncio
from datetime import datetime, timezone, date
from pathlib import Path

import requests
import httpx
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─────────────────────────────────────────────
# 🔑  ENV VARS
# ─────────────────────────────────────────────
TG_TOKEN         = os.environ.get("TG_TOKEN", "YOUR_TOKEN_HERE")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY", "YOUR_ANTHROPIC_KEY_HERE")
NEWS_KEY         = os.environ.get("NEWS_KEY", "")
MEMORY_FILE      = os.environ.get("MEMORY_FILE", "bot_memory.json")
STARTING_BALANCE = 100.0
POLYMARKET_API    = "https://gamma-api.polymarket.com/markets"
NEWS_CACHE: dict  = {}
NEWS_CACHE_TTL    = 1800   # 30 min
MARKETS_CACHE     = None
MARKETS_TS        = 0.0
MARKETS_CACHE_TTL = 600    # 10 min

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 🤖  CLAUDE HAIKU
# ══════════════════════════════════════════════

def call_claude(prompt: str, max_tokens: int = 800, use_search: bool = False) -> str:
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ.get("ANTHROPIC_KEY", ""),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


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
# 📡  POLYMARKET API (async - 2000 ринків)
# ══════════════════════════════════════════════

MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

def is_question_expired(question: str) -> bool:
    q = question.lower()
    for mon, day in re.findall(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ ]+([0-9]{1,2})', q):
        try:
            mn = MONTHS.get(mon[:3], 0)
            if mn:
                candidate = date(datetime.now(timezone.utc).year, mn, int(day))
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

async def fetch_markets_async() -> list[dict]:
    """Завантажує до 2000 ринків паралельно через httpx."""
    all_markets = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        tasks = [
            client.get(POLYMARKET_API, params={"limit": 100, "offset": page * 100, "active": "true"})
            for page in range(20)  # 20 pages x 100 = 2000 markets
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        for r in responses:
            try:
                if isinstance(r, Exception):
                    continue
                batch = r.json()
                if batch:
                    all_markets.extend(batch)
            except Exception:
                pass

    log.info("Loaded %d markets total", len(all_markets))
    return all_markets

def filter_good_markets(markets: list[dict]) -> list[dict]:
    """Фільтрує ринки: активні, 48 годин, не прострочені."""
    now = datetime.now(timezone.utc)
    seen = set()
    topic_count = {}
    TOPICS = ["bitcoin", "btc", "ethereum", "eth", "trump", "fed", "war",
              "iran", "russia", "china", "nvidia", "apple", "taylor", "election"]

    valid = []
    for m in markets:
        # deduplicate
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)

        question = m.get("question", "")
        if not question or not m.get("outcomePrices"):
            continue
        if m.get("resolved") or m.get("closed"):
            continue
        if is_question_expired(question):
            continue

        # Skip if question mentions a future date >2 days away
        skip = False
        for mon, day in re.findall(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ ]+([0-9]{1,2})', question.lower()):
            try:
                mn = MONTHS.get(mon[:3], 0)
                if mn:
                    candidate = date(now.year, mn, int(day))
                    if (candidate - now.date()).days > 2:
                        skip = True
                        break
            except Exception:
                pass
        if skip:
            continue

        # Check end date
        end_dt = parse_end_date(m)
        if end_dt:
            hours_left = (end_dt - now).total_seconds() / 3600
            if hours_left < 0 or hours_left > 48:
                continue

        # Volume filter: skip low liquidity markets
        vol = float(m.get("volume24hr") or m.get("volumeClob") or 0)
        if vol < 100:
            continue

        # Price filter: skip if all prices are extreme (>95% or <1%)
        prices = parse_prices(m)
        if prices:
            price_values = list(prices.values())
            if all(p > 0.95 or p < 0.01 for p in price_values):
                continue

        # Diversity: max 2 per topic
        q = question.lower()
        topic = next((kw for kw in TOPICS if kw in q), "other")
        if topic_count.get(topic, 0) >= 2:
            continue
        topic_count[topic] = topic_count.get(topic, 0) + 1

        valid.append(m)

    # Add _min_price and detect arbitrage
    for m in valid:
        prices = parse_prices(m)
        if prices:
            m["_min_price"] = min(prices.values())
            vals = list(prices.values())
            if len(vals) == 2:
                total = sum(vals)
                if total < 0.97:
                    m["_arbitrage"] = True
                    m["_arb_gap"] = round(1 - total, 3)
                    log.info("Arbitrage: %s (gap=%.3f)", m.get("question","")[:50], 1-total)

    # Sort: arbitrage first, then by min price
    valid.sort(key=lambda x: (not x.get("_arbitrage"), x.get("_min_price", 1)))
    log.info("Filtered to %d good markets", len(valid))
    return valid

def fetch_markets() -> list[dict]:
    """Синхронна обгортка для async з кешем 10 хв."""
    global MARKETS_CACHE, MARKETS_TS
    if MARKETS_CACHE is not None and time.time() - MARKETS_TS < MARKETS_CACHE_TTL:
        log.info("Using cached markets (%d)", len(MARKETS_CACHE))
        return MARKETS_CACHE
    try:
        all_markets = asyncio.run(fetch_markets_async())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        all_markets = loop.run_until_complete(fetch_markets_async())

    valid = filter_good_markets(all_markets)
    random.shuffle(valid)
    result = valid[:50]
    MARKETS_CACHE = result
    MARKETS_TS = time.time()
    return result

def fetch_market(market_id: str) -> dict | None:
    try:
        r = requests.get(f"{POLYMARKET_API}/{market_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Market fetch error: %s", e)
        return None


# ══════════════════════════════════════════════
# 📰  NEWS API
# ══════════════════════════════════════════════

def search_news(query: str) -> list[str]:
    """Шукає новини через NewsAPI."""
    if not NEWS_KEY:
        return []
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query[:50],
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 5,
                "apiKey": NEWS_KEY,
            },
            timeout=10,
        )
        data = r.json()
        return [a["title"] for a in data.get("articles", [])[:5]]
    except Exception as e:
        log.warning("News error: %s", e)
        return []

def fetch_news_for_market(question: str) -> str:
    """Отримує новини з кешем щоб не витрачати API ліміт."""
    key = question[:40]
    # Check cache
    if key in NEWS_CACHE:
        ts, data = NEWS_CACHE[key]
        if time.time() - ts < NEWS_CACHE_TTL:
            return data
    # Fetch fresh
    articles = search_news(question)
    if articles:
        result = " | ".join(articles[:3])
        NEWS_CACHE[key] = (time.time(), result)
        return result
    # Fallback to DuckDuckGo
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": question[:50], "format": "json", "no_html": "1"},
            timeout=8,
        )
        data = r.json()
        text = data.get("AbstractText", "")
        result = text[:300] if text else ""
        NEWS_CACHE[key] = (time.time(), result)
        return result
    except Exception:
        return ""


# ══════════════════════════════════════════════
# 🧠  AI ANALYSIS
# ══════════════════════════════════════════════

def extract_json(text: str) -> dict | None:
    """Safely extract JSON even if Claude adds extra text."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Try regex extract
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None

def build_context(memory: dict) -> str:
    closed = [b for b in memory["bets"] if b["status"] in ("closed", "cancelled")][-5:]
    if not closed:
        return "Нет предыдущих ставок."
    lines = ["Предыдущие ставки (учись на них):"]
    for b in closed:
        result = "ВЫИГРЫШ" if (b.get("pnl") or 0) > 0 else "ПРОИГРЫШ"
        lines.append(f"- {b['question'][:60]} | {b['pick']} | {result} | ${b.get('pnl', 0):.2f}")
    return "\n".join(lines)

def kelly_bet(true_prob: float, price: float, balance: float) -> float:
    """Правильний Kelly Criterion: f = (p*b - q) / b де b = odds."""
    try:
        if price <= 0 or price >= 1:
            return 1.0
        odds = (1 / price) - 1  # decimal odds
        kelly = (true_prob * odds - (1 - true_prob)) / odds
        kelly = max(0, min(kelly, 0.05))  # cap at 5% bankroll
        return max(1.0, min(round(balance * kelly, 2), 5.0))
    except Exception:
        return 1.0

def ai_analyse_batch(batch: list, mode: str, context: str) -> dict | None:
    now = datetime.now(timezone.utc)
    summaries = []
    for m in batch:
        prices = parse_prices(m)
        price_str = ", ".join(f"{o}: {p:.1%}" for o, p in prices.items())
        end_dt = parse_end_date(m)
        hours = f"{(end_dt - now).total_seconds()/3600:.0f}h" if end_dt else "?"
        vol = m.get("volume24hr", 0)
        summaries.append(f"[id:{m.get('id')}] {m['question']} | {price_str} | Vol:${vol} | Closes:{hours}")

    # Fetch news for top 2 markets
    news_parts = []
    for m in batch[:2]:
        news = fetch_news_for_market(m["question"])
        if news:
            news_parts.append(f"{m['question'][:40]}: {news[:200]}")
    news_str = "\n".join(news_parts) if news_parts else "Нет новостей."

    if mode == "lottery":
        strategy = "ЛОТЕРЕЯ: Найди рынок где исход с ценой <8% реально более вероятен. Ищи неожиданные события."
        bet_field = '"bet_usd": <1-2>,'
    else:
        strategy = "VALUE: Найди рынок где толпа ошибается на 10%+. Смотри на оба направления."
        bet_field = '"bet_usd": <2-4>,'

    prompt = f"""Ты профессиональный трейдер prediction markets.

ИСТОРИЯ СТАВОК:
{context}

НОВОСТИ:
{news_str}

РЫНКИ:
{chr(10).join(summaries)}

ЗАДАЧА: {strategy}

Сначала подумай: какой рынок выглядит неправильно оцененным и почему?
Проверь: если рынок о крипте - соответствует ли условие текущей цене?

ПРАВИЛА:
- Не выбирай рынки с прошедшими датами
- Все текстовые поля на РУССКОМ
- Если нет хорошей возможности - верни score: 0

Ответь ТОЛЬКО валидным JSON:
{{
  "thoughts": "<рассуждения 1-2 предложения>",
  "mode": "{mode}",
  "market_id": "<id>",
  "question": "<перевод на русский>",
  "pick": "<YES или NO>",
  "market_price": <0.01-0.99>,
  "true_probability": <0.01-0.99>,
  "potential_multiplier": <целое число>,
  "reason": "<вывод на русском с реальными данными>",
  {bet_field}
  "score": <0-100>
}}"""

    try:
        text = call_claude(prompt, max_tokens=600, use_search=True)
        result = extract_json(text)
        if not result:
            log.warning("Claude returned invalid JSON: %s", text[:100])
            return None
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

    mode    = random.choices(["lottery", "value"], weights=[20, 80])[0]
    context = build_context(memory)

    batches = [markets[i:i+20] for i in range(0, len(markets), 20)][:3]
    log.info("Analysing %d markets in %d batches (mode=%s)", len(markets), len(batches), mode)

    candidates = []
    for i, batch in enumerate(batches):
        log.info("Batch %d/%d", i+1, len(batches))
        result = ai_analyse_batch(batch, mode, context)
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
            msgs.append(f"↩️ *Возврат*\n{bet['question']}\nРынок закрылся без результата +${bet['wager']:.2f}")
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
                f"Выбрал {bet['pick']} -> {winner}\n"
                f"Прибыль: *+${profit:.2f}* | Баланс: ${memory['balance']:.2f}"
            )
        else:
            memory["total_profit"] -= wager
            memory["stats"]["losses"] += 1
            memory["stats"][f"{mode}_losses"] = memory["stats"].get(f"{mode}_losses", 0) + 1
            bet["pnl"] = round(-wager, 2)
            msgs.append(
                f"❌ *ПРОИГРЫШ* {'🎰' if mode=='lottery' else '🎯'}\n"
                f"{bet['question']}\n"
                f"Выбрал {bet['pick']} -> {winner}\n"
                f"Потеряно: -${wager:.2f} | Баланс: ${memory['balance']:.2f}"
            )

    save_memory(memory)
    return msgs


# ══════════════════════════════════════════════
# 🎮  PLACE BET
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

    wager = float(analysis.get("bet_usd", kelly_bet(true_prob, price, memory["balance"])))
    wager = max(1.0, min(wager, 5.0))

    if memory["balance"] < wager:
        wager = round(memory["balance"] * 0.05, 2)
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
        "thoughts": analysis.get("thoughts", ""),
        "pnl": None,
    })
    save_memory(memory)

    emoji = "🎰" if mode == "lottery" else "🎯"
    await message.reply_text(
        f"{emoji} *Ставка! [{mode.upper()}]*\n\n"
        f"📋 {analysis.get('question', matched['question'])}\n"
        f"📌 Выбор: *{pick}* @ {price:.1%}\n"
        f"💥 Множитель: *x{multiplier}*\n"
        f"💰 Ставка: ${wager:.2f}\n"
        f"🏆 Если выиграет: ~${wager*multiplier:.0f}\n"
        f"💼 Баланс: ${memory['balance']:.2f}\n\n"
        f"💭 {analysis.get('thoughts','')}\n"
        f"🧠 {analysis.get('reason','—')}",
        parse_mode="Markdown",
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
        [InlineKeyboardButton("🔍 Диагностика", callback_data="check")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    news_status = "NewsAPI подключен" if NEWS_KEY else "NewsAPI не подключен (добавь NEWS_KEY)"
    await update.message.reply_text(
        "🤖 *Polymarket Bot v4.0*\n\n"
        "Claude Haiku + веб-поиск + NewsAPI\n"
        "Стратегия: 60% лотерея + 40% value\n"
        "Частота: каждые 30 минут\n"
        "Рынков: до 2000 (async)\n\n"
        f"📰 {news_status}",
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
    elif query.data == "check":     await _do_check(msg)

async def _do_analyse(message):
    msg = await message.reply_text("⏳ Завантажую ринки...")
    try:
        memory  = load_memory()
        markets = fetch_markets()
        if not markets:
            await msg.edit_text("❌ Не знайдено ринків. Спробуй пізніше.")
            return
        await msg.edit_text(f"🧠 Claude аналізує {len(markets)} ринків...")
        already_bet = {b["market_id"] for b in memory["bets"] if b["status"] == "open"}
        analysis = ai_analyse(markets, memory, skip_ids=already_bet)
        if not analysis:
            await msg.edit_text("❌ Claude не знайшов хорошої можливості зараз.")
            return
        await msg.delete()
        await place_bet(message, memory, analysis, markets)
    except Exception as e:
        log.error("Analyse error: %s", e)
        await msg.edit_text(f"❌ Помилка: {str(e)[:100]}")

async def _do_stats(message):
    memory = load_memory()
    s = memory["stats"]
    total = s["wins"] + s["losses"]
    wr  = (s["wins"] / total * 100) if total else 0
    roi = (memory["total_profit"] / s["total_wagered"] * 100) if s["total_wagered"] else 0
    open_count = sum(1 for b in memory["bets"] if b["status"] == "open")
    drawdown = memory["balance"] - memory.get("peak_balance", STARTING_BALANCE)
    await message.reply_text(
        f"📊 *Статистика v4.0*\n\n"
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
            f"{emoji} {b['question'][:50]}...\n"
            f"  -> *{b['pick']}* | ${b['wager']:.2f} | x{b.get('potential_multiplier','?')} | {b['created_at'][:10]}"
        )
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_resolve(message):
    await message.reply_text("🔍 Перевіряю результати...")
    memory  = load_memory()
    results = resolve_bets(memory)
    if not results:
        await message.reply_text("Ринки ще не закрились.")
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
    lines  = ["🏆 *Топ 3 лучших:*\n"]
    for b in by_pnl[:3]:
        lines.append(f"{'🎰' if b.get('mode')=='lottery' else '🎯'} {b['question'][:45]}...\n   *+${b['pnl']:.2f}* | {b['pick']}")
    lines.append("\n💀 *Топ 3 худших:*\n")
    for b in by_pnl[-3:]:
        lines.append(f"❌ {b['question'][:45]}...\n   *${b['pnl']:.2f}* | {b['pick']}")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _do_history(message):
    memory  = load_memory()
    history = memory.get("balance_history", [])
    if len(history) < 2:
        await message.reply_text("📈 Мало даних. Зачекай трохи!")
        return
    balances = [h["balance"] for h in history[-20:]]
    min_b, max_b = min(balances), max(balances)
    lines = []
    for row in range(8, 0, -1):
        threshold = min_b + (max_b - min_b) * row / 8
        bar   = "".join("█" if b >= threshold else "░" for b in balances)
        label = f"${threshold:.0f}|" if row in (8, 4, 1) else "      |"
        lines.append(f"`{label}{bar}`")
    diff = balances[-1] - balances[0]
    sign = "+" if diff >= 0 else ""
    await message.reply_text(
        f"📈 *График баланса*\n\n" + "\n".join(lines) +
        f"\n\n💼 ${balances[0]:.2f} -> ${balances[-1]:.2f} ({sign}{diff:.2f})",
        parse_mode="Markdown",
    )

async def _do_check(message):
    msg = await message.reply_text("🔍 Диагностика...")
    lines = []

    # Polymarket
    try:
        r = requests.get(POLYMARKET_API, params={"limit": 5, "active": "true"}, timeout=10)
        r.raise_for_status()
        lines.append(f"✅ Polymarket API — OK")
    except Exception as e:
        lines.append(f"❌ Polymarket API — {str(e)[:50]}")

    # Claude
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ.get("ANTHROPIC_KEY",""),
                     "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
        r.raise_for_status()
        lines.append("✅ Claude Haiku API — OK")
    except Exception as e:
        lines.append(f"❌ Claude API — {str(e)[:80]}")

    # NewsAPI
    if NEWS_KEY:
        try:
            r = requests.get("https://newsapi.org/v2/everything",
                params={"q": "bitcoin", "pageSize": 1, "apiKey": NEWS_KEY}, timeout=8)
            r.raise_for_status()
            lines.append("✅ NewsAPI — OK")
        except Exception as e:
            lines.append(f"❌ NewsAPI — {str(e)[:50]}")
    else:
        lines.append("⚠️ NewsAPI — не підключений (додай NEWS_KEY в Railway)")

    # Markets count
    try:
        now = datetime.now(timezone.utc)
        all_m = []
        for offset in [0, 100, 200, 300, 400]:
            r2 = requests.get(POLYMARKET_API,
                params={"limit": 100, "active": "true", "offset": offset}, timeout=10)
            batch = r2.json()
            if not batch: break
            all_m.extend(batch)
        valid = filter_good_markets(all_m)
        lines.append(f"✅ Ринків всього: {len(all_m)} | Після фільтру: {len(valid)}")
    except Exception as e:
        lines.append(f"❌ Ошибка рынков: {str(e)[:50]}")

    # Memory
    try:
        memory = load_memory()
        open_bets = sum(1 for b in memory["bets"] if b["status"] == "open")
        lines.append(f"✅ Память — ${memory['balance']:.2f} | Ставок: {open_bets}")
    except Exception as e:
        lines.append(f"❌ Память — {str(e)[:50]}")

    # Env
    tg  = "✅" if os.environ.get("TG_TOKEN") else "❌"
    ant = "✅" if os.environ.get("ANTHROPIC_KEY") else "❌"
    nws = "✅" if os.environ.get("NEWS_KEY") else "⚠️"
    lines.append(f"{tg} TG_TOKEN | {ant} ANTHROPIC_KEY | {nws} NEWS_KEY")

    result = "\n".join(lines)
    await msg.edit_text(f"🔍 *Диагностика v4.0:*\n\n{result}", parse_mode="Markdown")

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
        "• Загружаю до 2000 рынков (async)\n"
        "• Ищу новости через NewsAPI\n"
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
            self._bot = bot
            self._chat_id = chat_id
        async def reply_text(self, text, **kwargs):
            await self._bot.send_message(chat_id=self._chat_id, text=text, **kwargs)

    memory = load_memory()
    await place_bet(FakeMessage(ctx.bot, chat_id), memory, analysis, markets)


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
async def cmd_check(u, c):     await _do_check(u.message)

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
    app.add_handler(CommandHandler("check",     cmd_check))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🚀 Bot v4.0 started!")
    app.run_polling()

if __name__ == "__main__":
    main()
