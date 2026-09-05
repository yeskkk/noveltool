"use strict";
(() => {
 const U=window.NovelUI,$=id=>document.getElementById(id);let state=null,revision=0,timer=null;
 const labels={waiting_sync:'等待设定同步',running:'检查中',pausing:'即将暂停',paused:'已暂停',done:'检查单元已完成',partial:'部分单元失败',stale:'旧版本检查',interrupted:'已中断',failed:'失败',not_applicable:'没有后文可检查',open:'待核对',resolved:'已处理',ignored:'已忽略'};
 async function history(){const r=await U.api('/api/consistency');revision=r.revision_no;$('revision-info').textContent=`当前正文 Revision ${revision}。检查最新一次范围返修/手工替换；追加或撤销不属于这个比较入口。`;
  $('history').replaceChildren();for(const j of r.jobs)$('history').append(U.button(`Revision ${j.base_revision_no} · ${labels[j.status]} · ${j.created_at}`,async()=>load(await U.api(`/api/consistency/${j.id}`))));
  if(!state&&r.jobs.length)load(await U.api(`/api/consistency/${r.jobs[0].id}`));
 }
 function load(r){state=r;const j=r.job;$('results').hidden=false;
  $('job-info').textContent=`${labels[j.status]} · Revision ${j.base_revision_no} · 后文已检查 ${r.checked_chars}/${j.total_chars} 字符 · ${r.issues.length} 条可能问题${r.stale?' · 当前正文已变化，以下为历史检查':''}`;
  $('job-error').textContent=[j.error,...r.errors,r.notice].filter(Boolean).join('；');
  $('change-before').textContent=j.change.old_text;$('change-after').textContent=j.change.new_text;
  $('resume-check').disabled=r.active||r.stale||['done','not_applicable'].includes(j.status);$('pause-check').disabled=!r.active;
  $('units').replaceChildren();for(const u of r.units){const line=U.node('p',`单元 ${u.ordinal+1} · ${u.status} · ${u.char_count} 字符${u.requires_review?' · 结果经修复，需人工核对':''}${u.error?' · '+u.error:''}`);line.append(U.button('查看当时输入',async()=>{$('input').textContent=JSON.stringify(await U.api(`/api/consistency/${j.id}/input/${u.ordinal}`),null,2);}));$('units').append(line);}
  drawIssues();if(timer)clearTimeout(timer);if(r.active)timer=setTimeout(async()=>{try{load(await U.api(`/api/consistency/${j.id}`));}catch(e){U.tell(e.message,true);}},1400);
 }
 function drawIssues(){const parent=$('issues');parent.replaceChildren();if(!state)return;
  const filter=$('issue-filter').value;
  for(const issue of state.issues){if(filter!=='all'&&issue.status!==filter)continue;
   const card=U.node('section',undefined,'card');card.append(U.node('h4',`${issue.severity.toUpperCase()} · ${issue.finding.title} · ${labels[issue.status]}`),U.node('p',issue.finding.explanation));
   for(const e of issue.evidence){card.append(U.node('strong',e.label),U.node('pre',e.quote,'wrapped'));}
   for(const status of ['open','resolved','ignored']){const button=U.button(labels[status],async()=>load(await U.api(`/api/consistency/issues/${issue.id}`,{method:'PUT',body:JSON.stringify({expected_version:issue.version,status})})));button.disabled=status===issue.status;card.append(button);}
   parent.append(card);
  }
  if(!parent.children.length)parent.append(U.node('p',state.issues.length?'当前筛选没有问题。':'此任务尚未报告问题。仍须核对完成比例、失败单元和模型判断；无报告不代表全文一致。','hint'));
 }
 $('start-check').addEventListener('click',async()=>{try{$('start-check').disabled=true;load(await U.api('/api/consistency',{method:'POST',body:JSON.stringify({expected_revision_no:revision,output_reserve:Number($('output-reserve').value)})}));await history();U.tell('检查已建立；只报告问题，不改正文。');}catch(e){U.tell(e.message,true);}finally{$('start-check').disabled=false;}});
 for(const action of ['resume','pause'])$(action+'-check').addEventListener('click',async()=>{try{load(await U.api(`/api/consistency/${state.job.id}/${action}`,{method:'POST'}));}catch(e){U.tell(e.message,true);}});
 $('refresh-check').addEventListener('click',()=>history().catch(e=>U.tell(e.message,true)));$('issue-filter').addEventListener('change',drawIssues);
 U.init().then(history).then(()=>U.tell('这里是提示性检查，问题处理状态由你决定。')).catch(e=>U.tell(e.message,true));
})();
