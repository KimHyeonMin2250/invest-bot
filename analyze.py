"""
매일 실행되는 하락일 매수 판단 엔진.
1. 종목 시세 수집 (야후 파이낸스)
2. 전일 대비 하락률 계산
3. 하락일 매수 규칙 적용 (기본풀 + 급락풀)
4. 슬랙으로 오늘의 매수 지시 발송
"""
import json
import os
import datetime
import calendar
import urllib.request

import yfinance as yf
import requests

PORTFOLIO_FILE = "portfolio.json"


# ── 데이터 입출력 ────────────────────────────────────────
def load():
    with open(PORTFOLIO_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 거래일 계산 (이번 달 남은 평일 수) ───────────────────
def trading_days_left(today):
    year, month = today.year, today.month
    last_day = calendar.monthrange(year, month)[1]
    days = 0
    for d in range(today.day, last_day + 1):
        wd = datetime.date(year, month, d).weekday()
        if wd < 5:  # 월~금
            days += 1
    return max(days, 1)


# ── 시세 수집 ────────────────────────────────────────────
def fetch_prices(targets):
    prices = {}
    for ticker, info in targets.items():
        try:
            hist = yf.Ticker(ticker).history(period="10d")
            if hist.empty or len(hist) < 2:
                print(f"  ! 데이터 부족: {ticker}")
                continue
            close = hist["Close"]
            current = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            change = (current - prev) / prev * 100
            ma5 = float(close.tail(5).mean())
            prices[ticker] = {
                "name": info["name"],
                "type": info["type"],
                "account": info["account"],
                "monthly": info["monthly"],
                "current": round(current, 2),
                "change": round(change, 2),
                "vs_ma5": round((current - ma5) / ma5 * 100, 2),
            }
        except Exception as e:
            print(f"  ! 시세 실패 {ticker}: {e}")
    return prices


# ── 하락일 매수 판단 ─────────────────────────────────────
def decide(prices, base_ratio, dip_ratio, days_left):
    """
    각 종목마다:
      기본 매수 = (월 배정액 * base_ratio) / 이번달 총 거래일 근사
      급락 보너스 = 하락폭에 따라 급락풀에서 차등 투입
    """
    actions = []
    for ticker, p in prices.items():
        monthly = p["monthly"]
        base_pool = monthly * base_ratio
        dip_pool = monthly * dip_ratio

        # 기본 매수 (매 거래일 꾸준히)
        base_buy = round(base_pool / 20 / 1000) * 1000  # 월 20거래일 가정, 천원 단위

        # 급락 보너스 판단
        change = p["change"]
        is_etf = p["type"] == "etf"
        bonus = 0
        signal = ""

        if is_etf:
            if change <= -3:
                bonus = round(dip_pool * 0.30 / 1000) * 1000
                signal = "🔴 큰 급락 (-3% 이하)"
            elif change <= -2:
                bonus = round(dip_pool * 0.20 / 1000) * 1000
                signal = "🟠 급락 (-2%대)"
            elif change <= -1:
                bonus = round(dip_pool * 0.10 / 1000) * 1000
                signal = "🟡 소폭 하락"
        else:  # 개별주는 변동성 커서 기준을 넓게
            if change <= -7:
                bonus = round(dip_pool * 0.35 / 1000) * 1000
                signal = "🔴 큰 급락 (-7% 이하)"
            elif change <= -5:
                bonus = round(dip_pool * 0.22 / 1000) * 1000
                signal = "🟠 급락 (-5%대)"
            elif change <= -3:
                bonus = round(dip_pool * 0.12 / 1000) * 1000
                signal = "🟡 하락 (-3%대)"

        total = base_buy + bonus
        actions.append({
            "ticker": ticker,
            "name": p["name"],
            "account": p["account"],
            "current": p["current"],
            "change": change,
            "base_buy": base_buy,
            "bonus": bonus,
            "total": total,
            "signal": signal,
        })
    return actions


# ── 슬랙 메시지 생성 & 발송 ──────────────────────────────
def build_slack_message(actions, today):
    total_today = sum(a["total"] for a in actions)

    lines = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 오늘의 매수 판단 ({today})"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"오늘 총 매수 권장액: *{total_today:,}원*"}
        ]},
        {"type": "divider"},
    ]

    for a in actions:
        chg = a["change"]
        chg_txt = f"{chg:+.2f}%"
        signal_txt = f"\n{a['signal']}" if a["signal"] else ""
        acct = "ISA" if a["account"] == "ISA" else "일반계좌"

        detail = f"기본 {a['base_buy']:,}원"
        if a["bonus"] > 0:
            detail += f" + 급락 보너스 {a['bonus']:,}원"

        lines.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{a['name']}* ({acct})\n"
                    f"현재가 {a['current']:,} · 전일대비 {chg_txt}{signal_txt}\n"
                    f"👉 *오늘 {a['total']:,}원 매수* ({detail})"
                )
            }
        })

    lines.append({"type": "divider"})
    lines.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "카카오페이·토스에서 직접 매수 후, 앱에서 기록해주세요. 매수 판단은 참고용이며 투자 책임은 본인에게 있습니다."
        }]
    })
    return {"blocks": lines}


def send_slack(message):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print("SLACK_WEBHOOK_URL 없음 — 콘솔에만 출력")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return
    resp = requests.post(url, json=message, timeout=10)
    print(f"슬랙 발송: {resp.status_code}")


# ── 메인 ─────────────────────────────────────────────────
def main():
    today = datetime.date.today()
    print(f"[{today}] 분석 시작")

    data = load()
    targets = data["targets"]
    cfg = data["pool_config"]

    prices = fetch_prices(targets)
    if not prices:
        print("시세를 하나도 못 가져옴 — 종료")
        return

    days_left = trading_days_left(today)
    actions = decide(prices, cfg["base_ratio"], cfg["dip_ratio"], days_left)

    # 오늘 판단 기록
    data["last_analysis"] = {
        "date": today.isoformat(),
        "actions": actions,
    }
    save(data)

    message = build_slack_message(actions, today.isoformat())
    send_slack(message)
    print("완료")


if __name__ == "__main__":
    main()
