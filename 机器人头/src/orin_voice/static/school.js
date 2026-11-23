'use strict';
// Staff and kiosk views share one browser cookie jar, including across open tabs.
window.schoolIdentity = (() => {
  const key = 'xinyu.account.change';
  let identity, checking, leaving = false;
  function leave(reason) {
    if (leaving) return;
    leaving = true;
    // Clear rendered personal data synchronously. Keep empty elements in place
    // while outstanding async handlers finish against the outgoing document.
    document.documentElement.style.display='none';
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    let textNode;
    while((textNode=walker.nextNode())) textNode.nodeValue='';
    document.querySelectorAll('input,textarea').forEach(n=>{n.value='';});
    document.querySelectorAll('audio,video').forEach(n=>{n.pause();n.removeAttribute('src');n.load();});
    document.querySelectorAll('a[download], .downloads a').forEach(n=>n.removeAttribute('href'));
    location.replace(reason === 'handoff' || location.pathname === '/teacher/records' ? '/school' : location.pathname+location.search+location.hash);
  }
  async function verify() {
    if (checking || leaving) return checking;
    checking = (async () => {
      const response = await fetch('/api/school/me', {cache:'no-store'});
      if (!response.ok) throw new Error('无法核对当前账号');
      const actor = (await response.json()).actor;
      const current = JSON.stringify(actor && [actor.username,actor.school_id,actor.role,actor.allowed_school_ids]);
      if (identity !== undefined && current !== identity) leave('account');
      identity = current;
      return actor;
    })().finally(() => {checking = null;});
    return checking;
  }
  function watch() {
    function check() {verify().catch(() => { if(location.pathname==='/teacher/records') leave('account'); });}
    const stamp = localStorage.getItem(key);
    window.addEventListener('storage', e => {
      if(e.key===key && e.newValue) leave(e.newValue.split('|')[0]);
    });
    window.addEventListener('pageshow', e => {
      if(e.persisted) {
        const latest=localStorage.getItem(key);
        leave(latest!==stamp && latest?.startsWith('handoff|')?'handoff':'account');
      }
    });
    document.addEventListener('visibilitychange', () => {if(!document.hidden) check();});
    window.setInterval(check,4000);
    return verify();
  }
  function announce(reason) {
    // The marker is a random change nonce, never an account, session or access token.
    localStorage.setItem(key, reason+'|'+crypto.randomUUID());
  }
  function changedLocally(reason) {
    identity = undefined;
    announce(reason);
  }
  return {watch,verify,changedLocally,leave};
})();

if (document.getElementById('credentials')) {
const el=id=>document.getElementById(id), roles={teacher:'心理老师',school_admin:'学校管理员',reviewer:'审校人',principal:'校长',district_admin:'教育主管单位'};
let actor, pane, pollTimer, order=0;
async function api(path,data) {
  const response=await fetch('/api/school/'+path,data===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const value=await response.json(); if(!response.ok)throw new Error(value.error||'请求失败');
  const handoff=path==='ops/start' && data?.handoff===true;
  if(handoff || ['login','logout','account/password'].includes(path)) {
    clearTimeout(pollTimer); ++order;
    el('home').hidden=true; el('workspace').replaceChildren(); el('attention').replaceChildren();
    el('identity').textContent=''; el('sections').replaceChildren();
    window.schoolIdentity.changedLocally(handoff?'handoff':'account');
  }
  return value;
}
function node(tag,text){const a=document.createElement(tag);if(text!=null)a.textContent=text;return a;}
function error(e){el('message').textContent=e.message||String(e);}
function accountPanel(root){
  function form(title,fields,send){const box=node('form');box.append(node('h2',title));
    fields.forEach(([key,label,type,values])=>{const l=node('label',label),i=document.createElement(values?'select':'input');
      if(values)values.forEach(v=>{const o=node('option',roles[v]||v);o.value=v;i.append(o);});else{i.type=type;i.autocomplete=key==='current'?'current-password':type==='password'?'new-password':'off';}
      i.name=key;i.required=true;l.append(i);box.append(l);});
    box.append(node('button','保存'));box.onsubmit=async e=>{e.preventDefault();try{await send(Object.fromEntries(new FormData(box)));box.reset();}catch(err){error(err);}};
    root.append(box);}
  form('修改自己的密码',[['current','当前密码','password'],['password','新密码（至少 12 字）','password']],async d=>{await api('account/password',d);await boot();el('message').textContent='密码已更新，请重新登录。';});
  if(actor.role==='school_admin')form('添加本校账号',[['username','账号','text'],['password','初始密码（至少 12 字）','password'],['role','职务','',['teacher','reviewer','principal']]],async d=>{await api('account/create',d);el('message').textContent='本校账号已创建。';});
}
async function selectPane(name){
  pane=name;el('message').textContent='';const root=node('div');el('workspace').replaceChildren(root);
  [...el('sections').querySelectorAll('button')].forEach(b=>b.setAttribute('aria-current',b.dataset.key===name));
  try{
    if(name==='ops')await window.mountSchoolOps(root,actor,(a,d={})=>api('ops/'+a,d));
    if(name==='reports')await window.mountSchoolReports(root,actor,(a,d={})=>api('reports/'+a,d));
    if(name==='content')await window.mountSchoolContent(root,actor);
    if(name==='account')accountPanel(root);
  }catch(e){if(root.isConnected)error(e);}
}
async function pollAlerts(epoch){
  if(epoch!==order||actor?.role!=='teacher')return;
  try{const data=await api('ops/alerts_feed',{});if(epoch!==order)return;
    const box=el('attention');box.hidden=!data.count;box.replaceChildren();
    if(data.count){box.append(document.createTextNode('有 '+data.count+' 条预警待老师查看 '));const btn=node('button','去办理');btn.onclick=()=>selectPane('ops');box.append(btn);}
  }catch(e){if(epoch===order){el('attention').hidden=false;el('attention').textContent='预警列表未刷新，请检查工作台连接。';}}
  if(epoch===order)pollTimer=setTimeout(()=>pollAlerts(epoch),1000);
}
async function boot(){
  const epoch=++order;clearTimeout(pollTimer);el('attention').hidden=true;el('message').textContent='';
  actor=await window.schoolIdentity.verify();if(epoch!==order)return;el('home').hidden=!actor;el('login').hidden=!!actor;if(!actor){el('workspace').replaceChildren();return;}
  el('identity').textContent=roles[actor.role]+' · '+actor.username;
  const nav=el('sections');nav.replaceChildren();const choices=[];
  if(actor.role==='teacher')choices.push(['ops','任务与学生档案']);
  choices.push(['reports','量表与报告']);
  if(['teacher','reviewer','school_admin'].includes(actor.role))choices.push(['content','话术与课程']);
  choices.push(['account','账号']);
  choices.forEach(([k,t])=>{const b=node('button',t);b.type='button';b.dataset.key=k;b.onclick=()=>selectPane(k);nav.append(b);});
  if(actor.role==='teacher'){const a=node('a','访谈原始记录 ↗');a.href='/teacher/records';nav.append(a);pollAlerts(epoch);}
  await selectPane(choices[0][0]);
}
el('credentials').onsubmit=async e=>{e.preventDefault();try{await api('login',Object.fromEntries(new FormData(e.target)));e.target.reset();await boot();}catch(err){error(err);}};
el('logout').onclick=async()=>{try{await api('logout',{});await boot();}catch(e){error(e);}};window.schoolIdentity.watch().then(boot).catch(error);
}
