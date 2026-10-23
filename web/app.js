/* All support replies stay in the application conversation. */
let company = 'acme', view = 'chat', mode = 'demo', conversations = [], eventController;
const $ = s => document.querySelector(s);
const token = role => `demo-${company}-${role}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options = {}, role = 'rep') {
  const requestedCompany = company;
  const response = await fetch(path, {...options, headers: {Authorization:`Bearer ${token(role)}`, ...(options.body instanceof FormData ? {} : {'Content-Type':'application/json'}), ...options.headers}});
  if (!response.ok) { const error = await response.json(); throw Error(error.detail || `Request failed (${response.status})`); }
  const result = await response.json();
  if (requestedCompany !== company) throw Error('Workspace changed; previous request was discarded.');
  return result;
}
function notice(text, error = false) { $('#notice').textContent = text; $('#notice').className = error ? 'error' : ''; }
function candidateList(items) { return items.map(c => `<div class="candidate"><strong>${esc(c.title)} · page ${c.page} · ${esc(c.kind)}</strong><p>${esc(c.text)}</p><span class="badge ${c.passed ? 'pass' : 'review'}">${c.passed ? 'Passed' : 'Review'}</span>Relevance ${Number(c.relevance).toFixed(2)} / Answerability ${Number(c.answerability).toFixed(2)}</div>`).join(''); }
async function refreshTickets() {
  const tickets = await api('/api/tickets');
  $('#ticket-count').textContent = tickets.filter(t => t.status === 'open').length;
  $('#ticket-list').innerHTML = tickets.length ? tickets.map(t => `<article class="ticket"><span class="badge ${t.status === 'resolved' ? 'pass' : 'review'}">${esc(t.status)}</span><h3>${esc(t.question)}</h3><details><summary>Retrieved evidence (${t.rejected.length})</summary>${candidateList(t.rejected)}</details>${t.status === 'resolved' ? `<p>${esc(t.reply)}</p><span class="badge">${t.saved_document ? 'Saved to knowledge' : 'Reply in conversation'}</span>` : `<form data-ticket="${t.id}"><label>Reply in the customer conversation<textarea name="reply" required minlength="2" placeholder="Write a helpful reply…"></textarea></label><div class="ticket-footer"><label class="inline-label"><input type="checkbox" name="save">Save as knowledge</label><button class="primary">Post reply in chat</button></div></form>`}</article>`).join('') : '<div class="empty">No questions need review. Ask an unsupported question in customer chat to open a ticket.</div>';
  document.querySelectorAll('[data-ticket]').forEach(form => form.onsubmit = async event => {
    event.preventDefault(); const button = form.querySelector('button'); button.disabled = true;
    try { const result = await api(`/api/tickets/${form.dataset.ticket}/reply`, {method:'POST',body:JSON.stringify({text:form.elements.reply.value,save_knowledge:form.elements.save.checked})}); notice(result.knowledge_saved ? 'Reply posted in chat and saved to knowledge.' : 'Reply posted in the customer conversation.'); await refreshTickets(); await refreshConversations(); }
    catch(error) {notice(error.message,true);} finally {button.disabled=false;}
  });
}
async function refreshKnowledge() {
  const [documents,jobs,thresholds] = await Promise.all([api('/api/documents'),api('/api/jobs'),api('/api/thresholds')]);
  $('#documents').innerHTML=documents.map(d=>`<div class="document"><strong>${esc(d.title)}</strong><small>${d.pages || 0} pages / ${d.chunks} indexed records</small></div>`).join('');
  $('#jobs').innerHTML=jobs.map(j=>`<div class="job">${esc(j.title)}: ${esc(j.status)} (${j.completed}/${j.total} pages)${j.error?`<p>${esc(j.error)}</p>`:''}</div>`).join('');
  $('#threshold-form').innerHTML = ['text_relevance','text_answerability','image_relevance','image_answerability'].map(k=>`<label>${esc(k.replaceAll('_',' '))}<div class="threshold-row"><input type="number" name="${k}" min="0" max="1" step=".01" value="${thresholds[k]}" required><span>0 to 1</span></div></label>`).join('')+'<button class="primary">Save thresholds</button>';
}
async function refreshDecisions() {
  const rows=await api('/api/decisions');
  $('#decision-list').innerHTML=rows.length?rows.map(d=>`<details class="log"><summary>${esc(d.outcome)} / attempt ${d.attempt} / ${esc(d.created)}</summary><pre>${esc(JSON.stringify(d.detail,null,2))}</pre></details>`).join(''):'<div class="empty">No decisions yet. Ask a question to create an evidence trace.</div>';
}
async function refreshConversations() {
  for (const item of conversations.filter(c=>!c.resolved)) {
    const row=await api(`/api/conversations/${item.id}`,{},'customer');
    if(row.status==='resolved') {item.resolved=true; const node=document.createElement('div');node.className='message answer';node.innerHTML='<span class="author">Support representative</span>'+esc(row.answer);$('#messages').append(node);}
  }
}
async function refresh() {await refreshTickets(); if(view==='knowledge') await refreshKnowledge(); if(view==='decisions') await refreshDecisions(); await refreshConversations();}
async function streamEvents() {
  eventController?.abort(); eventController=new AbortController();
  try {
    const response=await fetch('/api/events',{headers:{Authorization:`Bearer ${token('rep')}`},signal:eventController.signal});
    if(!response.ok) throw Error('Events unavailable');
    const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='';
    while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let split;while((split=buffer.indexOf('\n\n'))>=0){const event=buffer.slice(0,split);buffer=buffer.slice(split+2);if(event.includes('data:'))await refresh();}}
  } catch(error){if(error.name!=='AbortError')$('#connection').textContent='Live updates paused; switch views to refresh';}
}
async function renderEvidence(result) {
  const requestedCompany = company;
  $('#evidence').innerHTML=`<span class="badge ${result.status==='answered'?'pass':'review'}">${esc(result.status==='answered'?'Evidence passed':'Human review')}</span>${result.attempts?`<p>Answered after search ${result.attempts}.</p>`:'<p>The evidence did not support an answer.</p>'}`;
  for(const c of result.citations){
    const source=document.createElement('div');source.className='source';source.innerHTML=`<strong>${esc(c.title)}</strong><p>Page ${c.page} / ${esc(c.kind)}</p>`;
    if(c.image_id){const response=await fetch(`/api/pages/${c.image_id}`,{headers:{Authorization:`Bearer ${token('customer')}`}});if(response.ok){const blob=await response.blob();if(requestedCompany!==company)return;const img=document.createElement('img');img.alt=`${c.title}, page ${c.page}`;img.src=URL.createObjectURL(blob);img.onload=()=>URL.revokeObjectURL(img.src);source.append(img);}}
    if(requestedCompany!==company)return;
    $('#evidence').append(source);
  }
  const detail=document.createElement('details');detail.innerHTML=`<summary>Candidate scores (${result.candidates.length})</summary>${candidateList(result.candidates)}`;$('#evidence').append(detail);
}
$('#chat-form').onsubmit=async event=>{
  event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;notice('Checking company documents…');
  try {
    const question=$('#question').value;let screenshot=null;const file=$('#screenshot').files[0];
    if(file){if(file.size>5_000_000)throw Error('Choose an image smaller than 5 MB'); screenshot=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});}
    const result=await api('/api/chat',{method:'POST',body:JSON.stringify({question,screenshot,screenshot_caption:$('#caption').value})},'customer');
    for(const [author,text,css] of [['You',question,'customer'],['Support assistant',result.answer,'answer']]){const n=document.createElement('div');n.className='message '+css;n.innerHTML=`<span class="author">${author}</span>${esc(text)}`;$('#messages').append(n);}
    if(result.status==='escalated')conversations.push({id:result.conversation_id,resolved:false});
    await renderEvidence(result);$('#question').value='';notice(result.status==='answered'?'Answer grounded in the pages shown.':'Question added to the review inbox. A representative can reply here.');await refreshTickets();
  }catch(error){notice(error.message,true);}finally{button.disabled=false;}
};
document.querySelectorAll('[data-question]').forEach(b=>b.onclick=()=>{$('#question').value=b.dataset.question;$('#question').focus();});
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=async()=>{view=b.dataset.view;document.querySelectorAll('.view').forEach(s=>s.hidden=s.id!==view);document.querySelectorAll('[data-view]').forEach(n=>n.classList.toggle('active',n===b));$('#view-title').textContent=b.textContent.replace(/\s+\d+$/,'');try{await refresh();}catch(error){notice(error.message,true);}});
$('#company').onchange=async()=>{company=$('#company').value;conversations=[];for(const id of ['messages','ticket-list','documents','jobs','threshold-form','decision-list'])$('#'+id).innerHTML='';$('#evidence').innerHTML='<div class="empty">Ask a question to inspect its sources.</div>';$('#workspace-name').textContent=$('#company').selectedOptions[0].textContent;notice('Workspace changed. Evidence and tickets belong to this company.');try{await refresh();streamEvents();}catch(error){notice(error.message,true);}};
$('#upload-form').onsubmit=async event=>{event.preventDefault();const body=new FormData();body.append('file',$('#document').files[0]);try{await api('/api/documents',{method:'POST',body});notice('Document queued. Indexing progress appears below.');await refreshKnowledge();}catch(error){notice(error.message,true);}};
$('#threshold-form').onsubmit=async event=>{event.preventDefault();const values={};new FormData(event.target).forEach((v,k)=>values[k]=Number(v));try{await api('/api/thresholds',{method:'PUT',body:JSON.stringify(values)});notice('Evidence thresholds saved for this company.');}catch(error){notice(error.message,true);}};
(async()=>{try{const health=await fetch('/api/health').then(r=>r.json());mode=health.mode;$('#mode').textContent=mode==='demo'?'Offline demonstration':'Remote models';$('#caption-label').hidden=mode!=='demo';if(!health.demo_tokens){notice('This demo UI uses synthetic account tokens. Configure authenticated clients for your custom tokens.',true);return;}await refresh();streamEvents();}catch(error){notice(error.message,true);}})();
