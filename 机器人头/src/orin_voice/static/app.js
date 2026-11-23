'use strict';
const $ = id => document.getElementById(id);
const phaseNames = {idle:'等待开始',ready:'等待回答',preparing:'正在准备语音',listening:'正在听你说',speaking:'正在提问 / 回复',transcribing:'正在识别回答',stopping:'正在结束',stopped:'已结束',completed:'已完成',error:'设备启动失败',archived:'历史记录'};
const statusNames = {in_progress:'进行中',completed:'已完成',stopped:'已结束',pending:'待回答',answering:'待选分',answered:'已回答',skipped:'已跳过'};
let selectedId = null, activeId = null, state = null, config = null, busy = false, connected = false, answerSignature = '', eventSignature = '', historySignature = '', optionSignature = '';
let canReadRecords = false;
function element(tag, className, text) { const el = document.createElement(tag); if(className) el.className=className; if(text !== undefined) el.textContent=text; return el; }
function showError(message) { $('error').textContent=message || ''; $('error').hidden=!message; }
async function api(path, options={}) {
  const response = await fetch(path,{cache:'no-store',...options});
  let data; try { data=await response.json(); } catch { throw new Error('服务返回了无法读取的响应'); }
  if(!response.ok) {
    const err=new Error(data.error || `请求失败（${response.status}）`);err.status=response.status;throw err;
  }
  return data;
}
function sessionUrl(id) { return '/api/sessions/'+encodeURIComponent(id); }
function editable() { return connected && state && state.mode!=='archived' && state.status==='in_progress' && !['preparing','stopping','error'].includes(state.phase); }
function setButtons() {
  const enabled = !!editable() && !busy;
  for(const id of ['repeat','skip','answer-text','send']) $(id).disabled=!enabled;
  $('previous').disabled=!enabled || !state || ['tasks','task_reflection','intake','greeting'].includes(state.stage) || state.current_index===0;
  $('finish-task').hidden=state?.stage!=='tasks';
  $('finish-task').disabled=!enabled||!state?.current_question?.segments?.length;
  const scoredStage=!state || !state.stage || ['assessment','task_reflection'].includes(state.stage);
  document.querySelectorAll('.option').forEach(el=>el.disabled=!enabled||!scoredStage);
  $('options').hidden=!scoredStage || !!state && state.status!=='in_progress';
  $('begin-assessment').disabled=!enabled;
  $('begin-assessment').hidden=state?.stage!=='greeting';
  $('start').disabled=!connected || busy || (activeId && window.activeStatus==='in_progress') || window.engineStopping;
  $('stop').disabled=busy || !state || state.mode==='archived' || state.status!=='in_progress' || state.phase==='stopping';
  $('mode').disabled=!!(activeId && window.activeStatus==='in_progress');
  safetyNotice(state);
}
function safetyNotice(data) {
  if(!data?.safety_hold)return;
  $('question').textContent='我们先停下来，请身边信任的大人陪着你。';
  $('question-help').textContent='可以请老师过来，也可以选择“结束并保存”。';
  for(const id of ['repeat','skip','previous','finish-task','answer-text','send'])$(id).disabled=true;
  $('options').hidden=true;$('begin-assessment').hidden=true;
}
function primary(data=state) { return (data?.instrument||config?.instrument)==='PRIMARY-21'; }
function renderOptions(data=state) {
  const responseOptions=data?.response_options || (primary(data)?{'0':'没有','1':'偶尔','2':'有时','3':'经常'}:config?.response_options) || {};
  const signature=JSON.stringify(responseOptions); if(signature===optionSignature) return; optionSignature=signature;
  $('options').replaceChildren();
  Object.entries(responseOptions).forEach(([score,label])=>{
    const button=element('button','option'); button.type='button'; button.append(element('strong','',score),element('span','',label));
    button.addEventListener('click',()=>action('answer',{score:Number(score)})); $('options').append(button);
  });
  setButtons();
}
function renderScores(data) {
  $('scores').replaceChildren();
  const isPrimary=primary(data), scoring=data.scoring||{}, task=data.task_scoring||{};
  const labels=isPrimary?{mood:'心情与兴趣',worry:'担心与身体感受',tension:'紧绷与调节'}:{depression:'抑郁维度',anxiety:'焦虑维度',stress:'压力维度'};
  if(isPrimary) {
    const first=scoring.complete&&scoring.total_score!==null&&scoring.total_score!==undefined?`${scoring.total_score} / 63`:'还没有答齐';
    const second=task.complete&&task.total_score!==null&&task.total_score!==undefined?`${task.total_score} / ${task.total_range?.[1]||9}`:'请看下面三项记录';
    $('score-overview').textContent=`二十一题自评：${first}。任务后自评：${second}。`;
    $('score-note').textContent='两份记录都来自你自己的选择，题数不同，总分不直接比较。朗读和故事不会自动变成心理分数；这些题目还没有经过正式验证。';
    Object.entries(scoring.subscales||{}).forEach(([key,value])=>{
      const row=element('div'); const after=task.subscales?.[key];
      const before=value.complete?`${typeof value.mean_score==='number'?Number(value.mean_score.toFixed(2)):'—'} / 3`:'未答齐';
      row.append(element('span','',labels[key]||key),element('span','',`21题均值 ${before} · 分享后 ${after?.complete?after.raw_score+' / 3':'未答齐'}`)); $('scores').append(row);
    });
  } else {
    $('score-overview').textContent='历史 DASS-21 记录';
    $('score-note').textContent='历史量表使用原有计分规则，不与小学自编题目直接比较。';
    Object.entries(scoring.subscales||{}).forEach(([key,value])=>{ const row=element('div'); row.append(element('span','',labels[key]||key),element('span','',value.complete?`${value.doubled_score} / 42`:'未答齐')); $('scores').append(row); });
  }
}
function renderHistory(items) {
  const signature=JSON.stringify([items,selectedId]); if(signature===historySignature) return; historySignature=signature;
  $('history').replaceChildren(); if(!items.length) { $('history').append(element('p','muted','还没有会话')); return; }
  items.forEach(item=>{
    const label=item.created_at ? new Date(item.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : item.session_id;
    const button=element('button',item.session_id===selectedId?'active':'',label);
    button.append(element('span','',`${statusNames[item.status]||item.status} · ${item.completed_count||0} / ${item.total_questions||21} 已回答`));
    button.addEventListener('click',()=>selectSession(item.session_id)); $('history').append(button);
  });
}
async function selectSession(id) { selectedId=id; answerSignature=''; eventSignature=''; showError(''); try { render(await api(sessionUrl(id))); } catch(e) { showError(e.message); } }
function render(data) {
  state=data; selectedId=data.session_id;
  const q=data.current_question, questions=data.questions||[], answered=questions.filter(q=>q.status==='answered').length, skipped=questions.filter(q=>q.status==='skipped').length;
  const intake=data.intake_questions||[], intakeAnswered=intake.filter(q=>q.status==='answered').length, intakeSkipped=intake.filter(q=>q.status==='skipped').length;
  const tasks=data.tasks||[], taskAnswered=tasks.filter(q=>q.status==='answered').length, taskSkipped=tasks.filter(q=>q.status==='skipped').length;
  const reflections=data.task_reflections||[], reflectionAnswered=reflections.filter(q=>q.status==='answered').length, reflectionSkipped=reflections.filter(q=>q.status==='skipped').length;
  const total=data.total_questions||(questions.length+tasks.length+reflections.length)||21, done=answered+skipped+taskAnswered+taskSkipped+reflectionAnswered+reflectionSkipped;
  const workflowDone=done;
  const unrecognized=data.unrecognized_segments||[];
  const audioCount=questions.reduce((n,q)=>n+(q.segments||[]).filter(s=>s.audio_path).length,0)+unrecognized.length;
  $('phase').textContent=phaseNames[data.phase]||data.phase;
  $('progress-count').textContent=`${workflowDone} / ${total}`;
  $('progress').max=total; $('progress').value=workflowDone;
  $('progress-label').textContent=data.status==='completed'?'本次访谈已完成，记录已保存。':(data.mode==='archived'?'已保存的历史记录':`已记录 ${workflowDone} 项${skipped+taskSkipped+intakeSkipped+reflectionSkipped?'，跳过 '+(skipped+taskSkipped+intakeSkipped+reflectionSkipped)+' 项':''}`);
  const reflectionIndex=Math.max(0,reflections.findIndex(item=>item.id===q?.id));
  const currentNumber=data.stage==='task_reflection'?questions.length+tasks.length+reflectionIndex+1:data.stage==='tasks' ? questions.length+(data.task_index||0)+1 : (data.current_index||0)+1;
  $('question-number').textContent=q ? `问题 ${String(currentNumber).padStart(2,'0')} / ${total}` : (data.status==='completed'?'感谢你的参与':'会话记录');
  if(data.stage==='intake') $('question-number').textContent=`基本资料 ${(data.intake_index||0)+1} / 5`;
  $('question').textContent=q ? q.question : (data.status==='completed'?'这一段对话，已经好好记录。':'这次访谈已保存。');
  $('question-help').textContent=q ? (data.stage==='intake' ? '先认识一下你。暂时不想说的话，可以跳过。' : data.stage==='tasks' ? '可以慢慢说，分几次补充也可以。说完后告诉我“回答完了”，或点“完成当前任务”。' : data.stage==='task_reflection' ? '分享结束啦。再想一想这一周，选一个最像你的答案：没有、偶尔、有时、经常。' : primary(data)?'想想过去这一周，这种感觉出现得多吗？选择“没有、偶尔、有时、经常”就好。':'请回想过去一周，选择零到三分。') : (data.overall_summary||'你的回答已经保存，老师可以查看。');
  if(data.stage==='task_reflection') $('question-number').textContent=`分享后的自评 ${reflectionIndex+1} / ${reflections.length||3}`;
  if(data.stage==='greeting'&&data.status==='in_progress') {
    $('question-number').textContent='机器人已唤醒 · 自由交谈';
    $('question').textContent='你好，我是小心，很高兴见到你。';
    $('question-help').textContent='可以先聊一聊。准备好了就告诉我，或点下面的按钮，我们聊聊这一周的二十一件小事。';
  }
  $('answered-count').textContent=answered+taskAnswered+intakeAnswered+reflectionAnswered; $('skipped-count').textContent=skipped+taskSkipped+intakeSkipped+reflectionSkipped; $('audio-count').textContent=audioCount+intake.reduce((n,q)=>n+(q.segments||[]).filter(s=>s.audio_path).length,0)+tasks.reduce((n,q)=>n+(q.segments||[]).filter(s=>s.audio_path).length,0)+reflections.reduce((n,q)=>n+(q.segments||[]).filter(s=>s.audio_path).length,0);
  $('save-note').textContent=`会话 ${data.session_id}`;
  $('listening-text').textContent=data.phase==='preparing'?'正在连接开发板的麦克风和耳机……':data.phase==='transcribing'?'正在识别这段回答……':data.phase==='speaking'?'正在播放题目，你可以随时打断。':data.mode==='text'?'当前为文字模式，点击选分或输入回答。':data.phase==='listening'?(['assessment','task_reflection'].includes(data.stage)?'我在听，可以说没有、偶尔、有时、经常。':'我在听，慢慢说就好。'):'回答与录音保存在本机。';
  $('input-level').hidden=data.mode!=='voice';
  $('input-meter').value=Math.min(100,Math.sqrt((data.audio?.peak_rms||0)/.08)*100);
  $('input-label').textContent=data.audio?.speech_detected?'正在接收你的声音':'麦克风输入';
  if(data.phase==='listening'&&data.audio?.speech_detected) $('listening-text').textContent='听到你在说话，停顿后会识别并保存。';
  $('listening').classList.toggle('live',['speaking','listening','transcribing'].includes(data.phase));
  if(canReadRecords) {
    const json=$('download-json'); json.href=sessionUrl(data.session_id)+'/answers.json'; json.classList.remove('disabled');
    const summary=$('download-summary'); summary.href=sessionUrl(data.session_id)+'/summary.txt'; summary.classList.toggle('disabled',!data.overall_summary);
    for(const [id,file] of [['download-package','session_package.json'],['download-zip','session_package.zip']]) {
      $(id).href=sessionUrl(data.session_id)+'/'+file;$(id).classList.toggle('disabled',!data.export_ready);
    }
    const recordings=data.full_audio?.tracks||[];
    if($('full-recordings').dataset.session!==data.session_id||$('full-recordings').dataset.count!==String(recordings.length)) {
      $('full-recordings').replaceChildren();
      recordings.forEach(track=>{const audio=element('audio');audio.controls=true;audio.preload='none';audio.src=sessionUrl(data.session_id)+'/'+track.path;
        $('full-recordings').append(element('p','muted small',track.speaker==='microphone'?'整场麦克风录音':'机器人实际输出语音（分段紧凑轨道）'),audio);});
      $('full-recordings').dataset.session=data.session_id;$('full-recordings').dataset.count=String(recordings.length);
    }
    renderScores(data);
    const signature=JSON.stringify([data.session_id,intake,questions,unrecognized,tasks,reflections]);
    if(signature!==answerSignature) { answerSignature=signature; renderAnswers([...intake,...questions],unrecognized,[...tasks,...reflections]); }
  }
  renderOptions(data);
  const finished=!canReadRecords && data.status!=='in_progress';
  if(finished) {
    $('intake-form').reset(); $('answer-text').value=''; $('question-help').textContent='老师可以查看已保存的访谈。';
    $('save-note').textContent='本次访谈已保存。';
  }
  const events=finished?[]:data.events||[]; const eSignature=JSON.stringify([data.session_id,events]);
  if(eSignature!==eventSignature) { eventSignature=eSignature; const box=$('transcript'); const nearBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-50; box.replaceChildren(); if(!events.length) box.append(element('div','empty',finished?'本次对话已结束。':'回答后在这里显示本次对话。')); events.forEach(event=>{ const row=element('div','message '+event.role); row.append(element('span','avatar',event.role==='user'?'你':event.role==='assistant'?'心':'!'),element('div','text',event.text)); box.append(row); }); if(nearBottom) box.scrollTop=box.scrollHeight; }
  if(data.error) showError(data.error);
  setButtons();
}
function renderAnswers(questions,unrecognized=[],tasks=[]) {
  const box=$('answers-list'); const opened=new Set([...box.querySelectorAll('details[open]')].map(el=>el.dataset.id)); box.replaceChildren();
  const recorded=questions.filter(q=>q.status!=='pending'||q.segments?.length);
  if(!recorded.length&&!unrecognized.length&&!tasks.some(q=>q.status!=='pending'||q.segments?.length)) { box.append(element('div','empty','暂无已保存的回答。')); return; }
  recorded.forEach(q=>{
    const item=element('details','answer-item'); item.dataset.id=q.id; item.open=opened.has(q.id);
    const summary=element('summary'); summary.append(element('span','',q.id+' · '+(statusNames[q.status]||q.status)),element('span','',q.score===null||q.score===undefined?'—':q.score+' 分'));
    item.append(summary,element('p','item-question',q.question),element('div','answer-content',q.answer_text||'此题未记录文字回答。'));
    (q.segments||[]).forEach((segment,index)=>{
      if(segment.audio_path) { const label=element('div','muted small',`回答录音 ${index+1}`); const audio=element('audio'); audio.controls=true; audio.preload='none'; audio.src=sessionUrl(selectedId)+'/audio/'+encodeURIComponent(segment.audio_path.split('/').pop()); item.append(label,audio); }
    }); box.append(item);
  });
  tasks.filter(q=>q.status!=='pending'||q.segments?.length).forEach(q=>{
    const item=element('details','answer-item'); item.dataset.id=q.id;
    const summary=element('summary'); summary.append(element('span','',q.title||q.id),element('span','',(statusNames[q.status]||q.status)+(q.score!==null&&q.score!==undefined?' · '+q.score+' 分':'')));
    item.append(summary,element('p','item-question',q.question),element('div','answer-content',q.answer_text||'此任务未记录文字回答。'));
    (q.segments||[]).forEach((segment,index)=>{if(segment.audio_path){const audio=element('audio');audio.controls=true;audio.preload='none';audio.src=sessionUrl(selectedId)+'/audio/'+encodeURIComponent(segment.audio_path.split('/').pop());item.append(element('div','muted small',`任务录音 ${index+1}`),audio);}});
    box.append(item);
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
function renderRobot(robot) {
  const phases={disabled:'未启用自动唤醒',standby:'正在待机',active:'正在与用户交互'};
  $('robot-state').textContent=phases[robot.phase]||robot.phase;
  const hardware=robot.hardware||{},vision=robot.vision||{},voice=robot.voice_wake||{},chat=robot.dialogue||{};
  $('robot-detail').textContent=[hardware.backend==='dry_run'?'头部动作：模拟（未运动）':`头部连接：${hardware.connected?'已连接':'未连接'}`,`视觉：${vision.phase}`,`语音唤醒：${voice.phase}`,`对话模型：${chat.phase}`,robot.error||vision.error||voice.error||''].filter(Boolean).join(' · ');
  $('robot-arm').disabled=robot.enabled;$('robot-disarm').disabled=!robot.enabled;
  $('robot-wake').disabled=busy||!!(activeId&&window.activeStatus==='in_progress');
}
async function robotAction(name) {
  if(busy)return;busy=true;setButtons();showError('');
  try{const result=await api('/api/robot/actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:name,mode:$('mode').value})});
    if(result.session_id){activeId=result.session_id;window.activeStatus=result.status;render(result);}else renderRobot(result);
  }catch(e){showError(e.message);}finally{busy=false;setButtons();}
}
async function poll() {
  try {
    const status=await api('/api/status');
    const nextActive=status.active_session_id;
    if(!canReadRecords && selectedId && selectedId!==nextActive) {
      window.schoolIdentity.leave('access'); return;
    }
    activeId=nextActive;
    window.engineStopping=['occupied','stopping'].includes(status.phase);
    if(!selectedId && activeId) selectedId=activeId;
    if(activeId) { const live=await api(sessionUrl(activeId)); window.activeStatus=live.status; if(selectedId===activeId) render(live); }
    else window.activeStatus=null;
    if(selectedId && selectedId!==activeId) render(await api(sessionUrl(selectedId)));
    if(canReadRecords) renderHistory((await api('/api/sessions')).sessions);
    renderRobot(await api('/api/robot/status'));
    connected=true; $('connection').textContent=status.phase==='occupied'?'设备正在进行另一场访谈':'本机服务已连接'; setButtons();
  } catch(e) { if(e.status===401 || e.status===403) { window.schoolIdentity.leave('access'); return; } connected=false; $('connection').textContent='服务未连接'; showError('无法连接本机服务，请检查网页服务是否仍在运行。'); }
  setTimeout(poll,1000);
}
$('start').addEventListener('click',async()=>{
  if(busy) return; busy=true; setButtons(); showError('');
  try { if(!$('intake-form').reportValidity()) return; const profile={name:$('profile-name').value.trim(),height_cm:$('profile-height').value,weight_kg:$('profile-weight').value,gender:$('profile-gender').value.trim(),age:$('profile-age').value}; const data=await api('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:$('mode').value,workflow:true,robot:$('mode').value==='voice',profile})}); activeId=data.session_id; window.activeStatus=data.status; answerSignature=''; eventSignature=''; render(data); }
  catch(e) { showError(e.message); } finally { busy=false; setButtons(); }
});
$('robot-arm').addEventListener('click',()=>robotAction('arm'));
$('robot-wake').addEventListener('click',()=>robotAction('wake'));
$('robot-disarm').addEventListener('click',()=>robotAction('disarm'));
$('begin-assessment').addEventListener('click',()=>action('answer',{text:'开始测评'}));
$('stop').addEventListener('click',()=>action('stop'));
$('repeat').addEventListener('click',()=>action('repeat'));
$('skip').addEventListener('click',()=>action('skip'));
$('finish-task').addEventListener('click',()=>action('finish'));
$('previous').addEventListener('click',()=>action('previous'));
$('answer-form').addEventListener('submit',event=>{event.preventDefault(); const text=$('answer-text').value.trim(); if(text) action('answer',{text});});
$('live-nav').addEventListener('click',()=>{if(activeId) selectSession(activeId);});
$('refresh-history').addEventListener('click',async()=>{try { historySignature=''; if(canReadRecords) renderHistory((await api('/api/sessions')).sessions); } catch(e) {showError(e.message);} });
(async()=>{try {
  const actor=await window.schoolIdentity.watch();
  canReadRecords=actor?.role==='teacher';
  document.querySelectorAll('[data-staff-only]').forEach(node=>node.hidden=!canReadRecords);
  config=await api('/api/questions'); renderOptions(); await poll();
}catch(e){showError(e.message);$('connection').textContent='服务未连接';}})();
