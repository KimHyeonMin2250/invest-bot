"""
투자 알림 엔진 v5
- MARKET 환경변수로 ISA(국내ETF) / general(미국주식) 분리 알림
- Claude: 매일 코멘트 + 급락 성격 구분 + 월간 리뷰 + 매도 신호 해설
- 보유 종목 실시간 평가(전 종목) + Firestore 보유현황 연동 + 매도 타이밍 알림
"""
import json, os, datetime, calendar
import yfinance as yf
import requests

PF = "portfolio.json"
MARKET = os.environ.get("MARKET", "all")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-5"

# 앱과 동일한 방식(공개 REST, 인증 없음)으로 Firestore에서 보유 현황을 읽어옵니다.
FIREBASE_PROJECT = "invest-bot-6ab6e"
FIRESTORE_DOC_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
    "/databases/(default)/documents/portfolio/main"
)

DEFAULT_SELL_CONFIG = {
    "take_profit_pct": 30,       # 개별주 평단 대비 +N% 익절 제안
    "stop_loss_pct": -15,        # 개별주 평단 대비 -N% 손절 경고
    "ma20_overheat_pct": 15,     # 20일 이동평균 대비 +N% 과열 시 일부 매도 고려
}


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


def fetch_all_prices(targets, fx, fx_spread_pct=0.3):
    """실시간 평가용: 시장(ISA/general) 구분 없이 전체 종목 시세를 조회합니다.
    fx_spread_pct: 원화로 환전할 때 실제로 발생하는 스프레드(%)를 반영해 평가금액을 보수적으로 계산.
    """
    prices = {}
    sell_fx = fx * (1 - fx_spread_pct / 100)
    for tk, info in targets.items():
        try:
            h = yf.Ticker(tk).history(period="20d")
            if h.empty or len(h) < 2: continue
            c = h["Close"]
            cur = float(c.iloc[-1])
            is_stock = info["type"] == "stock"
            mult = sell_fx if is_stock else 1
            prices[tk] = {
                "name": info["name"], "account": info["account"], "type": info["type"],
                "current": round(cur, 2),
                "krw_price": round(cur * mult),
                "krw_price_gross": round(cur * fx) if is_stock else round(cur * mult),
                "ma20_krw": round(float(c.tail(20).mean()) * mult),
            }
        except Exception as e:
            print(f"평가 시세 실패 {tk}: {e}")
    return prices


def fs_value(v):
    """Firestore REST 응답의 typed value를 파이썬 값으로 변환."""
    if v is None: return None
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    if "arrayValue" in v:
        return [fs_value(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: fs_value(val) for k, val in v["mapValue"].get("fields", {}).items()}
    return None


def fetch_holdings():
    """Firestore에서 보유 종목(수량/투자원금) 읽기. 앱과 동일하게 인증 없는 공개 REST 접근."""
    return fetch_state()["holdings"]


def fetch_state():
    """Firestore 전체 상태(예치금 + 보유 종목) 읽기."""
    try:
        r = requests.get(FIRESTORE_DOC_URL, timeout=15)
        if r.status_code != 200:
            print(f"Firestore 읽기 실패: {r.status_code}")
            return {"deposit": 0, "holdings": []}
        fields = r.json().get("fields", {})
        deposit = fs_value(fields.get("deposit")) or 0
        raw_holdings = fields.get("holdings")
        holdings = fs_value(raw_holdings) or [] if raw_holdings else []
        return {"deposit": deposit, "holdings": holdings}
    except Exception as e:
        print(f"Firestore 읽기 예외: {e}")
        return {"deposit": 0, "holdings": []}


def eval_holdings(holdings, valuation):
    """보유 종목 평가금액 합계 (시세 없는 종목은 투자원금으로 대체)."""
    total = 0
    for h in holdings:
        v = valuation.get(h.get("ticker"))
        if v:
            total += (h.get("shares") or 0) * v["krw_price"]
        else:
            total += h.get("invested") or 0
    return total


def append_history(data, key, entry, cap=100):
    """알림/코멘트류를 최신 1건이 아니라 누적 기록으로 저장 (최근 cap개만 유지)."""
    hist = data.get(key, [])
    hist.append(entry)
    data[key] = hist[-cap:]


def upsert_history(data, today, total, deposit, eval_total, holdings=None):
    """일별 자산 스냅샷 기록 (하루 여러 번 실행돼도 그날 값은 마지막 것으로 덮어씀).
    holdings를 같이 저장해서 git 커밋 기록 자체가 Firestore 데이터의 백업 역할도 하도록 함.
    """
    hist = [h for h in data.get("asset_history", []) if h.get("date") != today.isoformat()]
    entry = {"date": today.isoformat(), "total": round(total),
              "deposit": round(deposit), "eval": round(eval_total)}
    if holdings is not None:
        entry["holdings_backup"] = [
            {"ticker": h.get("ticker"), "name": h.get("name"), "shares": h.get("shares"),
             "invested": h.get("invested"), "account": h.get("account")}
            for h in holdings
        ]
    hist.append(entry)
    hist.sort(key=lambda h: h["date"])
    data["asset_history"] = hist[-600:]  # 20개월 프로젝트 + 여유


def rebalance_suggestion(holdings, valuation, targets, threshold_pct=5):
    """목표 비중(월 계획 금액 기준) 대비 현재 비중이 threshold_pct% 이상 벌어진 종목에 대해
    매수/매도 제안 금액을 계산. Claude가 아니라 결정론적 계산 — Claude는 이 결과를 서술만 함.
    """
    total_planned = sum(v["monthly"] for v in targets.values())
    if total_planned == 0: return []
    cur_value = {}
    for h in holdings:
        tk = h.get("ticker")
        v = valuation.get(tk)
        if not v: continue
        cur_value[tk] = cur_value.get(tk, 0) + (h.get("shares") or 0) * v["krw_price"]
    total_now = sum(cur_value.values())
    if total_now <= 0: return []

    suggestions = []
    for tk, info in targets.items():
        target_w = info["monthly"] / total_planned
        cur_w = cur_value.get(tk, 0) / total_now
        diff_pct = (cur_w - target_w) * 100
        if abs(diff_pct) >= threshold_pct:
            diff_amount = round((target_w - cur_w) * total_now)
            suggestions.append({
                "ticker": tk, "name": info["name"],
                "current_weight": round(cur_w * 100, 1), "target_weight": round(target_w * 100, 1),
                "action": "buy" if diff_amount > 0 else "sell",
                "amount_krw": abs(diff_amount),
            })
    suggestions.sort(key=lambda s: -abs(s["current_weight"] - s["target_weight"]))
    return suggestions


def compute_pace(history, total, goal, start_date_str, end_date_str, today=None):
    """최근 자산 증가 속도로 목표 도달 예상일을 추정하고, 남은 기간 필요 월 투자금을 계산."""
    today = today or datetime.date.today()
    try:
        end = datetime.date.fromisoformat(end_date_str)
    except Exception:
        return {"status": "unknown"}

    months_remaining = max(0.1, (end.year - today.year) * 12 + (end.month - today.month)
                           + (end.day - today.day) / 30)
    remaining_amount = max(0, goal - total)
    required_monthly_remaining = round(remaining_amount / months_remaining)

    usable = sorted([h for h in history if h.get("total") is not None], key=lambda h: h["date"])
    daily_rate, projected_date, status, months_diff = None, None, "unknown", None
    if len(usable) >= 2:
        first, last = usable[0], usable[-1]
        try:
            d0 = datetime.date.fromisoformat(first["date"])
            d1 = datetime.date.fromisoformat(last["date"])
            days = (d1 - d0).days
            if days >= 7:
                daily_rate = (last["total"] - first["total"]) / days
        except Exception:
            pass

    if daily_rate and daily_rate > 0 and total < goal:
        days_needed = (goal - total) / daily_rate
        projected = today + datetime.timedelta(days=days_needed)
        projected_date = projected.isoformat()
        months_diff = round(((end.year - projected.year) * 12 + (end.month - projected.month))
                            + (end.day - projected.day) / 30, 1)
        status = "ahead" if projected < end else ("behind" if projected > end else "on_track")
    elif total >= goal:
        status, projected_date = "reached", today.isoformat()

    return {
        "status": status, "projected_date": projected_date, "months_diff": months_diff,
        "required_monthly_remaining": required_monthly_remaining,
        "daily_rate": round(daily_rate) if daily_rate else None,
    }


def sell_signals(holdings, valuation, cfg):
    """보유 종목별 매도 신호 계산 (익절/손절/과열)."""
    tp = cfg.get("take_profit_pct", 30)
    sl = cfg.get("stop_loss_pct", -15)
    heat = cfg.get("ma20_overheat_pct", 15)
    signals = []
    for h in holdings:
        tk = h.get("ticker")
        shares = h.get("shares") or 0
        invested = h.get("invested") or 0
        if not tk or shares <= 0 or invested <= 0: continue
        v = valuation.get(tk)
        if not v: continue
        value = shares * v["krw_price"]
        pl_pct = (value - invested) / invested * 100
        is_stock = v["type"] == "stock"
        msgs = []
        if is_stock and pl_pct >= tp:
            msgs.append(f"💰 수익 실현 제안 (평단 대비 {pl_pct:+.1f}%)")
        if is_stock and pl_pct <= sl:
            msgs.append(f"🔻 손절 라인 도달 (평단 대비 {pl_pct:+.1f}%)")
        ma20 = v.get("ma20_krw")
        if ma20 and v["krw_price"] >= ma20 * (1 + heat / 100):
            over = (v["krw_price"] / ma20 - 1) * 100
            msgs.append(f"🌡️ MA20 대비 {over:+.1f}% 과열, 일부 매도 고려")
        if msgs:
            signals.append({
                "ticker": tk, "name": h.get("name", tk),
                "pl_pct": round(pl_pct, 1), "value": round(value),
                "invested": invested, "messages": msgs,
            })
    return signals


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


def decide(prices, cfg, carry, scale, pending):
    acts, new_carry = [], dict(carry)
    for tk, p in prices.items():
        base, sig = daily_budget(p, cfg["base_ratio"], cfg["dip_ratio"], scale)
        skipped = pending.get(tk, 0)
        avail = base + carry.get(tk, 0) + skipped
        price = p["krw_price"]
        if p["account"] == "ISA":
            shares = int(avail // price)
            spent = shares * price
            left = avail - spent
        else:
            spent = round(base + skipped)
            shares = round(spent / price, 4)
            left = 0
        new_carry[tk] = round(left, 2)
        acts.append({"ticker": tk, "name": p["name"], "account": p["account"],
            "current": p["current"], "krw_price": price, "change": p["change"],
            "signal": sig, "shares": shares, "spent": round(spent),
            "carry": round(left), "available": round(avail),
            "skipped_added": round(skipped)})
    return acts, new_carry


def get_pending(data, market):
    log = data.get("action_log", [])
    key = "last_isa" if market == "isa" else "last_general"
    last = data.get(key) or {}
    last_acts = {a["ticker"]: a for a in last.get("actions", [])}

    pending = {}
    for entry in log:
        if entry.get("settled"): continue
        if entry.get("status") != "skipped": continue
        tk = entry["ticker"]
        if tk not in last_acts: continue
        pending[tk] = pending.get(tk, 0) + last_acts[tk].get("spent", 0)
        entry["settled"] = True

    for entry in log:
        if entry.get("status") == "bought":
            entry["settled"] = True

    return pending


def call_claude(prompt, max_tokens=500):
    if not API_KEY: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40)
        data = r.json()
        print(f"Claude 응답 구조: {list(data.keys())}")
        # 응답 구조 유연하게 처리
        if "content" in data:
            for block in data["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block["text"].strip()
        if "error" in data:
            print(f"Claude API 에러: {data['error']}")
        return None
    except Exception as e:
        print(f"Claude 실패: {e}")
        return None


def claude_comment(acts):
    s = "\n".join(f"- {a['name']}: 전일대비 {a['change']:+.1f}%, "
                  f"{'MA5 대비 하회' if a['signal'] else '정상 범위'}" for a in acts)
    return call_claude(
        "아래는 오늘 규칙 기반으로 계산된 매수 계획입니다. 2~3문장으로 짧게 "
        "시장 상황 코멘트와 주의점을 한국어로 말해주세요. 매수 결정을 뒤집지 말고 참고 의견만.\n\n" + s, 300)


def claude_dip_check(acts, prices):
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


def claude_monthly(holdings, targets, pace=None):
    if not holdings: return None
    total = sum(h.get("invested", 0) for h in holdings)
    if total == 0: return None
    lines = [f"- {h.get('name', h['ticker'])}: {h.get('invested',0):,}원 "
             f"({h.get('invested',0)/total*100:.0f}%)" for h in holdings]
    plan = [f"- {v['name']}: 목표 {v['monthly']/20000*100:.0f}%" for v in targets.values()]
    pace_line = ""
    if pace and pace.get("status") not in (None, "unknown"):
        label = {"ahead": "목표 기한보다 빠른 페이스", "behind": "목표 기한보다 느린 페이스",
                  "on_track": "목표 기한과 거의 일치", "reached": "이미 목표 달성"}.get(pace["status"], "")
        pace_line = (f"\n\n[목표 페이스]\n{label}. "
                     f"현재 속도면 {pace.get('projected_date','미상')}경 1억 도달 예상. "
                     f"기한(2028-03-31) 안에 맞추려면 남은 기간 월 {pace.get('required_monthly_remaining',0):,}원 필요.")
    return call_claude(
        "월간 포트폴리오 리뷰입니다. 현재 보유 비중이 계획 대비 얼마나 틀어졌는지, "
        "목표 페이스는 어떤지 점검하고 다음 달 조정이 필요하면 알려주세요. 유능한 자산관리사 "
        "톤으로 4~5문장, 한국어로.\n\n"
        f"[현재 비중]\n" + "\n".join(lines) + f"\n\n[계획 비중]\n" + "\n".join(plan) + pace_line, 600)


def claude_sell_note(signals):
    if not signals: return None
    lines = [f"- {s['name']}: 손익 {s['pl_pct']:+.1f}%, " + " / ".join(s["messages"]) for s in signals]
    return call_claude(
        "아래는 보유 종목 중 매도 신호가 발생한 종목들입니다. 종목별로 1~2문장씩, "
        "왜 이 신호가 떴는지와 지금 팔지 계속 들고갈지에 대한 균형잡힌 참고 의견을 "
        "한국어로 제안해주세요. 확정적으로 강요하지 말고, 확실하지 않으면 확실하지 않다고 "
        "솔직히 말하세요.\n\n" + "\n".join(lines), 500)


def build_msg(acts, today, market, comment, dip_note, monthly,
              signals=None, sell_note=None):
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
        add = f"\n↩️ 어제 건너뛴 {a['skipped_added']:,}원 합산됨" if a.get("skipped_added") else ""
        if a["account"] == "general":
            pt = f"${a['current']:,} (약 {a['krw_price']:,}원)"
            bt = f"👉 *{a['spent']:,}원어치* (약 {a['shares']}주, 소수점)"
        else:
            pt = f"{a['current']:,}원"
            bt = f"👉 *{a['shares']}주 매수* (약 {a['spent']:,}원)"
        b.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{a['name']}*\n현재가 {pt} · 전일대비 {a['change']:+.2f}%{sig}{add}\n{bt}"}})

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

    if signals:
        lines = [f"*{s['name']}* ({s['pl_pct']:+.1f}%)\n" + "\n".join(s["messages"]) for s in signals]
        b += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn",
            "text": "📤 *매도 신호*\n" + "\n\n".join(lines)}}]
    if sell_note:
        b += [{"type": "section", "text": {"type": "mrkdwn", "text": f"🤖 *매도 의견*\n{sell_note}"}}]

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
    pending = get_pending(data, MARKET)
    if pending:
        print(f"건너뛴 금액 이월: {pending}")
    acts, new_carry = decide(prices, data["pool_config"], carry, scale, pending)

    comment = claude_comment(acts)
    dip_note = claude_dip_check(acts, prices)

    # ── 보유 종목 실시간 평가 + 매도 신호 + 자산 히스토리/페이스 (시장 구분 없이 매번 갱신) ──
    valuation = fetch_all_prices(data["targets"], fx)
    state = fetch_state()
    holdings, deposit = state["holdings"], state["deposit"]
    eval_total = eval_holdings(holdings, valuation)
    total = deposit + eval_total

    cfg = data.get("sell_config") or DEFAULT_SELL_CONFIG
    signals = sell_signals(holdings, valuation, cfg)
    sell_note = claude_sell_note(signals)
    if sell_note:
        append_history(data, "sell_note_history", {"date": today.isoformat(), "text": sell_note, "signals": signals})

    upsert_history(data, today, total, deposit, eval_total, holdings)
    pace = compute_pace(data["asset_history"], total, data["goal"], data["start_date"],
                        data.get("end_date", "2028-03-31"), today)

    rebalance = rebalance_suggestion(holdings, valuation, data["targets"])

    monthly = claude_monthly(holdings, data["targets"], pace) if today.day == 1 else None
    if monthly:
        append_history(data, "monthly_review_history", {"date": today.isoformat(), "text": monthly})

    data["carryover"] = new_carry
    key = "last_isa" if MARKET == "isa" else "last_general"
    data[key] = {"date": today.isoformat(), "actions": acts,
                 "ai_comment": comment, "dip_note": dip_note}
    if comment or dip_note:
        append_history(data, "daily_comment_history",
                        {"date": today.isoformat(), "market": MARKET, "comment": comment, "dip_note": dip_note})
    data["fx"] = fx
    data["valuation"] = valuation
    data["valuation_date"] = datetime.datetime.now().isoformat(timespec="minutes")
    data["sell_signals"] = signals
    data["sell_note"] = sell_note
    data["sell_config"] = cfg
    data["goal_pace"] = pace
    data["rebalance_suggestions"] = rebalance
    save(data)

    send(build_msg(acts, today.isoformat(), MARKET, comment, dip_note, monthly,
                    signals, sell_note))
    print("완료")


if __name__ == "__main__":
    main()
