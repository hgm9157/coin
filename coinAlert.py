from flask import Flask
import threading
import requests
import time
from datetime import datetime, timedelta, timezone
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


def is_funding_within_30min(funding_next_apply: int) -> bool:
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST)
    now_ts = now_kst.timestamp()

    seconds_left = funding_next_apply - now_ts
    return 0 < seconds_left <= 1800  # 30분 = 1800초


def seconds_to_hours(seconds):
    return round(seconds / 3600, 2)


# ✅ Gate.io 펀딩비 조회 함수
def get_gateio_latest_funding_rate(contract: str) -> float:
    url = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
    headers = {"Accept": "application/json"}
    params = {"contract": contract, "limit": 1}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        return float(data[0]["r"]) if data else None
    except Exception as e:
        print(f"[{contract}] 펀딩비 조회 실패:", e)
        return None


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

        funding_next_apply = float(data["funding_next_apply"])  # 펀딩 남은시간

        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        now_ts = now.timestamp()

        seconds_left = int(funding_next_apply - now_ts)
        time_left = str(timedelta(seconds=seconds_left))

        spot_price = get_spot_contracts(symbol)
        future_price = float(data["last_price"])

        if spot_price is None:
            print(f"{symbol}: 현물가격 없음 → 패스")
            return

        if future_price is None:
            print(f"{symbol}: 선물가격 없음 → 패스")
            return

        diff = float(spot_price) - float(future_price)
        funding_interval_hr = seconds_to_hours(data["funding_interval"])

        # 펀딩 남은시간 30분이내인 경우 실시간
        if is_funding_within_30min(seconds_left):
            funding_rate = float(data["funding_rate"]) * 100
        # 더남은 경우 이전회차 펀딩비율
        else:
            funding_rate = get_gateio_latest_funding_rate(symbol) * 100

        # 계산
        daily_apr = float(apr) / 365
        funding_times_per_day = int(24 / funding_interval_hr)
        daily_funding_fee = -funding_rate * funding_times_per_day  # % 단위
        expected_daily_return = round(daily_apr - daily_funding_fee, 4)

        # 출력
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔔 <b>{symbol}</b> 알림\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💱 <b>현물가격</b> : {spot_price} USDT\n"
            f"📈 <b>선물가격</b> : {future_price} USDT\n"
            f"↔️ <b>현물-선물 갭</b> : {format(diff, '.6f')} USDT\n\n"
            f"⏳ <b>펀딩 주기</b> : {funding_interval_hr}시간\n"
            f"💸 <b>펀딩비율</b> : {round(funding_rate,4)}%\n"
            f"🕒 <b>다음 펀딩까지</b> : {time_left}\n\n"
            f"📌 <b>APR</b> : {apr}%\n"
            f"📅 <b>일간 APR</b> : {round(daily_apr, 4)}%\n"
            f"💰 <b>하루 펀딩비</b> : {round(daily_funding_fee, 4)}%\n"
            f"📊 <b>예상 일 수익률</b> : {expected_daily_return}%"
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
