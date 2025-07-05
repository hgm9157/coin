from flask import Flask
import threading
import requests
import time
import traceback
from datetime import datetime, timedelta, timezone
import os

app = Flask(__name__)

USER = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
    "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    "alerts_enabled": True,
    "monitoring_coins": set(),
    "entry_coins": set(),
    "coin_list": set(),
    "interval": 30  # 감시 주기 (초)
}

last_update_id = None
openCondition = 0.4
closeCondition = 1


def is_funding_within_30min(funding_next_apply: int) -> bool:
    now_utc_ts = datetime.utcnow().timestamp()
    seconds_left = funding_next_apply - now_utc_ts
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"🕒 현재 KST: {now_kst} | 남은 초: {seconds_left:.2f}")
    return 0 < seconds_left <= 1800


def seconds_to_hours(seconds):
    return round(seconds / 3600, 2)


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
        symbols = [item["name"] for item in contracts if not item["in_delisting"]]
        return symbols
    except Exception as e:
        print("❌ 오류 발생:", e)
        return []


def get_futures_contracts(symbol, apr):
    global openCondition, closeCondition

    if symbol not in get_gateio_usdt_futures_symbols():
        return
    if not USER["alerts_enabled"]:
        return
    if symbol.replace("_USDT", "").upper() not in USER["monitoring_coins"]:
        return

    url = f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        spot_price = get_spot_contracts(symbol)
        future_price = float(data["last_price"])

        if spot_price is None or future_price is None or spot_price == 0 or future_price == 0:
            print(f"⚠️ {symbol} - 가격이 None 또는 0 (spot: {spot_price}, future: {future_price})")
            return

        spot_price = float(spot_price)
        future_price = float(future_price)
        diff = spot_price - future_price
        gap_pct = (diff / future_price) * 100

        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        funding_next_apply = float(data["funding_next_apply"])
        seconds_left = funding_next_apply - now.timestamp()
        time_left = str(timedelta(seconds=seconds_left))
        funding_interval_hr = seconds_to_hours(data["funding_interval"])

        if is_funding_within_30min(funding_next_apply):
            funding_rate = float(data["funding_rate"]) * 100
        else:
            funding_rate = get_gateio_latest_funding_rate(symbol) * 100

        daily_apr = float(apr) / 365
        funding_times_per_day = int(24 / funding_interval_hr)
        daily_funding_fee = -funding_rate * funding_times_per_day
        expected_daily_return = round(daily_apr - daily_funding_fee, 4)

        coin = symbol.replace("_USDT", "").upper()
        msg_type = None

        if coin in USER["monitoring_coins"]:
            if coin in USER["entry_coins"]:
                if closeCondition <= expected_daily_return:
                    msg_type = "🔻 포지션 정리 추천"
            elif expected_daily_return <= openCondition:
                msg_type = "🔺 포지션 진입 추천"

        if msg_type:
            msg = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{msg_type} - <b>{symbol}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💱 <b>현물가격</b> : {spot_price} USDT\n"
                f"📈 <b>선물가격</b> : {future_price} USDT\n"
                f"↔️ <b>현물-선물 갭</b> : {gap_pct:.6f}%\n\n"
                f"⏳ <b>펀딩 주기</b> : {funding_interval_hr}시간\n"
                f"💸 <b>펀딩비율</b> : {round(funding_rate, 4)}%\n"
                f"🕒 <b>다음 펀딩까지</b> : {time_left}\n\n"
                f"📌 <b>APR</b> : {apr}%\n"
                f"📅 <b>일간 APR</b> : {round(daily_apr, 4)}%\n"
                f"💰 <b>하루 펀딩비</b> : {round(daily_funding_fee, 4)}%\n"
                f"📊 <b>예상 일 수익률</b> : {expected_daily_return}%"
            )
            send_telegram_message(msg)

    except Exception as e:
        print(f"❌ {symbol} 오류:", e)
        traceback.print_exc()


def monitor_loop():
    while True:
        if not USER["alerts_enabled"]:
            time.sleep(USER["interval"])
            continue
        apr_dict = get_active_launchpool_aprs()
        USER["coin_list"] = apr_dict.keys()
        for coin, apr in apr_dict.items():
            symbol = f"{coin}_USDT"
            get_futures_contracts(symbol, apr)
        print(f"⏳ {USER['interval']}초 후 반복...\n")
        time.sleep(USER["interval"])


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


def startup_notify():
    msg = (
        "✅ <b>코인 감시 봇이 실행되었습니다.</b>\n"
        "명령어를 확인하려면 <b>/</b> 입력하세요."
    )
    send_telegram_message(msg)


def telegram_command_listener():
    global last_update_id, openCondition, closeCondition
    url = f"https://api.telegram.org/bot{USER['bot_token']}/getUpdates"
    while True:
        try:
            params = {"timeout": 60}
            if last_update_id:
                params["offset"] = last_update_id + 1
            r = requests.get(url, params=params, timeout=65)
            r.raise_for_status()
            updates = r.json()["result"]
            for update in updates:
                last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip().upper()
                print(f"✅ 명령어 입력 : {text}")
                chat_id = str(message.get("chat", {}).get("id"))
                if chat_id != USER["chat_id"]:
                    continue

                if text == "/":
                    send_telegram_message(
                        "<b>📘 명령어 안내</b>\n\n"
                        "▶ <b>중지</b> / <b>실행</b>\n"
                        "▶ <b>정보</b> / <b>초기화</b>\n"
                        "▶ <b>추가 [코인]</b> / <b>제거 [코인]</b>\n"
                        "▶ <b>진입 [코인]</b>\n"
                        "▶ <b>기준 [하한,상한]</b>\n"
                        "▶ <b>주기 [초]</b>\n"
                    )

                elif text == "중지":
                    USER["alerts_enabled"] = False
                    send_telegram_message("⛔ 알림이 중지되었습니다.")

                elif text == "실행":
                    USER["alerts_enabled"] = True
                    send_telegram_message("✅ 알림이 재개되었습니다.")

                elif text == "정보":
                    msg = (
                        "<b>📊 현재 상태 요약</b>\n\n"
                        "🔹 전체 감시 가능 코인 ({0}개)\n"
                        "{1}\n\n"
                        "🔸 모니터링 중 ({2}개)\n"
                        "{3}\n\n"
                        "🚀 진입 대상 ({4}개)\n"
                        "{5}"
                    ).format(
                        len(USER["coin_list"]),
                        ", ".join(USER["coin_list"]) or "없음",
                        len(USER["monitoring_coins"]),
                        ", ".join(USER["monitoring_coins"]) or "없음",
                        len(USER["entry_coins"]),
                        ", ".join(USER["entry_coins"]) or "없음",
                    )
                    send_telegram_message(msg)

                elif text.startswith("주기 "):
                    try:
                        seconds = int(text.split(" ")[1])
                        USER["interval"] = seconds
                        send_telegram_message(f"⏱️ 감시 주기: {seconds}초")
                    except:
                        send_telegram_message("⚠️ 포맷 오류: 주기 180")

                elif text.startswith("추가 "):
                    try:
                        coin = text.split(" ")[1].upper()
                        if coin not in USER["coin_list"]:
                            send_telegram_message("⚠️ 존재하지 않는 코인입니다.")
                            continue
                        USER["monitoring_coins"].add(coin)
                        send_telegram_message(f"✅ {coin} 감시 대상 추가됨.")
                    except:
                        send_telegram_message("⚠️ 포맷 오류: 추가 DMC")

                elif text.startswith("제거 "):
                    coin = text.split(" ")[1].upper()
                    USER["monitoring_coins"].discard(coin)
                    USER["entry_coins"].discard(coin)
                    send_telegram_message(f"🛑 {coin} 감시 제거됨.")

                elif text.startswith("진입 "):
                    try:
                        coin = text.split(" ")[1].upper()
                        if coin not in USER["coin_list"]:
                            send_telegram_message("⚠️ 존재하지 않는 코인입니다.")
                            continue
                        USER["entry_coins"].add(coin)
                        USER["monitoring_coins"].add(coin)
                        send_telegram_message(f"✅ {coin} 포지션 진입 대상 추가됨.")
                    except:
                        send_telegram_message("⚠️ 포맷 오류: 진입 DMC")

                elif text == "초기화":
                    USER["monitoring_coins"] = set()
                    USER["entry_coins"] = set()
                    send_telegram_message("✅ 초기화 완료")

                elif text.startswith("기준 "):
                    try:
                        parts = text.split(" ")[1]
                        openCondition, closeCondition = map(float, parts.split(","))
                        send_telegram_message(f"📈 기준 변경: {openCondition} ~ {closeCondition}")
                    except:
                        send_telegram_message("⚠️ 포맷 오류: 기준 0.4,1")
        except Exception as e:
            print("❌ 명령 수신 오류:", e)
            traceback.print_exc()
        time.sleep(3)


@app.route("/")
def index():
    return "Bot is running."


if __name__ == "__main__":
    threading.Thread(target=telegram_command_listener, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    startup_notify()
    app.run(host="0.0.0.0", port=8080)
