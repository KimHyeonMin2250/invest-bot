"""
주간 리뷰 (매주 일요일 저녁)
- 보유 비중, 목표 페이스를 종합해 Claude가 자산관리사 톤으로 요약
- Claude 호출 + 슬랙 발송 있음. daily.yml(평일 전용)과 독립적으로 동작해서
  월요일 없이도, 1일이 주말이어도 정기적으로 자산 상태를 짚어줌
"""
import datetime
import analyze as a


def pace_summary_line(pace):
    if not pace or pace.get("status") in (None, "unknown"):
        return "아직 페이스를 계산할 데이터가 부족합니다 (최소 일주일치 자산 기록 필요)."
    if pace["status"] == "reached":
        return "이미 목표 금액에 도달했습니다."
    label = {"ahead": "목표 기한보다 빠른 페이스입니다", "behind": "목표 기한보다 느린 페이스입니다",
              "on_track": "목표 기한과 거의 맞아떨어지는 페이스입니다"}.get(pace["status"], "")
    return (f"{label}. 현재 속도면 {pace.get('projected_date')}경 1억 도달 예상이고, "
            f"기한(2028-03-31) 안에 맞추려면 남은 기간 동안 월 {pace.get('required_monthly_remaining',0):,}원이 필요합니다.")


def claude_weekly(holdings, pace, history):
    total = sum(h.get("invested", 0) for h in holdings)
    if holdings and total > 0:
        weights = "\n".join(f"- {h.get('name', h['ticker'])}: {h.get('invested',0):,}원 "
                            f"({h.get('invested',0)/total*100:.0f}%)" for h in holdings)
    else:
        weights = "아직 보유 종목이 없습니다 (시작일 이전이거나 매수 전)."

    week_change = ""
    if len(history) >= 2:
        target = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        past = [h for h in history if h["date"] <= target]
        if past:
            diff = history[-1]["total"] - past[-1]["total"]
            week_change = f"\n\n[최근 1주 자산 변화]\n{diff:+,}원"

    prompt = (
        "당신은 든든하고 유능한 전담 자산관리사입니다. 아래 정보로 이번 주 리뷰를 작성해주세요. "
        "차갑지 않고 신뢰감 있는 톤으로, 확정적으로 강요하지 말고 참고 의견으로 말하세요. "
        "확실하지 않은 부분은 확실하지 않다고 솔직히 말하세요. 4~6문장, 한국어로.\n\n"
        f"[현재 보유 비중]\n{weights}\n\n"
        f"[목표 페이스]\n{pace_summary_line(pace)}"
        f"{week_change}"
    )
    return a.call_claude(prompt, 700)


def main():
    data = a.load()
    today = datetime.date.today()
    fx = a.fetch_fx()
    valuation = a.fetch_all_prices(data["targets"], fx)
    state = a.fetch_state()
    holdings, deposit = state["holdings"], state["deposit"]
    eval_total = a.eval_holdings(holdings, valuation)
    total = deposit + eval_total

    a.upsert_history(data, today, total, deposit, eval_total)
    pace = a.compute_pace(data["asset_history"], total, data["goal"], data["start_date"],
                          data.get("end_date", "2028-03-31"), today)
    review = claude_weekly(holdings, pace, data["asset_history"])

    data["fx"] = fx
    data["valuation"] = valuation
    data["valuation_date"] = datetime.datetime.now().isoformat(timespec="minutes")
    data["goal_pace"] = pace
    data["weekly_review"] = {"date": today.isoformat(), "text": review}
    a.save(data)

    if review:
        a.send({"blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📅 주간 리뷰 · {today.isoformat()}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": review}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": "참고용이며 투자 책임은 본인에게 있습니다."}]},
        ]})
    print("주간 리뷰 완료")


if __name__ == "__main__":
    main()
