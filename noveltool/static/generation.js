"use strict";
(() => {
 const U=window.NovelUI,$=id=>document.getElementById(id);
 let catalog=null,doc=null,task=null,timer=null,cardTask=null,formDirty=false;
 let draftId=null,draftVersion=0,draftAck='',draftRequest=null,draftTimer=null,draftConflict=false,committing=false,saveOperation=null;
 const cards=new Map();
 const labels={generating:"生成中",pausing:"即将暂停",paused:"已暂停",ready:"候选已返回",failed:"生成失败",interrupted:"已中断",stale:"基线过期",committed:"已确认",running:"请求中",complete:"范围内",length_mismatch:"超出字数范围"};
 const draftChanged=()=>draftId!==null&&$("draft").value!==draftAck;
 // Match Python str.isspace(), including U+0085 and U+001C..001F, not JS \s.
 const whitespace=/^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]$/u;
 const count=text=>Array.from(text).filter(c=>!whitespace.test(c)).length;
 function contextBody(){const rewrite=$("task-type").value==="rewrite";return {task_type:rewrite?"rewrite":"continue",start_cp:rewrite?Number($("rewrite-start").value):null,end_cp:rewrite?Number($("rewrite-end").value):null,expected_revision_no:doc.revision_no,expected_version:catalog.version,
  instruction:$("instruction").value,min_chars:Number($("min-chars").value),max_chars:Number($("max-chars").value),
  output_reserve:$("reserve").value?Number($("reserve").value):null,pinned_entities:Array.from($("pins").selectedOptions).map(x=>x.value)};}
 async function history(){const data=await U.api('/api/generation');$("history").replaceChildren();
  for(const r of data.tasks)$("history").append(U.button(`${r.created_at} · ${labels[r.status]||r.status} · ${r.candidate_count} 个`,async()=>{
    if(committing)throw new Error('正在确认正文，请不要切换任务。');await saveDraft(true);load(await U.api(`/api/generation/${r.id}`));}));
  if(!data.tasks.length)$("history").append(U.node('p','还没有生成任务。'));}
 async function refresh(reset=true){
  [catalog,doc]=await Promise.all([U.api('/api/settings'),U.api('/api/manuscript')]);const cfg=(await U.api('/api/config')).config;
  if(reset){$("min-chars").value=cfg.min_chars;$("max-chars").value=cfg.max_chars;$("candidate-count").value=cfg.candidate_count;}
  $("project-info").textContent=`正文 Revision ${doc.revision_no} · 写作模型 ${cfg.writer_model||'尚未配置'} · ${catalog.entities.length} 个当前实体。`;
  const selected=reset?[]:Array.from($("pins").selectedOptions).map(o=>o.value);
  $("pins").replaceChildren(...catalog.entities.map(e=>{const o=U.node('option',e.name);o.value=e.id;o.selected=selected.includes(e.id);return o;}));
  $("rewrite-source").value=doc.text;
  if(reset){$("rewrite-start").value=0;$("rewrite-end").value=Math.min(100,Array.from(doc.text).length);formDirty=false;}
  showSelection();await history();if(task)load(await U.api(`/api/generation/${task.id}`));
 }
 function schedule(){if(timer)clearTimeout(timer);if(task?.live||task?.draft_dirty)timer=setTimeout(async()=>{
   try{const id=task.id;const v=await U.api(`/api/generation/${id}`);if(task?.id===id)load(v);}
   catch(e){U.tell(e.message,true);schedule();}},1200);}
 function load(v,replaceDraft=false){
  if(!replaceDraft&&draftId===v.id&&v.draft_version<draftVersion){schedule();return;}
  task=v;
  if(draftId!==v.id||replaceDraft){
    if(draftTimer)clearTimeout(draftTimer);
    draftId=v.id;draftVersion=v.draft_version;draftAck=v.draft_text;draftConflict=false;$("draft").value=v.draft_text;
    $("allow-length").checked=false;$("commit-notice").textContent=v.status==='committed'?'这是已经确认的历史草稿；正文可能后来被撤销或返修，请到正文页查看当前文本。':'确认时可启动增量设定同步；同步失败不会回滚正文。';
  }else if(!draftRequest&&v.draft_version>draftVersion){
    if(draftChanged())draftConflict=true;
    else{draftVersion=v.draft_version;draftAck=v.draft_text;$("draft").value=v.draft_text;}
  }
  draw();schedule();
 }
 function drawDraft(){if(!task)return;
  const chars=count($("draft").value),within=task.min_chars<=chars&&chars<=task.max_chars;
  $("draft-count").textContent=`当前 ${chars} 字 · 本任务 ${task.min_chars}–${task.max_chars} 字 · ${within?'范围内':'超出范围；提交需要明确勾选允许'}`;
  let status=task.status==='committed'?'已确认并保存到正文':draftConflict?'草稿版本冲突或保存失败；本地输入保留。先复制备份，再重试保存或载入服务端草稿。':draftRequest?'正在同步草稿到程序':draftChanged()?'当前修改仅在浏览器中，尚未同步':task.draft_dirty?`草稿 v${draftVersion} 在程序内存，等待集中写盘`:`草稿 v${draftVersion} 已保存到磁盘`;
  if(task.draft_dirty&&task.draft_save_error)status+='；写盘失败：'+task.draft_save_error;
  $("draft-status").textContent=status;$("draft-status").className=draftConflict?'message error':'hint';
  $("draft").readOnly=committing||task.status==='committed';$("allow-length").disabled=committing||task.status==='committed';
  $("save-draft").disabled=committing||task.status==='committed';$("reload-draft").disabled=committing;
  $("commit-draft").disabled=committing||task.live||task.stale||task.status==='committed'||draftConflict||chars===0;
 }
 function changed(){drawDraft();if(draftTimer)clearTimeout(draftTimer);
  if(!draftConflict)draftTimer=setTimeout(()=>saveDraft(false).catch(e=>U.tell(e.message,true)),900);}
 async function saveDraft(force){
  if(saveOperation){await saveOperation;return saveDraft(force);}
  saveOperation=persistDraft(force);
  try{return await saveOperation;}finally{saveOperation=null;}
 }
 async function persistDraft(force){
  if(draftTimer)clearTimeout(draftTimer);
  if(draftRequest)await draftRequest;
  if(!task||task.status==='committed')return;
  if(draftConflict&&!force)throw new Error('草稿尚未同步，请先处理保存冲突。');
  // Serialize requests. Edits made during a request are sent in the next loop,
  // never overwritten by its response or by a polling response.
  while(draftChanged()||(force&&task.draft_dirty)){
    const id=draftId,text=$("draft").value,version=draftVersion;
    draftRequest=U.api(`/api/generation/${id}/draft`,{method:'PUT',body:JSON.stringify({text,expected_draft_version:version,force_save:force})});
    drawDraft();
    let v;
    try{v=await draftRequest;}
    catch(e){draftConflict=true;throw e;}
    finally{draftRequest=null;drawDraft();}
    if(draftId!==id)throw new Error('任务已变化；请检查原任务草稿。');
    draftVersion=v.draft_version;draftAck=text;draftConflict=false;load(v);
  }
  drawDraft();
 }
 function adopt(text){
  if(!task||committing||task.status==='committed')return;
  if($("draft").value&&$("draft").value!==text&&!confirm('用这个候选替换当前最终草稿？'))return;
  $("draft").value=text;$("draft").setSelectionRange(text.length,text.length);changed();$("draft").focus();
 }
 function insertSelection(textarea){
  if(!task||committing||task.status==='committed')return;
  const text=textarea.value.slice(textarea.selectionStart,textarea.selectionEnd);
  if(!text)throw new Error('请先在这个候选中选中需要的文字。');
  const draft=$("draft");draft.setRangeText(text,draft.selectionStart,draft.selectionEnd,'end');changed();draft.focus();
 }
 function draw(){if(!task)return;$("task-panel").hidden=false;
  $("task-info").textContent=`${labels[task.status]||task.status} · 基于 Revision ${task.base_revision_no} · ${task.model} · ${task.min_chars}–${task.max_chars} 字 · 可用文本 ${task.usable_count}/${task.candidate_count}，范围内 ${task.in_range_count}/${task.candidate_count}`;
  $("task-error").textContent=[task.error,task.stale?'正文或设定已变化，旧候选不能直接确认。':'',...(task.context_warnings||[]).filter(w=>w.includes('旧自动分析'))].filter(Boolean).join('；');
  $("pause").disabled=committing||!task.live;$("resume").disabled=committing||task.live||task.stale||task.status==='committed';
  if(cardTask!==task.id){cards.clear();$("candidates").replaceChildren();cardTask=task.id;}
  for(const slot of task.slots){let c=cards.get(slot.index);
   if(!c){const el=U.node('section',undefined,'card'),title=U.node('h4',`候选 ${slot.index+1}`),meta=U.node('p',undefined,'hint'),text=U.node('textarea');text.className='prose';text.readOnly=true;text.setAttribute('aria-label',`候选 ${slot.index+1} 正文`);
    const retry=U.button('单独重新生成',async()=>{await saveDraft(false);load(await U.api(`/api/generation/${task.id}/regenerate/${slot.index}`,{method:'POST'}));U.tell('只重试这一项；上一可用版本会继续保留。');});
    const take=U.button('全部采用到草稿',()=>adopt(text.value)),part=U.button('选中文字插入草稿',()=>insertSelection(text));
    const attempts=U.node('details'),summary=U.node('summary','尝试记录'),log=U.node('div');attempts.append(summary,log);
    el.append(title,meta,text,take,part,retry,attempts);$("candidates").append(el);c={el,meta,text,retry,take,part,log,signature:''};cards.set(slot.index,c);}
   const latest=slot.latest_attempt,candidate=slot.candidate;
   c.meta.textContent=candidate?`${candidate.char_count} 字 · ${labels[candidate.status]}${slot.duplicate_of!==null?` · 与候选 ${slot.duplicate_of+1} 完全重复`:''}${latest&&latest.id!==candidate.id?` · 最新尝试：${labels[latest.status]||latest.status}，保留上一可用版本`:''}`:(latest?`${labels[latest.status]||latest.status}：${latest.error||''}`:'尚未生成');
   if(c.text.value!==(candidate?.text||''))c.text.value=candidate?.text||'';
   c.retry.disabled=task.live||task.stale||task.status==='committed'||committing;
   c.take.disabled=c.part.disabled=!candidate||task.status==='committed'||committing;
   const tries=task.attempts.filter(a=>a.candidate_index===slot.index),signature=JSON.stringify(tries.map(a=>[a.id,a.status]));
   if(signature!==c.signature){c.signature=signature;c.log.replaceChildren();for(const a of tries){const d=U.node('details');d.append(U.node('summary',`第 ${a.attempt_no} 次 · ${labels[a.status]||a.status}${a.error?' · '+a.error:''}`));if(a.text)d.append(U.node('pre',a.text,'wrapped'));c.log.append(d);}}
  }
  const rewrite=task.task_type==='rewrite';
  $("task-target-panel").hidden=!rewrite;$("check-after-label").hidden=!rewrite;$("task-target").textContent=task.rewrite_target?.selected_text||'';
  $("preview-rewrite").hidden=!rewrite;$("preview-rewrite").disabled=committing||draftConflict;
  $("commit-draft").textContent=rewrite?'确认替换原选区':'确认追加到正文';
  drawDraft();
 }
 function guard(fn){return ()=>Promise.resolve().then(fn).catch(e=>U.tell(e.message,true));}
 $("generate-form").addEventListener('input',()=>{formDirty=true;});
 $("refresh").addEventListener('click',guard(async()=>{if(committing)throw new Error('正在确认正文，请不要重载项目。');if(formDirty&&!confirm('重新载入将重置 A/B/n 和固定实体，继续？'))return;await saveDraft(true);await refresh();U.tell('项目已重新载入；历史任务仍使用原来的参数。');}));
 $("preview").addEventListener('click',guard(async()=>{$("context-preview").textContent=JSON.stringify(await U.api('/api/context/preview',{method:'POST',body:JSON.stringify(contextBody())}),null,2);U.tell('上下文预览完成，未调用模型。');}));
 $("generate-form").addEventListener('submit',async e=>{e.preventDefault();if(committing)return;$("start").disabled=true;
  try{await saveDraft(true);load(await U.api('/api/generation',{method:'POST',body:JSON.stringify({context:contextBody(),candidate_count:Number($("candidate-count").value)})}));await history();U.tell('任务已创建；候选按顺序独立生成并逐个保存，正文不变。');}
  catch(err){U.tell(err.message,true);}finally{$("start").disabled=false;}});
 $("pause").addEventListener('click',guard(async()=>load(await U.api(`/api/generation/${task.id}/pause`,{method:'POST'}))));
 $("resume").addEventListener('click',guard(async()=>load(await U.api(`/api/generation/${task.id}/resume`,{method:'POST'}))));
 $("task-context").addEventListener('click',guard(async()=>{$("context-preview").textContent=JSON.stringify(await U.api(`/api/generation/${task.id}/context`),null,2);U.tell('已载入该任务创建时的上下文，不是重新根据当前正文生成。');}));
 $("draft").addEventListener('input',changed);$("allow-length").addEventListener('change',drawDraft);
 $("save-draft").addEventListener('click',guard(async()=>{await saveDraft(true);U.tell('草稿已保存到磁盘，正文未修改。');}));
 $("reload-draft").addEventListener('click',guard(async()=>{
  if(!confirm('重新载入会丢弃本页尚未同步的输入，请先复制备份。继续？'))return;
  if(draftTimer)clearTimeout(draftTimer);if(draftRequest)try{await draftRequest;}catch(_){/* local text retained until confirmed reload */}
  load(await U.api(`/api/generation/${task.id}`),true);U.tell('已载入服务端草稿。');}));
 $("commit-draft").addEventListener('click',guard(async()=>{
  if(!task||committing)return;
  if(!confirm(task.task_type==='rewrite'?'用最终草稿替换本任务指定的原文范围？范围外不变，确认后可撤销。':'将最终草稿追加为已确认正文？确认后可在正文页撤销。'))return;
  committing=true;draw();
  try{
    await saveDraft(true);
    const result=await U.api(`/api/generation/${task.id}/commit`,{method:'POST',body:JSON.stringify({text:$("draft").value,expected_draft_version:draftVersion,allow_out_of_range:$("allow-length").checked,sync_after_commit:$("sync-after-commit").checked,check_after_commit:$("check-after-commit").checked})});
    load(result.task);await refresh(false);$("commit-notice").textContent=result.notice;
    U.tell(`已确认到 Revision ${result.revision.revision_no}。${result.notice}`);
  }finally{committing=false;draw();}
 }));
 function showSelection(){
  const rewrite=$("task-type").value==='rewrite';$("rewrite-controls").hidden=!rewrite;
  $("start").textContent=rewrite?'生成 n 个返修':'生成 n 个续写';
  const a=Number($("rewrite-start").value),b=Number($("rewrite-end").value),chars=Array.from(doc?.text||'');
  $("rewrite-selection").textContent=Number.isInteger(a)&&Number.isInteger(b)&&a>=0&&a<b&&b<=chars.length?`将替换 [${a}, ${b})：\n`+chars.slice(a,b).join(''):'请选择非空且合法的返修范围。';
 }
 $("task-type").addEventListener('change',showSelection);
 for(const id of ['rewrite-start','rewrite-end'])$(id).addEventListener('input',showSelection);
 $("rewrite-use-selection").addEventListener('click',()=>{const el=$("rewrite-source");$("rewrite-start").value=Array.from(el.value.slice(0,el.selectionStart)).length;$("rewrite-end").value=Array.from(el.value.slice(0,el.selectionEnd)).length;showSelection();});
 $("rewrite-use-lines").addEventListener('click',guard(async()=>{
  const r=await U.api('/api/manuscript/line-range',{method:'POST',body:JSON.stringify({expected_revision_no:doc.revision_no,first_line:Number($("rewrite-first-line").value),last_line:Number($("rewrite-last-line").value)})});
  $("rewrite-start").value=r.start_cp;$("rewrite-end").value=r.end_cp;showSelection();
 }));
 $("preview-rewrite").addEventListener('click',guard(async()=>{
  await saveDraft(true);const r=await U.api(`/api/generation/${task.id}/preview`,{method:'POST',body:JSON.stringify({text:$("draft").value,expected_draft_version:draftVersion})});
  $("rewrite-diff").textContent=r.diff+(r.diff_truncated?'\n（差异较长，展示前 600 行；完整原文及草稿在上方）':'');
  U.tell(r.notice+(r.stale?' 任务基线已过期，不能直接确认。':''));
 }));
 function applyLink(){const q=new URLSearchParams(location.search);if(q.get('mode')!=='rewrite')return;
  if(Number(q.get('revision'))!==doc.revision_no)throw new Error('链接基于旧正文版本，未自动套用范围；请从当前正文重新选择。');
  const a=Number(q.get('start')),b=Number(q.get('end'));if(!q.has('start')||!q.has('end')||!Number.isInteger(a)||!Number.isInteger(b)||a<0||a>=b||b>Array.from(doc.text).length)throw new Error('链接中的返修范围无效。');
  $("task-type").value='rewrite';$("rewrite-start").value=a;$("rewrite-end").value=b;showSelection();
 }
 window.addEventListener('beforeunload',e=>{if(draftChanged()||draftRequest){e.preventDefault();e.returnValue='';}});
 U.init().then(refresh).then(applyLink).then(()=>U.tell('选择本次要求和 A/B/n，可以先预览上下文。')).catch(e=>U.tell(e.message,true));
})();
