'use strict';
const $ = id => document.getElementById(id);
const state = {key:'', servers:[], status:null, selected:null, page:0, busy:false, favorites:new Set(), lastNotice:null};
const speed = {running:false, phase:null, device:null, raw:null, abort:null, operation:null, stage:''};
try {state.favorites = new Set(JSON.parse(localStorage.getItem('protonnode-favorites') || '[]'));} catch (_) {}
try {speed.device=JSON.parse(sessionStorage.getItem('protonnode-device-speed') || 'null');} catch (_) {}
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function notice(message, error=false) { $('notice').textContent=message; $('notice').classList.toggle('error',error); $('notice').hidden=!message; }
async function api(path, options={}) {
  const response = await fetch(path, {...options, cache:'no-store', signal:AbortSignal.timeout(30000), headers:{'Content-Type':'application/json','X-ProtonNode-Request':'1', ...(state.key ? {'X-API-Key':state.key} : {}), ...options.headers}});
  const data = await response.json();
  if (!response.ok) {if(response.status===401){$('auth').hidden=false; $('identity').textContent='Authentication required';} throw new Error(data.error || `Request failed (${response.status})`);}
  $('auth').hidden=true; return data;
}
function filtered() {
  const term=$('search').value.toLowerCase().trim(), country=$('country').value, mode=$('mode').value;
  return state.servers.filter(s => (!country || s.country===country) && (mode==='any' || s[mode]) && (!$('favorites-only').checked || state.favorites.has(s.hostname)) && (!term || [s.name,s.city,s.country,s.hostname].join(' ').toLowerCase().includes(term)));
}
function renderServers() {
  const all=filtered(), total=Math.max(1,Math.ceil(all.length/7)); state.page=Math.min(state.page,total-1);
  if(state.selected && !all.some(s=>s.hostname===state.selected)) state.selected=null;
  $('count').textContent=`${all.length.toLocaleString()} matching ${all.length===1?'server':'servers'}`;
  $('page').textContent=`${state.page+1} / ${total}`;
  $('previous').disabled=state.page===0; $('next').disabled=state.page>=total-1;
  const markup=all.length ? all.slice(state.page*7,state.page*7+7).map(s => `<div class="server-row ${state.selected===s.hostname?'selected':''}" data-server="${escapeHTML(s.hostname)}"><input type="radio" name="server" aria-label="${escapeHTML(s.name+' '+s.city)}" ${state.selected===s.hostname?'checked':''}><div class="server-main"><span class="server-name">${escapeHTML(s.city || s.country)} <span class="muted">· ${escapeHTML(s.name)}</span></span><span class="server-detail">${escapeHTML(s.country)}${state.status?.server?.hostname===s.hostname?' · Current route':''}</span></div>${s.tor?'<span class="server-cap">TOR</span>':s.p2p?'<span class="server-cap">P2P</span>':''}<button class="favorite quiet ${state.favorites.has(s.hostname)?'saved':''}" data-favorite="${escapeHTML(s.hostname)}" aria-label="${state.favorites.has(s.hostname)?'Remove favorite':'Add favorite'} ${escapeHTML(s.name)}" aria-pressed="${state.favorites.has(s.hostname)}">${state.favorites.has(s.hostname)?'★':'☆'}</button></div>`).join('') : '<p class="empty">No matching servers. Try another country, route type, or search.</p>';
  if(state.listMarkup!==markup){
    const active=document.activeElement, row=active?.closest('[data-server]'), focusHost=row?.dataset.server, isFavorite=!!active?.dataset.favorite;
    $('server-list').innerHTML=markup; state.listMarkup=markup;
    if(focusHost) for(const replacement of $('server-list').querySelectorAll('[data-server]')) if(replacement.dataset.server===focusHost) replacement.querySelector(isFavorite?'button':'input').focus({preventScroll:true});
  }
  const selected=state.servers.find(s=>s.hostname===state.selected);
  $('selected-name').textContent=selected ? `${selected.city || selected.country} · ${selected.name}` : 'Choose a destination';
  $('selected-detail').textContent=selected ? selected.hostname : 'An exact server is saved after verification.';
  $('connect').disabled=!selected || state.busy || !state.status?.ready;
}
function renderStatus(data) {
  state.status=data; $('demo').hidden=!data.demo; $('identity').textContent=data.identity;
  const active=data.operations.find(o=>['queued','running'].includes(o.state));
  state.busy=!!active || speed.running;
  $('state').textContent=active?.request.action==='speedtest'?'Speed test running':active?'Changing route':data.connection==='connected'?'Connected':data.connection==='disconnected'?'Disconnected':'Connection unavailable';
  $('state').className=`badge ${data.connection}`;
  $('connection-title').textContent=data.server ? `${data.server.city || data.server.country}, ${data.server.country}`.replace(/^(.+), \1$/,'$1') : 'No active route';
  $('current-server').textContent=data.server ? `${data.server.name} · ${data.server.hostname}` : 'Choose a server below to connect';
  $('public-ip').textContent=data.probe.public_ip || '—';
  $('checked').textContent=data.probe.checked_at ? `Internet checked ${new Date(data.probe.checked_at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}` : 'No verified internet connection';
  $('saved-summary').textContent=data.saved ? 'Selection saved · Restored automatically after restart' : 'Selections persist after a successful connection.';
  $('progress').hidden=!active && data.ready;
  $('progress-title').textContent=active?.stage || 'Restoring saved connection';
  $('progress-detail').textContent=active?.request.action==='speedtest' ? 'Measuring Proton only. No VPN settings are changed.' : active?.error ? `${active.error}. Attempting recovery…` : 'Tailnet access stays available. This page can be closed safely.';
  $('reconnect').disabled=state.busy || !data.ready || !data.saved;
  $('reconnect').textContent=data.connection==='disconnected'?'Connect saved':'Reconnect';
  $('disconnect').disabled=state.busy || !data.ready || data.connection==='disconnected' || !data.saved;
  $('history').innerHTML=data.operations.length ? data.operations.slice(0,5).map(o=>`<div class="activity ${escapeHTML(o.state)}"><div class="activity-title"><i></i><strong>${escapeHTML(o.request.action==='speedtest'?'Proton speed test':o.request.action==='switch' ? (o.result?.server?.name || o.request.selection?.hostname || o.request.selection?.city || 'Server change') : o.request.action[0].toUpperCase()+o.request.action.slice(1))}</strong></div><p>${escapeHTML(o.error || o.stage)}</p><time>${escapeHTML(new Date(o.created*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}))} · ${escapeHTML(o.state)}</time></div>`).join('') : '<p class="muted">Your connection changes will appear here.</p>';
  const latest=data.operations[0];
  if(latest && ['failed','succeeded','interrupted'].includes(latest.state) && latest.id!==state.lastNotice) {
    state.lastNotice=latest.id;
    if(latest.state==='failed') notice(`${latest.error}. ${latest.stage}.`,true);
  }
  renderServers();
  renderSpeed(data,active);
}
async function loadCatalog() {
  const data=await api('/api/v1/servers'); state.servers=data.servers;
  const selected=$('country').value;
  $('country').innerHTML='<option value="">All countries</option>'+[...new Set(state.servers.map(s=>s.country))].sort().map(c=>`<option value="${escapeHTML(c)}">${escapeHTML(c)}</option>`).join('');
  $('country').value=selected; renderServers();
}
async function poll() { try {renderStatus(await api('/api/v1/status'));} catch(error) {notice(error.message,true);$('state').textContent='Controller unavailable';} }
async function operate(action, selection) {
  if(state.busy) return;
  state.busy=true; $('connect').disabled=true;
  try {
    const body={action}; if(selection) body.selection=selection;
    // Each user gesture gets one key; a network retry reuses the same request.
    const key=globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const options={method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify(body)};
    let operation;
    try {operation=await api('/api/v1/operations',options);} catch(error) {if(error instanceof TypeError) operation=await api('/api/v1/operations',options); else throw error;}
    notice('Connection change accepted. You can leave this page and return to check progress.');
    $('progress').hidden=false; $('progress-title').textContent=operation.stage;
    await poll();
  } catch(error) {notice(error.message,true); state.busy=false; renderServers();}
}
$('server-list').addEventListener('click', event=>{
  const favorite=event.target.closest('[data-favorite]');
  if(favorite){const name=favorite.dataset.favorite;state.favorites.has(name)?state.favorites.delete(name):state.favorites.add(name);try{localStorage.setItem('protonnode-favorites',JSON.stringify([...state.favorites]));}catch(_){}renderServers();return;}
  const row=event.target.closest('[data-server]'); if(row){state.selected=row.dataset.server;renderServers();}
});
for(const id of ['search','country','mode','favorites-only']) $(id).addEventListener('input',()=>{state.page=0;renderServers();});
$('previous').onclick=()=>{state.page--;renderServers();}; $('next').onclick=()=>{state.page++;renderServers();};
$('connect').onclick=()=>operate('switch',{hostname:state.selected,mode:$('mode').value});
$('reconnect').onclick=()=>operate(state.status?.connection==='disconnected'?'connect':'reconnect');
$('disconnect').onclick=()=>operate('disconnect');
$('refresh').onclick=()=>loadCatalog().catch(e=>notice(e.message,true));
$('auth-form').onsubmit=async event=>{event.preventDefault();state.key=$('api-key').value.trim();$('api-key').value='';notice('');await initialize();};
const example=`curl '${location.origin}/api/v1/operations' \\\n  -H 'Authorization: Bearer YOUR_CONTROL_KEY' \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-ProtonNode-Request: 1' \\\n  -H 'Idempotency-Key: miami-switch-001' \\\n  --data '{"action":"switch","selection":{"country":"United States","city":"Miami","mode":"p2p"}}'`;
$('api-example').textContent=example;
$('copy-api').onclick=async()=>{try{await navigator.clipboard.writeText(example);$('copy-result').textContent='Copied';}catch(_){const range=document.createRange();range.selectNodeContents($('api-example'));getSelection().removeAllRanges();getSelection().addRange(range);$('copy-result').textContent='Select and copy the example below.';}};
function formatSpeed(value) {return Number.isFinite(value) && value>=0 ? value.toLocaleString(undefined,{maximumFractionDigits:1}) : '—';}
function speedTime(value) {return new Date(value*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function speedMessage(text,error=false) {$('speed-message').textContent=text;$('speed-message').classList.toggle('error',error);}
function renderSpeed(data=state.status,active=data?.operations?.find(o=>['queued','running'].includes(o.state))) {
  const recent=data?.last_speedtest?.result;
  if(recent && (!speed.raw || recent.measured_at>=speed.raw.measured_at)) speed.raw=recent;
  for(const [prefix,result] of [['raw',speed.raw],['device',speed.device]]) {
    $(prefix+'-down').textContent=formatSpeed(result?.download_median_mbps);
    $(prefix+'-up').textContent=formatSpeed(result?.upload_median_mbps);
  }
  $('raw-context').textContent=speed.raw ? `Last result · ${speed.raw.server.name} · ${speed.raw.server.city} · ${speedTime(speed.raw.measured_at)} · ${speed.raw.public_ip}` : 'Not tested yet. Runs on the gateway, without Tailscale.';
  $('device-context').textContent=speed.device ? `Last result · ${speed.device.server.name} · ${speedTime(speed.device.measured_at)} · Proton IP verified: ${speed.device.public_ip}` : (speed.deviceMessage || 'Not tested yet. Select protonnode as this device’s exit node first.');
  const recentTest=data?.operations?.find(o=>o.request.action==='speedtest' && ['succeeded','failed','interrupted'].includes(o.state));
  const cooldown=recentTest ? Math.max(0,Math.ceil(30-(Date.now()/1000-recentTest.updated))) : 0;
  $('run-speed').disabled=speed.running || !!active || !!cooldown || !data?.ready || data?.connection!=='connected';
  $('run-speed').textContent=speed.running || active?.request.action==='speedtest' ? 'Testing…' : cooldown ? `Run again in ${cooldown}s` : 'Run speed test ↗';
  $('include-device').disabled=speed.running || !!active;
  $('speed-data-cost').textContent=$('include-device').checked ? 'Click to run · Up to 420 MB total · 210 MB on this device' : 'Click to run · About 210 MB on the gateway · Device test off';
  const running=speed.running || active?.request.action==='speedtest';
  $('speed-progress').hidden=!running;
  $('speed-stage').textContent=speed.phase==='device' ? speed.stage : active?.request.action==='speedtest' ? active.stage : speed.stage || 'Preparing the speed test';
}
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
const median = values => [...values].sort((a,b)=>a-b)[Math.floor(values.length/2)];
async function publicSpeedRequest(path,init,consume,timeout=30000) {
  // This client deliberately never receives the dashboard's API-key headers.
  const abort=new AbortController();
  const cancelled=()=>abort.abort();
  if(speed.abort?.signal.aborted) throw new Error('Device test stopped. Keep this page visible and click Run to try again.');
  speed.abort?.signal.addEventListener('abort',cancelled,{once:true});
  const timer=setTimeout(()=>abort.abort(),timeout);
  try {
    const response=await fetch('https://speed.cloudflare.com'+path,{...init,signal:abort.signal,mode:'cors',credentials:'omit',redirect:'error',cache:'no-store',referrerPolicy:'no-referrer'});
    if(!response.ok) throw new Error(`Cloudflare returned ${response.status}. No device result was recorded.`);
    return await consume(response);
  } catch(error) {
    if(abort.signal.aborted) throw new Error('Device test stopped or timed out. Keep this page visible and click Run to try again.');
    throw error;
  } finally {
    clearTimeout(timer);speed.abort?.signal.removeEventListener('abort',cancelled);abort.abort();
  }
}
async function devicePublicIP() {
  return publicSpeedRequest('/cdn-cgi/trace',{method:'GET'},async response=>{
    const match=(await response.text()).match(/^ip=(.+)$/m);
    if(!match) throw new Error('Could not verify this device’s public IP. No full-path result was recorded.');
    return match[1].trim();
  },15000);
}
async function measureDevice(raw) {
  speed.phase='device';speed.abort=new AbortController();speed.stage='Checking this device’s Proton IP';renderSpeed();
  if(await devicePublicIP()!==raw.public_ip) throw new Error('This device is not exiting through the tested Proton IP. Select protonnode in Tailscale and click Run again.');
  speed.deviceMessage='Proton IP verified. Measuring this device’s full exit path…';
  const result={download_mbps:[],upload_mbps:[],server:raw.server,public_ip:raw.public_ip,
    download_bytes:50000000,upload_bytes:20000000,samples:3,provider:'Cloudflare'};
  const upload=new Uint8Array(20000000),deadline=performance.now()+150000;
  for(const [direction,size] of [['download',50000000],['upload',20000000]]) {
    for(let sample=0;sample<3;sample++) {
      if(performance.now()>deadline) throw new Error('Device test exceeded its time limit. No device result was recorded.');
      speed.stage=`Testing this device’s ${direction} ${sample+1}/3`;renderSpeed();
      const started=performance.now();
      if(direction==='download') {
        await publicSpeedRequest(`/__down?bytes=${size}&nonce=${Date.now()}-${sample}`,{method:'GET'},async response=>{
          const reader=response.body.getReader();let count=0;
          try {while(true){const chunk=await reader.read();if(chunk.done) break;count+=chunk.value.byteLength;if(count>size) throw new Error('Unexpected download size');}}
          finally {reader.releaseLock();}
          if(count!==size) throw new Error('Incomplete device download. No device result was recorded.');
        });
      } else {
        await publicSpeedRequest('/__up',{method:'POST',body:upload,headers:{'Content-Type':'text/plain'}},async response=>{await response.arrayBuffer();});
      }
      const elapsed=(performance.now()-started)/1000;
      result[direction+'_mbps'].push(Math.round(size*8/elapsed/1000000*100)/100);
    }
  }
  speed.stage='Verifying the exit path after the test';renderSpeed();
  const latest=await api('/api/v1/status');
  if(latest.connection!=='connected' || latest.server?.hostname!==raw.server.hostname || latest.probe.public_ip!==raw.public_ip || await devicePublicIP()!==raw.public_ip) throw new Error('The exit path changed during the test. Device results were discarded.');
  result.download_median_mbps=median(result.download_mbps);result.upload_median_mbps=median(result.upload_mbps);result.measured_at=Date.now()/1000;
  return result;
}
async function runSpeedTests() {
  if(speed.running || state.busy || !state.status?.ready) return;
  const includeDevice=$('include-device').checked;
  speed.running=true;speed.phase='proton';speed.stage='Starting the Proton test';speed.deviceMessage=null;
  if(includeDevice){speed.device=null;try{sessionStorage.removeItem('protonnode-device-speed');}catch(_){}}
  state.busy=true;renderSpeed();renderServers();speedMessage('Tests run only after this click. The gateway test can finish even if this page is closed.');
  try {
    const key=globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    let operation=await api('/api/v1/operations',{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify({action:'speedtest'})});
    speed.operation=operation.id;
    const deadline=performance.now()+240000;
    while(!['succeeded','failed','interrupted'].includes(operation.state)) {
      speed.stage=operation.stage;renderSpeed();
      if(performance.now()>deadline) throw new Error('The gateway test is still running. Its result will appear when it finishes; it will not be restarted automatically.');
      await pause(1200);operation=await api('/api/v1/operations/'+operation.id);
    }
    if(operation.state!=='succeeded') throw new Error(operation.error || operation.stage);
    speed.raw=operation.result;renderSpeed();
    if(includeDevice && !state.status.demo) {
      speed.device=await measureDevice(speed.raw);
      try{sessionStorage.setItem('protonnode-device-speed',JSON.stringify(speed.device));}catch(_){}
      speedMessage(`Both results used ${speed.raw.server.name} with verified Proton IP ${speed.raw.public_ip}.`);
    } else speedMessage(state.status.demo ? 'Synthetic preview only. No external device test was started.' : 'Raw Proton test complete. The device test was not requested.');
  } catch(error) {
    const message=error instanceof TypeError ? 'The browser could not reach Cloudflare. Check connectivity or browser blocking and click Run again.' : error.message;
    if(speed.phase==='device') speed.deviceMessage=message;
    speedMessage(message,true);
  } finally {
    speed.abort?.abort();speed.running=false;speed.phase=null;speed.stage='';
    await poll();renderSpeed();
  }
}
$('run-speed').addEventListener('click',runSpeedTests);
$('include-device').addEventListener('change',()=>renderSpeed());
document.addEventListener('visibilitychange',()=>{if(document.hidden && speed.phase==='device') speed.abort?.abort();});

async function initialize(){try{await loadCatalog();await poll();}catch(error){notice(error.message,true);}}
initialize();
let polling=false;
setInterval(async()=>{if(document.hidden || polling) return;polling=true;try{await poll();}finally{polling=false;}},4000);
