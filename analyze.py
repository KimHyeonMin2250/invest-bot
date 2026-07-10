"""
매일 실행되는 하락일 매수 판단 엔진 (v3).

변경점:
- 금액이 아니라 '몇 주 살지'로 변환 (ISA 소수점 매수 불가 대응)
- 오늘 못 산 예산은 다음날로 이월 (carryover)
- ANTHROPIC_API_KEY가 있으면 Claude가 규칙 결과를 한 겹 검토
"""
import json
import os
import datetime
import calendar

import yfinance as yf
import requests

PORTFOLIO_FILE = "portfolio.json"


def load():
    with open(PORTFOLIO_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def trading_days_left(today):
    last_day = calendar.monthrange(today.year, today.month)[1]
    return max(sum(
        1 for d in range(today.day, last_day + 1)
        if datetime.date(today.year, today.month, d).weekday() < 5
    ), 1)


def fetch_fx():
    """원달러 환율. 실패 시 보수적 기본값."""
    try:
        hist = yf.Ticker("USDKRW=X").history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"  ! 환율 수집 실패: {e}")
    return 1400.0  # 기본값


def fetch_prices(targets, fx):
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
            # 개별주(달러)는 원화로 환산, ETF(원화)는 그대로
            krw_price = current * fx if info["type"] == "stock" else current
            prices[ticker] = {
                **info,
                "current": round(current, 2),
                "krw_price": round(krw_price),
                "change": round((current - prev) / prev * 100, 2),
                "ma5": round(float(close.tail(5).mean()), 2),
            }
        except Exception as e:
            print(f"  ! 시세 실패 {ticker}: {e}")
    return prices


def daily_budget(p, base_ratio, dip_ratio, budget_scale):
    # 이번 달 예산 비율만큼 종목 배정액을 조정 (예: 예산 절반이면 monthly도 절반)
    monthly = p["monthly"] * budget_scale
    base_buy = monthly * base_ratio / 20
    dip_pool = monthly * dip_ratio
    change = p["change"]
    is_etf = p["type"] == "etf"

    bonus, signal = 0, ""
    if is_etf:
        if change <= -3:   bonus, signal = dip_pool * 0.30, "🔴 큰 급락 (-3% 이하)"
        elif change <= -2: bonus, signal = dip_pool * 0.20, "🟠 급락 (-2%대)"
        elif change <= -1: bonus, signal = dip_pool * 0.10, "🟡 소폭 하락"
    else:
        if change <= -7:   bonus, signal = dip_pool * 0.35, "🔴 큰 급락 (-7% 이하)"
        elif change <= -5: bonus, signal = dip_pool * 0.22, "🟠 급락 (-5%대)"
        elif change <= -3: bonus, signal = dip_pool * 0.12, "🟡 하락 (-3%대)"

    return base_buy + bonus, signal


def decide(prices, cfg, carryover, budget_scale):
    actions = []
    new_carryover = {}
    for ticker, p in prices.items():
        base, signal = daily_budget(p, cfg["base_ratio"], cfg["dip_ratio"], budget_scale)
        prev_left = carryover.get(ticker, 0)
        available = base + prev_left
        price = p["krw_price"]   # 원화 기준 (개별주는 환산됨)

        if p["account"] == "ISA":
            # ISA: 소수점 매수 불가 → 정수 주, 잔액 이월
            shares = int(available // price)
            spent = shares * price
            left = available - spent
            frac_shares = shares                    # 정수
        else:
            # 일반계좌(토스): 소수점 매수 가능 → 배정액 전부 사용
            spent = round(base)                     # 오늘 배정액 그대로 매수
            left = 0                                # 이월 없음
            frac_shares = round(base / price, 4)    # 소수점 주식 수 (참고용)

        new_carryover[ticker] = round(left, 2)

        actions.append({
            "ticker": ticker,
            "name": p["name"],
            "account": p["account"],
            "current": p["current"],
            "krw_price": price,
            "change": p["change"],
            "signal": signal,
            "shares": frac_shares,
            "spent": round(spent),
            "carry": round(left),
            "available": round(available),
        })
    return actions, new_carryover


def claude_review(actions, prices):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        summary = "\n".join(
            f"- {a['name']}: 전일대비 {a['change']:+.1f}%, {a['shares']}주 매수 예정"
            for a in actions
        )
        prompt = (
            "당신은 신중한 투자 어드바이저입니다. 아래는 오늘 규칙 기반으로 계산된 "
            "매수 계획입니다. 시장 상황을 고려해 이 계획에 대한 2~3문장의 간단한 코멘트와 "
            "주의할 점을 한국어로 말해주세요. 매수 자체를 뒤집지는 말고, 참고 의견만 주세요.\n\n"
            f"{summary}"
        )
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"Claude 검토 실패(무시하고 진행): {e}")
        return None


def build_message(actions, today, ai_comment):
    total_spent = sum(a["spent"] for a in actions)
    buy_lines = [a for a in actions if a["spent"] > 0]
    skip_lines = [a for a in actions if a["spent"] == 0]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 오늘의 매수 ({today})"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"오늘 매수 합계 약 *{total_spent:,}원*"}
        ]},
        {"type": "divider"},
    ]

    for a in buy_lines:
        sig = f"\n{a['signal']}" if a["signal"] else ""
        if a["account"] == "general":
            # 일반계좌: 금액 기준 소수점 매수
            price_txt = f"${a['current']:,} (약 {a['krw_price']:,}원)"
            buy_txt = f"👉 *{a['spent']:,}원어치 매수* (약 {a['shares']}주, 소수점)"
            acct = "일반계좌·토스"
        else:
            # ISA: 정수 주
            price_txt = f"{a['current']:,}원"
            buy_txt = f"👉 *{a['shares']}주 매수* (약 {a['spent']:,}원)"
            acct = "ISA"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*{a['name']}* ({acct})\n"
            f"현재가 {price_txt} · 전일대비 {a['change']:+.2f}%{sig}\n"
            f"{buy_txt}"
        )}})

    if skip_lines:
        skip_txt = "\n".join(
            f"· {a['name']}: 예산 모으는 중 (이월 {a['carry']:,}원)"
            for a in skip_lines
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*오늘 스킵 (다음날 이월)*\n{skip_txt}"}})

    if ai_comment:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🤖 *Claude 코멘트*\n{ai_comment}"}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "카카오페이·토스에서 직접 매수 후 앱에 기록하세요. 참고용이며 투자 책임은 본인에게 있습니다."}]})
    return {"blocks": blocks}


def send_slack(message):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return
    r = requests.post(url, json=message, timeout=10)
    print(f"슬랙 발송: {r.status_code}")


def main():
    today = datetime.date.today()
    print(f"[{today}] 분석 시작")

    data = load()
    fx = fetch_fx()
    print(f"원달러 환율: {fx}")
    prices = fetch_prices(data["targets"], fx)
    if not prices:
        print("시세 수집 실패 — 종료")
        return

    carryover = data.get("carryover", {})
    # 이번 달 예산 비율 = 이번 달 실제 예산 / 기준 총액
    base_total = data.get("base_monthly_total", 2000000)
    cur_budget = data.get("current_month_budget", base_total)
    budget_scale = cur_budget / base_total if base_total else 1.0
    print(f"이번 달 예산 비율: {budget_scale:.2f} (예산 {cur_budget:,}원)")

    actions, new_carryover = decide(prices, data["pool_config"], carryover, budget_scale)
    ai_comment = claude_review(actions, prices)

    data["carryover"] = new_carryover
    data["last_analysis"] = {"date": today.isoformat(), "actions": actions, "ai_comment": ai_comment}
    save(data)

    send_slack(build_message(actions, today.isoformat(), ai_comment))
    print("완료")


if __name__ == "__main__":
    main()
