name: Live Valuation Refresh

on:
  schedule:
    # 15분 간격이지만 정각(0/15/30/45분)은 GitHub 전역에서 가장 몰리는 시간대라
    # 지연/스킵이 잦아서 일부러 어중간한 분(7/22/37/52)으로 offset
    # 00:07-06:52 UTC = 09:07-15:52 KST (국내장)
    # 13:07-22:52 UTC = 미국 프리마켓~애프터마켓 대략 포함
    - cron: '7,22,37,52 0-6,13-22 * * 1-5'
  workflow_dispatch: {}

jobs:
  refresh:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install yfinance requests

      - name: Refresh valuation
        run: python valuation.py

      - name: Save state
        run: |
          git config --local user.email "bot@github.com"
          git config --local user.name "Investment Bot"
          git add portfolio.json
          git diff --staged --quiet || git commit -m "valuation refresh $(date -u +'%Y-%m-%d %H:%M UTC')"
          git pull --rebase --autostash || true
          git push
