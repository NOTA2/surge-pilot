"""Discovery-only backtest over every daily +20% candidate in the stored scan.

The rule reads only completed minute bars and never places an order.
"""

import argparse
import html
import json
import statistics
from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .matched_controls import select_all_near_misses
from .scan import connect_database, selection_summary

NY = ZoneInfo("America/New_York")
MIN_PRICE = Decimal("0.10")
PRIOR_RISE = Decimal("1.20")
SHORT_RISE = Decimal("1.08")
LOOKBACK = timedelta(minutes=5)


def first_signal(bars, prior_close, earliest=time(4)):
    """First close with +20% from prior close and +8% from an observed close in 5m."""
    recent = deque()
    for index, (when, _, close) in enumerate(bars):
        while recent and recent[0][0] < when - LOOKBACK:
            recent.popleft()
        if (when.time() >= earliest and close >= MIN_PRICE
                and close >= prior_close * PRIOR_RISE and recent
                and close >= min(item[1] for item in recent) * SHORT_RISE):
            return index
        recent.append((when, close))
    return None


def _minute_bars(database, symbol, date):
    result = []
    for stamp, high, close in database.execute(
            "SELECT timestamp,high,close FROM minute WHERE symbol=? AND date=? ORDER BY timestamp",
            (symbol, date)):
        when = datetime.fromisoformat(stamp).astimezone(NY)
        if time(4) <= when.time() < time(16):
            result.append((when, Decimal(high), Decimal(close)))
    return result


def _logs(database, table):
    if not database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return {}
    return {(symbol, date): status for symbol, date, status in database.execute(
        f"SELECT symbol,date,status FROM {table}")}


def _counter():
    return {"candidates": 0, "full_day_fetched": 0, "with_minute_bars": 0,
            "signals": 0, "before_50pct": 0, "after_50pct": 0,
            "unverified_winner_signals": 0, "first_bar_50pct": 0,
            "catchable_after_first_bar": 0, "premarket_signals": 0,
            "lead_minutes": []}


def _finish(counter):
    leads = counter.pop("lead_minutes")
    counter["median_lead_minutes"] = round(statistics.median(leads), 1) if leads else None
    return counter


def analyze(db_path="data/private/research.sqlite"):
    selection = selection_summary(db_path)
    near = select_all_near_misses(db_path)
    database = connect_database(db_path)
    universe_stocks = database.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    minute_log = _logs(database, "minute_log")
    premarket_log = _logs(database, "premarket_log")
    matched_log = _logs(database, "near_miss_log")
    all_log = _logs(database, "near_miss_all_log")
    midpoint = selection["sessions"][len(selection["sessions"]) // 2]
    scopes = {scope: {name: {"all": _counter(), "first_half": _counter(),
                             "second_half": _counter()}
                      for name in ("winners", "near_miss", "extreme_gap_5x")}
              for scope in ("all_hours", "regular_only")}
    for name, cases in (("winners", selection["winners"]), ("near_miss", near)):
        for case in cases:
            symbol, date = case["symbol"], case["date"]
            previous = database.execute(
                "SELECT close FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
                (symbol, date)).fetchone()
            daily = database.execute(
                "SELECT open,high FROM daily WHERE symbol=? AND date=?",
                (symbol, date)).fetchone()
            if not previous or not daily or Decimal(previous[0]) <= 0:
                continue
            prior_close = Decimal(previous[0])
            opening, high = map(Decimal, daily)
            categories = [name]
            if name == "winners" and opening >= prior_close * 5:
                categories.append("extreme_gap_5x")
            keys = ("all", "first_half" if date < midpoint else "second_half")
            counters = [scopes[scope][category][key] for scope in scopes
                        for category in categories for key in keys]
            for counter in counters:
                counter["candidates"] += 1
            pair = (symbol, date)
            fetched = ((minute_log.get(pair) in ("ok", "empty")
                        and premarket_log.get(pair) in ("ok", "empty")) if name == "winners"
                       else matched_log.get(pair) == "ok" or all_log.get(pair) == "ok")
            for counter in counters:
                counter["full_day_fetched"] += fetched
            bars = _minute_bars(database, symbol, date)
            if not bars:
                continue
            for counter in counters:
                counter["with_minute_bars"] += 1
            hit = None
            if name == "winners":
                base = prior_close if high >= prior_close * Decimal("1.5") else opening
                hit = next((index for index, bar in enumerate(bars)
                            if bar[1] >= base * Decimal("1.5")), None)
                for counter in counters:
                    counter["first_bar_50pct"] += hit == 0
                    counter["catchable_after_first_bar"] += hit is not None and hit > 0
            for scope, earliest in (("all_hours", time(4)), ("regular_only", time(9, 30))):
                signal = first_signal(bars, prior_close, earliest)
                if signal is None:
                    continue
                for category in categories:
                    for key in keys:
                        counter = scopes[scope][category][key]
                        counter["signals"] += 1
                        counter["premarket_signals"] += bars[signal][0].time() < time(9, 30)
                        if name == "winners":
                            if hit is None:
                                counter["unverified_winner_signals"] += 1
                            elif signal < hit:
                                counter["before_50pct"] += 1
                                counter["lead_minutes"].append(
                                    (bars[hit][0] - bars[signal][0]).total_seconds() / 60)
                            else:
                                counter["after_50pct"] += 1
    database.close()
    for scope_groups in scopes.values():
        for subsets in scope_groups.values():
            for key in subsets:
                _finish(subsets[key])
    groups = scopes["all_hours"]
    winners = groups["winners"]["all"]
    misses = groups["near_miss"]["all"]
    extreme = groups["extreme_gap_5x"]["all"]
    total_alerts = winners["signals"] + misses["signals"]
    non_extreme_alerts = total_alerts - extreme["signals"]
    regular_winners = scopes["regular_only"]["winners"]["all"]
    regular_misses = scopes["regular_only"]["near_miss"]["all"]
    regular_alerts = regular_winners["signals"] + regular_misses["signals"]
    return {"generated_at": datetime.now(NY).isoformat(),
            "period": [selection["sessions"][0], selection["sessions"][-1]],
            "universe_stocks": universe_stocks,
            "half_split_at": midpoint,
            "rule": {"label": "전일 종가 +20% AND 이전 5분 관측 종가 최저점 대비 +8%",
                     "prior_close_rise_pct": 20, "lookback_minutes": 5,
                     "short_window_rise_pct": 8, "minimum_price_usd": 0.1,
                     "hours_new_york": "04:00-15:59",
                     "evaluation": "completed 1-minute close; no orders or position sizing"},
            "groups": groups,
            "regular_groups": scopes["regular_only"],
            "observed_alerts": total_alerts,
            "regular_observed_alerts": regular_alerts,
            "regular_pre50_alert_fraction_pct": (
                round(regular_winners["before_50pct"] / regular_alerts * 100, 2)
                if regular_alerts else None),
            "average_alerts_per_session": round(total_alerts / len(selection["sessions"]), 1),
            "catchable_recall_pct": (
                round(winners["before_50pct"] / winners["catchable_after_first_bar"] * 100, 2)
                if winners["catchable_after_first_bar"] else None),
            "observed_pre50_alert_fraction_pct": (
                round(winners["before_50pct"] / total_alerts * 100, 2) if total_alerts else None),
            "non_extreme_pre50_alert_fraction_pct": (
                round((winners["before_50pct"] - extreme["before_50pct"])
                      / non_extreme_alerts * 100, 2) if non_extreme_alerts else None),
            "coverage_note": "All daily +20% high symbol-days that do not meet either +50% definition are included. The all-hours cohort can miss stocks that exceeded +20% only in premarket, because the daily high generally represents regular-session trading. The regular-only view is closer to complete for this daily scan, subject to provider inconsistencies and missing bars. Collection log status 'ok' does not prove every minute traded or was delivered.",
            "limitations": [
                "+50% 여부와 첫 도달 시각은 평가에만 사용합니다. 발견 규칙에는 전일 종가와 그 시점까지 완료된 분봉 종가만 사용합니다.",
                "일봉 50% 급등 정의는 전일 종가 또는 당일 시가 대비 고가입니다. 극단적 시가 갭은 기업행위 가능성이 있으므로 별도 표시하지만 검증 없이 자동 제외하지 않습니다.",
                "분봉이 드문 장전에는 이전 5분 내 관측 종가가 없으면 신호가 나지 않습니다. 첫 관측 분봉에서 이미 50%인 사례도 사전 포착할 수 없습니다.",
                "장전만 +20%를 넘고 정규장 일봉 고가가 +20% 미만인 종목은 후보 수집에서 빠질 수 있습니다. 장전 포함 경보 비율은 전체 시장 적중률이 아닙니다.",
                "같은 30거래일에서 선택한 탐색 규칙입니다. 별도 기간 검증과 실시간 호가 관측 전에는 성능을 확정할 수 없습니다.",
                "이 보고서는 발견 성능만 계산합니다. 매수 수익률, 체결 가능성, 주문량은 평가하지 않습니다.",
            ]}


def render_html(result):
    e = lambda value: html.escape(str(value if value is not None else "—"))
    groups = result["groups"]
    def table_rows(which):
        return ''.join(
            f'<tr><td>{e(label)} · {e(suffix)}</td><td>{e(which[name][period]["candidates"])}</td>'
            f'<td>{e(which[name][period]["full_day_fetched"])}</td>'
            f'<td>{e(which[name][period]["signals"])}</td>'
            f'<td>{e(which[name][period]["before_50pct"] if name != "near_miss" else "—")}</td>'
            f'<td>{e(which[name][period]["after_50pct"] if name != "near_miss" else "—")}</td>'
            f'<td>{e(which[name][period]["premarket_signals"])}</td></tr>'
            for period, suffix in (("all", "전체"), ("first_half", "앞 15일"),
                                   ("second_half", "뒤 15일"))
            for name, label in (("winners", "50% 급등"), ("near_miss", "20~49% 정체"),
                                ("extreme_gap_5x", "시가 5배 이상 · 급등")))
    rows = ('<tr><th colspan="7">장전 포함 · 일봉 후보 표본</th></tr>' + table_rows(groups)
            + '<tr><th colspan="7">정규장 첫 신호만 · 일봉 후보 기준에 더 가깝게 포괄</th></tr>'
            + table_rows(result["regular_groups"]))
    notes = ''.join(f'<li>{e(note)}</li>' for note in result["limitations"])
    win, near = groups["winners"]["all"], groups["near_miss"]["all"]
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SurgePilot · 발견 규칙 검증</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#09131d;color:#eaf4f3;font:15px system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 20px}}h1{{font-size:32px}}p,li{{color:#abc0c7;line-height:1.6}}section{{background:#11232e;border:1px solid #29434d;border-radius:13px;padding:22px;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{background:#17313c;border-radius:9px;padding:15px}}.card strong{{display:block;font-size:26px;margin-top:8px}}.card span{{color:#abc0c7;font-size:12px}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px;border-bottom:1px solid #29434d;text-align:left}}th{{color:#abc0c7}}</style></head><body><main><p>SURGEPILOT / OFFLINE DISCOVERY STUDY</p><h1>급등주 발견 규칙 검증</h1><p>{e(result["period"][0])} ~ {e(result["period"][1])} · {e(result["rule"]["label"])} · 04:00~15:59 ET · 최저 $0.10</p><section><div class="grid"><div class="card"><span>50% 급등 전 첫 신호</span><strong>{e(win["before_50pct"])}/{e(win["candidates"])}</strong></div><div class="card"><span>20~49% 정체 후보 신호</span><strong>{e(near["signals"])}/{e(near["candidates"])}</strong></div><div class="card"><span>관측 경보 중 사전 급등 발견</span><strong>{e(result["observed_pre50_alert_fraction_pct"])}%</strong></div></div><p>{e(result["universe_stocks"])}종목 · 30거래일 · 첫 경보 {e(result["observed_alerts"])}건(하루 평균 {e(result["average_alerts_per_session"])}건). 첫 분봉 이후 +50%에 도달해 분봉상 사전 포착 가능한 {e(win["catchable_after_first_bar"])}건 중 {e(win["before_50pct"])}건을 발견했습니다({e(result["catchable_recall_pct"])}%). 발견에서 +50%까지 선행 중앙값은 {e(win["median_lead_minutes"])}분입니다. 경보 중 사전 급등 발견은 {e(win["before_50pct"])}/{e(result["observed_alerts"])}건이며 실거래 적중률이 아닙니다.</p></section><section><h2>후보 전체와 시간 구간별 검증</h2><div class="table"><table><thead><tr><th>구분</th><th>일봉 후보</th><th>종일 분봉 수집</th><th>첫 신호</th><th>50% 전</th><th>50% 이후</th><th>장전 신호</th></tr></thead><tbody>{rows}</tbody></table></div><p>급등주의 분봉 첫 +50% 도달 이전에 나온 신호만 성공으로 셉니다. 장중 고가가 먼저 +50%에 닿은 같은 분봉의 종가 신호는 늦은 신호입니다.</p></section><section><h2>해석 조건</h2><ul>{notes}</ul></section></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description="Discovery-only historical test")
    parser.add_argument("--db", default="data/private/research.sqlite")
    parser.add_argument("--output", default="reports/private/discovery.html")
    parser.add_argument("--json-output", default="reports/private/discovery.json")
    parser.add_argument("--public-summary", default="docs/discovery-summary.json")
    args = parser.parse_args()
    result = analyze(args.db)
    for name, content in ((args.output, render_html(result)),
                          (args.json_output, json.dumps(result, ensure_ascii=False, indent=2)),
                          (args.public_summary, json.dumps(result, ensure_ascii=False, indent=2))):
        target = Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    win = result["groups"]["winners"]["all"]
    near = result["groups"]["near_miss"]["all"]
    print(f"discovery before50={win['before_50pct']}/{win['candidates']} "
          f"near_alerts={near['signals']}/{near['candidates']} "
          f"near_fetched={near['full_day_fetched']}", flush=True)


if __name__ == "__main__":
    main()
