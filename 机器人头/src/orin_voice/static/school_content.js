'use strict';

window.mountSchoolContent = async function (container, context = {}) {
  const el = (tag, text) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const call = async (action, payload = {}) => {
    const res = await fetch('/api/school/content/' + action, {
      method:'POST', headers:{'Content-Type':'application/json'}, credentials:'same-origin',
      body:JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw Error(data.error || '操作失败');
    return data;
  };
  const stages = ['小学','初中','高中'];
  const kinds = {lesson:'教案与课件',article:'科普读物',script:'场景话术'};
  const states = {draft:'草稿',pending:'待审',published:'已发布',rejected:'退回',retired:'已下架',superseded:'旧版'};
  const field = (host, name, control) => {
    const label = el('label');
    label.append(el('span', name), control); host.append(label);
    return control;
  };
  const input = (value = '') => { const x = el('input'); x.value = value; return x; };
  const multi = (value = '', rows = 3) => {
    const n = el('textarea'); n.value = value; n.rows = rows; return n;
  };
  const select = (items, value) => {
    const n = el('select');
    for (const [key, text] of items) {
      const o = el('option',text); o.value = key; n.append(o);
    }
    if (value !== undefined) n.value = value;
    return n;
  };
  const options = xs => xs.map(v => [v,v]);
  const button = (host, title, fn) => {
    const b=el('button',title); b.type='button';
    b.onclick=async e=>{
      e.preventDefault(); b.disabled=true;
      try { await fn(); }
      catch(err) { notice.textContent=err.message || String(err); }
      finally { b.disabled=false; }
    };
    host.append(b); return b;
  };
  const group = (host, tag='div') => { const n=el(tag); host.append(n); return n; };
  const line = (host, text) => host.append(el('p',text));
  const sub = (host, text) => host.append(el('h3',text));
  const download = (name, object) => {
    const url=URL.createObjectURL(new Blob([JSON.stringify(object,null,2)],{type:'application/json'}));
    const a=el('a'); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  };

  container.replaceChildren(el('h2','教研与课程'));
  if (context.role && !['teacher','reviewer','school_admin'].includes(context.role)) {
    line(container,'此账号可在报告中查看学校汇总。'); return;
  }
  const nav=group(container,'nav');
  const notice=el('p'); notice.setAttribute('role','status'); notice.setAttribute('aria-live','polite'); container.append(notice);
  const panel=group(container,'section');
  let filters={state:'',stage:'',kind:'',query:''}, canReview=false;
  const clear = () => { panel.replaceChildren(); notice.textContent=''; };
  const announce = msg => { notice.textContent=msg; };
  const date = s => s ? new Date(s).toLocaleString() : '';
  button(nav,'内容库',browse); button(nav,'编写材料',create); if(context.role==='teacher')button(nav,'班级课堂',classes);

  async function browse() {
    const data=await call('list',filters);
    clear(); canReview=data.can_review;
    const form=group(panel);
    const stage=field(form,'学龄',select([['','全部学龄'],...options(stages)],filters.stage));
    const kind=field(form,'类型',select([['','全部类型'],...Object.entries(kinds)],filters.kind));
    const state=field(form,'状态',select([['','全部状态'],...Object.entries(states)],filters.state));
    const query=field(form,'主题或标题',input(filters.query));
    button(form,'查询',async()=>{ filters={stage:stage.value,kind:kind.value,state:state.value,query:query.value}; await browse(); });
    const packages=group(panel,'details'); packages.append(el('summary','试点课程与离线内容包'));
    line(packages,'试点材料包含四年级 16 周教案和 4 类话术。导入后按本校流程审校。');
    button(packages,'载入试点课程与话术',async()=>{
      const d=await call('pilot'); await browse(); announce('新增 '+d.added+' 个草稿，已有 '+d.skipped+' 个。');
    });
    const upload=el('input'); upload.type='file'; upload.accept='.json,application/json'; field(packages,'本地 JSON 内容包',upload);
    button(packages,'导入为待校审草稿',async()=>{
      if (!upload.files[0]) throw Error('请选择内容包文件');
      if (upload.files[0].size>1000000) throw Error('文件需小于 1 MB');
      const pack=JSON.parse(await upload.files[0].text());
      const d=await call('import',{package:pack}); await browse(); announce('新增 '+d.added+' 个草稿，已有 '+d.skipped+' 个。');
    });
    if (!data.items.length) { line(panel,'当前没有符合条件的材料。'); return; }
    const chosen=new Set();
    button(panel,'导出勾选的已发布材料',async()=>{
      if (!chosen.size) throw Error('请勾选要导出的已发布材料');
      download('心语离线教学内容包.json',await call('export',{ids:[...chosen]}));
    });
    const list=group(panel,'ul');
    for (const item of data.items) {
      const li=group(list,'li');
      if (item.state==='published') {
        const tick=el('input'); tick.type='checkbox'; tick.setAttribute('aria-label','选中 '+item.title);
        tick.onchange=()=>tick.checked?chosen.add(item.id):chosen.delete(item.id); li.append(tick);
      }
      button(li,item.title,()=>detail(item.id));
      li.append(document.createTextNode(' '+item.stage+' · '+kinds[item.kind]+' · '+states[item.state]+' · v'+item.version));
    }
  }
  async function detail(id) {
    const row=await call('get',{id}); const item=row.item, body=item.body;
    clear(); sub(panel,item.title);
    line(panel,item.stage+' · '+item.topic+' · '+states[row.state]+' · v'+row.version+' · 编写账号 '+row.author);
    line(panel,item.source);
    if(item.provenance) {
      const upstream=item.provenance;
      line(panel,'内容包记载的外部审校　v'+upstream.version+' · '+upstream.reviewer+' · '+upstream.reviewed_at+' · '+upstream.review_note);
    }
    if (row.reviewer) line(panel,'审校 '+row.reviewer+' · '+date(row.reviewed_at)+' · '+row.review_note);
    if (item.kind==='script') {
      line(panel,'匹配短语　'+body.keywords.join('、')); line(panel,'回应　'+body.reply); line(panel,'追问　'+body.followup);
    } else {
      if (item.kind==='lesson') {
        line(panel,body.grade+' · 第 '+body.week+' 周 · '+body.minutes+' 分钟');
        for (const [key,label] of [['objective','目标'],['materials','材料'],['interaction','互动'],['feedback','反馈'],['observation','观察']]) line(panel,label+'　'+body[key]);
      }
      const outline=group(panel,'details'); outline.append(el('summary','查看课件（'+body.slides.length+' 页）'));
      for(const s of body.slides) { sub(outline,s.title); line(outline,s.text); }
      if(row.state==='published' && context.role==='teacher') {
        button(panel,'播放课件',()=>player(id,item));
        button(panel,'选择班级开课',()=>start(id));
      }
    }
    const histori=await call('history',{id});
    if(histori.items[0]?.id===id) button(panel,'编辑新版本',()=>edit(item,id));
    if(row.state==='draft') button(panel,'提交审校',async()=>{ await call('submit',{id}); await detail(id); });
    if(row.state==='pending' && canReview) {
      const note=field(panel,'审校意见',multi());
      for(const [decision,label] of [['publish','审校通过并发布'],['reject','退回修改']]) {
        button(panel,label,async()=>{await call('review',{id,decision,note:note.value}); await detail(id);});
      }
    }
    if(row.state==='published' && canReview) {
      const note=field(panel,'下架理由',input());
      button(panel,'下架该版本',async()=>{await call('withdraw',{id,note:note.value});await detail(id);});
    }
    const log=group(panel,'details'); log.append(el('summary','版本与审校记录'));
    for (const v of histori.items) button(log,'v'+v.version+' '+states[v.state]+' · '+v.author+(v.reviewer?' · '+v.reviewer:''),()=>detail(v.id));
    if (item.kind!=='script' && context.role==='teacher') {
      const acts=(await call('activity',{id})).events;
      const log2=group(panel,'details'); log2.append(el('summary','单独播放与下架记录（'+acts.length+'）'));
      for (const e of acts) line(log2,date(e.created_at)+' · '+e.actor+' · '+eventTitle(e));
    }
  }
  function create() {
    clear(); sub(panel,'编写科普、课件或话术');
    const stage=field(panel,'学龄',select(options(stages)));
    const kind=field(panel,'内容类型',select(Object.entries(kinds)));
    const topic=field(panel,'主题',input());
    button(panel,'打开编辑器',async()=>{
      if(!topic.value.trim()) throw Error('请先填写主题');
      const item=kind.value==='script'
        ? {kind:'script',stage:stage.value,topic:topic.value,title:topic.value,source:'',body:{keywords:[],reply:'',followup:''}}
        : (await call('generate',{stage:stage.value,topic:topic.value,kind:kind.value})).item;
      edit(item);
    });
  }
  function edit(item,previous) {
    clear(); sub(panel,previous?'修订材料':'编辑新材料');
    const form=group(panel);
    const title=field(form,'标题',input(item.title)), stage=field(form,'学龄',select(options(stages),item.stage));
    const topic=field(form,'主题',input(item.topic)), source=field(form,'来源和编写依据',multi(item.source,2));
    const b=item.body, fields={};
    const capture=(key,label,value,rows=2)=>{ fields[key]=field(form,label,multi(value,rows)); };
    const rawSlides=[];
    if(item.kind==='script') {
      capture('keywords','匹配短语，每行一个',b.keywords.join('\n'));
      capture('reply','共情回应',b.reply);capture('followup','追问或求助邀请',b.followup);
    } else {
      if(item.kind==='lesson') {
        for (const [k,label] of [['grade','年级'],['week','周次'],['minutes','分钟数'],['objective','目标'],['materials','材料'],
          ['interaction','互动任务'],['feedback','离堂反馈'],['observation','观察要点']]) capture(k,label,b[k],1);
      }
      const slides=group(form);
      const addSlide=(s={title:'',text:''})=>{
        const box=group(slides,'fieldset');
        const node={title:field(box,'页标题',input(s.title)),text:field(box,'投影内容',multi(s.text))};
        rawSlides.push(node);
        button(box,'移除这页',()=>{rawSlides.splice(rawSlides.indexOf(node),1);box.remove();});
      };
      b.slides.forEach(addSlide);button(form,'加一页课件',()=>addSlide());
    }
    button(panel,'保存草稿',async()=>{
      const body=Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]));
      if(item.kind==='script') body.keywords=body.keywords.split('\n').map(s=>s.trim()).filter(Boolean);
      else {
        body.slides=rawSlides.map(s=>({title:s.title.value,text:s.text.value}));
        if(item.kind==='lesson') {body.week=Number(body.week); body.minutes=Number(body.minutes);}
      }
      const row=await call('save',{previous,item:{kind:item.kind,stage:stage.value,title:title.value,topic:topic.value,source:source.value,body}});
      await detail(row.id); announce('草稿已保存。');
    });
  }
  function eventTitle(e) {
    return ({page:'翻至第 '+(e.detail.page||'')+' 页',completed:'完成学习',interaction:e.detail.note,
             withdraw:'下架 · '+e.detail.note})[e.kind] || e.kind;
  }
  async function classes() {
    const data=await call('classes');clear();sub(panel,'班级课堂');
    if(!data.classes.length) line(panel,'从内容库选择已发布课件，然后选择班级开课。');
    for(const r of data.classes) button(panel,r.term+' '+r.class_name+' · '+r.title+' · '+(r.ended_at?'已结束':'进行中'),()=>room(r.id));
  }
  async function start(id) {
    const data=await call('roster');clear();sub(panel,'选择班级');
    if(!data.classes.length) {line(panel,'请先在本校名册登记班级和学期。');return;}
    const s=field(panel,'学期与班级',select(data.classes.map((r,i)=>[String(i),r.term+' · '+r.class_name])));
    button(panel,'开始课堂',async()=>{const r=await call('start_class',{id,...data.classes[Number(s.value)]});await room(r.class_id);});
  }
  async function room(class_id) {
    const data=await call('class_detail',{class_id}), r=data.class;
    clear();sub(panel,r.term+' '+r.class_name+' · '+r.item.title);
    line(panel,'授课账号 '+r.teacher+' · '+date(r.started_at)+(r.ended_at?' 至 '+date(r.ended_at):''));
    if(!r.ended_at) {
      button(panel,'播放本次课件',()=>player(r.item_id,r.item,class_id));
      const member=field(panel,'参与对象',select([['','全班'],...data.members.map(m=>[m.id,m.name])]));
      const note=field(panel,'互动回答或课堂观察',multi());
      button(panel,'记下互动',async()=>{await call('record',{class_id,kind:'interaction',student_id:member.value,note:note.value});await room(class_id);});
      button(panel,'登记完成学习',async()=>{await call('record',{class_id,kind:'completed',student_id:member.value});await room(class_id);});
      const feedback=field(panel,'班级反馈',multi()), observation=field(panel,'教师观察',multi());
      button(panel,'结束并归档课堂',async()=>{await call('finish_class',{class_id,feedback:feedback.value,observation:observation.value});await room(class_id);});
    } else {
      line(panel,'班级反馈　'+r.feedback);line(panel,'教师观察　'+r.observation);
    }
    sub(panel,'学习与互动记录');
    const names=new Map(data.members.map(m=>[m.id,m.name]));
    for(const e of data.events) line(panel,date(e.created_at)+' · '+(e.student_id?(names.get(e.student_id)||'班级历史成员'):'全班')+' · '+eventTitle(e));
  }
  async function player(id,item,class_id=null,page=0) {
    await call('record',{id,class_id,kind:'page',page:page+1});
    const pages=item.body.slides;clear();
    line(panel,item.title+' · '+(page+1)+' / '+pages.length);
    const slide=group(panel,'article');sub(slide,pages[page].title);
    line(slide,pages[page].text);
    if(page) button(panel,'上一页',()=>player(id,item,class_id,page-1));
    if(page<pages.length-1) button(panel,'下一页',()=>player(id,item,class_id,page+1));
    else button(panel,'完成学习',async()=>{await call('record',{id,class_id,kind:'completed'}); await (class_id?room(class_id):detail(id));});
    button(panel,'返回',()=>class_id?room(class_id):detail(id));
  }
  try { await browse(); } catch(e) {notice.textContent=e.message;}
};
