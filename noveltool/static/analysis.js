"use strict";
const ui=window.NovelUI, $=id=>document.getElementById(id);
let plan=null, busy=false;
function body(){
  if (!plan || plan.stale) throw new Error("请先创建当前正文的有效分块计划");
  return {plan_id:plan.id, expected_revision_no:plan.base_revision_no, pass_type:$("pass").value};
}
async function showRun(id){
  const r=await ui.api(`/api/analysis/runs/${encodeURIComponent(id)}`);
  $("result-meta").textContent=`${r.status} · 分块 ${r.ordinal+1} · ${r.model} · ${r.stale?"已过期":"当前版本"} · ${r.requires_review?"经过修复，请人工核对":"结构与引用通过不代表含义一定正确"}${r.error?" · "+r.error:""}`;
  $("observations").replaceChildren();
  for(const o of r.observations){
    const card=ui.node("article",undefined,"observation");
    card.append(ui.node("h4",`${o.kind} · ${o.status}`),ui.node("pre",JSON.stringify(o.payload,null,2),"wrapped"));
    for(const e of o.evidence) card.append(ui.node("blockquote",e.quote),ui.node("small",`${e.scope} · ${e.block_id.slice(0,8)} · 字符 [${e.start_cp}, ${e.end_cp})`));
    $("observations").append(card);
  }
  if(!r.observations.length) $("observations").textContent="没有候选事实（可能是空结果或运行失败）。";
}
async function refresh(){
  const result=await ui.api("/api/import/plan"); plan=result.plan;
  const selected=$("chunk").value; $("chunk").replaceChildren();
  $("plan-info").textContent=plan?`Revision ${plan.base_revision_no} · ${plan.chunk_count} 块 · ${plan.stale?"已过期："+plan.stale_reason:"可分析"}`:"尚无计划。";
  for(const c of plan?.chunks||[]){ const o=ui.node("option",`第 ${c.ordinal+1} 块 · ${c.core_chars} 字符 · 估计 ${c.estimated_tokens} tokens`);o.value=c.ordinal;$("chunk").append(o); }
  if([...$("chunk").options].some(o=>o.value===selected)) $("chunk").value=selected;
  const data=await ui.api("/api/analysis/runs"+(plan?`?plan_id=${encodeURIComponent(plan.id)}`:""));
  $("runs").replaceChildren();
  for(const r of data.runs){const line=ui.node("p");line.append(ui.button(`第 ${r.ordinal+1} 块 · ${r.pass_type} · ${r.status}${r.stale?" · 过期":""}`,()=>showRun(r.id)));$("runs").append(line);}
  if(!data.runs.length)$("runs").textContent="还没有运行记录。";
  $("run").disabled=busy||!plan||plan.stale; $("preview").disabled=!plan||plan.stale;
}
$("preview").onclick=async()=>{try{const data=await ui.api(`/api/analysis/chunks/${Number($("chunk").value)}/preview`,{method:"POST",body:JSON.stringify(body())});$("prompt").textContent=JSON.stringify(data,null,2);ui.tell("预览完成，没有调用模型。");}catch(e){ui.tell(e.message,true);}};
$("run").onclick=async()=>{if(busy)return;busy=true;$("run").disabled=true;try{ui.tell("正在分析；成功结果会连同证据保存。修改正文会使本次结果过期。");const r=await ui.api(`/api/analysis/chunks/${Number($("chunk").value)}/run`,{method:"POST",body:JSON.stringify(body())});await showRun(r.id);ui.tell(r.stale?"正文已变化，结果已隔离保存为过期。":"分析完成，候选事实已保存到待审区。");}catch(e){ui.tell(e.message,true);}finally{busy=false;await refresh().catch(e=>ui.tell(e.message,true));}};
$("refresh").onclick=()=>refresh().catch(e=>ui.tell(e.message,true));
(async()=>{try{await ui.init();await refresh();ui.tell("先预览模型将读取的文本，再选择单块分析。");}catch(e){ui.tell(e.message,true);}})();

let latestJob=null;
async function pollJob(){
  try{latestJob=(await ui.api("/api/analysis/jobs/latest")).job;
    $("job-status").textContent=latestJob?`${latestJob.status} · 完成 ${latestJob.progress.completed}/${latestJob.progress.total} · 失败 ${latestJob.progress.failed}${latestJob.error?" · "+latestJob.error:""}`:"没有全书任务。";
    $("start-job").disabled=!!latestJob?.active; $("pause-job").disabled=!latestJob?.active;
  }catch(e){ui.tell(e.message,true);}
}
$("start-job").onclick=async()=>{try{const passes=["facts","links","narrative"].filter(p=>$("job-"+p).checked);await ui.api("/api/analysis/jobs",{method:"POST",body:JSON.stringify({...body(),passes,retry_failed:$("retry-failed").checked})});ui.tell("全书任务已在本程序中启动；完成一个分块就保存一个检查点。");await pollJob();}catch(e){ui.tell(e.message,true);}};
$("pause-job").onclick=async()=>{try{if(latestJob){await ui.api(`/api/analysis/jobs/${latestJob.id}/pause`,{method:"POST",body:"{}"});await pollJob();}}catch(e){ui.tell(e.message,true);}};
$("show-catalog").onclick=async()=>{try{const data=await ui.api("/api/knowledge");$("catalog").textContent=JSON.stringify(data,null,2);$("catalog-counts").textContent=`${data.entities.length} 个实体 · ${data.events.length} 个事件 · ${data.relationships.length} 个关系 · ${data.threads.length} 条线索 · ${data.review_queue.length} 项待审`;ui.tell(data.notice);}catch(e){ui.tell(e.message,true);}};
setTimeout(function tick(){pollJob().finally(()=>setTimeout(tick,2000));},1000);
