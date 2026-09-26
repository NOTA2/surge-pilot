let report = null;
let tab = 'trades';
const $ = (id) => document.getElementById(id);
const money = (value) => value == null ? '—' : '$' + Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = (value) => value == null ? '—' : `${Number(value)>0?'+':''}${Number(value).toFixed(2)}%`;
const rate = (value) => value == null ? '—' : `${Number(value).toFixed(2)}%`;
const date = (value) => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)) : '—';
const stamp = (value) => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) : '—';
const dayKey = (value) => value ? new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)) : '';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g,(char)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const tone = (value) => Number(value) >= 0 ? 'positive' : 'negative';

function setReport(data) {
  if (!data || !data.summary || !Array.isArray(data.equity) || !Array.isArray(data.trades) || !Array.isArray(data.signals)) throw Error('SurgePilot 보고서 형식이 아닙니다.');
  report = data;
  const summary = data.summary;
  $('report-kind').textContent = ({research:'과거 데이터 검증',backtest:'과거 데이터 백테스트',historical_phase:'실제 과거 데이터 검증',paper:'실시간 가상매매'})[data.kind] || '보고서';
  $('demo-warning').hidden = !data.is_demo;
  $('source').textContent = ({synthetic_demo:'합성 예제',historical_csv:'과거 CSV',toss_historical:'토스 과거 시세',toss_live_quotes:'토스 실시간 시세'})[data.source] || data.source || '—';
  const first = data.equity[0]?.time, last = data.equity.at(-1)?.time;
  $('period').textContent = data.period ? data.period.join(' – ') : first && last ? `${date(first)} – ${date(last)}` : '—';
  $('config-short').textContent = `${data.strategy?.lookback_minutes || '—'}분 / +${data.strategy?.rise_pct || '—'}%`;
  $('equity-final').textContent = money(summary.final_equity_usd);
  $('equity-initial').textContent = `시작 ${money(summary.initial_cash_usd)}`;
  $('return').textContent = pct(summary.net_return_pct);
  $('return').className = tone(summary.net_return_pct);
  $('drawdown').textContent = pct(summary.max_drawdown_pct == null ? null : -summary.max_drawdown_pct);
  $('trade-win').textContent = `${summary.trades ?? 0}건 · ${summary.win_rate_pct == null ? '—' : Number(summary.win_rate_pct).toFixed(0) + '%'}`;
  $('signals-count').textContent = `매수 신호 ${summary.signals ?? data.signals.length}건`;
  const times = [...data.trades.map((t)=>t.exit_time),...data.signals.map((s)=>s.time)];
  const days = [...new Set(times.map(dayKey))].filter(Boolean).sort().reverse();
  $('day-filter').innerHTML = '<option value="all">전체 거래일</option>' + days.map((d)=>`<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join('');
  const symbols = [...new Set([...data.trades,...data.signals].map((item)=>item.symbol))].filter(Boolean).sort();
  $('symbol-filter').innerHTML = '<option value="all">전체 종목</option>' + symbols.map((s)=>`<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  drawChart(data.equity);
  showValidation(data);
  showRules(data.strategy || {});
  $('limitations').innerHTML = (data.limitations || []).map((line)=>`<li>${escapeHtml(line)}</li>`).join('');
  $('table-empty').textContent = data.aggregate_only ? '공개 보고서에는 종목별 거래를 싣지 않았습니다. 로컬 상세 보고서를 열어 확인하세요.' : '선택한 조건에 해당하는 기록이 없습니다.';
  renderTable();
}

function drawChart(points) {
  const svg = $('chart');
  if (!points.length) { svg.innerHTML=''; return; }
  const series = points.length > 800 ? points.filter((_,i)=>i % Math.ceil(points.length/800) === 0 || i===points.length-1) : points;
  const values = series.map((p)=>Number(p.equity));
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max-min)*.15, max*.002, 1);
  const lower=min-pad, upper=max+pad, w=800, h=280;
  const coords=values.map((v,i)=>`${(i/(values.length-1||1)*w).toFixed(1)},${(h-(v-lower)/(upper-lower)*h).toFixed(1)}`);
  const line=coords.join(' '), end=coords.at(-1).split(',');
  let grid=''; for(let i=0;i<4;i++){const y=20+i*80;grid+=`<line x1="0" x2="800" y1="${y}" y2="${y}" stroke="#29424a" stroke-dasharray="4 6"/>`;}
  svg.innerHTML=`<defs><linearGradient id="fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#63e8b0" stop-opacity=".29"/><stop offset="1" stop-color="#63e8b0" stop-opacity="0"/></linearGradient></defs>${grid}<polygon points="0,280 ${line} 800,280" fill="url(#fill)"/><polyline points="${line}" fill="none" stroke="#63e8b0" stroke-width="2.5" stroke-linejoin="round"/><circle cx="${end[0]}" cy="${end[1]}" r="5" fill="#63e8b0" stroke="#10252b" stroke-width="3"/>`;
  $('chart-start').textContent=date(points[0].time);
  $('chart-end').textContent=date(points.at(-1).time);
}

function row(label,value,cls='') {return `<div class="validation-row"><span>${escapeHtml(label)}</span><strong class="${cls}">${escapeHtml(value)}</strong></div>`;}
function showValidation(data) {
  let html='';
  if(data.kind==='research') {
    html += row('학습 기간',`${data.train_dates?.join(' → ') || '—'}`);
    html += row('별도 검증 기간',`${data.holdout_dates?.join(' → ') || '—'}`);
    html += row('학습 수익률',pct(data.train_summary?.net_return_pct),tone(data.train_summary?.net_return_pct));
    html += row('별도 검증 수익률',pct(data.summary.net_return_pct),tone(data.summary.net_return_pct));
    html += row('50% 급등 사례 포착',data.event_analysis?.event_recall_pct == null?'사례 없음':`${data.event_analysis.caught_before_50pct}/${data.event_analysis.days_with_50pct_rise} (${pct(data.event_analysis.event_recall_pct)})`);
    html += row('무작위 종목·일자 수익률',pct(data.random_control?.summary?.net_return_pct),tone(data.random_control?.summary?.net_return_pct));
    html += '<p class="validation-note">매개변수는 학습 기간에서만 선택했습니다. 무작위 표본은 보조 검사이며, 실제 판단에는 전체 종목의 별도 기간 결과와 체결 가능성을 함께 봐야 합니다.</p>';
  } else if(data.kind==='historical_phase') {
    html += row('검증 단계',data.phase==='winners'?'50% 급등 사례':'전일 거래대금 상위 종목');
    html += row('선택 종목·일자',`${data.coverage?.selected_symbol_days ?? '—'}개`);
    html += row('분봉 확보',`${data.coverage?.with_minute_data ?? '—'}개 · ${rate(data.coverage?.coverage_pct)}`);
    html += row('정규장 양끝 관측',`${data.coverage?.near_full_regular_session ?? '—'}개`);
    if(data.phase==='winners'){
      html += row('50% 사례 장중 도달',`${data.event_analysis?.reached_50pct_in_regular_session ?? '—'}개`);
      html += row('첫 1분 이후 포착 가능',`${data.event_analysis?.catchable_after_first_minute ?? '—'}개`);
      html += row('50% 도달 전 신호',data.event_analysis?.early_signal_recall_pct == null?'측정 불가':`${data.event_analysis.signalled_before_50pct}개 · ${rate(data.event_analysis.early_signal_recall_pct)}`);
      html += row('50% 도달 전 매수',`${data.event_analysis?.bought_before_50pct ?? '—'}개`);
      html += '<p class="validation-note">급등 사례는 사후에 골랐습니다. 이 단계의 수익률만으로 전략 성과를 판단할 수 없습니다.</p>';
    }else{
      html += row('순수익률',pct(data.summary.net_return_pct),tone(data.summary.net_return_pct));
      html += '<p class="validation-note">매수 가능 종목은 전일 거래대금 추정치로 선정했습니다. 실제 거래대금이 아닌 일봉 가격×거래량 근사치입니다.</p>';
    }
  } else if(data.kind==='paper') {
    html += row('시장 날짜',data.market_date || '—');
    html += row('현재 보유 종목',`${data.positions?.length || 0}개`);
    html += row('완료된 거래',`${data.trades.length}건`);
    html += row('마지막 갱신',stamp(data.generated_at));
    html += '<p class="validation-note">가격 스냅샷으로 계산한 가상 체결입니다. 실제 주문은 발생하지 않았습니다.</p>';
  } else {
    html += row('백테스트 거래',`${data.trades.length}건`);
    html += row('매수 신호',`${data.signals.length}건`);
    html += row('가정한 수수료',`${data.strategy?.fee_bps ?? '—'} bp / 편도`);
    html += row('가정한 체결 불리함',`${data.strategy?.slippage_bps ?? '—'} bp / 편도`);
  }
  $('validation').innerHTML=html;
}

function showRules(s) {
  const items=[['진입 관찰',`${s.lookback_minutes ?? '—'}분 동안 +${s.rise_pct ?? '—'}%`],['고점 대비 청산',`-${s.trail_pct ?? '—'}%`],['매수가 대비 손절',`-${s.stop_pct ?? '—'}%`],['종목당 목표 비중',`${s.allocation_pct ?? '—'}%`],['하루 최대 진입',`${s.max_entries_per_day ?? '—'}개`],['마감 전 전량 청산',`${s.flatten_minutes ?? '—'}분 전`]];
  $('rules').innerHTML=items.map(([label,value])=>`<div class="rule"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
}

function renderTable() {
  if(!report)return;
  const day=$('day-filter').value,symbol=$('symbol-filter').value;
  const entries=(tab==='trades'?report.trades:report.signals).filter((item)=>(day==='all'||dayKey(item.exit_time||item.time)===day)&&(symbol==='all'||item.symbol===symbol)).slice().reverse();
  if(tab==='trades'){
    $('table-head').innerHTML='<tr><th>종목</th><th>매수</th><th>매도</th><th>수량</th><th>순손익</th><th>수익률</th><th>청산 이유</th></tr>';
    $('table-body').innerHTML=entries.map((t)=>`<tr><td class="ticker">${escapeHtml(t.symbol)}</td><td>${stamp(t.entry_time)} · ${money(t.entry_price)}</td><td>${stamp(t.exit_time)} · ${money(t.exit_price)}</td><td>${escapeHtml(t.quantity)}</td><td class="${tone(t.pnl_usd)}">${money(t.pnl_usd)}</td><td class="${tone(t.return_pct)}">${pct(t.return_pct)}</td><td class="reason">${escapeHtml(t.reason)}</td></tr>`).join('');
  }else{
    $('table-head').innerHTML='<tr><th>종목</th><th>감지 시각</th><th>관측 가격</th><th>상승률</th><th>결정</th></tr>';
    $('table-body').innerHTML=entries.map((s)=>`<tr><td class="ticker">${escapeHtml(s.symbol)}</td><td>${stamp(s.time)}</td><td>${money(s.price)}</td><td class="positive">${pct(s.rise_pct)}</td><td class="reason">${escapeHtml(s.decision)}</td></tr>`).join('');
  }
  $('table-empty').hidden=entries.length>0;
}

document.querySelectorAll('.tab').forEach((button)=>button.addEventListener('click',()=>{tab=button.dataset.tab;document.querySelectorAll('.tab').forEach((b)=>{b.classList.toggle('active',b===button);b.setAttribute('aria-selected',String(b===button));});renderTable();}));
['day-filter','symbol-filter'].forEach((id)=>$(id).addEventListener('change',renderTable));
$('file').addEventListener('change',async(event)=>{const file=event.target.files?.[0];if(!file)return;try{setReport(JSON.parse(await file.text()));}catch(error){alert(`보고서를 열 수 없습니다: ${error.message}`);}});
fetch('./report.json').then((response)=>{if(!response.ok)throw Error('보고서를 찾을 수 없습니다.');return response.json();}).then(setReport).catch((error)=>{$('report-kind').textContent='보고서 없음';$('validation').textContent=error.message;});

function showStudy(data) {
  if(!data || !Array.isArray(data.period) || !data.winners) throw Error('연구 요약 형식이 아닙니다.');
  $('study-period').textContent = `${data.period[0]} – ${data.period[1]}`;
  const winner = data.winners, top = data.top100;
  const cards = [
    ['전체 조사',`${Number(data.universe_stocks || 0).toLocaleString()}종목 · ${data.session_count}거래일`],
    ['50% 급등 사례',`${winner.events ?? '—'}건 · 시가 대비 ${winner.open_to_high_50pct ?? '—'}건`],
    ['기존 매매 규칙 사전 신호',winner.early_signals == null?'분봉 검증 중':`${winner.early_signals}/${winner.catchable_after_first_minute}건`],
    ['전일 거래대금 상위 100',top?.minute_covered == null ? `${top?.pairs ?? '—'} 종목·일자 검증 예정` : `${top.minute_covered}/${top.pairs} 종목·일자 확보`]
  ];
  $('study-summary').innerHTML=cards.map(([label,value])=>`<div class="study-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('')+
    `<p class="study-note">급등 사례 분봉 ${escapeHtml(winner.minute_covered ?? '—')}/${escapeHtml(winner.events ?? '—')}건 확보. 급등주는 사후 선정이므로 수익률 평가에 쓰지 않습니다. 상위 100은 전일 일봉 가격 × 거래량으로 추정한 거래대금 순위입니다. 기업행위로 인한 가격 변화를 추가 확인해야 합니다.</p>`;
  $('study-top-result').innerHTML=top?.net_return_pct == null ? '<span>상위 100종목의 손익 검증이 끝나면 이곳에 실제 집계 결과가 표시됩니다.</span>' :
    `<span>상위 100종목 기본 규칙</span><strong class="${tone(top.net_return_pct)}">${pct(top.net_return_pct)}</strong><span>최대 낙폭 ${pct(-top.max_drawdown_pct)} · 거래 ${escapeHtml(top.trades)}건 / ${escapeHtml(top.days_with_trade)}일 · 승률 ${rate(top.win_rate_pct)}</span>`;
  if(!Array.isArray(data.liquidity) || !data.liquidity.length)return;
  $('study-liquidity').innerHTML='<h3>진입 시점 누적 거래대금 조건</h3><table><thead><tr><th>최소 거래대금</th><th>50% 전 신호</th><th>50% 전 매수</th><th>기준 진입 놓침</th><th>공통 진입 지연</th><th>상위 100 거래</th><th>상위 100 수익률</th></tr></thead><tbody>'+
    data.liquidity.map((item)=>`<tr><td>${money(item.threshold_usd)}</td><td>${item.winners_early_signal_events == null?'검증 중':escapeHtml(item.winners_early_signal_events)+'건'}</td><td>${item.winners_early_bought_events == null?'검증 중':escapeHtml(item.winners_early_bought_events)+'건'}</td><td>${item.winners_baseline_buys_missed == null?'—':escapeHtml(item.winners_baseline_buys_missed)+'건'}</td><td>${item.winners_median_entry_delay_minutes == null?'—':escapeHtml(item.winners_median_entry_delay_minutes)+'분'}</td><td>${item.top100_trades == null?'검증 중':escapeHtml(item.top100_trades)+'건'}</td><td class="${tone(item.top100_return_pct)}">${pct(item.top100_return_pct)}</td></tr>`).join('')+'</tbody></table>';
  if(Array.isArray(data.costs) && data.costs.length){
    $('study-costs').innerHTML='<h3>체결 불리함 가정</h3><table><thead><tr><th>편도 체결 불리함</th><th>편도 수수료</th><th>거래</th><th>상위 100 수익률</th><th>최대 낙폭</th></tr></thead><tbody>'+
      data.costs.map((item)=>`<tr><td>${escapeHtml(item.slippage_bps_each_side)} bp</td><td>${escapeHtml(item.fee_bps_each_side)} bp</td><td>${escapeHtml(item.trades)}건</td><td class="${tone(item.net_return_pct)}">${pct(item.net_return_pct)}</td><td>${pct(-item.max_drawdown_pct)}</td></tr>`).join('')+'</tbody></table>';
  }
}
fetch('./study-summary.json').then((response)=>{if(!response.ok)throw Error('연구 요약 없음');return response.json();}).then(showStudy).catch(()=>{$('study-summary').textContent='실제 데이터 수집 및 검증을 진행 중입니다.';});

function showRecall(data) {
  if (!data || !Array.isArray(data.grid) || !data.coverage || !data.selected) throw Error('포착률 요약 형식이 아닙니다.');
  const c = data.coverage, selected = data.selected, strategy = data.strategy;
  const rows = [...data.grid].sort((a,b)=>b.all_event_recall_pct-a.all_event_recall_pct || a.lookback_minutes-b.lookback_minutes || a.rise_pct-b.rise_pct);
  const warning = c.events_with_premarket_bars < c.raw_50pct_events ?
    `<p class="study-recall-warning">장전 분봉 확보 ${escapeHtml(c.events_with_premarket_bars)}/${escapeHtml(c.raw_50pct_events)}건. 개장 전에 이미 50% 상승한 종목의 사전 포착률은 아직 평가되지 않았습니다.</p>` : '';
  $('study-recall').innerHTML = `<h3>거래대금 조건 없는 포착률 실험 · ${escapeHtml(strategy.lookback_minutes)}분 / +${escapeHtml(strategy.rise_pct)}%</h3>`+
    `<p class="study-note">확인된 사전 신호 ${escapeHtml(selected.signals_before_50pct)}/${escapeHtml(c.raw_50pct_events)}건 (${rate(selected.all_event_recall_pct)}). 저장된 첫 분봉 이후 50% 도달한 ${escapeHtml(c.catchable_after_first_bar)}건 기준으로는 ${rate(selected.recall_pct)}입니다. 다음 분봉 매수 가능 ${escapeHtml(selected.fills_before_50pct)}건. 장 시작 시 이미 +50%인 사례 ${escapeHtml(c.regular_open_gap_events)}건 중 개장 전 사전 신호 ${escapeHtml(selected.gap_signals_before_open)}건입니다.</p>`+
    warning+
    `<table><thead><tr><th>N</th><th>M</th><th>사전 신호</th><th>전체 사례 기준</th><th>관측 가능 기준</th><th>다음 봉 매수 가능</th></tr></thead><tbody>`+
    rows.map((item)=>`<tr${item.lookback_minutes===strategy.lookback_minutes && item.rise_pct===strategy.rise_pct ? ' class="study-selected"':''}><td>${escapeHtml(item.lookback_minutes)}분</td><td>+${escapeHtml(item.rise_pct)}%</td><td>${escapeHtml(item.signals_before_50pct)}건</td><td>${rate(item.all_event_recall_pct)}</td><td>${rate(item.recall_pct)}</td><td>${escapeHtml(item.fills_before_50pct)}건</td></tr>`).join('')+
    '</tbody></table><p class="study-note">같은 30거래일에서 조건을 비교한 탐색 결과입니다. 장전 체결 가능성과 별도 기간 성과는 아직 검증되지 않았습니다.</p>';
}

fetch('./recall-summary.json').then((response)=>{if(!response.ok)throw Error('포착률 요약 없음');return response.json();}).then(showRecall).catch(()=>{$('study-recall').textContent='포착률 요약을 불러올 수 없습니다.';});
