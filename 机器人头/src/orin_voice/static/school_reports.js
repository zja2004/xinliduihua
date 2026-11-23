(() => {
  'use strict';
  const el = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function add(parent, ...children) { parent.append(...children); return parent; }
  function field(parent, title, tag = 'input', type = 'text') {
    const label = el('label', title);
    const input = el(tag);
    if (tag === 'input') input.type = type;
    add(label, input); add(parent, label);
    return input;
  }
  function option(select, value, name) { const o=el('option',name); o.value=value; select.append(o); }
  function choice(parent,title,values) {
    const select=field(parent,title,'select');
    values.forEach(([key,name])=>option(select,key,name));
    return select;
  }
  function table(headers, rows) {
    const wrap=el('div'); wrap.style.overflowX='auto';
    const t=el('table'), head=el('thead'), tr=el('tr'), body=el('tbody');
    headers.forEach(h=>tr.append(el('th',h))); head.append(tr);
    rows.forEach(row=>{const tr=el('tr');row.forEach(x=>tr.append(el('td',x==null?'隐藏／无记录':String(x))));body.append(tr);});
    return add(wrap,add(t,head,body));
  }
  function dl(entries) {
    const root=el('dl');
    entries.forEach(([k,v])=>{if(v!==undefined&&v!==null&&v!=='') add(root,el('dt',k),el('dd',String(v)));});
    return root;
  }
  function download(name, text) {
    const url=URL.createObjectURL(new Blob(['\uFEFF',text],{type:'text/plain;charset=utf-8'}));
    const a=el('a'); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),5000);
  }
  const metricNames={'interviewed':'实访人数','due':'应访人数','completed':'排期已完成人数',
    'coverage_pct':'覆盖率（%）','alerts':'预警人数','handled':'已完成处置人数','referrals':'人工转介人数',
    'approved':'已通过审核','received':'已收回执','archived':'已归档'};
  const fieldNames = {
    title:'量表名称',instrument:'量表编号',version:'版本号',owner:'版权方及基金会确认的授权方',
    license_ref:'许可文件编号和使用范围',norms:'适用常模及样本来源',validation_ref:'信效度依据',
    review_body:'负责审校的专家或机构',threshold_basis:'计分、分界点及依据文件',
    confidence_basis:'结论可信程度和依据',uncertainty:'解释限制及不确定性'
  };
  const statusLabels={builtin:'内置固定计分',draft:'待专家审校',approved:'已审通过',rejected:'已退回'};
  function renderScores(data) {
    const part=el('section');
    for (const [key,title] of [['questionnaire','固定问卷'],['supplement','任务后三项自评']]) {
      const record=data[key];
      if(!record?.themes?.length) continue;
      add(part,el('h4',title),el('p',record.total!=null?'总分 '+record.total:record.complete?'按维度查看':'未答齐，部分分数空缺'),
        table(['维度','原始分'],record.themes.map(t=>[t.theme,t.raw_score??'缺项'])));
    }
    for(const row of data.supplemental_scales||[]) {
      add(part,el('h4',row.title||('补充测评 '+row.ordinal)),dl([['原始总分',row.total],
        ['有效性',row.interpretable===false?'未通过该版本有效性规则，暂停解释':'已按该版本计分'],
        ['复核等级',row.level||row.review_band||'未给出'],['版本',row.version],
        ['规则版本指纹',row.rule_fingerprint],['规则依据',row.rule_basis],['常模',row.norms],['可信度依据',row.confidence_basis],
        ['不确定性',row.uncertainty],['审校记录',row.evidence_ref],['审校人',row.reviewer]]));
      if(row.dimensions?.length) part.append(table(['维度','原始分','复核等级'],row.dimensions.map(x=>[x.name,x.raw,x.level||'未给出'])));
    }
    return part;
  }
  function parentText(p) {
    const text=[p.audience,p.instrument+'　'+p.version,p.basis,p.confidence,p.uncertainty,p.interpretation,''];
    for(const [key,title] of [['questionnaire','固定问卷'],['supplement','任务后三项明确自评']]) {
      const x=p[key]; if(!x?.themes?.length) continue;
      text.push(title, '总分 '+(x.total??'无可用总分'),...x.themes.map(t=>t.theme+'　'+(t.raw_score??'缺项')),'');
    }
    (p.supplemental_scales||[]).forEach(s=>text.push('补充测评 '+s.ordinal+'　原始总分 '+s.total+
      '　复核等级 '+(s.review_band||'未给出')+'　学校已审规则版本指纹 '+s.rule_fingerprint+(s.interpretable===false?'　有效性未通过，暂停解释':'')));
    return text.join('\n');
  }
  window.mountSchoolReports = async function(container, actor, request) {
    container.replaceChildren(); const message=el('p');message.setAttribute('role','status');container.append(message);
    function section(title) {const sec=el('section');sec.className='teacher-section';add(container,add(sec,el('h3',title)));return sec;}
    function button(parent,title,fn) {
      const b=el('button',title);b.type='button';b.onclick=async()=>{
        b.disabled=true; message.textContent='';
        try {await fn();} catch(e) { message.textContent=e.message||String(e); }
        finally { b.disabled=false; }
      };
      parent.append(b);return b;
    }
    function details(parent,title) {
      const d=el('details');add(d,el('summary',title));parent.append(d);return d;
    }
    const role=actor.role;
    if(role!=='reviewer') {
      const dash=section(role==='district_admin'?'教育局区域驾驶舱':'学校驾驶舱');
      const month=new Date().toISOString().slice(0,7);
      const from=field(dash,'起始月','input','month'), to=field(dash,'结束月','input','month');from.value=to.value=month;
      const out=el('div');
      async function loadDashboard() {
        const d=await request('dashboard',{from_month:from.value,to_month:to.value});
        out.replaceChildren(el('p',d.data_source),el('p',d.method),el('p',d.privacy));
        const keys=Object.keys(metricNames);
        const groups=[...(d.regional_months?.length?[{school:'授权区域合计',months:d.regional_months}]:[]),...d.schools];
        for(const s of groups) {
          add(out,el('h4',s.school+(s.source?' · '+s.source:'')),table(['年月',...Object.values(metricNames)],s.months.map(m=>[m.month,...keys.map(k=>m[k])])));
          if(s.provenance?.length) out.append(table(['来源月份','校方签发时间','区局导入时间','可信签名公钥指纹'],s.provenance.map(r=>[r.month,r.signed_at,r.imported_at,r.fingerprint])));
          const risks=s.months.filter(m=>m.risk_distribution);
          if(risks.length) out.append(table(['年月','预防','关注','风险','高危','待复核'],risks.map(m=>[m.month,...['预防','关注','风险','高危','待复核'].map(k=>m.risk_distribution[k])])));
          if(s.resources?.length) out.append(table(['资源登记月','心理老师人数','可用面谈室','可用工时'],s.resources.map(r=>[r.month,r.counselors,r.rooms,r.hours])));
        }
        if(d.policy_suggestions?.length) add(out,el('h4','工作建议'),...d.policy_suggestions.map(t=>el('p',t)));
        if(!d.schools?.length) out.append(el('p','当前账号暂无授权学校。'));
        button(out,'下载本期汇总报告',()=>download('学校心理服务汇总-'+from.value+'至'+to.value+'.txt',
          [d.audience+'心理服务汇总',d.from_month+' 至 '+d.to_month,d.data_source,d.method,d.privacy,
            ...groups.flatMap(s=>[[s.school,s.source].filter(Boolean).join('　'),...(s.provenance||[]).map(p=>[p.month,p.signed_at,p.imported_at,p.fingerprint,p.digest].join('　')),...s.months.map(m=>[m.month,...keys.map(k=>metricNames[k]+' '+(m[k]??'隐藏／无记录'))].join('　')+
              (m.risk_distribution?'　风险分布 '+Object.entries(m.risk_distribution).map(([k,v])=>k+' '+v).join('、'):'')),
              ...(s.resources||[]).map(r=>r.month+'　心理老师 '+r.counselors+'　面谈室 '+r.rooms+'　可用工时 '+r.hours)]),
            ...(d.policy_suggestions||[])].join('\n')));
      }
      button(dash,'查看时间窗',loadDashboard);dash.append(out);
      if(['school_admin','principal'].includes(role)){
        button(dash,'导出签名汇总包',async()=>{
          const packet=await request('export_packet',{from_month:from.value,to_month:to.value});
          download('学校汇总-'+packet.payload.school+'-'+from.value+'-'+to.value+'.json',JSON.stringify(packet));
        });
      }
      if(role==='district_admin'){
        const transfer=details(dash,'导入学校离线汇总');
        const file=field(transfer,'经学校签名的 JSON 汇总包','input','file');file.accept='.json';
        button(transfer,'验签并导入',async()=>{
          if(!file.files.length||file.files[0].size>262144)throw Error('请选择不超过 256 KB 的学校汇总文件');
          const raw=await file.files[0].text();
          const result=await request('import_packet',JSON.parse(raw.replace(/^\uFEFF/,'')));
          await loadDashboard();message.textContent=result.school+' 验签通过；更新 '+result.updated+' 月，重复 '+result.duplicates+' 月，已忽略旧版 '+result.older+' 月。';
        });
      }
      if(['school_admin','principal'].includes(role)){
        const form=details(dash,'登记当月服务资源');
        const period=field(form,'资源所属月','input','month');period.value=month;
        const units={};Object.entries({counselors:'心理老师人数',rooms:'可用面谈室数',hours:'月度可用工时'}).forEach(([key,label])=>{
          units[key]=field(form,label,'input','number');units[key].min='0';units[key].step='1';});
        button(form,'保存资源登记',async()=>{
          if(Object.values(units).some(v=>v.value==='')) throw Error('请填写三项资源数量');
          await request('set_resources',{month:period.value,...Object.fromEntries(Object.entries(units).map(([k,v])=>[k,Number(v.value)]))});
          await loadDashboard();message.textContent='已保存资源登记。';
        });
      }
      await loadDashboard();
    }
    if(role==='district_admin') return;
    let catalog=await request('catalog',{});
    const library=section('量表与分级规则');const list=el('div');library.append(list);
    function fillCatalog(){
      list.replaceChildren(el('p',catalog.authorization),el('p',catalog.mht_status),el('p',catalog.rule_method));
      for(const r of catalog.instruments){
        const d=details(list,r.title+' · '+r.instrument+' '+r.version+' · '+statusLabels[r.state]);
        d.append(dl(Object.entries(fieldNames).map(([k,label])=>[label,r[k]]).concat([
          ['题单来源',r.source],['计分来源',r.scoring_source],['适用年龄',r.min_age!=null?r.min_age+'–'+r.max_age:undefined],
          ['分界点',r.cutoffs?.length?r.cutoffs.join('、'):undefined],['审校账号',r.reviewer],['签核材料',r.evidence_ref],['审校意见',r.notes]])));
        if(r.state!=='builtin') {
          button(d,'导出本版本规则与专家评审记录',()=>{
            const lines=[r.title+'　'+r.instrument+' '+r.version,'状态　'+statusLabels[r.state],'创建账号　'+r.creator,'创建时间　'+r.created_at,
              ...Object.entries(fieldNames).filter(([k])=>!['title','instrument','version'].includes(k)).map(([k,v])=>v+'　'+r[k]),
              '适龄范围　'+r.min_age+'–'+r.max_age,'选项分值　'+r.choices.map((v,i)=>i+' '+v).join('、'),
              '题目数　'+r.questions.length,'反向题号　'+(r.questions.filter(q=>q.reverse).map(q=>q.id).join('、')||'无'),
              '总分所用题号　'+r.total_items.join('、'),'总分四级分界点　'+(r.cutoffs.join('、')||'仅记录数值'),
              '维度及有效性规则　'+JSON.stringify(r.dimensions),'计分规则　'+catalog.rule_method,
              '审校账号　'+(r.reviewer||'待审'),'签核材料　'+(r.evidence_ref||'待审'),'审校意见　'+(r.notes||'待审'),'审校时间　'+(r.reviewed_at||'待审')];
            download(r.instrument+'-'+r.version+'-规则与评审记录.txt',lines.join('\n'));
          });
        }
        if(r.choices) d.append(el('p','选项　'+r.choices.join(' ／ ')));
        if(r.questions) d.append(table(['题号','题目','反向计分'],r.questions.map(q=>[q.id,q.text,q.reverse?'是':'否'])));
        if(r.total_items) d.append(el('p','总分题号　'+r.total_items.join('、')));
        if(r.dimensions?.length)d.append(table(['维度','题号','分界点','无效下界'],r.dimensions.map(x=>[x.name,x.items.join('、'),x.cutoffs.join('、')||'无',x.invalid_at_or_above??'无'])));
        if(role==='reviewer'&&r.state==='draft'){
          const decision=choice(d,'意见',[['approved','通过'],['rejected','退回']]);
          const ref=field(d,'授权和专家签核材料编号'),notes=field(d,'评审意见、适龄性与阈值依据','textarea');
          button(d,'提交审校结论',async()=>{
            await request('review',{instrument:r.instrument,version:r.version,decision:decision.value,evidence_ref:ref.value,notes:notes.value});
            catalog=await request('catalog',{});fillCatalog();message.textContent='审校记录已归档。';
          });
        }
      }
    }
    fillCatalog();
    if(role==='school_admin'){
      const form=details(library,'录入授权量表新版本');const controls={};
      Object.entries(fieldNames).forEach(([k,name])=>{controls[k]=field(form,name);});
      const minAge=field(form,'适用最小年龄','input','number'),maxAge=field(form,'适用最大年龄','input','number');
      const opts=field(form,'选项，每行一个，按 0 分起递增','textarea'), qs=field(form,'题目，每行一个。反向计分的题目前加 *','textarea');
      const cut=field(form,'四级分界点，以逗号隔开。仅记录数值时留空');
      const file=field(form,'导入完整授权版本 JSON（可包含维度和有效性规则）','input','file');file.accept='.json,application/json';
      function readForm(){
        if(!minAge.value||!maxAge.value) throw Error('请填写适用年龄');
        return {...Object.fromEntries(Object.entries(controls).map(([k,i])=>[k,i.value])),min_age:Number(minAge.value),max_age:Number(maxAge.value),
          choices:opts.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),
          questions:qs.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean).map((s,i)=>({id:'Q'+(i+1),text:s.replace(/^\*/,''),reverse:s.startsWith('*')})),
          cutoffs:cut.value.trim()?cut.value.split(/[,，、 ]+/).map(Number):[]};
      }
      button(form,'下载填报模板',()=>{
        const template=Object.fromEntries(Object.keys(fieldNames).map(k=>[k,'']));
        Object.assign(template,{min_age:null,max_age:null,choices:[],questions:[{id:'Q1',text:'',reverse:false}],total_items:['Q1'],cutoffs:[],dimensions:[]});
        download('授权量表版本模板.json',JSON.stringify(template,null,2));
      });
      button(form,'提交草稿供独立审校',async()=>{
        let data;
        if(file.files.length){
          if(file.files[0].size>512000) throw Error('量表文件超过 500 KB');
          data=JSON.parse(await file.files[0].text());
        } else data=readForm();
        await request('draft',data);catalog=await request('catalog',{});fillCatalog();message.textContent='草稿已提交，等待审校账号处理。';
      });
    }
    if(role!=='teacher') return;
    const individual=section('个案报告与家长导出'), select=field(individual,'已关联学籍的访谈','select');
    let nextOffset=0;
    async function addSessions(){
      const page=await request('sessions',{offset:nextOffset,limit:100});
      page.sessions.forEach(s=>option(select,s.session_id,[s.name,s.student_code,s.term,s.created_at].join(' · ')));
      nextOffset=page.next_offset??null;
    }
    await addSessions();
    if(nextOffset!==null){
      const more=button(individual,'载入更早的访谈',async()=>{await addSessions();more.hidden=nextOffset===null;});
    }
    if(!select.options.length){
      individual.append(el('p','尚无本校学籍关联的访谈，请在学生档案关联一场访谈后查看报告。'));return;
    }
    const view=el('div');select.addEventListener('change',()=>view.replaceChildren());
    button(individual,'查看老师报告',async()=>{
      const r=await request('teacher',{session_id:select.value});
      view.replaceChildren(el('h4',r.audience),el('p',r.instrument+'　'+r.version+'　'+r.term),
        dl([['计分方法',r.basis],['计分来源',r.scoring_source],['可信度依据',r.confidence],['不确定性',r.uncertainty]]),
        el('p',r.interpretation),renderScores(r));
      if(r.answers?.length)view.append(table(['题号','明确选择','状态'],r.answers.map(q=>[q.id,q.choice??'缺项',q.status])));
    });
    button(individual,'下载家长脱敏报告',async()=>{
      const r=await request('parent',{session_id:select.value});download('家长访谈摘要.txt',parentText(r));
    });individual.append(view);
    const released=catalog.instruments.filter(x=>x.state==='approved');if(!released.length)return;
    const administer=details(individual,'录入其他已审量表的明确选分');
    const version=choice(administer,'量表版本',released.map((r,i)=>[String(i),r.title+' '+r.version]));
    const items=el('div');administer.append(items);let fields={};
    function showQuestions(){
      items.replaceChildren();fields={};const r=released[Number(version.value)];
      for(const q of r.questions)fields[q.id]=choice(items,q.text,[['','请选择'],...r.choices.map((v,i)=>[String(i),v])]);
    }
    version.onchange=showQuestions;select.addEventListener('change',showQuestions);showQuestions();
    button(administer,'记录选分并生成报告',async()=>{
      if(Object.values(fields).some(x=>x.value===''))throw Error('请逐题记录学生的明确选分');
      const r=released[Number(version.value)];
      await request('assess',{session_id:select.value,instrument:r.instrument,version:r.version,
        answers:Object.fromEntries(Object.entries(fields).map(([k,i])=>[k,Number(i.value)]))});
      message.textContent='已保存。点击“查看老师报告”查阅计分、规则依据与签核记录。';
    });
  };
})();
