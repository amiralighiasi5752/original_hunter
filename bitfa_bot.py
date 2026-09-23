# -*- coding: utf-8 -*-
"""
BITFA Coin-Scoring Bot -- ماژول ۵: ربات هشدار/تحلیل تلگرام

نحوه‌ی کار: توی تلگرام به این بات پیام بده:
    /check ONDO
جواب می‌گیری: امتیاز اطمینان، ریزه‌کاری هر پلاگین، قیمت ورود، تارگت، استاپ،
و مبلغ پیشنهادی از سرمایه‌ات.

اجرا کاملاً دستیه -- بات هیچ معامله‌ای انجام نمی‌ده، فقط تحلیل می‌کنه.
هر درخواست به‌صورت خودکار در signals_log.csv ثبت می‌شه تا بعداً بتونیم
نتیجه‌ی واقعی‌ش رو برای کالیبراسیون بعدی اضافه کنیم.

نصب پیش‌نیاز:
    pip install pyTelegramBotAPI requests pandas

قبل از اجرا:
    1) توی تلگرام به @BotFather پیام بده و /newbot بزن تا یه توکن بگیری
    2) توکن رو جای BOT_TOKEN پایین بذار (یا در متغیر محیطی BITFA_BOT_TOKEN)
    3) python bitfa_bot.py
"""

import os
import csv
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import telebot

# ------------------------------------------------------------------
# تنظیمات
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BITFA_BOT_TOKEN", "PUT-YOUR-TOKEN-HERE")
CAPITAL_USD = 64.0          # سرمایه‌ی فعلی -- دستی به‌روز کن وقتی تغییر کرد
LOG_FILE = "signals_log.csv"

bot = telebot.TeleBot(BOT_TOKEN)


# ------------------------------------------------------------------
# گرفتن دیتا از MEXC (چون Binance از ایران مسدوده)
# ------------------------------------------------------------------
def get_mexc_klines(symbol_pair, limit=30):
    url = "https://api.mexc.com/api/v3/klines"
    params = {"symbol": symbol_pair, "interval": "1d", "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
    except Exception as e:
        return None, f"خطای اتصال: {e}"
    if r.status_code != 200:
        return None, f"خطای MEXC ({symbol_pair}): status={r.status_code} | {r.text[:150]}"
    rows = r.json()
    if not rows:
        return None, "دیتایی برنگشت."
    n_cols = len(rows[0])
    columns = (["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "trades", "taker_base",
                "taker_quote", "ignore"])[:n_cols]
    df = pd.DataFrame(rows, columns=columns)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df, None


# ------------------------------------------------------------------
# پلاگین‌های امتیازدهی -- همون منطقی که در Colab کالیبره کردیم
# ------------------------------------------------------------------
def plugin_trend_break(ohlcv: pd.DataFrame) -> float:
    if ohlcv is None or len(ohlcv) < 5:
        return 0.5
    recent = ohlcv.iloc[:-1]
    resistance = recent["high"].max()
    current_price = ohlcv["close"].iloc[-1]
    if current_price >= resistance:
        return 1.0
    ratio = current_price / resistance
    return max(0.0, min(1.0, (ratio - 0.9) / 0.1))


def plugin_volume_spike(ohlcv: pd.DataFrame) -> float:
    if ohlcv is None or len(ohlcv) < 10:
        return 0.5
    avg_vol = ohlcv["volume"].iloc[-11:-1].mean()
    last_vol = ohlcv["volume"].iloc[-1]
    if avg_vol == 0:
        return 0.5
    ratio = last_vol / avg_vol
    return max(0.0, min(1.0, ratio / 3))


def compute_rsi(closes: pd.Series, period=14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]


def plugin_rsi_momentum(ohlcv: pd.DataFrame) -> float:
    """کالیبره‌شده روی داده‌ی واقعی BITFA: RSI بالا = مومنتوم قوی، نه اشباع خرید."""
    if ohlcv is None or len(ohlcv) < 15:
        return 0.5
    rsi = compute_rsi(ohlcv["close"])
    if pd.isna(rsi):
        return 0.5
    return max(0.0, min(1.0, (rsi - 30) / 50))


# وزن‌های نهایی که در کالیبراسیون Colab به‌دست اومد (رابطه‌ی سطلی: پایین=~3%, متوسط=~35%, بالا=~50%)
PLUGINS = {
    "trend_break": (plugin_trend_break, 0.3),
    "volume_spike": (plugin_volume_spike, 0.2),
    "rsi": (plugin_rsi_momentum, 0.5),
    # اینجا بعداً FVG / ICT / Ichimoku / Smart Money اضافه می‌شه:
    # "fvg": (plugin_fvg, 0.0),
}


def score_ohlcv(ohlcv):
    breakdown, total, total_weight = {}, 0.0, 0.0
    for name, (func, weight) in PLUGINS.items():
        s = func(ohlcv)
        breakdown[name] = round(s, 2)
        total += s * weight
        total_weight += weight
    confidence = round((total / total_weight) * 100, 1) if total_weight else 0.0
    return confidence, breakdown


def position_size_pct(confidence_pct: float) -> float:
    if confidence_pct < 40:
        return 0.0
    if confidence_pct < 60:
        return 0.15
    return 0.30


# ------------------------------------------------------------------
# ثبت لاگ (برای کالیبراسیون بعدی)
# ------------------------------------------------------------------
def log_signal(symbol, confidence, entry, target, stop, size_usd):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp_utc", "symbol", "confidence_pct", "entry_price",
                        "target_price", "stop_price", "suggested_size_usd",
                        "actual_outcome_pct"])  # ستون آخر رو بعداً دستی پر کن
        w.writerow([
            datetime.now(timezone.utc).isoformat(), symbol, confidence,
            entry, target, stop, size_usd, ""
        ])


# ------------------------------------------------------------------
# دستور اصلی: /check SYMBOL
# ------------------------------------------------------------------
@bot.message_handler(commands=["check"])
def handle_check(message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "فرمت درست: /check ONDO")
        return

    symbol = parts[1].upper().replace("USDT", "")
    pair = symbol + "USDT"

    ohlcv, err = get_mexc_klines(pair)
    if err:
        bot.reply_to(message, f"❌ {symbol}: {err}")
        return

    confidence, breakdown = score_ohlcv(ohlcv)
    entry = ohlcv["close"].iloc[-1]
    target = round(entry * 1.5, 6)
    stop = round(entry * 0.85, 6)
    size_pct = position_size_pct(confidence)
    size_usd = round(CAPITAL_USD * size_pct, 2)

    if size_pct == 0:
        verdict = "🔴 رد -- امتیاز خیلی پایینه"
    elif size_pct < 0.3:
        verdict = "🟡 احتیاط -- پوزیشن کوچیک"
    else:
        verdict = "🟢 سیگنال قوی"

    reply = (
        f"*{symbol}*  |  امتیاز اطمینان: *{confidence}%*\n"
        f"{verdict}\n\n"
        f"قیمت فعلی: `{entry}`\n"
        f"تارگت (+۵۰٪): `{target}`\n"
        f"استاپ (-۱۵٪): `{stop}`\n"
        f"مبلغ پیشنهادی: *${size_usd}* از ${CAPITAL_USD}\n\n"
        f"جزئیات پلاگین‌ها:\n"
        f"- شکست مقاومت: {breakdown.get('trend_break')}\n"
        f"- حجم: {breakdown.get('volume_spike')}\n"
        f"- RSI: {breakdown.get('rsi')}"
    )
    bot.reply_to(message, reply, parse_mode="Markdown")
    log_signal(symbol, confidence, entry, target, stop, size_usd)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(
        message,
        "سلام! برای تحلیل یه ارز بنویس:\n/check ONDO\n\n"
        "این فقط تحلیل و هشداره -- خودت باید دستی معامله کنی."
    )


if __name__ == "__main__":
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        print("⚠️  اول توکن بات رو تنظیم کن (متغیر BOT_TOKEN یا BITFA_BOT_TOKEN).")
    else:
        print("بات روشن شد. توی تلگرام /check SYMBOL بفرست.")
        bot.infinity_polling()
