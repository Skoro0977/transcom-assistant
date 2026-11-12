import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Input, Select, Space, Typography } from 'antd';
import { api, post } from './api';
import { FactView, KINDS, type Accepted, type Fact } from './Review';

type Doc={id:string;title:string;category:string;version:number};
type Draft={id:string;version:number;data:Fact;status:string};
async function allDocuments(projectId:string){
  const rows:Doc[]=[];let total=1;
  while(rows.length<total){const page=await api<{items:Doc[];total:number}>(`/api/projects/${projectId}/documents?limit=200&offset=${rows.length}`);rows.push(...page.items);total=page.total;if(!page.items.length)break;}
  return rows;
}
export function ChatChanges({projectId}:{projectId:string}){
  const {message}=App.useApp();const [records,setRecords]=useState<Accepted[]>([]),[documents,setDocuments]=useState<Doc[]>([]);
  const [kind,setKind]=useState('requirement'),[target,setTarget]=useState<string|undefined>(),[text,setText]=useState(''),[patch,setPatch]=useState('{}'),[ai,setAi]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [draft,setDraft]=useState<Draft|null>(null),[before,setBefore]=useState<Record<string,unknown>>({});
  const choices=kind==='document'?documents.map(d=>({id:d.id,version:d.version,title:d.title})):records.filter(r=>r.kind===kind).map(r=>({id:r.id,version:r.version,title:r.data.title}));
  async function load(){const [r,d]=await Promise.all([api<Accepted[]>(`/api/projects/${projectId}/records`),allDocuments(projectId)]);setRecords(r);setDocuments(d);}
  useEffect(()=>{void load().catch(e=>setError(e.message));},[projectId]);
  return <Card title="Предложить изменение через чат"><Space orientation="vertical" style={{width:'100%'}}>
    <Alert type="info" title="Выберите карточку и опишите изменение" description="GigaChat получает только запрос и выбранную карточку. Новые значения появятся в предпросмотре. Подтверждение выполняется отдельной кнопкой. Название и категория документа меняются здесь, содержимое заменяется загрузкой новой редакции в разделе документов."/>
    {error&&<Alert type="warning" title={error}/>}
    <Space wrap><Select aria-label="Вид изменения" value={kind} style={{width:220}} onChange={v=>{setKind(v);setTarget(undefined);setDraft(null);setPatch('{}');}} options={Object.entries(KINDS).map(([value,label])=>({value,label}))}/><Select aria-label="Карточка для изменения" allowClear showSearch optionFilterProp="label" placeholder={kind==='document'?'Выберите документ':'Новая запись или выбранная карточка'} value={target} style={{width:480,maxWidth:'100%'}} onChange={v=>{setTarget(v);setDraft(null);setPatch('{}');}} options={choices.map(r=>({value:r.id,label:`${r.title} / ред. ${r.version}`}))}/></Space>
    <Input.TextArea aria-label="Запрос изменения" value={text} onChange={e=>setText(e.target.value)} rows={3} maxLength={12000}/>
    <Checkbox checked={ai} onChange={e=>setAi(e.target.checked)}>Подготовить новые значения через GigaChat</Checkbox>
    {!ai&&<><Typography.Text>Точные изменяемые поля в JSON. Даты YYYY-MM-DD. Остальные поля сохраняются.</Typography.Text><Input.TextArea aria-label="Поля изменения JSON" value={patch} onChange={e=>setPatch(e.target.value)} rows={5}/></>}
    <Button type="primary" loading={busy} disabled={text.trim().length<2||(kind==='document'&&!target)} onClick={async()=>{setBusy(true);setDraft(null);try{const selected=choices.find(r=>r.id===target);const response=await post<{clarification:string;proposal:Draft|null;before:Record<string,unknown>}>(`/api/projects/${projectId}/chat/changes`,{kind,target_id:target||null,expected_version:selected?.version||0,text,use_ai:ai,patch:ai?{}:JSON.parse(patch)});setDraft(response.proposal);setBefore(response.before||{});setError(response.clarification);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>Подготовить предложение</Button>
    {draft&&<Card title="Предпросмотр изменения"><p>Было:</p><pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(before,null,2)}</pre><p>Предлагается:</p><FactView data={draft.data}/><pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(draft.data,null,2)}</pre>{draft.status==='pending'?<Space><Button type="primary" loading={busy} onClick={async()=>{setBusy(true);try{await post(`/api/proposals/${draft.id}/decision`,{decision:'accepted',expected_version:draft.version,reason:text.slice(0,4000)});setDraft({...draft,status:'accepted'});await load();message.success('Изменение подтверждено и сохранено в истории');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}}>Подтвердить показанное изменение</Button><Button onClick={async()=>{try{await post(`/api/proposals/${draft.id}/decision`,{decision:'rejected',expected_version:draft.version,reason:'Отклонено в чате'});setDraft(null);}catch(e){message.error((e as Error).message);}}}>Отклонить</Button></Space>:<Alert type="success" title="Решение уже сохранено"/>}</Card>}
  </Space></Card>;
}
