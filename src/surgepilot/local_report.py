"""Offline, repeatable momentum research from the private SQLite cache."""

import argparse
import html
import json
import statistics
import webbrowser
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import run_backtest
from .scan import connect_database, iter_selected_bars, selection_summary
from .strategy import StrategyConfig

NY = ZoneInfo("America/New_York")
PRESET_LOOKBACKS = (2, 3, 5, 10)
PRESET_RISES = (Decimal("3"), Decimal("5"), Decimal("8"), Decimal("12"))


def first_signals(bars: list[dict], lookbacks: tuple[int, ...], rises: tuple[Decimal, ...],
                  min_price: Decimal, min_bar_volume: int):
    """First signal and first next-minute-fillable signal for each N/M."""
    found = {}
    fillable = {}
    for lookback in lookbacks:
        cursor = 0
        for index, bar in enumerate(bars):
            cutoff = bar["time"] - timedelta(minutes=lookback)
            while cursor < index and bars[cursor]["time"] < cutoff:
                cursor += 1
            if cursor >= index or bar["close"] < min_price or bar["volume"] < min_bar_volume:
                continue
            prior = min((bars[i]["close"] for i in range(cursor, index)
                         if bars[i]["close"] > 0), default=None)
            if prior is None:
                continue
            rise = (bar["close"] / prior - 1) * 100
            for threshold in rises:
                key = (lookback, threshold)
                if rise >= threshold:
                    found.setdefault(key, index)
                    if (key not in fillable and index + 1 < len(bars)
                            and bars[index + 1]["time"] - bar["time"] == timedelta(minutes=1)):
                        fillable[key] = index
    return found, fillable


def evaluate_recall(db_path: str, selection: dict, config: StrategyConfig) -> dict:
    lookbacks = tuple(sorted(set(PRESET_LOOKBACKS) | {config.lookback_minutes}))
    rises = tuple(sorted(set(PRESET_RISES) | {config.rise_pct}))
    keys = [(n, m) for n in lookbacks for m in rises]
    counts = {key: {"signals_before_50pct": 0, "fills_before_50pct": 0,
                    "gap_signals_before_open": 0, "leads": []} for key in keys}
    events = []
    database = connect_database(db_path)
    premarket_logs = {tuple(row[:2]): row[2] for row in database.execute(
        "SELECT symbol,date,status FROM premarket_log")}
    for event in selection["winners"]:
        symbol, date = event["symbol"], event["date"]
        daily = database.execute("SELECT open FROM daily WHERE symbol=? AND date=?", (symbol, date)).fetchone()
        prior = database.execute("SELECT close FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
                                 (symbol, date)).fetchone()
        if not daily or not prior:
            continue
        day_open, prior_close = Decimal(daily[0]), Decimal(prior[0])
        # Prefer the prior-close target. The open-to-high target is used only when
        # the event did not gain 50% from prior close, and only as an ex-post label.
        target = (prior_close if event["prev_close_to_high_pct"] >= 50 else day_open) * Decimal("1.5")
        bars = []
        for stamp, opening, high, close, volume in database.execute(
                "SELECT timestamp,open,high,close,volume FROM minute WHERE symbol=? AND date=? ORDER BY timestamp",
                (symbol, date)):
            when = datetime.fromisoformat(stamp).astimezone(NY)
            if time(4, 0) <= when.time() < time(16, 0):
                bars.append({"time": when, "open": Decimal(opening), "high": Decimal(high),
                             "close": Decimal(close), "volume": int(volume)})
        hit_index = next((i for i, bar in enumerate(bars) if bar["high"] >= target), None)
        catchable = hit_index is not None and hit_index > 0
        regular_open = datetime.combine(datetime.fromisoformat(date).date(), time(9, 30), NY)
        gap_at_open = day_open >= prior_close * Decimal("1.5")
        signals, fillable = first_signals(bars, lookbacks, rises, config.min_price, config.min_bar_volume)
        chosen_index = signals.get((config.lookback_minutes, config.rise_pct))
        chosen_fillable = fillable.get((config.lookback_minutes, config.rise_pct))
        first_signal = bars[chosen_index]["time"] if chosen_index is not None else None
        hit_time = bars[hit_index]["time"] if hit_index is not None else None
        signal_before_hit = bool(catchable and first_signal and first_signal < hit_time)
        fill_before_hit = bool(catchable and chosen_fillable is not None
                               and bars[chosen_fillable + 1]["time"] < hit_time)
        for key, index in signals.items():
            if not catchable or bars[index]["time"] >= hit_time:
                continue
            metric = counts[key]
            metric["signals_before_50pct"] += 1
            metric["leads"].append((hit_time - bars[index]["time"]).total_seconds() / 60)
            fill_index = fillable.get(key)
            if fill_index is not None and bars[fill_index + 1]["time"] < hit_time:
                metric["fills_before_50pct"] += 1
            if gap_at_open and bars[index]["time"] < regular_open:
                metric["gap_signals_before_open"] += 1
        events.append({"date": date, "symbol": symbol,
                       "type": "장 시작 전 +50%" if gap_at_open else "장중 +50%",
                       "premarket_status": premarket_logs.get((symbol, date), "not_collected"),
                       "first_observed": bars[0]["time"].isoformat() if bars else None,
                       "hit_time": hit_time.isoformat() if hit_time else None,
                       "hit_session": ("장전" if hit_time and hit_time < regular_open else "정규장") if hit_time else None,
                       "catchable": catchable,
                       "signal_time": first_signal.isoformat() if first_signal else None,
                       "signal_before_hit": signal_before_hit,
                       "fill_before_hit": fill_before_hit,
                       "lead_minutes": round((hit_time - first_signal).total_seconds() / 60, 1)
                       if signal_before_hit else None})
    database.close()
    catchable_total = sum(item["catchable"] for item in events)
    gap_total = sum(item["type"] == "장 시작 전 +50%" for item in events)
    grid = []
    for lookback, rise in keys:
        item = counts[(lookback, rise)]
        grid.append({"lookback_minutes": lookback, "rise_pct": float(rise),
                     "signals_before_50pct": item["signals_before_50pct"],
                     "fills_before_50pct": item["fills_before_50pct"],
                     "all_event_recall_pct": round(item["signals_before_50pct"] / len(selection["winners"]) * 100, 2)
                     if selection["winners"] else None,
                     "recall_pct": round(item["signals_before_50pct"] / catchable_total * 100, 2)
                     if catchable_total else None,
                     "fill_recall_pct": round(item["fills_before_50pct"] / catchable_total * 100, 2)
                     if catchable_total else None,
                     "gap_signals_before_open": item["gap_signals_before_open"],
                     "median_lead_minutes": round(statistics.median(item["leads"]), 1)
                     if item["leads"] else None})
    chosen = next(item for item in grid if item["lookback_minutes"] == config.lookback_minutes
                  and Decimal(str(item["rise_pct"])) == config.rise_pct)
    return {"events": events, "grid": grid, "selected": chosen,
            "coverage": {"raw_50pct_events": len(selection["winners"]),
                         "events_with_minute_bars": sum(item["first_observed"] is not None for item in events),
                         "events_with_premarket_bars": sum(item["premarket_status"] == "ok" for item in events),
                         "hit_in_observed_bars": sum(item["hit_time"] is not None for item in events),
                         "hit_in_first_observed_bar": sum(item["hit_time"] == item["first_observed"]
                                                          and item["hit_time"] is not None for item in events),
                         "catchable_after_first_bar": catchable_total,
                         "regular_open_gap_events": gap_total,
                         "premarket_gap_signals_before_open": chosen["gap_signals_before_open"]}}


def _daily_equity(points: list[dict]) -> list[dict]:
    last_by_day = {}
    for point in points:
        date = datetime.fromisoformat(point["time"]).astimezone(NY).date().isoformat()
        last_by_day[date] = {"date": date, "equity": point["equity"]}
    return [last_by_day[date] for date in sorted(last_by_day)]


def _exit_quality(database, trades: list[dict]) -> dict:
    captures = []
    for trade in trades:
        entry_at = datetime.fromisoformat(trade["entry_time"]).astimezone(NY)
        day = entry_at.date().isoformat()
        highs = [Decimal(high) for stamp, high in database.execute(
            "SELECT timestamp,high FROM minute WHERE symbol=? AND date=?",
            (trade["symbol"], day))
            if entry_at <= datetime.fromisoformat(stamp).astimezone(NY)
            < datetime.combine(entry_at.date(), time(16, 0), NY)]
        entry = Decimal(str(trade["entry_price"]))
        if highs and max(highs) > entry and trade["pnl_usd"] > 0:
            captures.append(float((Decimal(str(trade["exit_price"])) - entry) / (max(highs) - entry) * 100))
    return {"winning_trade_median_peak_capture_pct": round(statistics.median(captures), 2) if captures else None,
            "measured_winners": len(captures),
            "losing_trades": sum(trade["pnl_usd"] < 0 for trade in trades),
            "worst_trade_pnl_usd": min((trade["pnl_usd"] for trade in trades), default=None)}


def run_local(db_path: str, config: StrategyConfig, cash: Decimal = Decimal("10000"),
              top_n: int = 100) -> dict:
    if config.min_cumulative_dollar_volume != 0:
        raise ValueError("local recall study uses no dollar-volume gate")
    selection = selection_summary(db_path, top_n)
    recall = evaluate_recall(db_path, selection, config)
    top_pairs = {(symbol, item["date"]) for item in selection["top_prior"] for symbol in item["symbols"]}
    portfolio = run_backtest(iter_selected_bars(db_path, top_pairs), config, cash, assume_sorted=True)
    database = connect_database(db_path)
    quality = _exit_quality(database, portfolio["trades"])
    database.close()
    return {"kind": "local_research", "source": "toss_cached_historical",
            "generated_at": datetime.now(NY).isoformat(),
            "period": [selection["sessions"][0], selection["sessions"][-1]],
            "strategy": config.to_dict(), "recall": recall,
            "top_prior": {"selected_symbol_days": len(top_pairs),
                          "summary": portfolio["summary"],
                          "daily_equity": _daily_equity(portfolio["equity"]),
                          "trades": portfolio["trades"], "exit_quality": quality},
            "limitations": [
                "50% 사례는 정규장 일봉으로 사후 선정했습니다. 장전에만 급등하고 정규장에는 오르지 않은 종목은 빠집니다.",
                "장전 분봉은 포착률에 포함하지만 수익률 백테스트는 정규장 진입·청산만 계산합니다.",
                "장전·급등주 실제 호가 간격, 체결 가능 수량과 거래정지는 재현하지 못합니다.",
                "같은 30거래일에서 매개변수를 비교한 결과는 탐색용입니다. 별도 기간으로 다시 검증해야 합니다.",
                "50% 기준은 수정 전 가격을 사용하므로 액면병합·분할 여부를 별도 확인해야 합니다.",
            ]}


def render_html(result: dict) -> str:
    e = lambda value: html.escape(str(value if value is not None else "—"))
    summary = result["top_prior"]["summary"]
    recall = result["recall"]
    selected = recall["selected"]
    coverage = recall["coverage"]
    strategy = result["strategy"]
    grid_rows = "".join(
        f'<tr class="{"chosen" if row is selected else ""}"><td>{row["lookback_minutes"]}분</td>'
        f'<td>+{e(row["rise_pct"])}%</td><td>{e(row["signals_before_50pct"])}건</td>'
        f'<td>{e(row["all_event_recall_pct"])}%</td><td>{e(row["recall_pct"])}%</td>'
        f'<td>{e(row["fills_before_50pct"])}건</td>'
        f'<td>{e(row["gap_signals_before_open"])}건</td><td>{e(row["median_lead_minutes"])}분</td></tr>'
        for row in sorted(recall["grid"], key=lambda item: (-item["recall_pct"], item["lookback_minutes"], item["rise_pct"])))
    trade_rows = "".join(
        f'<tr><td>{e(item["symbol"])}</td><td>{e(item["entry_time"][:16])}</td>'
        f'<td>{e(item["exit_time"][:16])}</td><td>{e(item["pnl_usd"])} USD</td>'
        f'<td>{e(item["return_pct"])}%</td><td>{e(item["reason"])}</td></tr>'
        for item in result["top_prior"]["trades"])
    event_rows = "".join(
        f'<tr><td>{e(item["date"])}</td><td>{e(item["symbol"])}</td><td>{e(item["type"])}</td>'
        f'<td>{e(item["premarket_status"])}</td><td>{e(item["hit_time"][11:16] if item["hit_time"] else "—")}</td>'
        f'<td>{e(item["signal_time"][11:16] if item["signal_time"] else "—")}</td>'
        f'<td>{"매수 가능" if item["fill_before_hit"] else "신호만" if item["signal_before_hit"] else "놓침"}</td></tr>'
        for item in recall["events"])
    points = result["top_prior"]["daily_equity"]
    values = [float(item["equity"]) for item in points]
    low, high = min(values), max(values)
    spread = max(high - low, 1)
    polyline = " ".join(f"{round(i * 800 / max(len(values)-1,1),1)},{round(190-(value-low)/spread*170,1)}"
                        for i, value in enumerate(values))
    notes = "".join(f"<li>{e(line)}</li>" for line in result["limitations"])
    premarket_warning = (f'<section class="warning">장전 분봉 확보 {e(coverage["events_with_premarket_bars"])}/'
                         f'{e(coverage["raw_50pct_events"])}건. 미확보 사례의 개장 전 포착률은 측정할 수 없습니다. '
                         '아래 포착률은 현재 저장된 분봉 범위에서만 계산했습니다.</section>')
    command = (f"./run-local.sh --lookback {strategy['lookback_minutes']} --rise {strategy['rise_pct']} "
               f"--trail {strategy['trail_pct']} --stop {strategy['stop_pct']}")
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SurgePilot 로컬 연구</title>
<style>:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#09131d;color:#eaf4f3;font:15px system-ui,sans-serif}}main{{max-width:1250px;margin:auto;padding:32px 20px}}h1{{font-size:34px;margin:8px 0}}h2{{font-size:20px;margin:0 0 16px}}p,small,li{{color:#a8bdc4;line-height:1.6}}.eyebrow{{color:#63e8b0;letter-spacing:2px;font-size:11px;font-weight:700}}.panel{{background:#11232e;border:1px solid #29434d;border-radius:13px;padding:22px;margin:18px 0}}.warning{{background:#36291e;border:1px solid #9a7042;color:#ffe0b6;border-radius:10px;padding:15px;margin:16px 0}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#17313c;border-radius:10px;padding:16px}}.card span{{display:block;color:#a8bdc4;font-size:12px}}.card strong{{display:block;font-size:25px;margin-top:10px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.table-wrap{{overflow:auto;max-height:500px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:11px;border-bottom:1px solid #2b454f;text-align:left;font-size:12px}}th{{color:#9eb6be;position:sticky;top:0;background:#11232e}}.chosen{{background:#21433f;color:#8cf0c0}}code{{color:#8cf0c0}}.command{{background:#07151d;padding:14px;border-radius:8px;overflow:auto}}input{{background:#0d2630;border:1px solid #35525b;color:#fff;border-radius:7px;padding:9px;margin:10px 0;width:100%;max-width:300px}}svg{{width:100%;height:210px}}@media(max-width:850px){{.cards{{grid-template-columns:1fr 1fr}}.grid{{grid-template-columns:1fr}}}}</style></head><body><main>
<div class="eyebrow">SURGEPILOT / OFFLINE RESEARCH</div><h1>급등주 포착 연구</h1><p>기간 {e(result['period'][0])} ~ {e(result['period'][1])} · 생성 {e(result['generated_at'][:16])} · 저장된 로컬 데이터만 사용</p>
<section class="panel"><h2>현재 설정 · N={e(strategy['lookback_minutes'])}분 / M=+{e(strategy['rise_pct'])}% / X=-{e(strategy['trail_pct'])}% / Y=-{e(strategy['stop_pct'])}%</h2><p>거래대금 하한 0달러 · 분봉 거래량 하한 {e(strategy['min_bar_volume'])}주 · 가격 하한 {e(strategy['min_price'])}달러</p><div class="command"><code>{e(command)}</code></div><small>터미널에서 숫자를 바꿔 다시 실행하면 이 HTML이 갱신되고 자동으로 열립니다. API 호출은 없습니다.</small></section>
{premarket_warning if coverage['events_with_premarket_bars'] < coverage['raw_50pct_events'] else ''}
<section class="cards"><div class="card"><span>전체 사례 기준 확인된 사전 신호</span><strong>{e(selected['signals_before_50pct'])}/{e(coverage['raw_50pct_events'])} · {e(selected['all_event_recall_pct'])}%</strong></div><div class="card"><span>관측상 가능 사례 기준 포착률</span><strong>{e(selected['signals_before_50pct'])}/{e(coverage['catchable_after_first_bar'])} · {e(selected['recall_pct'])}%</strong></div><div class="card"><span>50% 전 다음 봉 매수 가능</span><strong>{e(selected['fills_before_50pct'])}건</strong></div><div class="card"><span>장 시작 전 +50% 사례 사전 신호</span><strong>{e(coverage['premarket_gap_signals_before_open'])}/{e(coverage['regular_open_gap_events'])}</strong></div></section>
<section class="panel"><h2>N·M 조합별 포착률</h2><p>전체 사례 기준은 {e(coverage['raw_50pct_events'])}건을 분모로 합니다. 관측 가능 기준은 저장된 첫 분봉 이후 50%에 도달한 {e(coverage['catchable_after_first_bar'])}건을 분모로 합니다. 신호는 해당 분봉 마감 뒤 확정되며, 매수 가능은 다음 1분봉이 있고 50% 도달 전일 때만 셉니다. 현재 선택한 조합은 강조했습니다.</p><div class="table-wrap"><table><thead><tr><th>N</th><th>M</th><th>사전 신호</th><th>전체 기준</th><th>관측 가능 기준</th><th>사전 매수 가능</th><th>개장 전 신호</th><th>중앙 선행 시간</th></tr></thead><tbody>{grid_rows}</tbody></table></div></section>
<div class="grid"><section class="panel"><h2>전일 거래대금 상위 100 · 정규장 손익</h2><div class="cards" style="grid-template-columns:1fr 1fr"><div class="card"><span>순수익률</span><strong>{e(summary['net_return_pct'])}%</strong></div><div class="card"><span>거래 / 승률</span><strong>{e(summary['trades'])}건 / {e(summary['win_rate_pct'])}%</strong></div><div class="card"><span>최대 낙폭</span><strong>-{e(summary['max_drawdown_pct'])}%</strong></div><div class="card"><span>수익 거래 고점 포착 중앙값</span><strong>{e(result['top_prior']['exit_quality']['winning_trade_median_peak_capture_pct'])}%</strong></div></div><svg viewBox="0 0 800 210" role="img" aria-label="일별 평가금액"><polyline points="{polyline}" fill="none" stroke="#63e8b0" stroke-width="3"/></svg><small>이 수익률은 장전 매수 성과가 아닙니다. 정규장 상위 100종목의 같은 N/M/X/Y 백테스트입니다.</small></section>
<section class="panel"><h2>관측 범위</h2><p>장전 분봉 확보 {e(coverage['events_with_premarket_bars'])}/{e(coverage['raw_50pct_events'])}건 · 50% 도달 분봉 확인 {e(coverage['hit_in_observed_bars'])}건 · 첫 관측 봉에서 이미 도달 {e(coverage['hit_in_first_observed_bar'])}건</p><p>고점 포착 비율은 진입 후 당일 분봉 최고가 대비 실제 계산된 청산 가격의 이익 비율입니다. 분봉 고가는 체결 가능한 가격을 뜻하지 않습니다.</p><ul>{notes}</ul></section></div>
<section class="panel"><h2>상위 100 상세 거래</h2><div class="table-wrap"><table><thead><tr><th>종목</th><th>매수</th><th>매도</th><th>손익</th><th>수익률</th><th>이유</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section>
<section class="panel"><h2>급등 사례별 신호</h2><input id="filter" placeholder="날짜·종목·결과 검색" oninput="filterRows()"><div class="table-wrap"><table id="events"><thead><tr><th>날짜</th><th>종목</th><th>유형</th><th>장전 데이터</th><th>50% 도달</th><th>첫 신호</th><th>결과</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>
</main><script>function filterRows(){{const q=document.getElementById('filter').value.toLowerCase();for(const r of document.querySelectorAll('#events tbody tr'))r.hidden=!r.textContent.toLowerCase().includes(q)}}</script></body></html>'''


def public_summary(result: dict) -> dict:
    """Only aggregates that are safe to publish via GitHub Pages."""
    recall = result["recall"]
    return {"generated_at": result["generated_at"], "period": result["period"],
            "strategy": {key: result["strategy"][key] for key in
                         ("lookback_minutes", "rise_pct", "trail_pct", "stop_pct", "min_price")},
            "signal_definition": "Rise from the lowest observed close within the preceding N minutes; no earlier price is carried into a sparse window.",
            "coverage": recall["coverage"], "selected": recall["selected"],
            "grid": recall["grid"], "top100": result["top_prior"]["summary"]}


def main():
    parser = argparse.ArgumentParser(description="Offline local research; no Toss API calls")
    parser.add_argument("--db", default="data/private/research.sqlite")
    parser.add_argument("--lookback", type=int, default=5)
    parser.add_argument("--rise", type=Decimal, default=Decimal("8"))
    parser.add_argument("--trail", type=Decimal, default=Decimal("7"))
    parser.add_argument("--stop", type=Decimal, default=Decimal("5"))
    parser.add_argument("--cash", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--min-price", type=Decimal, default=Decimal("1"))
    parser.add_argument("--min-bar-volume", type=int, default=0)
    parser.add_argument("--fee-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("15"))
    parser.add_argument("--output", default="reports/private/latest.html")
    parser.add_argument("--json-output", default="reports/private/latest.json")
    parser.add_argument("--public-summary", default="docs/recall-summary.json")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if args.min_bar_volume < 0 or args.min_price <= 0 or args.top < 1:
        parser.error("price and top must be positive; volume must be nonnegative")
    config = replace(StrategyConfig(), lookback_minutes=args.lookback, rise_pct=args.rise,
                     trail_pct=args.trail, stop_pct=args.stop, min_price=args.min_price,
                     min_bar_volume=args.min_bar_volume, min_cumulative_dollar_volume=Decimal("0"),
                     fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
    result = run_local(args.db, config, args.cash, args.top)
    html_path, json_path = Path(args.output), Path(args.json_output)
    public_path = Path(args.public_summary)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(result), encoding="utf-8")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    public_path.write_text(json.dumps(public_summary(result), ensure_ascii=False, indent=2), encoding="utf-8")
    chosen = result["recall"]["selected"]
    print(f"confirmed_recall={chosen['signals_before_50pct']}/{result['recall']['coverage']['raw_50pct_events']} "
          f"({chosen['all_event_recall_pct']}%) evaluable_recall={chosen['recall_pct']}% "
          f"fillable={chosen['fills_before_50pct']} "
          f"top{args.top}_return={result['top_prior']['summary']['net_return_pct']}%")
    print(f"HTML: {html_path.resolve()}")
    if not args.no_open:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
