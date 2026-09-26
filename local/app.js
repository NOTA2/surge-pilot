const token = document.querySelector('meta[name="surgepilot-token"]').content;
const $ = id => document.getElementById(id);
let lastVersion = null;
let lastPhase = null;

async function getJSON(path) {
  const response = await fetch(path, {cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '요청에 실패했습니다');
  return data;
}

async function postJSON(path, body) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-SurgePilot-Token':token}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '실행에 실패했습니다');
  return data;
}

function values() {
  return Object.fromEntries(['lookback','rise','trail','stop'].map(name => [name, $(name).value]));
}

function showStatus(status) {
  $('status-tag').textContent = ({idle:'대기',running:'실행 중',completed:'완료',error:'오류',canceled:'중단'})[status.phase] || status.phase;
  $('status-tag').className = `status-tag ${status.phase}`;
  $('status-message').textContent = status.message;
  $('status-time').textContent = status.started_at ? `시작 ${status.started_at.slice(0,16).replace('T',' ')} (미 동부시간)` : '';
  $('progress').hidden = status.phase !== 'running';
  $('cancel').hidden = status.phase !== 'running';
  $('run-report').disabled = status.phase === 'running';
  $('run-premarket').disabled = status.phase === 'running';
  $('run-patterns').disabled = status.phase === 'running';
  $('run-discovery').disabled = status.phase === 'running';
  $('run-discovery-collect').disabled = status.phase === 'running';
  if (status.action === 'patterns' && status.phase === 'completed' && lastPhase !== 'completed') {
    $('pattern-frame').src = `/pattern?v=${Date.now()}`;
  }
  if (['discovery','discovery_collect'].includes(status.action) && status.phase === 'completed' && lastPhase !== 'completed') {
    $('discovery-frame').src = `/discovery?v=${Date.now()}`;
  }
  if (status.report_version && (status.report_version !== lastVersion || status.phase !== lastPhase && status.phase === 'completed')) {
    lastVersion = status.report_version;
    $('report-frame').src = `/report?v=${status.report_version}`;
    $('report-frame').hidden = false;
    $('report-empty').hidden = true;
    $('open-report').hidden = false;
    loadSummary();
  }
  lastPhase = status.phase;
}

async function poll() {
  try { showStatus(await getJSON('/api/status')); }
  catch (error) { $('status-message').textContent = `서버 연결 오류: ${error.message}`; }
}

async function loadSummary() {
  try {
    const data = await getJSON('/api/summary');
    if (!data) return;
    const c = data.coverage, s = data.selected, t = data.top100;
    const cards = [
      ['전체 급등 사례 중 사전 신호', `${s.signals_before_50pct}/${c.raw_50pct_events} · ${s.all_event_recall_pct}%`],
      ['관측 가능 사례 기준', `${s.recall_pct}%`],
      ['다음 분봉 매수 가능', `${s.fills_before_50pct}건`],
      ['상위 100종목 정규장 손익', `${Number(t.net_return_pct)>0?'+':''}${t.net_return_pct}%`]
    ];
    $('metrics').replaceChildren(...cards.map(([label,value]) => {
      const card = document.createElement('div'); card.className='metric';
      const span=document.createElement('span'); span.textContent=label;
      const strong=document.createElement('strong'); strong.textContent=value;
      card.append(span,strong); return card;
    }));
    $('coverage-chip').textContent = `장전 ${c.events_with_premarket_bars}/${c.raw_50pct_events}건 확보`;
    for (const [id,value] of [['lookback',data.strategy.lookback_minutes],['rise',data.strategy.rise_pct],['trail',data.strategy.trail_pct],['stop',data.strategy.stop_pct]]) {
      if (document.activeElement !== $(id)) $(id).value=value;
    }
  } catch (error) { $('metrics').textContent=`보고서 요약을 읽지 못했습니다: ${error.message}`; }
}

async function checkIP() {
  $('ip-value').textContent = '조회 중…';
  try { $('ip-value').textContent = (await getJSON('/api/ip')).ip; }
  catch (error) { $('ip-value').textContent = error.message; }
}

async function run(action) {
  try { showStatus(await postJSON('/api/run', {action,...values()})); }
  catch (error) { $('status-message').textContent = error.message; }
}

$('research-form').addEventListener('submit', event => {event.preventDefault(); run('report');});
$('run-premarket').addEventListener('click', () => run('premarket'));
$('run-patterns').addEventListener('click', () => run('patterns'));
$('run-discovery').addEventListener('click', () => run('discovery'));
$('run-discovery-collect').addEventListener('click', () => run('discovery_collect'));
$('check-ip').addEventListener('click', checkIP);
$('cancel').addEventListener('click', async () => {
  try { showStatus(await postJSON('/api/cancel', {})); }
  catch (error) { $('status-message').textContent = error.message; }
});
poll(); checkIP(); setInterval(poll, 1000);
