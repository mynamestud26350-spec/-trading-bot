import os
import telebot
import pandas as pd
import ta
import requests
import time
import ccxt
import sqlite3
import threading
import logging
import feedparser
from datetime import datetime
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "GPAKXZYJ3V37KXER")
ALLOWED_USER_IDS = list(map(int, os.getenv("ALLOWED_USER_IDS", "").split(","))) if os.getenv("ALLOWED_USER_IDS") else []

if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    raise ValueError("❌ BYBIT ключи не найдены")

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ========== ФЛАГ АВТОТОРГОВЛИ ==========
auto_trade_active = [True]

# ========== АНТИФЛУД ==========
_user_last_request = {}
FLOOD_TIMEOUT = 10

def is_flooding(user_id):
    now = time.time()
    last = _user_last_request.get(user_id, 0)
    if now - last < FLOOD_TIMEOUT:
        return True
    _user_last_request[user_id] = now
    return False

# ========== ЗАЩИТА ДОСТУПА ==========
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

# ========== БАЗА ДАННЫХ ==========
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ticker_status ON trades(ticker, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON trades(timestamp)')
        conn.commit()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
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
    except Exception as e:
        logger.error(f"❌ Ошибка get_last_open_trade: {e}")
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
                   SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END),
                   SUM(pnl), AVG(pnl_percent)
            FROM trades WHERE date(timestamp)=date(?) AND status="closed"
        ''', (today,))
        r = cursor.fetchone()
        total = r[0] or 0
        win = r[1] or 0
        loss = r[2] or 0
        be = r[3] or 0
        pnl = r[4] or 0
        avg = r[5] or 0
        wr = (win / total * 100) if total > 0 else 0
        return f"""
📊 *ОТЧЁТ ЗА {today}*
━━━━━━━━━━━━━━━━━━━━━━━
📈 *Всего сделок:* {total}
🟢 *В прибыль:* {win}
🔴 *В убыток:* {loss}
⚪ *Безубыток:* {be}
🎯 *Winrate:* {wr:.1f}%
💰 *Общий P&L:* {pnl:.2f} USDT
📊 *Средний %:* {avg:.2f}%
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Не инвестрекомендация
"""
    except Exception as e:
        logger.error(f"❌ Ошибка get_daily_report: {e}")
        return "❌ Ошибка получения отчёта"
    finally:
        conn.close()

def get_all_trades():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT timestamp, ticker, side, amount_usdt, price, pnl, status FROM trades ORDER BY id DESC LIMIT 10')
        results = cursor.fetchall()
        if not results:
            return "📭 Нет сделок в истории"
        response = "📋 *ПОСЛЕДНИЕ СДЕЛКИ*\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        for r in results:
            ts, ticker, side, amount, price, pnl, status = r
            si = "🟢" if side == "buy" else "🔴"
            pi = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪")
            response += f"\n{si} {ticker} | {amount} USDT\n   Цена: {price:.2f} | {pi} P&L: {pnl:.2f}\n   {ts[:16]}\n"
        return response
    except Exception as e:
        logger.error(f"❌ Ошибка get_all_trades: {e}")
        return "❌ Ошибка получения истории"
    finally:
        conn.close()

init_db()

# ========== BYBIT ==========
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

# ========== ВАЛЮТНЫЕ ПАРЫ ==========
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

# ========== КЭШ ==========
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

# ========== ALPHA VANTAGE — ЗАГРУЗКА ДАННЫХ ==========
# Маппинг интервалов: наш формат -> Alpha Vantage формат
AV_INTERVAL_MAP = {
    '60m': '60min',
    '30m': '30min',
    '15m': '15min',
    '5m':  '5min',
    '1m':  '1min',
}

def fix_ticker(ticker):
    is_otc = False
    if ticker.lower().endswith('_otc'):
        is_otc = True
        ticker = ticker[:-4]
    ticker = ticker.upper().strip()
    return ticker, is_otc

def get_data_alpha_vantage(ticker, interval):
    """Загружает данные с Alpha Vantage. Возвращает pd.Series закрытий или None."""
    cache_key = f"av_{ticker}_{interval}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    av_interval = AV_INTERVAL_MAP.get(interval)
    if not av_interval:
        logger.warning(f"⚠️ Неизвестный интервал: {interval}")
        return None

    # Для форекс пар формируем from/to символы
    if len(ticker) == 6 and ticker.isalpha():
        from_sym = ticker[:3]
        to_sym = ticker[3:]
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=FX_INTRADAY"
            f"&from_symbol={from_sym}"
            f"&to_symbol={to_sym}"
            f"&interval={av_interval}"
            f"&outputsize=compact"
            f"&apikey={ALPHA_VANTAGE_KEY}"
        )
    else:
        # Для криптовалют и других активов
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=CRYPTO_INTRADAY"
            f"&symbol={ticker}"
            f"&market=USD"
            f"&interval={av_interval}"
            f"&outputsize=compact"
            f"&apikey={ALPHA_VANTAGE_KEY}"
        )

    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()

        # Ключ с данными в ответе
        time_key = None
        for k in data.keys():
            if 'Time Series' in k:
                time_key = k
                break

        if not time_key:
            logger.warning(f"⚠️ Alpha Vantage нет данных для {ticker} {interval}: {list(data.keys())}")
            return None

        ts_data = data[time_key]
        closes = []
        for dt_str in sorted(ts_data.keys()):
            try:
                close_val = float(ts_data[dt_str].get('4. close', 0))
                closes.append(close_val)
            except Exception:
                continue

        if len(closes) < 20:
            logger.warning(f"⚠️ Мало данных Alpha Vantage {ticker} {interval}: {len(closes)} свечей")
            return None

        series = pd.Series(closes)
        set_cached(cache_key, series)
        logger.info(f"✅ Alpha Vantage: {ticker} {interval} — {len(closes)} свечей")
        return series

    except Exception as e:
        logger.error(f"❌ Ошибка Alpha Vantage {ticker} {interval}: {e}")
        return None

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

# ========== АНАЛИЗ ТАЙМФРЕЙМОВ ==========
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
        logger.error(f"❌ Ошибка analyze_timeframe {timeframe_name}: {e}")
        return None

def multi_timeframe_analyst(ticker):
    base_ticker, is_otc = fix_ticker(ticker)

    # Alpha Vantage бесплатный план: 25 запросов/день, 5 запросов/мин
    # Берём только 3 таймфрейма чтобы не превысить лимит
    timeframes = [
        {'name': '1H',  'interval': '60m', 'weight': 3},
        {'name': '15M', 'interval': '15m', 'weight': 2},
        {'name': '5M',  'interval': '5m',  'weight': 1},
    ]

    results = {}
    total_trend_score = 0
    total_weight = 0

    for tf in timeframes:
        close = get_data_alpha_vantage(base_ticker, tf['interval'])
        analysis = analyze_timeframe(close, tf['name'])
        if analysis:
            results[tf['name']] = analysis
            total_trend_score += analysis['trend_score'] * tf['weight']
            total_weight += tf['weight']
        time.sleep(12)  # Alpha Vantage: макс 5 запросов в минуту на бесплатном плане

    if not results:
        return None, f"❌ Нет данных для {ticker}. Alpha Vantage не вернул данные.", None

    avg_trend = total_trend_score / total_weight if total_weight > 0 else 0

    if avg_trend > 0.3:
        overall_trend, overall_color = "📈 ТРЕНД ВВЕРХ", "🟢"
    elif avg_trend < -0.3:
        overall_trend, overall_color = "📉 ТРЕНД ВНИЗ", "🔴"
    else:
        overall_trend, overall_color = "🟡 ФЛЕТ", "🟡"

    signal = "⏸️ ВНЕ РЫНКА"
    if '5M' in results:
        m = results['5M']
        if avg_trend > 0.3 and m['entry_score'] > 0:
            signal = "🔵 СИЛЬНЫЙ BUY"
        elif avg_trend < -0.3 and m['entry_score'] < 0:
            signal = "🔴 СИЛЬНЫЙ SELL"
        elif avg_trend > 0.3:
            signal = "🟡 BUY (осторожно)"
        elif avg_trend < -0.3:
            signal = "🟡 SELL (осторожно)"

    tf_icons = []
    for tf_name in ['1H', '15M', '5M']:
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

def advanced_analysis(ticker):
    """Расширенный анализ на основе часового таймфрейма."""
    try:
        base_ticker, _ = fix_ticker(ticker)
        close = get_data_alpha_vantage(base_ticker, '60m')
        if close is None or len(close) < 20:
            return None

        ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        trend_slope = (float(ema50.iloc[-1]) - float(ema50.iloc[-2])) / float(ema50.iloc[-2]) * 100 if len(ema50) >= 2 else 0
        trend_val = max(0, min(100, 50 + trend_slope * 100))
        trend_signal = "BUY" if trend_val > 60 else ("SELL" if trend_val < 40 else "NEUTRAL")

        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi = 50 if pd.isna(rsi_val) else rsi_val
        impulse_signal = "BUY" if rsi < 30 else ("SELL" if rsi > 70 else "NEUTRAL")

        macd = ta.trend.MACD(close).macd()
        macd_sig = ta.trend.MACD(close).macd_signal()
        if len(macd) >= 2 and float(macd.iloc[-1]) > float(macd.iloc[-2]):
            reversal_signal, reversal_val = "BUY", 75
        elif len(macd) >= 2 and float(macd.iloc[-1]) < float(macd.iloc[-2]):
            reversal_signal, reversal_val = "SELL", 25
        else:
            reversal_signal, reversal_val = "NEUTRAL", 50

        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        current_price = float(close.iloc[-1])
        noise_val = abs((current_price - ema20) / ema20) * 100 if ema20 > 0 else 0
        noise_signal = "LOW VOL" if noise_val < 0.3 else ("HIGH VOL" if noise_val > 1 else "NORMAL")

        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_avg = bb.bollinger_mavg().iloc[-1]
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / bb_avg * 100 if bb_avg > 0 else 0
        market_val = min(100, bb_width * 10)
        market_signal = "TRENDING" if market_val > 30 else "RANGING"

        scores = [trend_signal, impulse_signal, reversal_signal]
        buy_count = sum(1 for s in scores if s == "BUY")
        sell_count = sum(1 for s in scores if s == "SELL")
        model_val = 50 + ((buy_count - sell_count) / len(scores)) * 50
        model_signal = "BUY" if model_val > 60 else ("SELL" if model_val < 40 else "NEUTRAL")

        params = [
            ("📈 Тренд (EMA50)", trend_val, trend_signal),
            ("⚡ Импульс (RSI)", rsi, impulse_signal),
            ("🔄 Разворот (MACD)", reversal_val, reversal_signal),
            ("🎛️ Шум/Фильтр", noise_val, noise_signal),
            ("🌡️ Настр. Рынка", market_val, market_signal),
            ("🎯 Модель", model_val, model_signal),
        ]
        buys = sum(1 for _, _, s in params if s == "BUY")
        sells = sum(1 for _, _, s in params if s == "SELL")
        neutrals = len(params) - buys - sells
        return params, buys, sells, neutrals

    except Exception as e:
        logger.error(f"❌ Ошибка advanced_analysis {ticker}: {e}")
        return None

# ========== НОВОСТИ ==========
def news_analyst(ticker):
    try:
        currency_code = ticker[:3].upper()
        keywords_map = {
            'EUR': ['euro', 'ecb', 'european'],
            'USD': ['dollar', 'fed', 'federal reserve'],
            'GBP': ['pound', 'sterling', 'boe'],
            'JPY': ['yen', 'boj'],
            'AUD': ['aussie', 'rba'],
            'CAD': ['loonie', 'canada', 'boc'],
            'CHF': ['franc', 'snb']
        }
        keywords = keywords_map.get(currency_code, [currency_code.lower()])
        feed = feedparser.parse(
            "https://www.investing.com/rss/news_FOREX.rss",
            request_headers={'User-Agent': 'Mozilla/5.0'}
        )
        if not feed.entries:
            return "НЕЙТРАЛЬНЫЕ", "HOLD", [], 0

        positive_words = ['surge', 'gain', 'rise', 'up', 'positive', 'strong', 'bullish']
        negative_words = ['drop', 'fall', 'decline', 'down', 'negative', 'weak', 'bearish']

        news_items = []
        for entry in feed.entries[:30]:
            title = entry.get('title', '')
            tl = title.lower()
            if any(kw in tl for kw in keywords):
                sentiment = sum(1 for w in positive_words if w in tl) - sum(1 for w in negative_words if w in tl)
                news_items.append({'title': title[:80], 'sentiment': sentiment})

        if not news_items:
            return "НЕЙТРАЛЬНЫЕ", "HOLD", [], 0

        news_items.sort(key=lambda x: abs(x['sentiment']), reverse=True)
        top = news_items[:3]
        total = sum(n['sentiment'] for n in top)

        if total > 0:
            return "🟢 ПОЗИТИВНЫЕ", "BUY", top, total
        elif total < 0:
            return "🔴 НЕГАТИВНЫЕ", "SELL", top, total
        return "⚪ НЕЙТРАЛЬНЫЕ", "HOLD", top, total
    except Exception as e:
        logger.error(f"❌ Ошибка news_analyst: {e}")
        return "⚪ НЕЙТРАЛЬНЫЕ", "HOLD", [], 0

# ========== ПОРТФЕЛЬНЫЙ МЕНЕДЖЕР ==========
def portfolio_manager(ticker):
    try:
        _, is_otc = fix_ticker(ticker)

        mtf_results, mtf_analysis, mtf_signal = multi_timeframe_analyst(ticker)
        if mtf_results is None:
            return mtf_analysis  # там уже текст ошибки

        # advanced_analysis использует кешированные данные из multi_timeframe_analyst
        advanced = advanced_analysis(ticker)
        adv_params, adv_buys, adv_sells, adv_neutrals = advanced if advanced else (None, 0, 0, 0)

        sentiment, _, news_list, _ = news_analyst(ticker)
        risk_level = "🟡 СРЕДНИЙ" if is_otc else "🟢 НИЗКИЙ"

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

        response = f"📊 *АНАЛИЗ: {ticker.upper()}*\n{mtf_analysis}"

        if adv_params:
            sig_map = {
                "BUY": "✅", "SELL": "❌", "NEUTRAL": "➖",
                "LOW VOL": "💤", "HIGH VOL": "🔥", "NORMAL": "➖",
                "TRENDING": "📈", "RANGING": "↔️"
            }
            response += "\n*ИНДИКАТОРЫ:*\n"
            for name, val, sig in adv_params:
                response += f"{sig_map.get(sig, '➖')} {name}: `{val:.1f}`\n"
            response += f"\n✅{adv_buys} ❌{adv_sells} ➖{adv_neutrals}\n"

        response += f"\n*Новости:* {sentiment}"
        response += f"\n*Риск:* {risk_level}"
        response += f"\n\n🎯 *ИТОГ: {final_signal}*"
        response += "\n\n⚠️ Не инвестрекомендация"
        if is_otc:
            response += "\n⚡ OTC — волатильность выше"
        return response

    except Exception as e:
        logger.error(f"❌ Ошибка portfolio_manager {ticker}: {e}")
        return f"❌ Ошибка анализа {ticker}. Попробуйте позже."

# ========== КРИПТО-ТРЕЙДИНГ ==========
def get_coin_balance(symbol):
    try:
        bal = exchange.fetch_balance()
        return bal[symbol]['free'] if symbol in bal else 0
    except Exception as e:
        logger.error(f"❌ Баланс {symbol}: {e}")
        return 0

def get_coin_price(symbol):
    try:
        t = exchange.fetch_ticker(f'{symbol}/USDT')
        return t['last']
    except Exception as e:
        logger.error(f"❌ Цена {symbol}: {e}")
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
        logger.error(f"❌ Покупка {symbol}: {e}")
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
        logger.error(f"❌ Продажа {symbol}: {e}")
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
    buttons = [telebot.types.InlineKeyboardButton(
        p.replace("_otc", "⚡") if "_otc" in p else p, callback_data=f"an_{p}"
    ) for p in cat["pairs"]]
    for i in range(0, len(buttons), 2):
        kb.row(buttons[i], buttons[i+1]) if i+1 < len(buttons) else kb.row(buttons[i])
    kb.add(
        telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=cat["back_to"]),
        telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, cat["title"]

# ========== ОБРАБОТЧИКИ ==========
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

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещён.")
        return
    uid = call.from_user.id
    data = call.data

    if data.startswith("an_") and is_flooding(uid):
        bot.answer_callback_query(call.id, "⏳ Подождите 10 сек. между запросами.", show_alert=True)
        return

    if data == "close_window":
        bot.edit_message_text("✅ *Окно закрыто*\n\nОтправьте /start для открытия меню.",
                              call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return

    if data == "back_to_menu":
        bot.edit_message_text("📋 *ГЛАВНОЕ МЕНЮ*", call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=create_main_menu())
        bot.answer_callback_query(call.id)
        return

    if data in ("menu_forex", "menu_forex_otc"):
        kb, title = create_category_menu("forex" if data == "menu_forex" else "forex_otc")
        if kb:
            bot.edit_message_text(title, call.message.chat.id, call.message.message_id,
                                  parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data in ("menu_crypto", "back_to_crypto"):
        bot.edit_message_text("📊 *КРИПТО-ТРЕЙДИНГ*", call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=create_crypto_menu())
        bot.answer_callback_query(call.id)
        return

    coin_map = {"crypto_btc":"BTC","crypto_eth":"ETH","crypto_sol":"SOL",
                "crypto_bnb":"BNB","crypto_xrp":"XRP","crypto_doge":"DOGE"}
    if data in coin_map:
        coin = coin_map[data]
        kb, title = create_coin_menu(coin, coin)
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=kb)
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
        bot.send_message(call.message.chat.id,
                         f"💰 *{coin} БАЛАНС*\n━━━━━━━━━━━━━━━\n{coin}: {balance:.8f}\nЦена: {price:.2f} USDT\nСтоимость: {value:.2f} USDT",
                         parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "crypto_report":
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"{get_daily_report()}\n\n{get_all_trades()}",
                         parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("auto_"):
        coin = data.replace("auto_", "")
        s = "🟢 Активна" if auto_trade_active[0] else "🔴 Остановлена"
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=f"crypto_{coin.lower()}"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id,
                         f"🤖 *Автоторговля {coin}*\n\nСтатус: {s}\n\n"
                         f"• RSI Buy < 30\n• RSI Sell > 70\n• Тейк: +3%\n• Стоп: -2%\n• Сумма: 10 USDT\n\n"
                         f"⚙️ /autostart | /autostop | /status\n\n⚠️ Реальный счёт Bybit!",
                         parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("an_"):
        ticker = data.replace("an_", "")
        bot.answer_callback_query(call.id, "🔍 Анализирую...")
        wait_msg = bot.send_message(call.message.chat.id,
                                    "⏳ *Анализирую...* Это займёт ~40 секунд из-за лимитов API.",
                                    parse_mode='Markdown')

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
                try:
                    bot.delete_message(chat_id, wait_id)
                except Exception:
                    pass
                bot.send_message(chat_id, result, parse_mode='Markdown', reply_markup=kb)
            except Exception as e:
                logger.error(f"❌ run_analysis {t}: {e}")
                try:
                    bot.edit_message_text("❌ Ошибка анализа. Попробуйте позже.", chat_id, wait_id)
                except Exception:
                    pass

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
    if is_flooding(message.from_user.id):
        bot.reply_to(message, "⏳ Подождите 10 секунд между запросами.")
        return
    wait_msg = bot.reply_to(message, "⏳ *Анализирую...* Это займёт ~40 секунд.", parse_mode='Markdown')

    def run_analysis(t=ticker, chat_id=message.chat.id, wait_id=wait_msg.message_id, orig=message):
        try:
            result = portfolio_manager(t)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("◀ В ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu"))
            try:
                bot.delete_message(chat_id, wait_id)
            except Exception:
                pass
            bot.reply_to(orig, result, parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            logger.error(f"❌ handle_message: {e}")
            try:
                bot.edit_message_text("❌ Ошибка анализа.", chat_id, wait_id)
            except Exception:
                pass

    threading.Thread(target=run_analysis, daemon=True).start()

# ========== АВТОТОРГОВЛЯ ==========
def auto_trade_loop():
    from ta.momentum import RSIIndicator
    coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
    settings = {c: {"rsi_buy": 30, "rsi_sell": 70, "take_profit": 3, "stop_loss": 2, "amount": 10} for c in coins}
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
                cfg = settings[coin]
                last = get_last_open_trade(coin)
                if last is None:
                    if rsi < cfg["rsi_buy"]:
                        r = buy_coin(coin, cfg["amount"])
                        logger.info(f"🟢 АВТО-ПОКУПКА {coin} RSI={rsi:.1f} | {r}")
                        time.sleep(2)
                else:
                    tid, qty, ep = last
                    pct = ((price - ep) / ep) * 100
                    if pct >= cfg["take_profit"]:
                        r = sell_coin(coin, qty)
                        logger.info(f"✅ ТЕЙК {coin}: +{pct:.2f}%")
                        time.sleep(2)
                    elif pct <= -cfg["stop_loss"]:
                        r = sell_coin(coin, qty)
                        logger.info(f"🛑 СТОП {coin}: {pct:.2f}%")
                        time.sleep(2)
                    elif rsi > cfg["rsi_sell"]:
                        r = sell_coin(coin, qty)
                        logger.info(f"🔴 АВТО-ПРОДАЖА {coin} RSI={rsi:.1f}")
                        time.sleep(2)
            except Exception as e:
                logger.error(f"⚠️ Автоторговля {coin}: {e}")
            time.sleep(1)
        time.sleep(60)

threading.Thread(target=auto_trade_loop, daemon=True).start()

# ========== WEBHOOK ==========
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
