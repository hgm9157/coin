from flask import Flask
import threading
import requests
import time
from datetime import datetime
import os

app = Flask(__name__)

# 사용자 설정 (1명만)
USER = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
    "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    "alerts_enabled": True,
    "black_list": set(),
}


INTERVAL = 60  # 주기 (초)
last_update_id = None


def seconds_to_hours(seconds):
    return round(seconds / 3600, 2)


def get_spot_contracts(symbol):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    url = "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=" + symbol
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        return data[0]["last"]
    except Exception as e:
        print(f"❌ 현물 오류: {symbol}", e)
    return None


def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{USER['bot_token']}/sendMessage"
    data = {"chat_id": USER["chat_id"], "text": message, "parse_mode": "HTML"}
    requests.post(url, data=data)


def get_gateio_usdt_futures_symbols():
    url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        contracts = response.json()
        symbols = [item["name"] for item in contracts if item["in_delisting"] == False]
        return symbols
    except Exception as e:
        print("❌ 오류 발생:", e)
        return []


def get_futures_contracts(symbol, apr):
    if symbol not in get_gateio_usdt_futures_symbols():
        return
    if not USER["alerts_enabled"]:
        return
    if symbol.replace("_USDT", "") in USER["black_list"]:
        return

    url = f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        spot_price = get_spot_contracts(symbol)
        future_price = float(data["last_price"])
        if spot_price is None or future_price is None:
            return
        diff = float(spot_price) - float(future_price)
        funding_interval_hr = seconds_to_hours(data["funding_interval"])
        funding_rate = round(float(data["funding_rate"]) * 100, 4)
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        daily_apr = float(apr) / 365
        funding_times_per_day = int(24 / funding_interval_hr)
        daily_funding_fee = -funding_rate * funding_times_per_day
        expected_daily_return = round(daily_apr - daily_funding_fee, 4)
        msg = (
            f"⏱ <b>{now}</b>\n"
            f"코인 : {symbol}\n"
            f"현물가격 : {spot_price}\n"
            f"선물가격 : {future_price}\n"
            f"현물-선물 갭 : {format(diff, '.6f')}\n"
            f"펀딩비계산주기 : {funding_interval_hr}시간\n"
            f"펀딩비율 : {funding_rate}%\n"
            f"APR : {apr}\n"
            f"일 APR (%) : {round(daily_apr, 4)}\n"
            f"하루 펀딩비 (%) : {round(daily_funding_fee, 4)}\n"
            f"기대수익(일%) : {expected_daily_return}"
        )
        send_telegram_message(msg)
    except Exception as e:
        print(f"❌ {symbol} 오류:", e)


def get_active_launchpool_aprs():
    url = "https://www.gate.io/apiw/v2/earn/launch-pool/project-list"
    params = {"page": 1, "pageSize": 50, "status": 0}
    result = {}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        projects = r.json()["data"]["list"]
        for item in projects:
            if item.get("project_state") != 1:
                continue
            coin_name = item.get("coin")
            for reward in item.get("reward_pools", []):
                if reward.get("coin") == coin_name:
                    apr = float(reward.get("rate_year", 0))
                    result[coin_name] = apr
                    break
    except Exception as e:
        print("❌ APR 불러오기 실패:", e)
    return result


def monitor_loop():
    while True:
        if not USER["alerts_enabled"]:
            time.sleep(INTERVAL)
            continue
        apr_dict = get_active_launchpool_aprs()
        for coin, apr in apr_dict.items():
            symbol = f"{coin}_USDT"
            get_futures_contracts(symbol, apr)
        print(f"⏳ {INTERVAL}초 후 반복...\n")
        time.sleep(INTERVAL)


def telegram_command_listener():
    global last_update_id
    url = f"https://api.telegram.org/bot{USER['bot_token']}/getUpdates"
    while True:
        try:
            params = {"timeout": 60}
            if last_update_id:
                params["offset"] = last_update_id + 1
            r = requests.get(url, params=params, timeout=65)
            updates = r.json()["result"]
            for update in updates:
                last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip().lower()
                chat_id = str(message.get("chat", {}).get("id"))
                if chat_id != USER["chat_id"]:
                    continue
                if text == "/":
                    msg = (
                        "<b>📘 명령어 안내</b>\n\n"
                        "▶ <b>중지</b>\n  - 현재 감시 및 알림을 일시 중지합니다.\n\n"
                        "▶ <b>다시실행</b>\n  - 감시를 다시 시작하고 텔레그램 알림을 재개합니다.\n\n"
                        "▶ <b>/감시제거 [코인]</b>\n  - 특정 코인을 감시 대상에서 제외합니다.\n  예: /감시제거 DMC\n\n"
                        "▶ <b>/감시복구 [코인]</b>\n  - 제외된 코인을 다시 감시 목록에 추가합니다.\n  예: /감시복구 DMC\n\n"
                        "▶ <b>/제외목록</b>\n  - 현재 제외된 코인 목록을 확인합니다."
                    )
                    send_telegram_message(msg)
                elif text == "중지":
                    USER["alerts_enabled"] = False
                    send_telegram_message("⛔ 알림이 중지되었습니다.")
                elif text == "다시실행":
                    USER["alerts_enabled"] = True
                    send_telegram_message("✅ 알림이 재개되었습니다.")
                elif text.startswith("/감시제거 "):
                    coin = text.split(" ")[1].upper()
                    USER["black_list"].add(coin)
                    send_telegram_message(
                        f"🛑 {coin} 감시 제외됨.\n📉 제외 목록: {', '.join(USER['black_list'])}"
                    )
                elif text.startswith("/감시복구 "):
                    coin = text.split(" ")[1].upper()
                    if coin in USER["black_list"]:
                        USER["black_list"].remove(coin)
                        send_telegram_message(
                            f"✅ {coin} 감시 재개됨.\n📉 제외 목록: {', '.join(USER['black_list'])}"
                        )
                    else:
                        send_telegram_message(f"⚠️ {coin} 은(는) 제외 목록에 없습니다.")
                elif text == "/제외목록":
                    if USER["black_list"]:
                        send_telegram_message(
                            "📋 제외된 코인 목록:\n" + ", ".join(USER["black_list"])
                        )
                    else:
                        send_telegram_message("📋 제외된 코인이 없습니다.")
        except Exception as e:
            print("❌ 명령 수신 오류:", e)
        time.sleep(5)


@app.route("/")
def index():
    return "Bot is running."


if __name__ == "__main__":
    threading.Thread(target=telegram_command_listener, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
