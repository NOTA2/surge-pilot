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
CONTINUATION_RULES = ("prior_20pct", "prior_20pct_and_5m_8pct", "15m_20pct")
ENTRY_METHODS = (
    ("immediate", "첫 +20% 직후"),
    ("pullback_recovery", "5% 눌림 후 2분 연속 회복"),
    ("delayed_breakout", "3분 관찰 후 종가 고점 1% 돌파"),
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


def _open_lows(database, symbol, date):
    result = []
    for stamp, opening, low in database.execute(
            "SELECT timestamp,open,low FROM minute WHERE symbol=? AND date=? ORDER BY timestamp",
            (symbol, date)):
        when = datetime.fromisoformat(stamp).astimezone(NY)
        if time(4) <= when.time() < time(16):
            result.append((when, Decimal(opening), Decimal(low)))
    return result


def _continuation(bars, open_lows, signal_index, stop_index):
    """First consecutive next-minute open; +10% target versus -5% stop within 30m."""
    entry_index = signal_index + 1
    if (entry_index >= stop_index or entry_index >= len(bars)
            or len(bars) != len(open_lows)
            or bars[entry_index][0] - bars[signal_index][0] != timedelta(minutes=1)
            or any(bar[0] != row[0] for bar, row in zip(bars, open_lows))):
        return None
    entered_at, entry, _ = open_lows[entry_index]
    if entry <= 0:
        return None
    window = [(bar[1], open_lows[index][2]) for index, bar in enumerate(bars[entry_index:], entry_index)
              if bar[0] <= entered_at + timedelta(minutes=30)]
    outcome = "neither"
    for high, low in window:
        target = high >= entry * Decimal("1.10")
        stop = low <= entry * Decimal("0.95")
        if target and stop:
            outcome = "ambiguous"
            break
        if stop:
            outcome = "stop"
            break
        if target:
            outcome = "target"
            break
    return {"outcome": outcome,
            "maximum_favorable_pct": float((max(high for high, _ in window) / entry - 1) * 100),
            "maximum_adverse_pct": float((min(low for _, low in window) / entry - 1) * 100)}


def _entry_signals(bars, watch_index):
    """Return close-confirmed entry signals using bars available up to each signal."""
    result = {"immediate": watch_index}
    watch_time = bars[watch_index][0]
    peak_close = bars[watch_index][2]
    dipped = False
    for index in range(watch_index + 1, len(bars)):
        when, _, close, _ = bars[index]
        if when > watch_time + timedelta(minutes=20):
            break
        previous_peak = peak_close
        if close <= previous_peak * Decimal("0.95"):
            dipped = True
        if (dipped and "pullback_recovery" not in result and index >= watch_index + 3
                and bars[index][0] - bars[index - 1][0] == timedelta(minutes=1)
                and bars[index - 1][0] - bars[index - 2][0] == timedelta(minutes=1)
                and close > bars[index - 1][2] > bars[index - 2][2]):
            result["pullback_recovery"] = index
        if ("delayed_breakout" not in result and when >= watch_time + timedelta(minutes=3)
                and index >= watch_index + 3
                and all(bars[j][0] - bars[j - 1][0] == timedelta(minutes=1)
                        for j in range(index - 2, index + 1))
                and close >= previous_peak * Decimal("1.01")):
            result["delayed_breakout"] = index
        peak_close = max(peak_close, close)
    return result


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
        continuation = {name: {"target_first": 0, "stop_first": 0, "ambiguous": 0,
                               "neither": 0, "mfe": [], "mae": []}
                        for name in CONTINUATION_RULES}
        entry_methods = {name: {"signals": 0, "consecutive_entries": 0,
                                "before_50pct_entries": 0, "target_first": 0,
                                "stop_first": 0, "ambiguous": 0, "neither": 0,
                                "before_50pct_target_first": 0,
                                "before_50pct_stop_first": 0,
                                "paired_immediate_entries": 0,
                                "paired_immediate_target_first": 0,
                                "paired_immediate_stop_first": 0,
                                "delays": [], "early_entries": 0, "late_entries": 0}
                         for name, _ in ENTRY_METHODS}
        alert_features = {"five_minute_turnover_usd": [], "cumulative_to_prior_turnover": [],
                          "premarket_alerts": 0, "five_minute_turnover_ge_100k": 0}
        covered = catchable = below_one = first_bar_50pct = premarket_50pct = regular_open_gap = 0
        for case in cases:
            symbol, date = case["symbol"], case["date"]
            prior = database.execute("SELECT high,low,close,volume FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
                                     (symbol, date)).fetchone()
            daily = database.execute("SELECT open FROM daily WHERE symbol=? AND date=?",
                                     (symbol, date)).fetchone()
            if not prior or not daily or Decimal(prior[2]) <= 0:
                continue
            prior_close = Decimal(prior[2])
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
            watch_index = first.get("prior_20pct")
            if watch_index is not None:
                alert_at = bars[watch_index][0]
                recent_turnover = sum(bar[2] * bar[3] for bar in bars[:watch_index + 1]
                                      if bar[0] >= alert_at - timedelta(minutes=5))
                cumulative_turnover = sum(bar[2] * bar[3] for bar in bars[:watch_index + 1])
                prior_turnover = ((Decimal(prior[0]) + Decimal(prior[1]) + prior_close) / 3
                                  * Decimal(prior[3]))
                alert_features["five_minute_turnover_usd"].append(float(recent_turnover))
                if prior_turnover > 0:
                    alert_features["cumulative_to_prior_turnover"].append(
                        float(cumulative_turnover / prior_turnover))
                alert_features["premarket_alerts"] += alert_at.time() < time(9, 30)
                alert_features["five_minute_turnover_ge_100k"] += recent_turnover >= 100000
            if any(name in first for name in CONTINUATION_RULES):
                open_lows = _open_lows(database, symbol, date)
                for name in CONTINUATION_RULES:
                    if name not in first:
                        continue
                    trade = _continuation(bars, open_lows, first[name], stop)
                    if trade is None:
                        continue
                    metric = continuation[name]
                    key = {"target": "target_first", "stop": "stop_first"}.get(
                        trade["outcome"], trade["outcome"])
                    metric[key] += 1
                    metric["mfe"].append(trade["maximum_favorable_pct"])
                    metric["mae"].append(trade["maximum_adverse_pct"])
            if watch_index is not None:
                if not any(name in first for name in CONTINUATION_RULES):
                    open_lows = _open_lows(database, symbol, date)
                immediate_trade = _continuation(bars, open_lows, watch_index, len(bars))
                for name, index in _entry_signals(bars, watch_index).items():
                    metric = entry_methods[name]
                    metric["signals"] += 1
                    trade = _continuation(bars, open_lows, index, len(bars))
                    if trade is None:
                        continue
                    metric["consecutive_entries"] += 1
                    metric["before_50pct_entries"] += group == "winners" and index + 1 < hit_index
                    metric["early_entries" if date < midpoint else "late_entries"] += 1
                    metric["delays"].append((bars[index][0] - bars[watch_index][0]).total_seconds() / 60)
                    key = {"target": "target_first", "stop": "stop_first"}.get(
                        trade["outcome"], trade["outcome"])
                    metric[key] += 1
                    if group == "winners" and index + 1 < hit_index and key in ("target_first", "stop_first"):
                        metric["before_50pct_" + key] += 1
                    if name != "immediate" and immediate_trade is not None:
                        metric["paired_immediate_entries"] += 1
                        immediate_key = {"target": "target_first", "stop": "stop_first"}.get(
                            immediate_trade["outcome"], immediate_trade["outcome"])
                        if immediate_key in ("target_first", "stop_first"):
                            metric["paired_immediate_" + immediate_key] += 1
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
        for metric in continuation.values():
            mfe, mae = metric.pop("mfe"), metric.pop("mae")
            metric["entries"] = len(mfe)
            metric["median_maximum_favorable_pct"] = round(statistics.median(mfe), 2) if mfe else None
            metric["median_maximum_adverse_pct"] = round(statistics.median(mae), 2) if mae else None
        for metric in entry_methods.values():
            delays = metric.pop("delays")
            metric["median_delay_minutes"] = round(statistics.median(delays), 1) if delays else None
        turn = alert_features.pop("five_minute_turnover_usd")
        relative = alert_features.pop("cumulative_to_prior_turnover")
        alert_features["alerts"] = len(turn)
        alert_features["median_five_minute_turnover_usd"] = round(statistics.median(turn), 2) if turn else None
        alert_features["median_cumulative_to_prior_turnover"] = (
            round(statistics.median(relative), 3) if relative else None)
        results[group] = {"selected": len(cases), "with_minute_bars": covered,
                          "catchable_after_first_bar": catchable,
                          "first_bar_already_50pct": first_bar_50pct,
                          "premarket_50pct": premarket_50pct,
                          "regular_open_gap_50pct": regular_open_gap,
                          "prior_close_below_1usd": below_one, "rules": counts,
                          "continuation": continuation, "entry_methods": entry_methods,
                          "alert_features": alert_features}
    database.close()
    return {"generated_at": datetime.now(NY).isoformat(),
            "period": [selection["sessions"][0], selection["sessions"][-1]],
            "half_split_at": midpoint, "minimum_signal_price_usd": 0.1,
            "definition": "Close-confirmed alerts from 04:00-15:59 New York time; winners count only before first observed +50% high; next-minute means a consecutive 1-minute bar before that high.",
            "groups": results,
            "rules": [{"key": name, "label": label} for name, label in RULES],
            "entry_method_labels": [{"key": name, "label": label} for name, label in ENTRY_METHODS],
            "entry_method_definition": "For winners, start with first +20% close before first +50% high; for controls, first +20% close. Alternative entry signal must occur within 20 clock minutes of watch. Entry assumes the next consecutive one-minute open. Outcomes are +10% high versus -5% low over 30 minutes after entry. Winner outcomes include entries after +50%; before_50pct_entries is reported separately. No fees, spread, slippage or real fill check.",
            "continuation_definition": "First close-confirmed alert, consecutive next-minute open strictly before +50% for winners; within 30 minutes, which is observed first: +10% high or -5% low? Same-minute both is ambiguous. Fees, spread and slippage excluded.",
            "limitations": [
                "급등 사례는 정규장 일봉에서 사후 선정한 455건이며 50% 도달 전에만 신호를 셉니다.",
                "일반 대조군과 20~49% 상승 후 멈춘 대조군은 각 200건을 날짜·전일 종가·전일 거래대금으로 표본 매칭했습니다. 전체 시장 오탐률이나 매수 적중률이 아닙니다.",
                "첫 관측 분봉에서 이미 50%에 도달한 사례는 이후 신호로 포착할 수 없습니다.",
                "분봉 종가 신호와 연속된 다음 분봉은 실제 호가·체결 가능성을 보장하지 않습니다.",
                "진입 후 +10% 목표와 -5% 손절의 선후는 분봉 고가·저가로만 계산했습니다. 같은 봉에서 둘 다 닿으면 순서를 알 수 없어 미확정으로 분류했습니다. 수수료와 호가 간격은 제외했습니다.",
                "새 진입 방식은 같은 30거래일에서 탐색한 가설입니다. 대조군 200건은 전체 후보군이 아니며, 급등 전 진입 여부는 사후에만 알 수 있습니다.",
                "30분 결과는 16:00 이전에 관측된 분봉만 사용합니다. 거래가 없는 분과 누락된 분봉을 구별하지 못하며, 실제 체결 가능한 호가를 알 수 없습니다.",
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
    watch_trade = groups["winners"]["continuation"]["prior_20pct"]
    labels = dict(RULES)
    continuation_rows = ''.join(
        f'<tr><td>{e(labels[name])}</td><td>{e("50% 급등" if group == "winners" else "20~49% 정체")}</td>'
        f'<td>{e(groups[group]["continuation"][name]["entries"])}</td>'
        f'<td>{e(groups[group]["continuation"][name]["target_first"])}</td>'
        f'<td>{e(groups[group]["continuation"][name]["stop_first"])}</td>'
        f'<td>{e(groups[group]["continuation"][name]["ambiguous"])}</td>'
        f'<td>{e(groups[group]["continuation"][name]["neither"])}</td>'
        f'<td>{e(groups[group]["continuation"][name]["median_maximum_adverse_pct"])}%</td></tr>'
        for name in CONTINUATION_RULES for group in ("winners", "near_miss"))
    volume_rows = ''.join(
        f'<tr><td>{e("50% 급등" if group == "winners" else "20~49% 정체")}</td>'
        f'<td>{e(groups[group]["alert_features"]["alerts"])}</td>'
        f'<td>${e(groups[group]["alert_features"]["median_five_minute_turnover_usd"])}</td>'
        f'<td>{e(groups[group]["alert_features"]["five_minute_turnover_ge_100k"])}</td>'
        f'<td>{e(groups[group]["alert_features"]["median_cumulative_to_prior_turnover"])}</td>'
        f'<td>{e(groups[group]["alert_features"]["premarket_alerts"])}</td></tr>'
        for group in ("winners", "near_miss"))
    volume_section = ('<section><h2>가격 외에 보이는 것 · 거래 참여</h2>'
                      '<p>첫 +20% 경보 시점까지 관측된 거래량만 사용했습니다. 5분 거래대금 10만 달러를 '
                      '필수 조건으로 걸면 급등주도 크게 놓칩니다. 누적 거래대금은 전일 일봉의 '
                      '대략적인 거래대금으로 나눈 값입니다.</p>'
                      '<div class="table"><table><thead><tr><th>사례</th><th>+20% 경보</th>'
                      '<th>최근 5분 거래대금 중앙값</th><th>5분 10만 달러 이상</th>'
                      '<th>전일 대비 누적 비율 중앙값</th><th>장전 경보</th></tr></thead>'
                      f'<tbody>{volume_rows}</tbody></table></div></section>')
    continuation_section = (f'<section><h2>포착 뒤 30분 · 매수 시점 검증</h2>'
                            '<p>첫 신호 다음 1분봉 시가에 진입한다고 가정했습니다. 급등주는 +50% 첫 도달 분봉보다 '
                            '앞선 진입만 셉니다. +10% 목표와 -5% 손절 중 먼저 관측된 쪽을 표시하며, '
                            '같은 분봉에서 둘 다 닿으면 순서를 알 수 없습니다. 수수료·호가 간격·체결 실패는 제외했습니다.</p>'
                            '<div class="table"><table><thead><tr><th>신호</th><th>사례</th><th>다음 봉 진입</th>'
                            '<th>+10% 먼저</th><th>-5% 먼저</th><th>같은 봉</th><th>미도달</th>'
                            f'<th>30분 최저 하락폭 중앙값</th></tr></thead><tbody>{continuation_rows}</tbody></table></div></section>')
    entry_rows = ''.join(
        f'<tr><td>{e(label)}</td><td>{e("50% 급등" if group == "winners" else "20~49% 정체")}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["signals"])}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["consecutive_entries"])}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["before_50pct_entries"] if group == "winners" else "—")}</td>'
        f'<td>{e(str(groups[group]["entry_methods"][name]["before_50pct_target_first"]) + " / " + str(groups[group]["entry_methods"][name]["before_50pct_stop_first"]) if group == "winners" else "—")}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["median_delay_minutes"])}분</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["target_first"])}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["stop_first"])}</td>'
        f'<td>{e(str(groups[group]["entry_methods"][name]["paired_immediate_target_first"]) + " / " + str(groups[group]["entry_methods"][name]["paired_immediate_stop_first"]) if name != "immediate" else "—")}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["ambiguous"])}</td>'
        f'<td>{e(groups[group]["entry_methods"][name]["neither"])}</td></tr>'
        for name, label in ENTRY_METHODS for group in ("winners", "near_miss"))
    entry_section = ('<section><h2>새 진입 방식 · 동일 +20% 감시 후보</h2>'
                     '<p>첫 +20% 종가 이후 20분 안에 진입 신호를 찾습니다. 눌림 방식은 종가 고점 대비 5% 하락 뒤 '
                     '연속 2분 상승, 돌파 방식은 3분 관찰 뒤 이전 종가 고점을 1% 초과해야 합니다. '
                     '매수는 신호 다음 연속 1분봉 시가로 가정합니다. 급등주에서는 +50% 이후 진입도 '
                     '목표·손절 결과에 포함하므로 급등 전 진입 수를 따로 확인하세요. '
                     '같은 분봉에서 목표·손절 모두 닿으면 순서를 알 수 없습니다.</p>'
                     '<div class="table"><table><thead><tr><th>진입 방식</th><th>사례</th><th>신호</th>'
                     '<th>다음 봉 진입</th><th>50% 전 진입</th><th>50% 전 목표 / 손절</th><th>감시 후 지연 중앙값</th>'
                     '<th>+10% 먼저</th><th>-5% 먼저</th><th>동일 후보 즉시 진입 목표 / 손절</th><th>같은 봉</th><th>미도달</th></tr></thead>'
                     f'<tbody>{entry_rows}</tbody></table></div>'
                     '<p>이 표본에서는 눌림 뒤 회복과 지연 돌파 모두 급등 전 진입 건수를 줄였습니다. '
                     '동일 후보의 즉시 진입 결과를 함께 비교하면 두 방식의 개선 근거가 없습니다. '
                     '따라서 자동 매수 규칙으로 채택하지 않습니다.</p></section>')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SurgePilot · 포착 패턴 연구</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#09131d;color:#eaf4f3;font:15px system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 20px}}h1{{font-size:32px}}p,li{{color:#abc0c7;line-height:1.6}}section{{background:#11232e;border:1px solid #29434d;border-radius:13px;padding:22px;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{background:#17313c;border-radius:9px;padding:15px}}.card strong{{display:block;font-size:26px;margin-top:8px}}.card span{{color:#abc0c7;font-size:12px}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px;border-bottom:1px solid #29434d;text-align:left}}th{{color:#abc0c7}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><main><p>SURGEPILOT / OFFLINE PATTERN STUDY</p><h1>급등주 조기 포착 패턴</h1><p>{e(result["period"][0])} ~ {e(result["period"][1])} · 장전 04:00부터 관측 · 최저 관측 가격 $0.10</p><section><div class="grid"><div class="card"><span>50% 급등 사례</span><strong>{e(groups["winners"]["selected"])}</strong></div><div class="card"><span>분봉 관측 가능</span><strong>{e(groups["winners"]["catchable_after_first_bar"])}</strong></div><div class="card"><span>첫 분봉에서 이미 50%</span><strong>{e(groups["winners"]["first_bar_already_50pct"])}</strong></div></div><p>공통 양상: 전일 종가 1달러 미만 {e(groups["winners"]["prior_close_below_1usd"])}건 · 장전 50% 도달 {e(groups["winners"]["premarket_50pct"])}건 · 정규장 시작 시 이미 50% 상승 {e(groups["winners"]["regular_open_gap_50pct"])}건.</p></section><section><h2>신호별 비교</h2><p>급등주는 50% 도달 전 신호만 계산합니다. 일반 종목과 20~49% 상승 후 멈춘 종목은 같은 날짜·비슷한 가격과 전일 거래대금의 대조군입니다. 아래 대조군 수치는 표본 내 신호 건수입니다.</p><div class="table"><table><thead><tr><th>관측 규칙</th><th>급등주 사전 신호</th><th>다음 봉 관측</th><th>50%까지 선행 중앙값</th><th>일반 대조군</th><th>20~49% 대조군</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>{volume_section}{continuation_section}{entry_section}<section><h2>해석</h2><p>전일 종가 +20%는 급등주 {e(watch["alerts"])}건을 일찍 감지하지만, 20~49% 대조군도 {e(groups["near_miss"]["rules"]["prior_20pct"]["alerts"])}건 감지했습니다. 15분 이내 +20%로 좁히면 급등주 사전 신호가 {e(slow["alerts"])}건으로 줄고, 50%까지 선행 중앙값은 {e(slow["median_lead_minutes"])}분입니다. 첫 +20% 경보 뒤 다음 분봉 진입이 가능했던 급등 사례 {e(watch_trade["entries"])}건에서도 +10% 목표 선도달은 {e(watch_trade["target_first"])}건, -5% 손절 선도달은 {e(watch_trade["stop_first"])}건입니다. 따라서 포착 신호와 매수 시점을 따로 설계해야 합니다.</p><ul>{notes}</ul></section></main></body></html>'''


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
