"""
투자 알림 엔진 v4
- MARKET 환경변수로 ISA(국내ETF) / general(미국주식) 분리 알림
- Claude: 매일 코멘트 + 급락 성격 구분 + 월간 리뷰
"""
import json, os, datetime, calendar
import yfinance as yf
import requests

PF = "portfolio.json"
MARKET = os.environ.get("MARKET", "all")          # isa | general | all
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-5"


def load():
    with open(PF, encoding="utf-8") as f: return json.load(f)

def save(d):
    with open(PF, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=2)


def fetch_fx():
    try:
        h = yf.Ticker("USDKRW=X").history(period="5d")
        if not h.empty: return round(float(h["Close"].iloc[-1]), 2)
    except Exception as e: print(f"환율 실패: {e}")
    return 1400.0


def fetch_prices(targets, fx, market):
    prices = {}
    for tk, info in targets.items():
        # 시장 필터
        if market == "isa" and info["account"] != "ISA": continue
        if market == "general" and info["account"] != "general": continue
        try:
            h = yf.Ticker(tk).history(period="20d")
            if h.empty or len(h) < 2: continue
            c = h["Close"]
            cur, prev = float(c.iloc[-1]), float(c.iloc[-2])
            krw = cur * fx if info["type"] == "stock" else cur
            prices[tk] = {**info,
                "current": round(cur, 2), "krw_price": round(krw),
                "change": round((cur - prev) / prev * 100, 2),
                "ma5": round(float(c.tail(5).mean()), 2),
                "ma20": round(float(c.tail(20).mean()), 2),
            }
        except Exception as e: print(f"시세 실패 {tk}: {e}")
    return prices


def daily_budget(p, base_r, dip_r, scale):
    monthly = p["monthly"] * scale
    base = monthly * base_r / 20
    pool = monthly * dip_r
    ch, is_etf = p["change"], p["type"] == "etf"
    bonus, sig = 0, ""
    if is_etf:
        if ch <= -3:   bonus, sig = pool * .30, "🔴 큰 급락 (-3% 이하)"
        elif ch <= -2: bonus, sig = pool * .20, "🟠 급락 (-2%대)"
        elif ch <= -1: bonus, sig = pool * .10, "🟡 소폭 하락"
    else:
        if ch <= -7:   bonus, sig = pool * .35, "🔴 큰 급락 (-7% 이하)"
        elif ch <= -5: bonus, sig = pool * .22, "🟠 급락 (-5%대)"
        elif ch <= -3: bonus, sig = pool * .12, "🟡 하락 (-3%대)"
    return base + bonus, sig


def decide(prices, cfg, carry, scale):
    acts, new_carry = [], dict(carry)
    for tk, p in prices.items():
        base, sig = daily_budget(p, cfg["base_ratio"], cfg["dip_ratio"], scale)
        avail = base + carry.get(tk, 0)
        price = p["krw_price"]
        if p["account"] == "ISA":
            shares = int(avail // price)
            spent = shares * price
            left = avail - spent
        else:
            shares = round(base / price, 4)
            spent = round(base)
            left = 0
        new_carry[tk] = round(left, 2)
        acts.append({"ticker": tk, "name": p["name"], "account": p["account"],
            "current": p["current"], "krw_price": price, "change": p["change"],
            "signal": sig, "shares": shares, "spent": round(spent),
            "carry": round(left), "available": round(avail)})
    return acts, new_carry


# ── Claude ───────────────────────────────────────────────
def call_claude(prompt, max_tokens=500):
    if not API_KEY: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40)
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Claude 실패: {e}")
        return None


def claude_comment(acts):
    """기능1: 매일 코멘트"""
    s = "\n".join(f"- {a['name']}: 전일대비 {a['change']:+.1f}%, "
                  f"{'MA5 대비 하회' if a['signal'] else '정상 범위'}" for a in acts)
    return call_claude(
        "아래는 오늘 규칙 기반으로 계산된 매수 계획입니다. 2~3문장으로 짧게 "
        "시장 상황 코멘트와 주의점을 한국어로 말해주세요. 매수 결정을 뒤집지 말고 참고 의견만.\n\n" + s, 300)


def claude_dip_check(acts, prices):
    """기능2: 급락 성격 구분 — 급락 신호가 있을 때만 호출"""
    dips = [a for a in acts if a["signal"]]
    if not dips: return None
    lines = []
    for a in dips:
        p = prices[a["ticker"]]
        lines.append(f"- {a['name']}: 전일대비 {a['change']:+.1f}%, "
                     f"현재가 {p['current']}, 5일평균 {p['ma5']}, 20일평균 {p['ma20']}")
    return call_claude(
        "아래 종목들이 오늘 하락했습니다. 각 하락이 (a) 시장 전체 조정인지 "
        "(b) 개별 종목 악재인지 판단해주세요. 개별 악재로 의심되면 '매수 보류 권장'이라고 "
        "명확히 경고하고, 단순 조정이면 '저가 매수 기회'로 안내하세요. "
        "확실하지 않으면 확실하지 않다고 솔직히 말하세요. 종목당 1~2문장, 한국어로.\n\n"
        + "\n".join(lines), 500)


def claude_monthly(data):
    """기능3: 월간 리뷰 — 매달 1일에만"""
    holdings = data.get("holdings", [])
    if not holdings: return None
    total = sum(h.get("invested", 0) for h in holdings)
    if total == 0: return None
    lines = [f"- {h.get('name', h['ticker'])}: {h.get('invested',0):,}원 "
             f"({h.get('invested',0)/total*100:.0f}%)" for h in holdings]
    targets = data["targets"]
    plan = [f"- {v['name']}: 목표 {v['monthly']/20000*100:.0f}%" for v in targets.values()]
    return call_claude(
        "월간 포트폴리오 리뷰입니다. 현재 보유 비중이 계획 대비 얼마나 틀어졌는지 "
        "점검하고, 다음 달 조정이 필요하면 알려주세요. 3~4문장, 한국어로.\n\n"
        f"[현재 비중]\n" + "\n".join(lines) + f"\n\n[계획 비중]\n" + "\n".join(plan), 500)


# ── 슬랙 ─────────────────────────────────────────────────
def build_msg(acts, today, market, comment, dip_note, monthly):
    total = sum(a["spent"] for a in acts)
    buys = [a for a in acts if a["spent"] > 0]
    skips = [a for a in acts if a["spent"] == 0]

    if market == "isa":
        title, hint = "🇰🇷 ISA 매수 (국내장)", "카카오페이 ISA에서 매수하세요"
    else:
        title, hint = "🇺🇸 일반계좌 매수 (미국장)", "토스에서 소수점 매수하세요"

    b = [{"type": "header", "text": {"type": "plain_text", "text": f"{title} · {today}"}},
         {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"오늘 매수 합계 약 *{total:,}원* · {hint}"}]},
         {"type": "divider"}]

    for a in buys:
        sig = f"\n{a['signal']}" if a["signal"] else ""
        if a["account"] == "general":
            pt = f"${a['current']:,} (약 {a['krw_price']:,}원)"
            bt = f"👉 *{a['spent']:,}원어치* (약 {a['shares']}주, 소수점)"
        else:
            pt = f"{a['current']:,}원"
            bt = f"👉 *{a['shares']}주 매수* (약 {a['spent']:,}원)"
        b.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{a['name']}*\n현재가 {pt} · 전일대비 {a['change']:+.2f}%{sig}\n{bt}"}})

    if skips:
        b.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*예산 모으는 중*\n" + "\n".join(
                f"· {a['name']}: 이월 {a['carry']:,}원" for a in skips)}})

    if dip_note:
        b += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn",
            "text": f"⚠️ *급락 분석*\n{dip_note}"}}]
    if comment:
        b += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn",
            "text": f"🤖 *Claude 코멘트*\n{comment}"}}]
    if monthly:
        b += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn",
            "text": f"📅 *월간 리뷰*\n{monthly}"}}]

    b += [{"type": "divider"}, {"type": "context", "elements": [{"type": "mrkdwn",
        "text": "매수 후 앱에서 '샀어요' 버튼을 눌러주세요. 참고용이며 투자 책임은 본인에게 있습니다."}]}]
    return {"blocks": b}


def send(msg):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print(json.dumps(msg, ensure_ascii=False, indent=2)); return
    print(f"슬랙: {requests.post(url, json=msg, timeout=10).status_code}")


def main():
    today = datetime.date.today()
    print(f"[{today}] market={MARKET}")
    data = load()
    fx = fetch_fx()
    prices = fetch_prices(data["targets"], fx, MARKET)
    if not prices:
        print("해당 시장 종목 없음"); return

    base_total = data.get("base_monthly_total", 2000000)
    budget = data.get("current_month_budget", base_total)
    scale = budget / base_total if base_total else 1.0

    carry = data.get("carryover", {})
    acts, new_carry = decide(prices, data["pool_config"], carry, scale)

    comment = claude_comment(acts)
    dip_note = claude_dip_check(acts, prices)
    monthly = claude_monthly(data) if today.day == 1 else None

    data["carryover"] = new_carry
    key = "last_isa" if MARKET == "isa" else "last_general"
    data[key] = {"date": today.isoformat(), "actions": acts,
                 "ai_comment": comment, "dip_note": dip_note}
    data["fx"] = fx
    save(data)

    send(build_msg(acts, today.isoformat(), MARKET, comment, dip_note, monthly))
    print("완료")


if __name__ == "__main__":
    main()
