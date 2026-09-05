"use strict";
(() => {
 const U=window.NovelUI,$=id=>document.getElementById(id);let revision=0;
 async function load(){
  const v=await U.api('/api/maintenance');revision=v.revision_no;
  $('overview').textContent=`Revision ${v.revision_no} · Schema ${v.schema_version} · 数据库 ${v.database_bytes} bytes · WAL ${v.wal_bytes} bytes · ${v.dirty_sections.length?'内存尚待保存：'+v.dirty_sections.join(', '):'程序内存已保存'}${v.last_save_error?' · 保存错误：'+v.last_save_error:''}`;
  $('recovery').textContent=JSON.stringify({本次启动接管的未结束记录:v.startup_recovery,最近未完成记录:v.unfinished,升级前备份:v.migration_backup},null,2);
  const history=await U.api('/api/revisions');$('revision-history').replaceChildren();
  for(const r of history.revisions)$('revision-history').append(U.button(`#${r.revision_no} · ${r.kind} · ${r.created_at}`,async()=>{
   const d=await U.api(`/api/maintenance/revisions/${r.revision_no}`);$('history-before').textContent=d.before;$('history-after').textContent=d.after;$('history-diff').textContent=d.diff;$('revision-notice').textContent=d.notice+(d.diff_truncated?' 差异超过 600 行，仅显示开头。':'');$('revision-detail').open=true;
  }));
  const runs=await U.api('/api/llm/runs');$('runs').replaceChildren();
  for(const r of runs.runs)$('runs').append(U.button(`${r.purpose} · ${r.model} · ${r.status} · ${r.finished_at}`,async()=>{
   $('run-detail').textContent=JSON.stringify(await U.api(`/api/maintenance/runs/${r.id}`),null,2);
  }));
 }
 $('refresh-maintenance').addEventListener('click',()=>load().catch(e=>U.tell(e.message,true)));
 $('save-project').addEventListener('click',async()=>{try{await U.api('/api/save',{method:'POST'});await load();U.tell('程序内存已经保存；浏览器未提交的内容不在其中。');}catch(e){U.tell(e.message,true);}});
 $('check-project').addEventListener('click',async()=>{try{$('check-project').disabled=true;const r=await U.api('/api/maintenance/check',{method:'POST',body:JSON.stringify({expected_revision_no:revision})});$('check-result').textContent=JSON.stringify(r,null,2);U.tell(r.ok?'列出的完整性检查通过；这不是语义证明。':'发现错误，请保留原项目并使用备份，不要继续覆盖。',!r.ok);}catch(e){U.tell(e.message,true);}finally{$('check-project').disabled=false;}});
 $('backup-project').addEventListener('click',async()=>{
  if(!confirm('保存已提交到程序内存的内容并下载完整项目备份？备份包含私人正文、历史与保留日志。'))return;
  try{$('backup-project').disabled=true;const response=await fetch('/api/maintenance/backup',{method:'POST',headers:{'Content-Type':'application/json','X-Noveltool-Token':U.token},body:JSON.stringify({expected_revision_no:revision}),credentials:'same-origin'});
   if(!response.ok){const e=await response.json();throw new Error(typeof e.detail==='string'?e.detail:JSON.stringify(e.detail));}
   const blob=await response.blob(),url=URL.createObjectURL(blob),a=U.node('a');a.href=url;a.download=`noveltool-revision-${revision}-backup.sqlite3`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);await load();U.tell('备份已交给浏览器下载，请确认文件实际保存成功。');
  }catch(e){U.tell(e.message,true);}finally{$('backup-project').disabled=false;}
 });
 U.init().then(load).then(()=>U.tell('维护操作不会调用模型。建议定期将备份放在另一处保存。')).catch(e=>U.tell(e.message,true));
})();
