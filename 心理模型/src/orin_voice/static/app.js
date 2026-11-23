'use strict';
const $ = id => document.getElementById(id);
const phaseNames = {idle:'等待开始',ready:'等待回答',preparing:'正在准备语音',listening:'正在听你说',speaking:'正在提问 / 回复',transcribing:'正在识别回答',stopping:'正在结束',stopped:'已结束',completed:'已完成',error:'设备启动失败',archived:'历史记录'};
const statusNames = {in_progress:'进行中',completed:'已完成',stopped:'已结束',pending:'待回答',answering:'待选分',answered:'已回答',skipped:'已跳过'};
let selectedId = null, activeId = null, state = null, config = null, busy = false, connected = false, answerSignature = '', eventSignature = '', historySignature = '';
function element(tag, className, text) { const el = document.createElement(tag); if(className) el.className=className; if(text !== undefined) el.textContent=text; return el; }
function showError(message) { $('error').textContent=message || ''; $('error').hidden=!message; }
async function api(path, options={}) {
  const response = await fetch(path,{cache:'no-store',...options});
  let data; try { data=await response.json(); } catch { throw new Error('服务返回了无法读取的响应'); }
  if(!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}
function sessionUrl(id) { return '/api/sessions/'+encodeURIComponent(id); }
function editable() { return connected && state && state.mode!=='archived' && state.status==='in_progress' && !['preparing','stopping','error'].includes(state.phase); }
function setButtons() {
  const enabled = !!editable() && !busy;
  for(const id of ['repeat','skip','answer-text','send']) $(id).disabled=!enabled;
  $('previous').disabled=!enabled || !state || state.current_index===0;
  document.querySelectorAll('.option').forEach(el=>el.disabled=!enabled);
  $('start').disabled=!connected || busy || (activeId && window.activeStatus==='in_progress') || window.engineStopping;
  $('stop').disabled=busy || !state || state.mode==='archived' || state.status!=='in_progress' || state.phase==='stopping';
  $('mode').disabled=!!(activeId && window.activeStatus==='in_progress');
}
function renderOptions() {
  $('options').replaceChildren();
  Object.entries(config.response_options).forEach(([score,label])=>{
    const button=element('button','option'); button.type='button'; button.append(element('strong','',score),element('span','',label));
    button.addEventListener('click',()=>action('answer',{score:Number(score)})); $('options').append(button);
  });
  setButtons();
}
function renderHistory(items) {
  const signature=JSON.stringify([items,selectedId]); if(signature===historySignature) return; historySignature=signature;
  $('history').replaceChildren(); if(!items.length) { $('history').append(element('p','muted','还没有会话')); return; }
  items.forEach(item=>{
    const label=item.created_at ? new Date(item.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : item.session_id;
    const button=element('button',item.session_id===selectedId?'active':'',label);
    button.append(element('span','',`${statusNames[item.status]||item.status} · ${item.completed_count||0} / 21 已回答`));
    button.addEventListener('click',()=>selectSession(item.session_id)); $('history').append(button);
  });
}
async function selectSession(id) { selectedId=id; answerSignature=''; eventSignature=''; showError(''); try { render(await api(sessionUrl(id))); } catch(e) { showError(e.message); } }
function render(data) {
  state=data; selectedId=data.session_id;
  const q=data.current_question, questions=data.questions||[], answered=questions.filter(q=>q.status==='answered').length, skipped=questions.filter(q=>q.status==='skipped').length;
  const unrecognized=data.unrecognized_segments||[];
  const audioCount=questions.reduce((n,q)=>n+(q.segments||[]).filter(s=>s.audio_path).length,0)+unrecognized.length;
  $('phase').textContent=phaseNames[data.phase]||data.phase;
  $('progress-count').textContent=`${answered+skipped} / 21`;
  $('progress').value=answered+skipped;
  $('progress-label').textContent=data.status==='completed'?'本次访谈已完成，记录已保存。':(data.mode==='archived'?'已保存的历史记录':`已记录 ${answered} 题${skipped?'，跳过 '+skipped+' 题':''}`);
  $('question-number').textContent=q ? `问题 ${String(data.current_index+1).padStart(2,'0')} / 21` : (data.status==='completed'?'感谢你的参与':'会话记录');
  $('question').textContent=q ? q.question : (data.status==='completed'?'这一段对话，已经好好记录。':'这次访谈已保存。');
  $('question-help').textContent=q ? '请回想过去一周。这句话有多符合你的情况？说出或点击一个分值，我们会继续下一题。' : (data.overall_summary||'可以在右侧查看逐题回答和录音，也可以导出 JSON。');
  $('answered-count').textContent=answered; $('skipped-count').textContent=skipped; $('audio-count').textContent=audioCount;
  $('save-note').textContent=`会话 ${data.session_id}`;
  $('listening-text').textContent=data.phase==='preparing'?'正在连接开发板的麦克风和耳机……':data.phase==='transcribing'?'正在识别这段回答……':data.phase==='speaking'?'正在播放题目，你可以随时打断。':data.mode==='text'?'当前为文字模式，点击选分或输入回答。':data.phase==='listening'?'正在持续收音，说出零到三分即可。':'回答与录音保存在本机。';
  $('input-level').hidden=data.mode!=='voice';
  $('input-meter').value=Math.min(100,Math.sqrt((data.audio?.peak_rms||0)/.08)*100);
  $('input-label').textContent=data.audio?.speech_detected?'正在接收你的声音':'麦克风输入';
  if(data.phase==='listening'&&data.audio?.speech_detected) $('listening-text').textContent='听到你在说话，停顿后会识别并保存。';
  $('listening').classList.toggle('live',['speaking','listening','transcribing'].includes(data.phase));
  const json=$('download-json'); json.href=sessionUrl(data.session_id)+'/answers.json'; json.classList.remove('disabled');
  const summary=$('download-summary'); summary.href=sessionUrl(data.session_id)+'/summary.txt'; summary.classList.toggle('disabled',!data.overall_summary);
  $('scores').replaceChildren();
  const labels={depression:'抑郁维度',anxiety:'焦虑维度',stress:'压力维度'};
  Object.entries(data.scoring?.subscales||{}).forEach(([key,value])=>{ const row=element('div'); row.append(element('span','',labels[key]||key),element('span','',value.complete?`${value.doubled_score} / 42`:'未答齐')); $('scores').append(row); });
  const signature=JSON.stringify([data.session_id,questions,unrecognized]);
  if(signature!==answerSignature) { answerSignature=signature; renderAnswers(questions,unrecognized); }
  const events=data.events||[]; const eSignature=JSON.stringify([data.session_id,events]);
  if(eSignature!==eventSignature) { eventSignature=eSignature; const box=$('transcript'); const nearBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-50; box.replaceChildren(); if(!events.length) box.append(element('div','empty','历史回答请在“逐题回答与录音”中查看。')); events.forEach(event=>{ const row=element('div','message '+event.role); row.append(element('span','avatar',event.role==='user'?'你':event.role==='assistant'?'心':'!'),element('div','text',event.text)); box.append(row); }); if(nearBottom) box.scrollTop=box.scrollHeight; }
  if(data.error) showError(data.error);
  setButtons();
}
function renderAnswers(questions,unrecognized=[]) {
  const box=$('answers-list'); const opened=new Set([...box.querySelectorAll('details[open]')].map(el=>el.dataset.id)); box.replaceChildren();
  const recorded=questions.filter(q=>q.status!=='pending'||q.segments?.length);
  if(!recorded.length&&!unrecognized.length) { box.append(element('div','empty','暂无已保存的回答。')); return; }
  recorded.forEach(q=>{
    const item=element('details','answer-item'); item.dataset.id=q.id; item.open=opened.has(q.id);
    const summary=element('summary'); summary.append(element('span','',q.id+' · '+(statusNames[q.status]||q.status)),element('span','',q.score===null||q.score===undefined?'—':q.score+' 分'));
    item.append(summary,element('p','item-question',q.question),element('div','answer-content',q.answer_text||'此题未记录文字回答。'));
    (q.segments||[]).forEach((segment,index)=>{
      if(segment.audio_path) { const label=element('div','muted small',`回答录音 ${index+1}`); const audio=element('audio'); audio.controls=true; audio.preload='none'; audio.src=sessionUrl(selectedId)+'/audio/'+encodeURIComponent(segment.audio_path.split('/').pop()); item.append(label,audio); }
    }); box.append(item);
  });
  unrecognized.forEach((segment,index)=>{
    const item=element('details','answer-item');
    const summary=element('summary','',`${segment.question_id} · 未识别的录音 ${index+1}`);
    const audio=element('audio'); audio.controls=true; audio.preload='none';
    audio.src=sessionUrl(selectedId)+'/audio/'+encodeURIComponent(segment.audio_path.split('/').pop());
    item.append(summary,element('p','muted small','检测到了声音，但未得到文字；该段不计分。'),audio);box.append(item);
  });
}
async function action(name, extra={}) {
  if(!selectedId||busy) return; busy=true; setButtons(); showError('');
  try {
    const data=await api(sessionUrl(selectedId)+'/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:name,question_id:state.current_question?.id,...extra})});
    render(data); if(name==='answer') $('answer-text').value='';
  } catch(e) { showError(e.message); } finally { busy=false; setButtons(); }
}
async function poll() {
  try {
    const status=await api('/api/status'); activeId=status.active_session_id;
    window.engineStopping=['stopping'].includes(status.phase);
    if(!selectedId && activeId) selectedId=activeId;
    if(activeId) { const live=await api(sessionUrl(activeId)); window.activeStatus=live.status; if(selectedId===activeId) render(live); }
    else window.activeStatus=null;
    if(selectedId && selectedId!==activeId) render(await api(sessionUrl(selectedId)));
    renderHistory((await api('/api/sessions')).sessions);
    connected=true; $('connection').textContent='本机服务已连接'; setButtons();
  } catch(e) { connected=false; $('connection').textContent='服务未连接'; showError('无法连接本机服务，请检查网页服务是否仍在运行。'); }
  setTimeout(poll,1000);
}
$('start').addEventListener('click',async()=>{
  if(busy) return; busy=true; setButtons(); showError('');
  try { const data=await api('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('mode').value})}); activeId=data.session_id; window.activeStatus=data.status; answerSignature=''; eventSignature=''; render(data); }
  catch(e) { showError(e.message); } finally { busy=false; setButtons(); }
});
$('stop').addEventListener('click',()=>action('stop'));
$('repeat').addEventListener('click',()=>action('repeat'));
$('skip').addEventListener('click',()=>action('skip'));
$('previous').addEventListener('click',()=>action('previous'));
$('answer-form').addEventListener('submit',event=>{event.preventDefault(); const text=$('answer-text').value.trim(); if(text) action('answer',{text});});
$('live-nav').addEventListener('click',()=>{if(activeId) selectSession(activeId);});
$('refresh-history').addEventListener('click',async()=>{try { historySignature=''; renderHistory((await api('/api/sessions')).sessions); } catch(e) {showError(e.message);} });
(async()=>{try {config=await api('/api/questions'); renderOptions(); await poll();}catch(e){showError(e.message);$('connection').textContent='服务未连接';}})();
