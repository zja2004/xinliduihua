(() => {
  'use strict';
  const labels = {
    open: '待处理', acknowledged: '已接单', closed: '已结案', scheduled: '待面访',
    in_progress: '进行中', stopped: '已中止', completed: '已完成', error: '异常结束',
    draft: '待审核', approved: '已审核', sent: '已人工送出', received: '已收回执',
    archived: '已归档', done: '已办理', report: '报告复核', todo: '待办',
    student: '学籍', alert: '预警', referral: '转介', evidence: '证据', enroll: '学籍登记',
    add: '登记', confirm: '人工核查', suggest: '本机模型摘录', dismiss: '退回摘录'
  };
  const translate = value => labels[value] || value || '—';
  function element(tag, text) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    return node;
  }
  function details(parent, title, expanded=false) {
    const node = element('details');
    node.open = expanded;
    node.append(element('summary', title)); parent.append(node); return node;
  }
  function section(parent, title) {
    const sec = element('section'); sec.className = 'teacher-section';
    sec.append(element('h3', title)); parent.append(sec); return sec;
  }
  function table(parent, headers, rows) {
    const wrap = element('div'); wrap.className = 'question-table';
    const table = element('table'), head = element('thead'), heading = element('tr'), body = element('tbody');
    headers.forEach(text => heading.append(element('th',text))); head.append(heading);
    rows.forEach(values => {
      const row = element('tr');
      values.forEach(value => {
        const cell = element('td');
        if (value instanceof Node) cell.append(value); else cell.textContent = value ?? '—';
        row.append(cell);
      });
      body.append(row);
    });
    table.append(head, body); wrap.append(table); parent.append(wrap); return wrap;
  }
  function form(parent, fields, submitText, onSubmit, reportError) {
    const node = element('form'); node.className = 'school-form'; const controls = {};
    fields.forEach(f => {
      const label = element('label', f.label);
      const input = element(f.options ? 'select' : f.long ? 'textarea' : 'input');
      if (f.options) {
        f.options.forEach(item => {
          const opt = element('option', Array.isArray(item) ? item[1] : item);
          opt.value = Array.isArray(item) ? item[0] : item; input.append(opt);
        });
      } else if (!f.long) input.type = f.type || 'text';
      input.name = f.key;
      if (f.example) input.placeholder = f.example;
      if (f.value != null) input.value = f.value;
      if (!f.optional) input.required = true;
      label.append(input); node.append(label); controls[f.key] = input;
    });
    const button = element('button', submitText); button.type='submit'; node.append(button);
    node.onsubmit = async event => {
      event.preventDefault(); if (button.disabled || !node.reportValidity()) return;
      button.disabled=true;
      try {
        const data = Object.fromEntries(Object.entries(controls).map(([key,el])=>[key,el.value.trim()]));
        await onSubmit(data);
      } catch (e) { reportError(e); } finally { button.disabled=false; }
    };
    parent.append(node); return controls;
  }
  function action(parent, title, fn, reportError) {
    const button=element('button',title); button.type='button';
    button.onclick=async()=>{
      if (button.disabled) return;
      button.disabled=true;
      try { await fn(); } catch(e) { reportError(e); } finally { button.disabled=false; }
    };
    parent.append(button); return button;
  }
  const day = v => v ? new Date(v).toLocaleDateString('zh-CN') : '—';
  const total = (n,range) => n == null ? '缺项' : n+(range ? ' / '+range[1] : '');
  function reportLink(sid) {
    const a=element('a','查看本场记录');
    a.href='/teacher/records#'+encodeURIComponent(sid);
    a.target='_blank'; a.rel='noopener'; return a;
  }
  const key = (key,label,attrs={})=>({key,label,...attrs});
  const opts = (key,label,values,attrs={})=>({key,label,options:values,...attrs});

  window.mountSchoolOps = async function(container, actor, request) {
    if (actor.role!=='teacher') return;
    const feedback = element('p'); feedback.setAttribute('role','status');
    const body = element('div'); container.replaceChildren(feedback,body);
    let activeTerm='', selectedStudent='', view='tasks', sequence=0, data, sessions;
    function error(e) {
      feedback.className='teacher-error'; feedback.textContent=e.message || String(e);
      feedback.scrollIntoView({block:'nearest'});
    }
    function info(text) { feedback.className=''; feedback.textContent=text; }
    async function update(name, values, message) {
      const out=await request(name,values);
      info(message); await refresh(); return out;
    }
    const entry=(parent,title,fields,operation,initial={},message='已保存')=>
      form(parent,fields,title,values=>update(operation,{...initial,...values},message),error);
    function studentName(id) {
      const s=data.students.find(x=>x.id===id);
      return s ? s.name+'（'+s.student_code+'）' : id ? '学生 '+id.slice(0,8) : '尚未关联名册';
    }
    function studentOptions() {
      return [...new Map(data.students.map(s=>[s.id,[s.id,studentName(s.id)]])).values()];
    }
    function schoolTerms() {
      return [...new Set(data.students.map(s=>s.term).filter(Boolean))];
    }
    function sessionOptions() {
      return sessions.map(s=>[s.id, [s.name,day(s.started_at),translate(s.status),s.id].filter(Boolean).join(' · ')]);
    }
    function chooseTerm(parent, s) {
      const terms=[...new Set(data.students.filter(row=>row.id===s).map(row=>row.term))];
      return opts('term','所属学期',terms,{value:terms.includes(activeTerm)?activeTerm:terms[0]});
    }
    async function start(values) {
      await request('start',{...values,robot:values.mode==='voice',handoff:true});
      window.location.assign('/');
    }
    function nav() {
      const n=element('nav'); n.setAttribute('aria-label','学生工作');
      [['tasks','老师任务中心'],['students','名册与成长档案'],['plans','学期面访计划']].forEach(([id,title])=>{
        const b=action(n,title,async()=>{view=id; await refresh();},error);
        if(id===view) b.setAttribute('aria-current','page');
      });
      body.append(n);
      const line=element('div'); body.append(line);
      form(line,[key('term','筛选学期',{value:activeTerm,optional:true,example:'留空查看所有学期'})],
        '应用筛选',async v=>{activeTerm=v.term; await refresh();},error);
    }
    function alerts(parent) {
      const open=data.alerts.filter(a=>a.status!=='closed');
      const box=details(parent,'预警与处置（'+open.length+' 条未结案）',open.length>0);
      if(!open.length) box.append(element('p','当前没有待办理预警。'));
      open.forEach(a=>{
        const card=details(box,studentName(a.student_id)+' · '+(a.level || '待人工复核')+' · '+translate(a.status),a.status==='open');
        card.append(element('p',a.summary));
        if(a.clue) card.append(element('p','原话　'+a.clue));
        card.append(element('p',[a.source,a.stage,day(a.created_at)].filter(Boolean).join(' · ')));
        entry(card,'保存处置记录',[
          opts('status','处理进度',[['acknowledged','接单／继续办理'],['closed','完成处置并结案']]),
          opts('level','人工确认等级',[['','待复核'],...data.risk_levels],{value:a.level || '',optional:true}),
          key('note','核查与处置记录',{long:true})
        ],'alert_update',{id:a.id},'处置记录已入档');
        if(a.student_id && a.level) {
          const ref=details(card,'登记人工转介');
          entry(ref,'保存待审核转介',[key('receiver','接收单位或接收方'),key('summary','转介档案摘要',{long:true}),
            key('resource','建议联系的资源')],'referral',{alert_id:a.id},'已登记，等待审核');
        } else if(!a.student_id && a.session_id && data.students.length) {
          const link=details(card,'把本场关联到名册');
          entry(link,'关联访谈',[opts('student_id','在册学生',studentOptions()),opts('term','学期',schoolTerms())],
            'link',{session_id:a.session_id},'本场访谈与预警已关联');
        }
      });
      const manual=details(box,'手工登记关注线索');
      if(!data.students.length) manual.append(element('p','请先在“名册与成长档案”登记学生。'));
      else entry(manual,'登记线索',[opts('student_id','学生',studentOptions()),opts('term','学期',schoolTerms()),
        opts('level','人工确认等级',[['','待复核'],...data.risk_levels],{optional:true}),key('summary','线索来源和简要情况',{long:true})],'alert');
    }
    function referrals(parent) {
      const open=data.referrals.filter(r=>r.status!=='archived');
      const box=details(parent,'人工转介（'+open.length+' 条办理中）',!!open.length);
      if(!open.length) box.append(element('p','当前没有需要跟踪的转介。'));
      const next={draft:['approved','确认审核结果'],approved:['sent','登记已人工送出'],sent:['received','登记接收方回执'],received:['archived','确认归档']};
      open.forEach(r=>{
        const card=details(box,studentName(r.student_id)+' → '+r.receiver+' · '+translate(r.status));
        card.append(element('p','已确认等级　'+r.level),element('p','档案摘要　'+r.summary),element('p','建议资源　'+r.resource));
        if(r.receipt) card.append(element('p','接收方回执　'+r.receipt));
        const [status,label]=next[r.status];
        entry(card,label,[key('note',status==='received'?'接收方、回执日期及凭据':'办理说明',{long:true})],
          'referral_update',{id:r.id,status},'已记录办理人、时间和说明');
      });
    }
    function assignments(parent, items) {
      if(!items.length) {parent.append(element('p','当前没有待面访排期。')); return;}
      items.forEach(a=>{
        const info=details(parent,a.name+' · '+a.class_name+' · '+a.due_date+(a.overdue?' · 已过期':''));
        info.append(element('p','学期　'+a.term+'　负责人　'+a.assignee));
        if(!a.session_id)
          form(info,[opts('mode','访谈方式',[['text','文字与按钮'],['voice','语音与按钮']])],'启动这场访谈',
            values=>start({...values,student_id:a.student_id,term:a.term,appointment_id:a.id}),error);
        else info.append(reportLink(a.session_id));
        entry(info,'改期或派给老师',[key('due_date','排期日期',{type:'date',value:a.due_date}),
          key('assignee','负责人',{value:a.assignee})],'appointment',{id:a.id},'排期已更新');
      });
    }
    function tasks() {
      const page=section(body,'老师任务中心');
      alerts(page); referrals(page);
      const appointments=data.appointments.filter(a=>a.status==='scheduled');
      const visit=details(page,'面访排期与提醒（'+appointments.length+' 人次）');
      assignments(visit,appointments);
      const pending=data.tasks.filter(t=>t.status==='open');
      ['todo','report'].forEach(kind=>{
        const items=pending.filter(t=>t.kind===kind);
        const box=details(page,translate(kind)+'（'+items.length+'）',!!items.length);
        if(!items.length) box.append(element('p','当前没有待办理事项。'));
        items.forEach(t=>{
          const row=details(box,t.title+' · '+(t.student_id?studentName(t.student_id):t.assignee));
          row.append(element('p',['负责人 '+t.assignee,t.term,t.due_date].filter(Boolean).join(' · ')));
          if(t.session_id) row.append(reportLink(t.session_id));
          entry(row,'办理完成',[key('note','处理记录',{value:kind==='report'?'已阅读本场访谈记录':'',optional:true})],
            'task_update',{id:t.id,status:'done'},'已办理');
        });
      });
      const add=details(page,'新增老师待办');
      entry(add,'创建待办',[key('title','待办事项'),key('assignee','负责人',{value:actor.username}),
        key('term','学期',{value:activeTerm,optional:true}),key('due_date','截止日期',{type:'date',optional:true})],'task');
    }
    function plans() {
      const page=section(body,'学期面访计划');
      const c=data.coverage;
      page.append(element('p','名册应访 '+c.required+' 人次　已完成 '+c.completed+'　覆盖率 '+
        (c.coverage_rate==null?'—':Math.round(c.coverage_rate*1000)/10+'%')+'　尚未排期 '+c.unscheduled+'　过期未完成 '+c.overdue));
      if(data.plans.length) table(page,['学期','班级','计划到期','负责人','已访／计划人次'],
        data.plans.map(p=>[p.term,p.class_name,p.due_date,p.assignee,p.completed+' / '+p.required]));
      const add=details(page,'按班级生成计划',!data.plans.length);
      if(!data.students.length) add.append(element('p','请先登记本学期的学生和班级。'));
      else entry(add,'生成并派发排期',[opts('term','学期',schoolTerms()),opts('class_name','班级',[...new Set(data.students.map(s=>s.class_name))]),
        key('due_date','面访到期日',{type:'date'}),key('assignee','负责人',{value:actor.username})],'plan',{},'班级名册已生成面访排期');
      assignments(details(page,'逐人调整面访排期'),data.appointments.filter(a=>a.status==='scheduled'));
    }
    function temporal(parent, title, periods, field) {
      const box=details(parent,title,true);
      if(!periods.length) {box.append(element('p','尚无记录。'));return;}
      table(box,[field==='year'?'学年':'学期','面访完成／场次','证据','预警','转介','每场原始自评分（按量表版本）'],
        periods.map(p=>[p[field],p.completed+' / '+p.sessions,p.observations,p.alerts,p.referrals,
          p.scores?.length ? p.scores.map(v=>day(v.at)+' '+v.instrument+(v.version?' v'+v.version:'')+' '+total(v.score,v.range)).join('；'):'无完整分数']));
    }
    function evidence(parent, card, student) {
      const box=details(parent,'六维证据卡',true);
      card.dimensions.forEach(dimension=>{
        const rows=card.evidence.filter(e=>e.dimension===dimension);
        const dim=details(box,dimension+'（'+rows.length+'）',!!rows.length);
        if(!rows.length) {dim.append(element('p','暂无记录'));return;}
        rows.forEach(e=>{
          const item=details(dim,e.layer+' · '+e.statement);
          item.append(element('p','来源　'+e.source),element('p','可信程度／依据　'+e.confidence),
            element('p',[e.term,e.author,day(e.created_at)].filter(Boolean).join(' · ')));
          if(e.confirmed_by) item.append(element('p','人工核查　'+e.confirmed_by+'　'+(e.confirm_note || '已记录')));
          if(e.session_id) item.append(reportLink(e.session_id));
          if(e.model) item.append(element('p','摘录状态　'+(e.review_status==='pending'?'待老师核查原话与主题':'已人工核查')+'　发起人　'+e.requested_by));
          if(e.review_status==='pending') entry(item,'退回这条模型摘录',[key('note','语境或主题不符的理由')],
            'dismiss',{id:e.id},'已退回，操作记录已保留');

          if(!e.read_only) entry(item,'保存人工核查',[opts('confidence','核查后可信程度',data.confidence_levels,{value:e.confidence}),
            key('note','核查依据',{long:true})],'confirm',{id:e.id},'核查意见已写入档案');
        });
      });
      const completed=card.sessions.filter(s=>['completed','stopped'].includes(s.status));
      if(completed.length) {
        const extraction=details(box,'从开放回答提取待核查原话');
        extraction.append(element('p','在本机调用 Psyche-R1 检索三个开放问题的已完成回答。老师核查每条原话的主题和语境后，可确认或退回。'));
        form(extraction,[opts('session_id','选择已结束的访谈',completed.map(s=>[s.session_id,s.term+' · '+day(s.created_at)+' · '+s.session_id]))],
          '用本机 Psyche-R1 摘录原话',async values=>{
            info('正在本机提取并核对原话……');
            const out=await request('suggest',values);
            info('新增 '+out.added+' 条待复核摘录，已有 '+out.existing+' 条，排除 '+out.rejected+
              ' 条未通过核对的候选；'+out.omitted+' 段原话因长度或数量限制留待老师手动查阅。');
            await refresh();
          },error);
      }
      if(card.dismissed_evidence?.length) {
        const rejected=details(box,'已退回摘录（'+card.dismissed_evidence.length+'）');
        table(rejected,['学期','原提议维度','原话','来源'],card.dismissed_evidence.map(r=>[r.term,r.dimension,r.statement,r.source]));
      }
      const add=details(box,'添加档案证据');
      entry(add,'保存证据',[chooseTerm(add,student),opts('dimension','维度',card.dimensions),opts('layer','证据层',card.layers),
        opts('confidence','可信程度',data.confidence_levels),key('source','可追溯来源或记录编号'),
        opts('session_id','关联本场访谈',[['','不关联'],...card.sessions.map(s=>[s.session_id,day(s.created_at)+' · '+s.session_id])],{optional:true}),
        key('statement','原话、观察或人工确认内容',{long:true})],'evidence',{student_id:student},'证据已入档');
    }
    function studentCard(parent, card) {
      const s=card.student, area=section(parent,s.name+' · 学生成长档案');
      area.append(element('p','校内学号 '+s.student_code+'　'+card.enrollments.map(e=>e.term+' '+e.class_name).join(' / ')));
      if(card.sessions.length) {
        const records=details(area,'逐场访谈与报告（'+card.sessions.length+'）');
        table(records,['学期','访谈日期','状态','报告'],card.sessions.map(s=>[s.term,day(s.created_at),translate(s.status),reportLink(s.session_id)]));
      }
      temporal(area,'按学期查看',card.semesters,'term'); temporal(area,'按学年查看',card.years,'year');
      evidence(area,card,s.id);
      if(card.scale_reports?.length) {
        const reports=details(area,'授权量表结果');
        table(reports,['日期','量表、版本','总分','专业评审','适用说明'],card.scale_reports.map(r=>[
          day(r.created_at),r.title+' '+r.version,r.total,r.reviewer,r.uncertainty]));
      }
      const history=details(area,'历次面访、证据、预警与转介');
      if(!card.timeline.length) history.append(element('p','暂无访谈或处置记录。'));
      else table(history,['日期','学期','类型','记录'],card.timeline.map(evt=>{
        let record='—';
        if(evt.kind==='访谈') record=reportLink(evt.id);
        if(evt.kind==='证据') record=card.evidence.find(e=>e.id===evt.id)?.statement;
        if(evt.kind==='预警') {const a=card.alerts.find(r=>r.id===evt.id); record=a.summary+' · '+translate(a.status);}
        if(evt.kind==='转介') {const r=card.referrals.find(r=>r.id===evt.id); record=r.receiver+' · '+translate(r.status)+' '+r.receipt;}
        return [day(evt.at),evt.term,evt.kind,record];
      }));
      if(card.history.length) table(details(area,'操作与回执记录'),['时间','事项','操作','经办人','记录'],
        card.history.map(h=>[new Date(h.created_at).toLocaleString('zh-CN'),translate(h.kind),translate(h.action),h.actor,h.note]));
      const meeting=details(area,'启动或关联访谈');
      form(meeting,[chooseTerm(meeting,s.id),opts('mode','访谈方式',[['text','文字与按钮'],['voice','语音与按钮']])],
        '开始新访谈',v=>start({...v,student_id:s.id}),error);
      if(sessions.length) entry(meeting,'关联已有访谈',[chooseTerm(meeting,s.id),opts('session_id','本校访谈',sessionOptions())],
        'link',{student_id:s.id},'已有访谈已归档到该生名下');
    }
    async function students(token) {
      const page=section(body,'学生名册');
      const add=details(page,'登记学籍／新增学期',!data.students.length);
      form(add,[key('student_code','稳定的校内学号'),key('name','姓名或代号'),key('class_name','班级'),
        key('term','学期',{example:'2026秋 或 2026-2027第一学期',value:activeTerm})],
        '保存学籍',async v=>{
          const r=await request('student',v); selectedStudent=r.id; activeTerm=v.term; info('学籍已保存'); await refresh();
        },error);
      if(!data.students.length) return;
      const valid=studentOptions();
      if(!valid.some(v=>v[0]===selectedStudent)) selectedStudent=valid[0][0];
      form(page,[opts('student_id','在册学生',valid,{value:selectedStudent})],'查看成长档案',async v=>{
        selectedStudent=v.student_id;await refresh();
      },error);
      const card=await request('student_file',{student_id:selectedStudent});
      if(token===sequence) studentCard(page,card);
    }
    async function refresh() {
      const token=++sequence;
      const [snapshot,archive] = await Promise.all([request('workspace',{term:activeTerm}),request('sessions',{})]);
      if(token!==sequence) return;
      data=snapshot; sessions=archive.sessions||[]; body.replaceChildren(); nav();
      if(view==='tasks') tasks(); else if(view==='plans') plans(); else await students(token);
    }
    try { await refresh(); } catch(e) {error(e);}
  };
})();
