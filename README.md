# 🤖 Polymarket Bot — Railway Deploy

## Файли
- `polymarket_bot.py` — основний бот
- `requirements.txt` — залежності
- `Procfile` — команда запуску для Railway

---

## 🚀 Деплой на Railway

### 1. Завантаж файли на GitHub
Створи новий репозиторій і залий всі 3 файли.

### 2. Зайди на railway.app
- New Project → Deploy from GitHub repo → вибери репо

### 3. Додай змінні середовища
У Railway → Variables → додай:
```
TELEGRAM_TOKEN   = токен від @BotFather
DEEPSEEK_API_KEY = ключ з platform.deepseek.com
```

### 4. Готово!
Railway сам встановить залежності і запустить бота.

---

## 📱 Команди бота
| Команда | Дія |
|---------|-----|
| `/start` | Старт |
| `/analyse` | Аналіз ринків + нова ставка |
| `/bets` | Відкриті ставки |
| `/resolve` | Перевірити результати |
| `/stats` | Баланс і статистика |
| `/autostart` | Авторежим щодня о 09:00 UTC |

---

## 💰 Вартість DeepSeek
- `deepseek-chat`: ~$0.00014 / 1K токенів
- 1 аналіз ≈ 500 токенів ≈ $0.00007
- **$2 вистачить на ~28 000 аналізів** 🔥
