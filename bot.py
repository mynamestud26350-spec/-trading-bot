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
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    raise ValueError("❌ BYBIT ключи не найдены")

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
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сделки: {e}")
    finally:
        conn.close()

def update_trade_pnl(trade_id, pnl, pnl_percent):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('UPDATE trades SET pnl=?, pnl_percent=?, status="closed" WHERE id=?', (pnl, pnl_percent, trade_id))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка обновления сделки: {e}")
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
    except Exception as e:
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

exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

FOREX_PAIRS = [
    'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDUSD', 'CADCHF', 'CADJPY', 'CHFJPY',
    'EURAUD', 'EURCAD', 'EURCHF', 'EURGBP', 'EURJPY', 'EURUSD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'GBPUSD',
    'USDCAD', 'USDCHF', 'USDJPY', 'NZDJPY'
]

OTC_PAIRS = [
    "AEDCNY_otc", "AUDCAD_otc", "AUDCHF_otc", "AUDJPY_otc", "AUDNZD_otc", "AUDUSD_otc",
    "BHDCNY_otc", "CADCHF_otc", "CADJPY_otc", "CHFJPY_otc", "CHFNOK_otc",
    "EURCHF_otc", "EURGBP_otc", "EURHUF_otc", "EURJPY_otc", "EURNZD_otc", "EURUSD_otc", "EURTRY_otc",
    "GBPAUD_otc", "GBPJPY_otc", "GBPUSD_otc", "JODCNY_otc", "KESUSD_otc", "LBPUSD_otc",
    "MADUSD_otc", "NGNUSD_otc", "NZDJPY_otc", "NZDUSD_otc", "OMRCNY_otc", "QARCNY_otc",
    "SARCNY_otc", "TNDUSD_otc", "UAHUSD_otc", "USDARS_otc", "USDBDT_otc", "USDBRL_otc",
    "USDCAD_otc", "USDCHF_otc", "USDCLP_otc", "USDCNH_otc", "USDCOP_otc", "USDDZD_otc",
    "USDEGP_otc", "USDIDR_otc", "USDINR_otc", "USDJPY_otc", "USDMXN_otc", "USDMYR_otc",
    "USDPHP_otc", "USDPKR_otc", "USDRUB_otc", "USDSGD_otc", "USDTHB_otc", "USDVND_otc",
    "YERUSD_otc", "ZARUSD_otc"
]

_cache = {}
CACHE_TTL = 120

def get_cached(key):
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def set_cached(key, data):
    _cache[key] = (data, time.time())

def fix_ticker(ticker):
    is_otc = False
    if ticker.lower().endswith('_otc'):
        is_otc = True
        ticker = ticker[:-4]
    ticker = ticker.upper().strip()
    if ticker in FOREX_PAIRS:
        return f"{ticker}=X", is_otc
    return ticker, is_otc

def get_forex_data(ticker, interval, period_days):
    cache_key = f"{ticker}_{interval}_{period_days}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached
    try:
        data = yf.download(ticker, period=period_days, interval=interval, progress=False, auto_adjust=False, threads=False)
        if data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            close = data['Close'].iloc[:, 0] if 'Close' in data else data.iloc[:, 0]
        else:
            close = data['Close'] if 'Close' in data else data.iloc[:, 0]
        close = close.dropna()
        if len(close) < 20:
            return None
        set_cached(cache_key, close)
        return close
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки {ticker}: {e}")
        return None

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def analyze_timeframe(close, timeframe_name):
    if close is None or len(close) < 20:
        return None
    try:
        current_price = float(close.iloc[-1])
        ema50 = calculate_ema(close, 50).iloc[-1] if len(close) >= 50 else current_price
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi = 50 if pd.isna(rsi_val) else rsi_val
        if current_price > ema50 * 1.0003:
            trend, trend_score = "📈 ВВЕРХ", 1
        elif current_price < ema50 * 0.9997:
            trend, trend_score = "📉 ВНИЗ", -1
        else:
            trend, trend_score = "➡️ ФЛЕТ", 0
        if rsi < 30 and trend_score >= 0:
            entry_signal, entry_score = "🚀 ПОТЕНЦИАЛЬНЫЙ BUY", 1
        elif rsi > 70 and trend_score <= 0:
            entry_signal, entry_score = "🚀 ПОТЕНЦИАЛЬНЫЙ SELL", -1
        elif trend_score > 0:
            entry_signal, entry_score = "📈 ИЩЕМ BUY", 0.5
        elif trend_score < 0:
            entry_signal, entry_score = "📉 ИЩЕМ SELL", -0.5
        else:
            entry_signal, entry_score = "⏸️ ЖДЁМ", 0
        return {
            'price': current_price, 'trend': trend, 'trend_score': trend_score,
            'rsi': rsi, 'entry_signal': entry_signal, 'entry_score': entry_score
        }
    except Exception as e:
        return None

def multi_timeframe_analyst(ticker):
    fixed_ticker, is_otc = fix_ticker(ticker)
    timeframes = [
        {'name': '1H',  'interval': '60m', 'period': '7d', 'weight': 3},
        {'name': '30M', 'interval': '30m', 'period': '5d', 'weight': 2},
        {'name': '15M', 'interval': '15m', 'period': '3d', 'weight': 2},
        {'name': '5M',  'interval': '5m',  'period': '2d', 'weight': 1},
        {'name': '1M',  'interval': '1m',  'period': '1d', 'weight': 1}
    ]
    results = {}
    total_trend_score = 0
    total_weight = 0
    for tf in timeframes:
        close = get_forex_data(fixed_ticker, tf['interval'], tf['period'])
        analysis = analyze_timeframe(close, tf['name'])
        if analysis:
            results[tf['name']] = analysis
            total_trend_score += analysis['trend_score'] * tf['weight']
            total_weight += tf['weight']
        time.sleep(0.3)
    if not results:
        return None, f"❌ Нет данных для {ticker}", None
    avg_trend = total_trend_score / total_weight if total_weight > 0 else 0
    if avg_trend > 0.3:
        overall_trend, overall_color = "📈 ТРЕНД ВВЕРХ", "🟢"
    elif avg_trend < -0.3:
        overall_trend, overall_color = "📉 ТРЕНД ВНИЗ", "🔴"
    else:
        overall_trend, overall_color = "🟡 ФЛЕТ", "🟡"
    signal = "⏸️ ВНЕ РЫНКА"
    if '1M' in results:
        m1 = results['1M']
        if avg_trend > 0.3 and m1['entry_score'] > 0:
            signal = "🔵 СИЛЬНЫЙ BUY"
        elif avg_trend < -0.3 and m1['entry_score'] < 0:
            signal = "🔴 СИЛЬНЫЙ SELL"
        elif avg_trend > 0.3:
            signal = "🟡 BUY (осторожно)"
        elif avg_trend < -0.3:
            signal = "🟡 SELL (осторожно)"
    tf_icons = []
    for tf_name in ['1H', '30M', '15M', '5M', '1M']:
        if tf_name in results:
            r = results[tf_name]
            if "ВВЕРХ" in r['trend']:
                tf_icons.append(f"{tf_name}⬆️")
            elif "ВНИЗ" in r['trend']:
                tf_icons.append(f"{tf_name}⬇️")
            else:
                tf_icons.append(f"{tf_name}➡️")
    tf_line = " ".join(tf_icons)
    response = f"""
{overall_color} *МУЛЬТИ-ТАЙМФРЕЙМ: {ticker.upper()}*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tf_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*ОБЩИЙ ТРЕНД:* {overall_trend}
🎯 *СИГНАЛ:* {signal}
⏰ Экспирация: 1-2 минуты (вход со следующей свечи)
"""
    if is_otc:
        response += "\n⚠️ OTC актив — волатильность выше!"
    return results, response, signal

def advanced_analysis(ticker, interval='60m'):
    try:
        fixed_ticker, _ = fix_ticker(ticker)
        close = get_forex_data(fixed_ticker, interval, '7d')
        if close is None or len(close) < 20:
            return None
        ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        trend_slope = (float(ema50.iloc[-1]) - float(ema50.iloc[-2])) / float(ema50.iloc[-2]) * 100 if len(ema50) >= 2 else 0
        trend_val = max(0, min(100, 50 + trend_slope * 100))
        trend_signal = "BUY" if trend_val > 60 else ("SELL" if trend_val < 40 else "NEUTRAL")
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi = 50 if pd.isna(rsi_val) else rsi_val
        rsi_signal = "BUY" if rsi < 30 else ("SELL" if rsi > 70 else "NEUTRAL")
        macd = ta.trend.MACD(close).macd()
        macd_sig = ta.trend.MACD(close).macd_signal()
        if len(macd) >= 2 and macd.iloc[-1] > macd_sig.iloc[-1]:
            macd_signal = "BUY"
        elif len(macd) >= 2 and macd.iloc[-1] < macd_sig.iloc[-1]:
            macd_signal = "SELL"
        else:
            macd_signal = "NEUTRAL"
        params = [
            ("📈 Тренд (EMA50)", trend_val, trend_signal),
            ("📊 Импульс (RSI)", rsi, rsi_signal),
            ("🔄 Тренд (MACD)", 0, macd_signal),
        ]
        buys = sum(1 for _, _, s in params if s == "BUY")
        sells = sum(1 for _, _, s in params if s == "SELL")
        neutrals = len(params) - buys - sells
        return params, buys, sells, neutrals
    except Exception as e:
        return None

def news_analyst(ticker):
    return "⚪ НЕЙТРАЛЬНЫЕ", "HOLD", [], 0

def risk_manager_binary(is_otc=False):
    if is_otc:
        return "🟡 СРЕДНИЙ", "1% депозита"
    return "🟢 НИЗКИЙ", "1% депозита"

def portfolio_manager(ticker):
    try:
        mtf_results, mtf_analysis, mtf_signal = multi_timeframe_analyst(ticker)
        if mtf_results is None:
            return mtf_analysis
        _, is_otc = fix_ticker(ticker)
        advanced = advanced_analysis(ticker, '60m')
        if advanced:
            adv_params, adv_buys, adv_sells, adv_neutrals = advanced
        else:
            adv_params, adv_buys, adv_sells, adv_neutrals = None, 0, 0, 0
        sentiment, _, _, _ = news_analyst(ticker)
        risk_level, _ = risk_manager_binary(is_otc)
        if adv_params:
            if adv_buys > adv_sells + 1:
                final_signal = "✅ ПОКУПАТЬ"
            elif adv_sells > adv_buys + 1:
                final_signal = "❌ ПРОДАВАТЬ"
            elif adv_buys > adv_sells:
                final_signal = "🟡 ПОКУПАТЬ (осторожно)"
            elif adv_sells > adv_buys:
                final_signal = "🟡 ПРОДАВАТЬ (осторожно)"
            else:
                final_signal = "⏸️ ДЕРЖАТЬ"
        else:
            if "СИЛЬНЫЙ BUY" in str(mtf_signal):
                final_signal = "✅ ПОКУПАТЬ"
            elif "СИЛЬНЫЙ SELL" in str(mtf_signal):
                final_signal = "❌ ПРОДАВАТЬ"
            elif "BUY" in str(mtf_signal):
                final_signal = "🟡 ПОКУПАТЬ (осторожно)"
            elif "SELL" in str(mtf_signal):
                final_signal = "🟡 ПРОДАВАТЬ (осторожно)"
            else:
                final_signal = "⏸️ ДЕРЖАТЬ"
        response = f"""
📊 *КОМПЛЕКСНЫЙ АНАЛИЗ: {ticker.upper()}*

*МУЛЬТИ-ТАЙМФРЕЙМ*
{mtf_analysis}
"""
        if adv_params:
            response += "\n*РАСШИРЕННЫЙ АНАЛИЗ:*\n"
            for name, val, sig in adv_params:
                icon = "✅" if sig == "BUY" else ("❌" if sig == "SELL" else "➖")
                response += f"{icon} {name}: {val:.1f}\n"
        response += f"""
*Новости:* {sentiment}
*Риск:* {risk_level}

🎯 *ИТОГ: {final_signal}*
⚠️ Не инвестрекомендация
"""
        if is_otc:
            response += "\n⚡ OTC — волатильность выше"
        return response
    except Exception as e:
        return f"❌ Ошибка анализа {ticker}"

def get_coin_balance(symbol):
    try:
        bal = exchange.fetch_balance()
        return bal[symbol]['free'] if symbol in bal else 0
    except:
        return 0

def get_coin_price(symbol):
    try:
        t = exchange.fetch_ticker(f'{symbol}/USDT')
        return t['last']
    except:
        return 0

def buy_coin(symbol, amount_usdt):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return f"❌ Нет цены {symbol}"
        qty = round(amount_usdt / price, 5)
        exchange.create_market_buy_order(f'{symbol}/USDT', qty)
        save_trade(symbol, "buy", amount_usdt, price, qty, "open")
        return f"✅ Куплено {qty} {symbol} по {price} USDT"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def sell_coin(symbol, qty):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return f"❌ Нет цены {symbol}"
        exchange.create_market_sell_order(f'{symbol}/USDT', qty)
        last = get_last_open_trade(symbol)
        if last:
            tid, _, ep = last
            pnl = (price - ep) * qty
            pct = ((price - ep) / ep) * 100
            update_trade_pnl(tid, pnl, pct)
            return f"✅ Продано {qty} {symbol} по {price} | P&L: {pnl:.2f} USDT ({pct:.2f}%)"
        return f"✅ Продано {qty} {symbol} по {price} USDT"
    except Exception as e:
        return f"❌ Ошибка: {e}"

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
    coins = [("₿ BTC","crypto_btc"),("⟠ ETH","crypto_eth"),("◎ SOL","crypto_sol"),
             ("🟡 BNB","crypto_bnb"),("💎 XRP","crypto_xrp"),("🐕 DOGE","crypto_doge")]
    for name, data in coins:
        kb.add(telebot.types.InlineKeyboardButton(name, callback_data=data))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_coin_menu(coin, symbol):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("💰 Баланс", callback_data=f"balance_{symbol}"))
    kb.add(telebot.types.InlineKeyboardButton("📊 Отчёт по сделкам", callback_data="crypto_report"))
    kb.add(telebot.types.InlineKeyboardButton("🤖 Автоторговля", callback_data=f"auto_{symbol}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, f"📊 *{coin} ({symbol})*"

def create_category_menu(category):
    categories = {
        "forex": {"title": "💰 ВАЛЮТНЫЕ ПАРЫ\nВыберите пару:", "pairs": FOREX_PAIRS, "back_to": "menu_forex"},
        "forex_otc": {"title": "⚡ OTC ПАРЫ\n⚠️ Повышенная волатильность!\nВыберите пару:", "pairs": OTC_PAIRS, "back_to": "menu_forex_otc"}
    }
    cat = categories.get(category)
    if not cat:
        return None, "Нет данных"
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    buttons = [telebot.types.InlineKeyboardButton(p.replace("_otc", "⚡") if "_otc" in p else p, callback_data=f"an_{p}") for p in cat["pairs"]]
    for i in range(0, len(buttons), 2):
        kb.row(buttons[i], buttons[i+1]) if i+1 < len(buttons) else kb.row(buttons[i])
    kb.add(
        telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=cat["back_to"]),
        telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, cat["title"]

@bot.message_handler(commands=['start', 'menu'])
def send_menu(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    bot.send_message(message.chat.id, "📋 *ГЛАВНОЕ МЕНЮ*", parse_mode='Markdown', reply_markup=create_main_menu())

@bot.message_handler(commands=['autostop'])
def autostop_handler(message):
    if not check_access(message): return
    auto_trade_active[0] = False
    bot.reply_to(message, "🛑 *Автоторговля остановлена.*", parse_mode='Markdown')

@bot.message_handler(commands=['autostart'])
def autostart_handler(message):
    if not check_access(message): return
    auto_trade_active[0] = True
    bot.reply_to(message, "🤖 *Автоторговля запущена.*", parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def status_handler(message):
    if not check_access(message): return
    s = "🟢 Активна" if auto_trade_active[0] else "🔴 Остановлена"
    bot.reply_to(message, f"🤖 *Автоторговля:* {s}", parse_mode='Markdown')

@bot.message_handler(commands=['test'])
def test_yahoo(message):
    try:
        data = yf.download("EURUSD=X", period="1d", interval="5m", progress=False)
        if data.empty:
            bot.reply_to(message, "❌ Yahoo Finance не работает на сервере (данные пустые)")
        else:
            price = data['Close'].iloc[-1]
            bot.reply_to(message, f"✅ Yahoo Finance работает! Цена EURUSD: {price:.5f}")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка Yahoo Finance: {e}")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещён.")
        return
    data = call.data
    if data == "close_window":
        bot.edit_message_text("✅ *Окно закрыто*\n\nОтправьте /start для открытия меню.", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return
    if data == "back_to_menu":
        bot.edit_message_text("📋 *ГЛАВНОЕ МЕНЮ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_main_menu())
        bot.answer_callback_query(call.id)
        return
    if data in ("menu_forex", "menu_forex_otc"):
        kb, title = create_category_menu("forex" if data == "menu_forex" else "forex_otc")
        if kb:
            bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data in ("menu_crypto", "back_to_crypto"):
        bot.edit_message_text("📊 *КРИПТО-ТРЕЙДИНГ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_crypto_menu())
        bot.answer_callback_query(call.id)
        return
    coin_map = {"crypto_btc":"BTC","crypto_eth":"ETH","crypto_sol":"SOL","crypto_bnb":"BNB","crypto_xrp":"XRP","crypto_doge":"DOGE"}
    if data in coin_map:
        coin = coin_map[data]
        kb, title = create_coin_menu(coin, coin)
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("balance_"):
        coin = data.replace("balance_", "")
        balance = get_coin_balance(coin)
        price = get_coin_price(coin)
        value = balance * price if price else 0
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=f"crypto_{coin.lower()}"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"💰 *{coin} БАЛАНС*\n━━━━━━━━━━━━━━━\n{coin}: {balance:.8f}\nЦена: {price:.2f} USDT\nСтоимость: {value:.2f} USDT", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data == "crypto_report":
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"{get_daily_report()}\n\n{get_all_trades()}", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("auto_"):
        coin = data.replace("auto_", "")
        s = "🟢 Активна" if auto_trade_active[0] else "🔴 Остановлена"
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=f"crypto_{coin.lower()}"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"🤖 *Автоторговля {coin}*\n\nСтатус: {s}\n\n• RSI Buy < 30\n• RSI Sell > 70\n• Тейк: +3%\n• Стоп: -2%\n• Сумма: 10 USDT\n\n⚙️ /autostart | /autostop | /status\n\n⚠️ Реальный счёт Bybit!", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("an_"):
        ticker = data.replace("an_", "")
        bot.answer_callback_query(call.id, "🔍 Анализирую...")
        wait_msg = bot.send_message(call.message.chat.id, "⏳ *Анализирую...* Подождите до 20 сек.", parse_mode='Markdown')
        def run_analysis(t=ticker, chat_id=call.message.chat.id, wait_id=wait_msg.message_id):
            try:
                result = portfolio_manager(t)
                back_cb = "menu_forex_otc" if "_otc" in t else "menu_forex"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(
                    telebot.types.InlineKeyboardButton("◀ НАЗАД К ПАРАМ", callback_data=back_cb),
                    telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
                )
                kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
                bot.delete_message(chat_id, wait_id)
                bot.send_message(chat_id, result, parse_mode='Markdown', reply_markup=kb)
            except Exception as e:
                logger.error(f"❌ run_analysis: {e}")
                bot.edit_message_text("❌ Ошибка анализа. Попробуйте позже.", chat_id, wait_id)
        threading.Thread(target=run_analysis, daemon=True).start()
        return

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    ticker = message.text.strip().upper()
    if ticker.startswith('/'):
        return
    wait_msg = bot.reply_to(message, "⏳ *Анализирую...* Подождите до 20 сек.", parse_mode='Markdown')
    def run_analysis(t=ticker, chat_id=message.chat.id, wait_id=wait_msg.message_id, orig=message):
        try:
            result = portfolio_manager(t)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("◀ В ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu"))
            bot.delete_message(chat_id, wait_id)
            bot.reply_to(orig, result, parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            logger.error(f"❌ handle_message: {e}")
            bot.edit_message_text("❌ Ошибка анализа.", chat_id, wait_id)
    threading.Thread(target=run_analysis, daemon=True).start()

def auto_trade_loop():
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
                        r = buy_coin(coin, 10)
                        logger.info(f"🟢 АВТО-ПОКУПКА {coin} RSI={rsi:.1f}")
                        time.sleep(2)
                else:
                    tid, qty, ep = last
                    pct = ((price - ep) / ep) * 100
                    if pct >= 3:
                        r = sell_coin(coin, qty)
                        logger.info(f"✅ ТЕЙК {coin}: +{pct:.2f}%")
                        time.sleep(2)
                    elif pct <= -2:
                        r = sell_coin(coin, qty)
                        logger.info(f"🛑 СТОП {coin}: {pct:.2f}%")
                        time.sleep(2)
                    elif rsi > 70:
                        r = sell_coin(coin, qty)
                        logger.info(f"🔴 АВТО-ПРОДАЖА {coin} RSI={rsi:.1f}")
                        time.sleep(2)
            except Exception as e:
                logger.error(f"⚠️ Автоторговля {coin}: {e}")
            time.sleep(1)
        time.sleep(60)

threading.Thread(target=auto_trade_loop, daemon=True).start()

def set_webhook():
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook: {webhook_url}")

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
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
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
