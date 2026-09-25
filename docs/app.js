let report = null;
let tab = 'trades';
const $ = (id) => document.getElementById(id);
const money = (value) => value == null ? '—' : '$' + Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = (value) => value == null ? '—' : `${Number(value)>0?'+':''}${Number(value).toFixed(2)}%`;
const date = (value) => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)) : '—';
const stamp = (value) => value ? new Intl.DateTimeFormat('ko-KR',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)) : '—';
const dayKey = (value) => value ? new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)) : '';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g,(char)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const tone = (value) => Number(value) >= 0 ? 'positive' : 'negative';

function setReport(data) {
  if (!data || !data.summary || !Array.isArray(data.equity) || !Array.isArray(data.trades) || !Array.isArray(data.signals)) throw Error('SurgePilot 보고서 형식이 아닙니다.');
  report = data;
  const summary = data.summary;
  $('report-kind').textContent = ({research:'과거 데이터 검증',backtest:'과거 데이터 백테스트',paper:'실시간 가상매매'})[data.kind] || '보고서';
  $('demo-warning').hidden = !data.is_demo;
  $('source').textContent = ({synthetic_demo:'합성 예제',historical_csv:'과거 CSV',toss_live_quotes:'토스 실시간 시세'})[data.source] || data.source || '—';
  const first = data.equity[0]?.time, last = data.equity.at(-1)?.time;
  $('period').textContent = first && last ? `${date(first)} – ${date(last)}` : '—';
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
