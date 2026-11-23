'use strict';
const $ = id => document.getElementById(id);
let selected = /^[a-zA-Z0-9_-]{1,100}$/.test(location.hash.slice(1)) ? location.hash.slice(1) : null;
const statuses={in_progress:'进行中',completed:'已完成',stopped:'已结束',pending:'待回答',answering:'回答中',answered:'已回答',skipped:'已跳过'};
const labels={name:'姓名',height_cm:'身高（cm）',weight_kg:'体重（kg）',gender:'性别',age:'年龄'};
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(path) { const response = await fetch(path,{cache:'no-store'}); const data=await response.json(); if(response.status===401 || response.status===403) window.schoolIdentity.leave('account'); if(!response.ok) throw new Error(data.error||`请求失败（${response.status}）`); return data; }
function showError(error) { $('error').textContent=error ? error.message || error : ''; $('error').hidden=!error; }
function date(value) { if(!value) return '—'; const d=new Date(value); return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN'); }
function sessionUrl(id) { return '/api/sessions/'+encodeURIComponent(id); }
function renderSessions(items) {
  const box=$('session-list'); box.replaceChildren();
  if(!items.length) { box.innerHTML='<p class="empty">还没有可查看的访谈记录。</p>'; return; }
  items.forEach(item=>{
    const button=document.createElement('button'); button.className='session-row'+(item.id===selected?' active':'');
    button.innerHTML=`<strong>${esc(item.name||'未填写姓名')}</strong><span>${esc(date(item.started_at))} · ${esc(statuses[item.status]||item.status)} · 音频 ${item.audio_count||0} · 视频 ${item.video_count||0}</span>`;
    button.onclick=()=>loadDetail(item.id); box.append(button);
  });
}
function mediaHtml(items) {
  if(!items?.length) return '<p class="muted small">没有保存录音或录像。</p>';
  return '<div class="media-grid">'+items.map(item=>{
    const src='/api/teacher/media/'+encodeURIComponent(item.id);
    const label=`${item.kind==='video'?'录像':'录音'} · ${esc(date(item.created_at))}`;
    return `<div class="media-item"><p>${label}<br>${esc(item.path)}</p>${item.kind==='video'?`<video controls preload="metadata" src="${src}"></video>`:`<audio controls preload="none" src="${src}"></audio>`}<a class="small" href="${src}" download>下载原文件</a></div>`;
  }).join('')+'</div>';
}
const primaryNames={mood:'心情与兴趣',worry:'担心与身体感受',tension:'紧绷与调节'};
function isPrimary(answers) { return answers.instrument==='PRIMARY-21'||answers.scoring?.instrument==='PRIMARY-21'||!!answers.scoring?.subscales?.mood; }
function numeric(value, digits=2) { return typeof value==='number'&&Number.isFinite(value)?Number(value.toFixed(digits)).toString():'—'; }
function scoresHtml(answers) {
  const scoring=answers.scoring||{}, subscales=scoring.subscales||{};
  if(!Object.keys(subscales).length) return '<p class="muted">暂无计分记录。</p>';
  if(isPrimary(answers)) {
    return `<div class="score-total"><strong>二十一题明确自评</strong><span>${scoring.complete?numeric(scoring.total_score)+' / 63':'未答齐，暂不计算总分'}</span><small>总均值 ${scoring.complete?numeric(scoring.mean_score):'—'} / 3 · 困扰出现频率</small></div><div class="score-grid">`+Object.entries(subscales).map(([key,x])=>`<div><label>${esc(primaryNames[key]||key)}</label><strong>${x.complete?numeric(x.raw_score)+' / '+esc(x.raw_range?.[1]||21):'未答齐'}</strong><span>均值 ${x.complete?numeric(x.mean_score):'—'} / 3 · ${esc(x.answered_count??x.answered??(7-(x.missing_item_ids||[]).length))} / 7 已回答</span></div>`).join('')+'</div><p class="basis-note">依据：每题由学生明确选择 0 没有、1 偶尔、2 有时、3 经常，按七题一维累加，并计算均值。缺少有效选择时，该维不计分，不用叙述补猜。</p>';
  }
  const names={depression:'抑郁维度',anxiety:'焦虑维度',stress:'压力维度'};
  return '<div class="score-grid">'+Object.entries(subscales).map(([key,x])=>`<div><label>${esc(names[key]||key)}</label><strong>${x.complete?esc(x.doubled_score)+' / 42':'未答齐'}</strong><span>原始分 ${esc(x.raw_score??'—')} · ${esc(x.answered_count??x.answered??(7-(x.missing_item_ids||[]).length))} / 7 已回答</span></div>`).join('')+'</div><p class="muted small">历史 DASS-21 按原规则乘二。此计分不能与小学自编题目直接比较，自述记录不构成诊断。</p>';
}
function taskScoresHtml(answers) {
  const scoring=answers.task_scoring;
  if(!scoring?.subscales) return '<p class="muted">尚未记录任务后的三项明确自评。</p>';
  return `<div class="score-total"><strong>3+1 任务后的三项自评</strong><span>${scoring.complete?numeric(scoring.total_score)+' / '+esc(scoring.total_range?.[1]||9):'未答齐，暂不计算总分'}</span><small>总均值 ${scoring.complete?numeric(scoring.mean_score):'—'} / 3</small></div><div class="score-grid">`+Object.entries(scoring.subscales).map(([key,x])=>`<div><label>${esc(primaryNames[key]||key)}</label><strong>${x.complete?numeric(x.raw_score)+' / 3':'未答齐'}</strong><span>依据任务后的一项明确自评</span></div>`).join('')+'</div><p class="basis-note">依据：固定朗读及三个开放问题完成后，再由学生明确回答三个同方向的自评问题。朗读是否流利、故事是否悲伤、词语或表情均不自动变成心理分数。</p>';
}
function comparisonHtml(answers) {
  if(!isPrimary(answers)) return '';
  const scoring=answers.scoring||{}, task=answers.task_scoring||{}, comparison=answers.score_comparison||{};
  const supplied=comparison.dimensions||{};
  const rows=Object.entries(primaryNames).map(([key,label])=>{
    const before=scoring.subscales?.[key], after=task.subscales?.[key], pair=supplied[key];
    const comparable=pair?pair.comparable:!!before?.complete&&!!after?.complete;
    const mean=pair?.questionnaire_mean??(before?.complete?before.mean_score:null);
    const rating=pair?.task_rating??(after?.complete?after.raw_score:null);
    return `<tr><td>${label}</td><td>${numeric(mean)} / 3</td><td>${numeric(rating)} / 3</td><td>${comparable?numeric(pair?.difference):'未答齐'}</td></tr>`;
  }).join('');
  return `<section class="teacher-section"><h3>同维度对照</h3><div class="question-table"><table><thead><tr><th>维度</th><th>21 题中七题的均值</th><th>任务后一项自评</th><th>数值差（任务后 − 21题均值）</th></tr></thead><tbody>${rows}</tbody></table></div><p class="basis-note">两份记录的题数不同：21 题总分为 0–63，任务后三项总分为 0–9，不能直接比较两个原始总分。表内统一使用每维 0–3 对照；数值变化也不代表症状变化或任务效果。</p><p class="validation-note">这是为小学访谈编写的未验证题组与自评记录，尚未建立常模、信效度或临床阈值，不提供疾病、严重程度或“好/坏”结论。老师应结合学生原话、场景及专业判断理解。</p></section>`;
}
function questionsHtml(questions) {
  return questions.length?'<div class="question-table"><table><thead><tr><th>题号</th><th>题目与回答</th><th>状态</th><th>分数</th></tr></thead><tbody>'+questions.map(q=>`<tr><td>${esc(q.id)}</td><td><div>${esc(q.question)}</div><p>${esc(q.answer_text||'未记录文字回答')}</p></td><td>${esc(statuses[q.status]||q.status)}</td><td>${esc(q.score??'—')}</td></tr>`).join('')+'</tbody></table></div>':'<p class="muted">暂无心理问卷记录。</p>';
}
function tasksHtml(data,answers) {
  const tasks=answers.tasks?.length?answers.tasks:data.tasks||[];
  if(!tasks.length) return '<p class="muted">暂无任务记录。</p>';
  return tasks.map(x=>`<article class="task"><div class="task-title"><strong>${esc(x.title||x.kind)}</strong><span class="muted">${esc(statuses[x.status]||x.metadata?.status||(x.answer_text?'已回答':'待回答'))}</span></div><p class="task-prompt">${esc(x.question||x.prompt)}</p>${x.source_question&&x.source_question!==(x.question||x.prompt)?`<details class="task-source"><summary>查看原始任务问题</summary><p class="task-prompt">${esc(x.source_question)}</p></details>`:''}<p class="task-answer">${esc(x.answer_text||'未回答')}</p></article>`).join('');
}
function recordingHtml(data) {
  const rec=data.recording||{}, video=rec.video, audio=rec.full_audio;
  function item(title,x) {
    if(!x) return `<div class="recording-status"><strong>${title}</strong><span class="muted">未找到录制状态记录</span></div>`;
    const state=x.error?'录制失败':x.complete?'已完整保存':x.running?'正在录制':'录制未完成';
    return `<div class="recording-status ${x.error?'failed':''}"><strong>${title} · ${state}</strong>${x.error?`<p>${esc(x.error)}</p>`:''}${x.frames!==undefined?`<span>已保存 ${esc(x.frames)} 帧 · ${esc(x.fps||'—')} FPS</span>`:''}${x.dropped_blocks?`<p>音频丢失块数：${esc(x.dropped_blocks)}</p>`:''}<details><summary>查看录制状态详情</summary><pre>${esc(JSON.stringify(x,null,2))}</pre></details></div>`;
  }
  return item('会话录像',video)+item('整场音频',audio);
}
function eventsHtml(events) {
  if(!events.length) return '<p class="muted">没有保存全文对话事件；可查看逐题回答。</p>';
  const names={user:'受访者',assistant:'机器人',system:'系统'};
  return '<div class="event-list">'+events.map(e=>`<article class="event ${esc(e.role)}"><div><strong>${esc(names[e.role]||e.role)}</strong><span>${esc(date(e.at))}${e.question_id?' · '+esc(e.question_id):''}${e.delivery?' · '+esc(e.delivery):''}</span></div><p>${esc(e.text)}</p>${e.error?`<p class="event-error">${esc(e.error)}</p>`:''}</article>`).join('')+'</div>';
}
function renderDetail(data) {
  selected=data.id; const answers={...(data.answers||{})};
  for(const key of ['task_scoring','score_comparison','scoring','task_reflections']) if(data[key]!==undefined) answers[key]=data[key];
  const profile=Object.entries(labels).map(([key,label])=>`<div><label>${label}</label><strong>${esc(data[key]??answers.participant?.[key]??'—')}</strong></div>`).join('');
  const downloads=Object.entries({'answers.json':'原始回答 JSON','transcript.json':'全文对话 JSON','session_package.json':'整场 JSON','session_package.zip':'全部媒体 ZIP'}).filter(([name])=>data.artifacts?.[name]).map(([name,label])=>`<a href="${sessionUrl(data.id)}/${name}" download>${label}</a>`).join('');
  const warnings=[...(data.review_warnings||[]),answers.store_warning,answers.video_error,answers.error].filter(Boolean);
  const primary=isPrimary(answers);
  $('detail').innerHTML=`<div class="card-top"><div><div class="eyebrow">${esc(statuses[data.status]||data.status)} · ${primary?'小学自编访谈记录':'历史访谈记录'}</div><h2>${esc(data.name||'未填写姓名')}</h2><p class="subheading">${esc(data.id)} · 开始于 ${esc(date(data.started_at))}${data.ended_at?' · 结束于 '+esc(date(data.ended_at)):''}</p></div><a href="/api/teacher/sessions/${encodeURIComponent(data.id)}/export" download>导出管理记录</a></div><div class="profile">${profile}</div><div class="review-downloads">${downloads}</div>${warnings.length?`<div class="teacher-error">${warnings.map(esc).join('<br>')}</div>`:''}<section class="teacher-section"><h3>本次总结</h3><p class="summary-text">${esc(data.summary||answers.overall_summary||'尚未生成总结。')}</p></section><section class="teacher-section"><h3>${primary?'第一份分数：二十一题明确自评':'历史 DASS-21 计分'}</h3>${scoresHtml(answers)}</section>${primary?`<section class="teacher-section"><h3>第二份分数：3+1 任务后的明确自评</h3>${taskScoresHtml(answers)}</section>${comparisonHtml(answers)}`:''}<section class="teacher-section"><h3>${primary?'二十一题逐题回答与计分依据':'心理问卷逐题回答'}</h3>${questionsHtml(answers.questions||[])}</section><section class="teacher-section"><h3>固定文本阅读与三个开放性问题</h3>${tasksHtml(data,answers)}</section>${primary?`<section class="teacher-section"><h3>任务后的三个自评问题与选择</h3>${questionsHtml(answers.task_reflections||[])}</section>`:''}${answers.intake_questions?.length?`<details class="teacher-section"><summary>基本资料逐项询问记录</summary>${questionsHtml(answers.intake_questions)}</details>`:''}<section class="teacher-section"><h3>录制状态</h3>${recordingHtml(data)}</section><section class="teacher-section"><h3>录音与录像</h3>${mediaHtml(data.media)}</section><section class="teacher-section"><h3>全文对话事件（${(data.events||[]).length} 条）</h3>${eventsHtml(data.events||[])}</section><details class="teacher-section"><summary>查看原始回答 JSON</summary><pre class="raw-json">${esc(JSON.stringify(answers,null,2))}</pre></details>`;
  renderSessions(window.sessionItems||[]);
}
async function loadDetail(id) { selected=id; showError(null); try { renderDetail(await api('/api/teacher/sessions/'+encodeURIComponent(id))); } catch(error) { showError(error); } }
async function refresh() { showError(null); try { const data=await api('/api/teacher/sessions'); window.sessionItems=data.sessions||data; renderSessions(window.sessionItems); if(selected) await loadDetail(selected); else if(window.sessionItems.length) await loadDetail(window.sessionItems[0].id); } catch(error) { showError(error); $('session-list').innerHTML='<p class="empty">管理数据接口尚未启动。</p>'; } }
$('refresh').onclick=refresh;
window.schoolIdentity.watch().then(actor => {
  if(actor?.role==='teacher') refresh();
  else window.schoolIdentity.leave('account');
}).catch(showError);
