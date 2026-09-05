"use strict";
(() => {
 const U=window.NovelUI,$=id=>document.getElementById(id);let state=null,timer=null;
 const names={synced:'抽取已覆盖全文',needs_review:'覆盖完成，有修复结果待人工核对',pending:'有范围待同步'};
 async function refresh(){
  state=await U.api('/api/sync');const c=state.coverage;
  $('coverage').textContent=`Revision ${c.revision_no} · ${names[c.status]} · ${c.selected_runs} 个有效分析单元`;
  $('counts').textContent=Object.entries(c.coverage).map(([k,n])=>`${k}: ${n}/${c.total_chars} 字符`).join(' · ');
  $('notice').textContent=c.notice;
  $('start-sync').disabled=c.busy||!c.total_chars;
  const j=state.job;$('job').textContent=j?`${j.status} · ${j.progress.completed}/${j.progress.total} 单元完成${j.error?' · '+j.error:''}${j.stale?' · 旧正文版本':''}`:'尚无同步或全书分析任务';
  $('pause-sync').disabled=!j?.active;
  if(timer)clearTimeout(timer);timer=setTimeout(()=>refresh().catch(e=>U.tell(e.message,true)),2000);
 }
 $('start-sync').addEventListener('click',async()=>{try{
  $('start-sync').disabled=true;const r=await U.api('/api/sync',{method:'POST',body:JSON.stringify({expected_revision_no:state.coverage.revision_no})});
  U.tell(`任务已启动，复用 ${r.reused_units} 个分析单元。失败项可在这里重试。`);await refresh();
 }catch(e){U.tell(e.message,true);$('start-sync').disabled=false;}});
 $('pause-sync').addEventListener('click',async()=>{try{await U.api(`/api/analysis/jobs/${state.job.id}/pause`,{method:'POST'});await refresh();}catch(e){U.tell(e.message,true);}});
 $('refresh-sync').addEventListener('click',()=>refresh().catch(e=>U.tell(e.message,true)));
 U.init().then(refresh).then(()=>U.tell('可复用的分析按原文范围核验；仅缺失单元调用模型。')).catch(e=>U.tell(e.message,true));
})();
