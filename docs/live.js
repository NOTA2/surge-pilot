const el=(id)=>document.getElementById(id);
const usd=(v)=>v==null?'—':'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const percent=(v)=>v==null?'—':`${Number(v)>0?'+':''}${Number(v).toFixed(2)}%`;
const clean=(v)=>String(v??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=(v)=>v?new Intl.DateTimeFormat('ko-KR',{timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v)):'—';
const clock=()=>{el('ny-clock').textContent=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date())+' ET';};
clock();setInterval(clock,1000);
let imported=false;
let fileHandle=null;

function chart(points){
  const svg=el('live-chart-svg');if(!points.length){svg.innerHTML='';return;}
  const series=points.length>600?points.filter((_,i)=>i%Math.ceil(points.length/600)===0||i===points.length-1):points;
  const values=series.map((p)=>Number(p.equity));const min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.15,max*.002,1);
  const path=values.map((v,i)=>`${(i/(values.length-1||1)*800).toFixed(1)},${(230-(v-min+pad)/(max-min+2*pad)*230).toFixed(1)}`).join(' ');
  let grid='';for(let i=0;i<4;i++){const y=15+i*65;grid+=`<line x1="0" x2="800" y1="${y}" y2="${y}" stroke="#29424a" stroke-dasharray="4 6"/>`;}
  svg.innerHTML=`${grid}<polyline points="${path}" fill="none" stroke="#63e8b0" stroke-width="2.5" stroke-linejoin="round"/>`;
  el('live-chart-start').textContent=when(points[0].time);el('live-chart-end').textContent=when(points.at(-1).time);
  el('point-count').textContent=`${points.length}개 관측값`;
}

function monitor(label,value,extra=''){return `<div class="validation-row"><span>${clean(label)}</span><strong class="${extra}">${clean(value)}</strong></div>`;}
function render(data){
  if(!data||data.kind!=='paper'||!data.summary||!Array.isArray(data.equity)||!Array.isArray(data.trades)||!Array.isArray(data.signals))throw Error('가상매매 보고서 형식이 아닙니다.');
  const demo=Boolean(data.is_demo),age=Date.now()-Date.parse(data.generated_at||0),stale=!demo&&(!Number.isFinite(age)||age>45000||age< -30000);
  el('live-demo').hidden=!demo;el('stale-warning').hidden=!stale;
  el('feed-status').textContent=demo?'합성 예제':stale?'갱신 지연':'가상매매 데이터 수신 중';
  el('live-dot').className=`status-dot${demo?' demo-dot':stale?' stale-dot':''}`;
  el('market-date').textContent=`보고서 시장 날짜 ${data.market_date||'—'}`;
  const s=data.summary,positions=data.positions||[];
  el('live-equity').textContent=usd(s.final_equity_usd);el('live-initial').textContent=`시작 ${usd(s.initial_cash_usd)}`;
  const pnl=Number(s.final_equity_usd)-Number(s.initial_cash_usd);el('live-pnl').textContent=`${pnl>=0?'+':''}${usd(pnl)}`;
  el('live-pnl').className=pnl>=0?'positive':'negative';el('live-return').textContent=percent(s.net_return_pct);
  el('live-open').textContent=`${positions.length}개`;el('live-positions-value').textContent=`진입 종목 ${new Set(data.signals.filter((x)=>x.decision==='bought').map((x)=>x.symbol)).size}개`;
  el('live-updated').textContent=when(data.generated_at);el('live-source').textContent=demo?'합성 데이터':data.source==='toss_live_quotes'?'토스증권 시세':'외부 보고서';
  chart(data.equity);
  el('live-monitor').innerHTML=monitor('데이터 상태',demo?'합성 예제':stale?'갱신 지연':'최근 갱신',stale?'negative':'positive')+monitor('시장 날짜',data.market_date||'—')+monitor('관측 신호',`${data.signals.length}건`)+monitor('완료된 거래',`${data.trades.length}건`)+monitor('실제 주문','없음')+'<p class="validation-note">이 화면은 가상매매 보고서를 표시합니다. 가격 관측 시점과 실제 주문 체결 시점은 다를 수 있습니다.</p>';
  el('positions').innerHTML=positions.map((p)=>{const gain=(Number(p.last_price)/Number(p.entry_price)-1)*100;return `<div class="position"><div class="position-top"><strong>${clean(p.symbol)}</strong><span>보유 ${clean(p.quantity)}주</span></div><div class="position-stats"><div><span>매수가</span><strong>${usd(p.entry_price)}</strong></div><div><span>최근가</span><strong>${usd(p.last_price)}</strong></div><div><span>평가 손익률</span><strong class="${gain>=0?'positive':'negative'}">${percent(gain)}</strong></div><div><span>평가금액</span><strong>${usd(Number(p.last_price)*Number(p.quantity))}</strong></div></div></div>`;}).join('');
  el('positions-empty').hidden=positions.length>0;
  el('signal-total').textContent=`${data.signals.length}건`;
  el('live-signals').innerHTML=data.signals.length?data.signals.slice(-8).reverse().map((x)=>`<div class="event"><div class="event-main"><strong>${clean(x.symbol)}</strong><span>${when(x.time)} · ${clean(x.decision)}</span></div><div class="event-value"><strong class="positive">${percent(x.rise_pct)}</strong><span>${usd(x.price)}</span></div></div>`).join(''):'<div class="empty">관측된 신호가 없습니다.</div>';
  el('closed-total').textContent=`${data.trades.length}건`;
  el('live-trades').innerHTML=data.trades.length?data.trades.slice(-8).reverse().map((x)=>`<div class="event"><div class="event-main"><strong>${clean(x.symbol)}</strong><span>${when(x.exit_time)} · ${clean(x.reason)}</span></div><div class="event-value"><strong class="${Number(x.pnl_usd)>=0?'positive':'negative'}">${Number(x.pnl_usd)>=0?'+':''}${usd(x.pnl_usd)}</strong><span>${percent(x.return_pct)}</span></div></div>`).join(''):'<div class="empty">아직 청산한 거래가 없습니다.</div>';
  el('live-limitations').innerHTML=(data.limitations||[]).map((x)=>`<li>${clean(x)}</li>`).join('');
}
async function refresh(){
  if(fileHandle){
    try{const file=await fileHandle.getFile();render(JSON.parse(await file.text()));el('live-source').textContent='로컬 파일 · 자동 갱신';}
    catch(error){el('feed-status').textContent='로컬 파일을 다시 연결하세요';el('stale-warning').hidden=false;}
    return;
  }
  if(imported)return;
  try{const response=await fetch('./live-report.json?ts='+Date.now(),{cache:'no-store'});if(!response.ok)throw Error('보고서를 찾을 수 없습니다.');render(await response.json());}
  catch(error){el('feed-status').textContent='보고서 연결 실패';el('stale-warning').hidden=false;}
}
if(typeof window.showOpenFilePicker!=='function')el('connect-file').hidden=true;
el('connect-file').addEventListener('click',async()=>{
  try{
    const [handle]=await window.showOpenFilePicker({multiple:false,types:[{description:'SurgePilot JSON report',accept:{'application/json':['.json']}}]});
    fileHandle=handle;imported=false;await refresh();
  }catch(error){if(error.name!=='AbortError')alert(`로컬 파일에 연결할 수 없습니다: ${error.message}`);}
});
el('live-file').addEventListener('change',async(e)=>{const file=e.target.files?.[0];if(!file)return;try{render(JSON.parse(await file.text()));fileHandle=null;imported=true;el('live-source').textContent='로컬 파일 · 1회 열기';}catch(error){alert(`보고서를 열 수 없습니다: ${error.message}`);}});
refresh();setInterval(refresh,15000);
