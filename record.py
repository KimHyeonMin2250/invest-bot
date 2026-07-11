"""
앱에서 '샀어요/건너뜀' 버튼을 누르면 실행되는 기록 스크립트
"""
import json, os, datetime

PF = "portfolio.json"

def load():
    with open(PF, encoding="utf-8") as f: return json.load(f)

def save(d):
    with open(PF, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def main():
    action_type = os.environ.get("ACTION_TYPE", "")
    ticker      = os.environ.get("TICKER", "")
    name        = os.environ.get("NAME", "")
    amount      = int(os.environ.get("AMOUNT", "0"))
    market      = os.environ.get("MARKET", "")
    today       = datetime.date.today().isoformat()

    print(f"기록: {action_type} / {ticker} / {amount}원 / {market}")

    data = load()

    # action_log에 기록
    data["action_log"] = data.get("action_log", [])
    data["action_log"].insert(0, {
        "type": action_type,
        "ticker": ticker,
        "name": name,
        "amount": amount,
        "market": market,
        "date": today,
        "settled": False
    })

    # 샀어요면 포트폴리오 반영
    if action_type == "bought" and amount > 0:
        # 예치금 차감
        data["deposit"] = max(0, data.get("deposit", 0) - amount)

        # 보유 종목 업데이트
        holdings = data.get("holdings", [])
        existing = next((h for h in holdings if h["ticker"] == ticker), None)
        if existing:
            existing["invested"] = existing.get("invested", 0) + amount
        else:
            holdings.append({"ticker": ticker, "name": name, "invested": amount})
        data["holdings"] = holdings

        # 거래 내역 추가
        data["transactions"] = data.get("transactions", [])
        data["transactions"].insert(0, {
            "type": "buy",
            "ticker": ticker,
            "name": name,
            "amount": amount,
            "date": today
        })

    save(data)
    print("저장 완료")

if __name__ == "__main__":
    main()
