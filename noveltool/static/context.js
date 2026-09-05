"use strict";
(() => {
  const U=window.NovelUI,$=id=>document.getElementById(id);
  let catalog=null,doc=null,dirty=false;
  const status={known:"已知",partial:"部分已知（不是完整集合）",unknown:"未知",conflict:"冲突，未确定"};
  async function refresh(){
    [catalog,doc]=await Promise.all([U.api("/api/settings"),U.api("/api/manuscript")]);
    const cfg=(await U.api("/api/config")).config;
    $("source-text").value=doc.text;$("state-position").value=Array.from(doc.text).length;
    $("end-cp").value=Math.min(Array.from(doc.text).length,100);$("min-chars").value=cfg.min_chars;$("max-chars").value=cfg.max_chars;
    $("project-info").textContent=`正文 Revision ${doc.revision_no} · ${catalog.text_length} 字符 · 安全预算 ${Math.floor(cfg.context_window*cfg.context_safety_ratio)} token（估计）`;
    $("pins").replaceChildren();catalog.entities.forEach(e=>{const o=U.node("option",e.name);o.value=e.id;$("pins").append(o);});
    $("package").hidden=true;dirty=false;await showState();
  }
  function showEntity(parent,e){
    const card=U.node("section",undefined,"card");card.append(U.node("h4",e.name));
    if(!e.effective_fields.length)card.append(U.node("p","此位置没有可用字段；不等于人物没有其他属性。","hint"));
    for(const f of e.effective_fields){const text=Array.isArray(f.value)?f.value.join("；"):f.value??"未确定";
      card.append(U.node("p",`${f.field}：${text} · ${status[f.status]||f.status}`));
      if(f.conflicts.length)card.append(U.node("p",f.conflicts.map(x=>`${x.reason}：${x.values.join(" / ")}`).join("；"),"hint"));
      if(f.history.length){const d=U.node("details");d.append(U.node("summary",`${f.field} 的变化依据（${f.history.length} 条）`));
        for(const r of f.history)d.append(U.node("p",`位置 ${r.at_cp} · ${r.source==="manual"?"人工":"自动"} · ${r.operation||"set"} → ${r.value}`));card.append(d);}
    }parent.append(card);
  }
  async function showState(){
    const at=Number($("state-position").value),state=await U.api(`/api/state?at_cp=${at}`);
    $("state-info").textContent=`位置 ${state.at_cp} / Revision ${state.revision_no}。${state.notice}`;
    const parent=$("state-result");parent.replaceChildren();state.entities.forEach(e=>showEntity(parent,e));
    if(!state.entities.length)parent.append(U.node("p","此位置尚无可用实体；可先分析前文或建立初始设定。"));
    state.warnings.forEach(w=>parent.append(U.node("p",w,"hint")));
    const names=Object.fromEntries(state.entities.map(e=>[e.id,e.name]));
    const related=U.node("details");related.append(U.node("summary",`关系 ${state.relationships.length} / 线索 ${state.threads.length}`));
    state.relationships.forEach(r=>related.append(U.node("p",`${names[r.a]} → ${names[r.b]}：${r.label}；${r.conflict?"当前描述冲突":r.description}`)));
    state.threads.forEach(t=>related.append(U.node("p",`${t.title}：${t.conflict?"状态冲突":t.status}；${t.description}`)));parent.append(related);
  }
  function guard(fn){return ()=>Promise.resolve().then(fn).catch(e=>U.tell(e.message,true));}
  $("refresh").addEventListener("click",guard(async()=>{if(dirty&&!confirm("重新载入会重置字数与定位参数，继续？"))return;await refresh();U.tell("项目状态已刷新，未调用模型。");}));
  $("show-state").addEventListener("click",guard(showState));
  $("state-end").addEventListener("click",guard(async()=>{$("state-position").value=Array.from(doc.text).length;await showState();}));
  async function locate(line,edge){const p=await U.api(`/api/settings/position?line=${Number(line)}&edge=${edge}`);if(p.revision_no!==doc.revision_no)throw new Error("正文已改变，请先重新载入再定位");return p.at_cp;}
  $("locate-state").addEventListener("click",guard(async()=>{$("state-position").value=await locate($("state-line").value,"end");await showState();}));
  $("use-lines").addEventListener("click",guard(async()=>{$("start-cp").value=await locate($("start-line").value,"start");$("end-cp").value=await locate($("end-line").value,"end");dirty=true;}));
  $("use-selection").addEventListener("click",()=>{const el=$("source-text");$("start-cp").value=Array.from(el.value.slice(0,el.selectionStart)).length;$("end-cp").value=Array.from(el.value.slice(0,el.selectionEnd)).length;dirty=true;});
  $("task-type").addEventListener("change",()=>{$("range-controls").hidden=$("task-type").value!=="rewrite";dirty=true;});
  $("context-form").addEventListener("input",()=>{dirty=true;});
  $("context-form").addEventListener("submit",async event=>{event.preventDefault();
    const rewrite=$("task-type").value==="rewrite";
    const body={task_type:$("task-type").value,expected_revision_no:doc.revision_no,expected_version:catalog.version,
      instruction:$("instruction").value,start_cp:rewrite?Number($("start-cp").value):null,end_cp:rewrite?Number($("end-cp").value):null,
      min_chars:Number($("min-chars").value),max_chars:Number($("max-chars").value),output_reserve:$("reserve").value?Number($("reserve").value):null,
      pinned_entities:Array.from($("pins").selectedOptions).map(o=>o.value)};
    try{const p=await U.api("/api/context/preview",{method:"POST",body:JSON.stringify(body)});
      $("package").hidden=false;$("budget").textContent=`输入估计 ${p.input_token_estimate} + 输出预留 ${p.output_token_reserve} = ${p.input_token_estimate+p.output_token_reserve} / 安全预算 ${p.safe_budget}。目标 ${p.min_chars}–${p.max_chars} 字。`;
      $("warnings").replaceChildren(...p.warnings.map(w=>U.node("p",w,"hint")));
      $("included").replaceChildren();$("dropped").replaceChildren();
      for(const s of p.sections){const d=U.node("details");d.append(U.node("summary",`${s.label} · ${s.required?"必需":"可选"} · 优先级 ${s.priority} · 内容估计 ${s.estimated_content_tokens}`));d.append(U.node("pre",s.text,"wrapped"));$(s.included?"included":"dropped").append(d);}
      if(!p.dropped_sections.length)$("dropped").append(U.node("p","没有省略可选项。"));
      $("messages").textContent=JSON.stringify(p.messages,null,2);U.tell("上下文预览完成；未发送模型请求，正文未改变。");
    }catch(e){U.tell(e.message,true);}
  });
  U.init().then(refresh).then(()=>U.tell("可按位置查看状态，或预览续写/返修所需上下文。")).catch(e=>U.tell(e.message,true));
})();
