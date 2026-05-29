import os
import telebot
import yfinance as yf
import pandas as pd
import ta
import requests
import time
import ccxt
import sqlite3
import threading
import logging
from datetime import datetime
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
ALLOWED_USER_IDS = list(map(int, os.getenv("ALLOWED_USER_IDS", "").split(","))) if os.getenv("ALLOWED_USER_IDS") else []

if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
auto_trade_active = [True]

def is_allowed(user_id):
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS

def check_access(call_or_message):
    uid = call_or_message.from_user.id
    if not is_allowed(uid):
        logger.warning(f"⛔ Попытка доступа: {uid}")
        return False
    return True

DB_PATH = 'trades.db'

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, ticker TEXT, side TEXT,
                amount_usdt REAL, price REAL, qty REAL,
                pnl REAL, pnl_percent REAL, status TEXT
            )
        ''')
        conn.commit()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
    finally:
        conn.close()

def save_trade(ticker, side, amount_usdt, price, qty, status="open"):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trades (timestamp, ticker, side, amount_usdt, price, qty, pnl, pnl_percent, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), ticker, side, amount_usdt, price, qty, 0, 0, status))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def update_trade_pnl(trade_id, pnl, pnl_percent):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('UPDATE trades SET pnl=?, pnl_percent=?, status="closed" WHERE id=?', (pnl, pnl_percent, trade_id))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def get_last_open_trade(ticker):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT id, qty, price FROM trades WHERE ticker=? AND status="open" ORDER BY id DESC LIMIT 1', (ticker,))
        return cursor.fetchone()
    except:
        return None
    finally:
        conn.close()

def get_daily_report():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT COUNT(*),
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
                   SUM(pnl)
            FROM trades WHERE date(timestamp)=date(?) AND status="closed"
        ''', (today,))
        r = cursor.fetchone()
        total = r[0] or 0
        win = r[1] or 0
        loss = r[2] or 0
        pnl = r[3] or 0
        wr = (win / total * 100) if total > 0 else 0
        return f"""
📊 *ОТЧЁТ ЗА {today}*
━━━━━━━━━━━━━━━━━━━━━━━
📈 *Всего сделок:* {total}
🟢 *В прибыль:* {win}
🔴 *В убыток:* {loss}
🎯 *Winrate:* {wr:.1f}%
💰 *Общий P&L:* {pnl:.2f} USDT
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Не инвестрекомендация
"""
    except:
        return "❌ Ошибка отчёта"
    finally:
        conn.close()

def get_all_trades():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT timestamp, ticker, side, amount_usdt, price, pnl, status FROM trades ORDER BY id DESC LIMIT 10')
        results = cursor.fetchall()
        if not results:
            return "📭 Нет сделок"
        response = "📋 *ПОСЛЕДНИЕ СДЕЛКИ*\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        for r in results:
            ts, ticker, side, amount, price, pnl, status = r
            si = "🟢" if side == "buy" else "🔴"
            pi = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪")
            response += f"\n{si} {ticker} | {amount} USDT\n   Цена: {price:.2f} | {pi} P&L: {pnl:.2f}\n   {ts[:16]}\n"
        return response
    except:
        return "❌ Ошибка истории"
    finally:
        conn.close()

init_db()

# ========== BYBIT (крипто) ==========
if BYBIT_API_KEY and BYBIT_API_SECRET:
    exchange = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True,
    })
else:
    exchange = None
    logger.warning("⚠️ Bybit не настроен")

# ========== ВАЛЮТНЫЕ ПАРЫ ==========
FOREX_PAIRS = ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF", "NZDUSD"]

def get_forex_price_investing(pair):
    """Получает цену с Investing.com через RSS"""
    try:
        url = f"https://www.investing.com/rss/news_{pair}.rss"
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code != 200:
            return None
        # Ищем цену в тексте RSS
        content = response.text
        import re
        # Простой поиск числа с точкой
        match = re.search(r'\d+\.\d+', content)
        if match:
            return float(match.group())
        return None
    except:
        return None

def get_forex_data_yahoo(pair):
    """Получает данные с Yahoo Finance (упрощённо)"""
    try:
        ticker = f"{pair}=X"
        data = yf.download(ticker, period="5d", interval="15m", progress=False, auto_adjust=False)
        if data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            close = data['Close'].iloc[:, 0] if 'Close' in data else data.iloc[:, 0]
        else:
            close = data['Close'] if 'Close' in data else data.iloc[:, 0]
        close = close.dropna()
        if len(close) < 20:
            return None
        return close
    except:
        return None

def analyze_pair(pair):
    """Упрощённый анализ валютной пары"""
    try:
        close = get_forex_data_yahoo(pair)
        if close is None:
            return f"❌ Нет данных для {pair}"
        
        current = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) >= 2 else current
        change = ((current - prev) / prev) * 100
        
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1] or 50
        
        if rsi < 30:
            signal = "🟢 ПОКУПАТЬ (BUY)"
        elif rsi > 70:
            signal = "🔴 ПРОДАВАТЬ (SELL)"
        else:
            signal = "⚪ ДЕРЖАТЬ (HOLD)"
        
        return f"""
📊 *АНАЛИЗ: {pair}*
━━━━━━━━━━━━━━━━━━━━━━━
💰 *Цена:* {current:.5f}
📈 *Изменение:* {change:+.2f}%
📉 *RSI (14):* {rsi:.1f}

🎯 *Сигнал:* {signal}
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Не инвестрекомендация
"""
    except Exception as e:
        return f"❌ Ошибка анализа {pair}: {e}"

# ========== КРИПТО-ТРЕЙДИНГ ==========
def get_coin_balance(symbol):
    if not exchange:
        return 0
    try:
        bal = exchange.fetch_balance()
        return bal[symbol]['free'] if symbol in bal else 0
    except:
        return 0

def get_coin_price(symbol):
    if not exchange:
        return 0
    try:
        t = exchange.fetch_ticker(f'{symbol}/USDT')
        return t['last']
    except:
        return 0

def buy_coin(symbol, amount):
    if not exchange:
        return "❌ Bybit не настроен"
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return "❌ Нет цены"
        qty = round(amount / price, 5)
        exchange.create_market_buy_order(f'{symbol}/USDT', qty)
        save_trade(symbol, "buy", amount, price, qty, "open")
        return f"✅ Куплено {qty} {symbol} по {price} USDT"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def sell_coin(symbol, qty):
    if not exchange:
        return "❌ Bybit не настроен"
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return "❌ Нет цены"
        exchange.create_market_sell_order(f'{symbol}/USDT', qty)
        last = get_last_open_trade(symbol)
        if last:
            tid, _, ep = last
            pnl = (price - ep) * qty
            pct = ((price - ep) / ep) * 100
            update_trade_pnl(tid, pnl, pct)
            return f"✅ Продано {qty} {symbol} | P&L: {pnl:.2f} ({pct:.2f}%)"
        return f"✅ Продано {qty} {symbol}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ========== МЕНЮ ==========
def create_main_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("💰 Валютные пары", callback_data="menu_forex"),
        telebot.types.InlineKeyboardButton("⚡ OTC пары", callback_data="menu_forex_otc"),
        telebot.types.InlineKeyboardButton("📊 Крипто-трейдинг", callback_data="menu_crypto")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_crypto_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    for sym in ["BTC","ETH","SOL","BNB","XRP","DOGE"]:
        kb.add(telebot.types.InlineKeyboardButton(f"₿ {sym}", callback_data=f"crypto_{sym.lower()}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_coin_menu(coin, sym):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("💰 Баланс", callback_data=f"balance_{sym}"))
    kb.add(telebot.types.InlineKeyboardButton("📊 Отчёт по сделкам", callback_data="crypto_report"))
    kb.add(telebot.types.InlineKeyboardButton("🤖 Автоторговля", callback_data=f"auto_{sym}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, f"📊 *{coin} ({sym})*"

def create_category_menu(category):
    pairs = FOREX_PAIRS if category == "forex" else [p + "_otc" for p in FOREX_PAIRS]
    title = "💰 ВАЛЮТНЫЕ ПАРЫ" if category == "forex" else "⚡ OTC ПАРЫ (⚠️ волатильность)"
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    for p in pairs:
        kb.add(telebot.types.InlineKeyboardButton(p.replace("_otc","⚡"), callback_data=f"an_{p}"))
    kb.add(
        telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"),
        telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, title

@bot.message_handler(commands=['start', 'menu'])
def send_menu(m):
    if not check_access(m): return
    bot.send_message(m.chat.id, "📋 *ГЛАВНОЕ МЕНЮ*", parse_mode='Markdown', reply_markup=create_main_menu())

@bot.message_handler(commands=['test'])
def test(m):
    bot.reply_to(m, "✅ Бот работает! Выбери валютную пару.")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call): return
    data = call.data
    if data == "close_window":
        bot.edit_message_text("✅ Окно закрыто", call.message.chat.id, call.message.message_id)
        return
    if data == "back_to_menu":
        bot.edit_message_text("📋 *ГЛАВНОЕ МЕНЮ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_main_menu())
        return
    if data in ("menu_forex", "menu_forex_otc"):
        kb, title = create_category_menu("forex" if data == "menu_forex" else "forex_otc")
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        return
    if data == "menu_crypto":
        bot.edit_message_text("📊 *КРИПТО-ТРЕЙДИНГ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_crypto_menu())
        return
    if data.startswith("crypto_"):
        sym = data.replace("crypto_", "").upper()
        kb, title = create_coin_menu(sym, sym)
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        return
    if data.startswith("balance_"):
        sym = data.replace("balance_", "")
        bal = get_coin_balance(sym)
        price = get_coin_price(sym)
        val = bal * price
        bot.send_message(call.message.chat.id, f"💰 *{sym} БАЛАНС*\n{bal:.8f} {sym}\nЦена: {price:.2f} USDT\nСтоимость: {val:.2f} USDT", parse_mode='Markdown')
        return
    if data == "crypto_report":
        bot.send_message(call.message.chat.id, get_daily_report() + "\n\n" + get_all_trades(), parse_mode='Markdown')
        return
    if data.startswith("auto_"):
        sym = data.replace("auto_", "")
        bot.send_message(call.message.chat.id, f"🤖 *Автоторговля {sym}*\nАктивна. Параметры: RSI<30 BUY, RSI>70 SELL", parse_mode='Markdown')
        return
    if data.startswith("an_"):
        ticker = data.replace("an_", "").replace("_otc", "")
        bot.answer_callback_query(call.id, "🔍 Анализирую...")
        msg = bot.send_message(call.message.chat.id, "⏳ Загружаю данные...")
        def analyze():
            res = analyze_pair(ticker)
            bot.delete_message(call.message.chat.id, msg.message_id)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД К ПАРАМ", callback_data="menu_forex" if "_otc" not in call.data else "menu_forex_otc"))
            bot.send_message(call.message.chat.id, res, parse_mode='Markdown', reply_markup=kb)
        threading.Thread(target=analyze).start()
        return

@bot.message_handler(func=lambda m: True)
def handle_text(m):
    if not check_access(m): return
    ticker = m.text.strip().upper()
    if ticker.startswith('/'): return
    msg = bot.reply_to(m, "⏳ Анализирую...")
    def analyze():
        res = analyze_pair(ticker.replace("_OTC", ""))
        bot.delete_message(m.chat.id, msg.message_id)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ В ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu"))
        bot.reply_to(m, res, parse_mode='Markdown', reply_markup=kb)
    threading.Thread(target=analyze).start()

def auto_trade_loop():
    if not exchange:
        return
    from ta.momentum import RSIIndicator
    coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
    logger.info("🤖 АВТОТОРГОВЛЯ ЗАПУЩЕНА")
    while True:
        if not auto_trade_active[0]:
            time.sleep(10)
            continue
        for coin in coins:
            try:
                price = get_coin_price(coin)
                if price == 0:
                    continue
                ohlcv = exchange.fetch_ohlcv(f'{coin}/USDT', timeframe='15m', limit=100)
                closes = [c[4] for c in ohlcv]
                rsi = RSIIndicator(pd.Series(closes), window=14).rsi().iloc[-1]
                last = get_last_open_trade(coin)
                if last is None:
                    if rsi < 30:
                        buy_coin(coin, 10)
                        logger.info(f"🟢 АВТО-ПОКУПКА {coin} RSI={rsi:.1f}")
                        time.sleep(2)
                else:
                    tid, qty, ep = last
                    pct = ((price - ep) / ep) * 100
                    if pct >= 3 or rsi > 70:
                        sell_coin(coin, qty)
                        logger.info(f"🔴 АВТО-ПРОДАЖА {coin} RSI={rsi:.1f} | P&L: {pct:.2f}%")
                        time.sleep(2)
                    elif pct <= -2:
                        sell_coin(coin, qty)
                        logger.info(f"🛑 СТОП-ЛОСС {coin}: {pct:.2f}%")
                        time.sleep(2)
            except Exception as e:
                logger.error(f"⚠️ Автоторговля {coin}: {e}")
            time.sleep(1)
        time.sleep(60)

if exchange:
    threading.Thread(target=auto_trade_loop, daemon=True).start()

def set_webhook():
    url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if url:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{url}/{TOKEN}")
        logger.info(f"✅ Webhook: {url}")

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
        return 'OK', 200
    return 'Bad request', 403

@app.route('/health')
def health():
    return 'OK', 200

@app.route('/')
def index():
    return 'Bot is running', 200

if __name__ == "__main__":
    set_webhook()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
