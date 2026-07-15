"""
실시간 시세 갱신 전용 스크립트
- 장중에 짧은 주기(예: 15분)로 돌려서 portfolio.json의 valuation을 자주 갱신
- Claude 호출 없음, 슬랙 알림 없음 (매수 판단용 daily.yml과 완전히 분리)
- analyze.py의 시세 조회/보유현황 조회/매도신호 로직을 그대로 재사용
"""
import datetime
import analyze as a


def main():
    data = a.load()
    fx = a.fetch_fx()
    valuation = a.fetch_all_prices(data["targets"], fx)
    if not valuation:
        print("시세 조회 실패, 종료")
        return

    holdings = a.fetch_holdings()
    cfg = data.get("sell_config") or a.DEFAULT_SELL_CONFIG
    signals = a.sell_signals(holdings, valuation, cfg)

    data["fx"] = fx
    data["valuation"] = valuation
    data["valuation_date"] = datetime.datetime.now().isoformat(timespec="minutes")
    data["sell_signals"] = signals
    a.save(data)
    print(f"시세 갱신 완료: {len(valuation)}개 종목, 매도신호 {len(signals)}건")


if __name__ == "__main__":
    main()
