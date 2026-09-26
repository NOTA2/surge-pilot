"""Offline comparison of surge alerts with ordinary and near-miss stocks."""

import argparse
import html
import json
import statistics
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .matched_controls import select_controls
from .scan import connect_database, selection_summary

NY = ZoneInfo("America/New_York")
RULES = (
    ("2m_5pct", "2분 +5%"),
    ("3m_3pct", "3분 +3%"),
    ("5m_8pct", "5분 +8%"),
    ("15m_20pct", "15분 +20%"),
    ("prior_10pct", "전일 종가 +10%"),
    ("prior_20pct", "전일 종가 +20%"),
    ("prior_20pct_and_3m_3pct", "+20% 이후 3분 +3%"),
    ("prior_20pct_and_5m_8pct", "+20% 이후 5분 +8%"),
    ("prior_10pct_and_5m_8pct", "+10% 이후 5분 +8%"),
)


def _bars(database, symbol, date):
    result = []
    for stamp, high, close, volume in database.execute(
            "SELECT timestamp,high,close,volume FROM minute WHERE symbol=? AND date=? ORDER BY timestamp",
            (symbol, date)):
        when = datetime.fromisoformat(stamp).astimezone(NY)
        if time(4) <= when.time() < time(16):
            result.append((when, Decimal(high), Decimal(close), int(volume)))
    return result


def _signals(bars, prior_close, stop_index):
    first = {}
    fillable = {}
    watch_seen = False
    for index in range(stop_index):
        when, _, close, _ = bars[index]
        if close < Decimal("0.1"):
            continue
        cumulative = (close / prior_close - 1) * 100
        if cumulative >= 20:
            watch_seen = True
        active = {"prior_10pct": cumulative >= 10,
                  "prior_20pct": cumulative >= 20}
        for minutes, threshold, name in ((2, 5, "2m_5pct"), (3, 3, "3m_3pct"), (5, 8, "5m_8pct"),
                                         (15, 20, "15m_20pct")):
            cutoff = when - timedelta(minutes=minutes)
            recent = [bar[2] for bar in bars[:index] if cutoff <= bar[0] < when and bar[2] > 0]
            active[name] = bool(recent and (close / min(recent) - 1) * 100 >= threshold)
        active["prior_20pct_and_3m_3pct"] = watch_seen and active["3m_3pct"]
        active["prior_20pct_and_5m_8pct"] = watch_seen and active["5m_8pct"]
        active["prior_10pct_and_5m_8pct"] = cumulative >= 10 and active["5m_8pct"]
        for name, enabled in active.items():
            if not enabled:
                continue
            first.setdefault(name, index)
            if (name not in fillable and index + 1 < stop_index
                    and bars[index + 1][0] - when == timedelta(minutes=1)):
                fillable[name] = index
    return first, fillable


def analyze(db_path="data/private/research.sqlite", control_limit=200):
    selection = selection_summary(db_path)
    groups = {"winners": selection["winners"],
              "ordinary": select_controls(db_path, control_limit, "ordinary"),
              "near_miss": select_controls(db_path, control_limit, "near_miss")}
    database = connect_database(db_path)
    results = {}
    midpoint = selection["sessions"][len(selection["sessions"]) // 2]
    for group, cases in groups.items():
        counts = {name: {"alerts": 0, "next_minute_before_50pct": 0,
                         "median_lead_minutes": None, "leads": [],
                         "regular_open_gap_alerts_before_open": 0,
                         "early_half_alerts": 0, "late_half_alerts": 0}
                  for name, _ in RULES}
        covered = catchable = below_one = first_bar_50pct = premarket_50pct = regular_open_gap = 0
        for case in cases:
            symbol, date = case["symbol"], case["date"]
            prior = database.execute("SELECT close FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
                                     (symbol, date)).fetchone()
            daily = database.execute("SELECT open FROM daily WHERE symbol=? AND date=?",
                                     (symbol, date)).fetchone()
            if not prior or not daily or Decimal(prior[0]) <= 0:
                continue
            prior_close = Decimal(prior[0])
            below_one += prior_close < 1
            if group == "winners":
                regular_open_gap += Decimal(daily[0]) >= prior_close * Decimal("1.5")
            bars = _bars(database, symbol, date)
            if not bars:
                continue
            covered += 1
            hit_index = None
            if group == "winners":
                base = prior_close if case["prev_close_to_high_pct"] >= 50 else Decimal(daily[0])
                target = base * Decimal("1.5")
                hit_index = next((i for i, bar in enumerate(bars) if bar[1] >= target), None)
                first_bar_50pct += hit_index == 0
                premarket_50pct += hit_index is not None and bars[hit_index][0].time() < time(9, 30)
                catchable += hit_index is not None and hit_index > 0
                if hit_index is None or hit_index == 0:
                    continue
            stop = hit_index if hit_index is not None else len(bars)
            first, fillable = _signals(bars, prior_close, stop)
            for name in first:
                metric = counts[name]
                metric["alerts"] += 1
                if (group == "winners" and Decimal(daily[0]) >= prior_close * Decimal("1.5")
                        and bars[first[name]][0].time() < time(9, 30)):
                    metric["regular_open_gap_alerts_before_open"] += 1
                metric["early_half_alerts" if date < midpoint else "late_half_alerts"] += 1
                if name in fillable:
                    metric["next_minute_before_50pct"] += 1
                if hit_index is not None:
                    metric["leads"].append((bars[hit_index][0] - bars[first[name]][0]).total_seconds() / 60)
        for metric in counts.values():
            leads = metric.pop("leads")
            metric["median_lead_minutes"] = round(statistics.median(leads), 1) if leads else None
        results[group] = {"selected": len(cases), "with_minute_bars": covered,
                          "catchable_after_first_bar": catchable,
                          "first_bar_already_50pct": first_bar_50pct,
                          "premarket_50pct": premarket_50pct,
                          "regular_open_gap_50pct": regular_open_gap,
                          "prior_close_below_1usd": below_one, "rules": counts}
    database.close()
    return {"generated_at": datetime.now(NY).isoformat(),
            "period": [selection["sessions"][0], selection["sessions"][-1]],
            "half_split_at": midpoint, "minimum_signal_price_usd": 0.1,
            "definition": "Close-confirmed alerts from 04:00-15:59 New York time; winners count only before first observed +50% high; next-minute means a consecutive 1-minute bar before that high.",
            "groups": results,
            "rules": [{"key": name, "label": label} for name, label in RULES],
            "limitations": [
                "급등 사례는 정규장 일봉에서 사후 선정한 455건이며 50% 도달 전에만 신호를 셉니다.",
                "일반 대조군과 20~49% 상승 후 멈춘 대조군은 각 200건을 날짜·전일 종가·전일 거래대금으로 표본 매칭했습니다. 전체 시장 오탐률이나 매수 적중률이 아닙니다.",
                "첫 관측 분봉에서 이미 50%에 도달한 사례는 이후 신호로 포착할 수 없습니다.",
                "분봉 종가 신호와 연속된 다음 분봉은 실제 호가·체결 가능성을 보장하지 않습니다.",
                "같은 30거래일에서 찾은 규칙은 별도 기간과 실시간 모의 투자로 검증해야 합니다.",
            ]}


def render_html(result):
    e = lambda value: html.escape(str(value if value is not None else "—"))
    groups = result["groups"]
    rows = []
    for rule in result["rules"]:
        name = rule["key"]
        win = groups["winners"]["rules"][name]
        ordinary = groups["ordinary"]["rules"][name]
        near = groups["near_miss"]["rules"][name]
        rows.append(f'<tr><td>{e(rule["label"])}</td><td>{e(win["alerts"])}/455</td>'
                    f'<td>{e(win["next_minute_before_50pct"])}</td><td>{e(win["median_lead_minutes"])}분</td>'
                    f'<td>{e(ordinary["alerts"])}/{e(groups["ordinary"]["selected"])}</td>'
                    f'<td>{e(near["alerts"])}/{e(groups["near_miss"]["selected"])}</td></tr>')
    notes = ''.join(f'<li>{e(note)}</li>' for note in result["limitations"])
    watch = groups["winners"]["rules"]["prior_20pct"]
    slow = groups["winners"]["rules"]["15m_20pct"]
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SurgePilot · 포착 패턴 연구</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#09131d;color:#eaf4f3;font:15px system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 20px}}h1{{font-size:32px}}p,li{{color:#abc0c7;line-height:1.6}}section{{background:#11232e;border:1px solid #29434d;border-radius:13px;padding:22px;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{background:#17313c;border-radius:9px;padding:15px}}.card strong{{display:block;font-size:26px;margin-top:8px}}.card span{{color:#abc0c7;font-size:12px}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px;border-bottom:1px solid #29434d;text-align:left}}th{{color:#abc0c7}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><main><p>SURGEPILOT / OFFLINE PATTERN STUDY</p><h1>급등주 조기 포착 패턴</h1><p>{e(result["period"][0])} ~ {e(result["period"][1])} · 장전 04:00부터 관측 · 최저 관측 가격 $0.10</p><section><div class="grid"><div class="card"><span>50% 급등 사례</span><strong>{e(groups["winners"]["selected"])}</strong></div><div class="card"><span>분봉 관측 가능</span><strong>{e(groups["winners"]["catchable_after_first_bar"])}</strong></div><div class="card"><span>첫 분봉에서 이미 50%</span><strong>{e(groups["winners"]["first_bar_already_50pct"])}</strong></div></div><p>공통 양상: 전일 종가 1달러 미만 {e(groups["winners"]["prior_close_below_1usd"])}건 · 장전 50% 도달 {e(groups["winners"]["premarket_50pct"])}건 · 정규장 시작 시 이미 50% 상승 {e(groups["winners"]["regular_open_gap_50pct"])}건.</p></section><section><h2>신호별 비교</h2><p>급등주는 50% 도달 전 신호만 계산합니다. 일반 종목과 20~49% 상승 후 멈춘 종목은 같은 날짜·비슷한 가격과 전일 거래대금의 대조군입니다. 아래 대조군 수치는 표본 내 신호 건수입니다.</p><div class="table"><table><thead><tr><th>관측 규칙</th><th>급등주 사전 신호</th><th>다음 봉 관측</th><th>50%까지 선행 중앙값</th><th>일반 대조군</th><th>20~49% 대조군</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section><section><h2>해석</h2><p>전일 종가 +20%는 급등주 {e(watch["alerts"])}건을 일찍 감지하지만, 20~49% 대조군도 {e(groups["near_miss"]["rules"]["prior_20pct"]["alerts"])}건 감지했습니다. 15분 이내 +20%로 좁히면 급등주 사전 신호가 {e(slow["alerts"])}건으로 줄고, 50%까지 선행 중앙값은 {e(slow["median_lead_minutes"])}분입니다. 따라서 +20%는 감시 목록 진입 후보이며, 이 표만으로 매수 규칙이나 수익률을 정할 수 없습니다.</p><ul>{notes}</ul></section></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description="Offline surge pattern comparison")
    parser.add_argument("--db", default="data/private/research.sqlite")
    parser.add_argument("--control-limit", type=int, default=200)
    parser.add_argument("--output", default="reports/private/pattern.html")
    parser.add_argument("--json-output", default="reports/private/pattern.json")
    parser.add_argument("--public-summary", default="docs/pattern-summary.json")
    args = parser.parse_args()
    result = analyze(args.db, args.control_limit)
    for path, content in ((args.output, render_html(result)),
                          (args.json_output, json.dumps(result, ensure_ascii=False, indent=2)),
                          (args.public_summary, json.dumps(result, ensure_ascii=False, indent=2))):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rule in result["rules"]:
        name = rule["key"]
        print(f"{name}: winners={result['groups']['winners']['rules'][name]['alerts']} "
              f"ordinary={result['groups']['ordinary']['rules'][name]['alerts']} "
              f"near_miss={result['groups']['near_miss']['rules'][name]['alerts']}")


if __name__ == "__main__":
    main()
