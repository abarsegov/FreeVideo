import test from 'node:test';
import assert from 'node:assert/strict';
import {proresAction,proresIdentity} from '../web/prores_export.js';
const id='2026-10-19/'+'a'.repeat(32), record={id,video:`FreeVideo/${id}/video.mp4`};
const t=en=>en;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const create=()=>({isConnected:true,setAttribute(){}});

test('only wrapper creation results have an export action',()=>{
    assert.equal(proresIdentity(record),id);
    assert.equal(proresIdentity({video:record.video}),id);
    assert.equal(proresIdentity({id:'../../secret'}),null);
    assert.equal(proresAction(record,t,{}, {enabled:false}),null);
});

test('missing RGB keeps button hidden and never submits an export',async()=>{
    const calls=[];
    const api={fetchApi:async(path,options)=>{calls.push(options.method);return Response.json({available:false,state:'unavailable'});}};
    const button=proresAction(record,t,api,{enabled:true,create});
    await tick(); assert.equal(button.hidden,true); assert.deepEqual(calls,['GET']);
});

test('one click starts fixed 422 export, tracks frames and downloads ready MOV',async()=>{
    const calls=[],downloads=[];
    const responses=[{available:true,state:'idle'}, {available:true,state:'queued'},
        {available:true,state:'encoding',progress:{done:2,total:4,phase:'video'}}, {available:true,state:'ready'}];
    const api={fetchApi:async(path,options)=>{calls.push({path,...options});return Response.json(responses.shift());}};
    const button=proresAction(record,t,api,{enabled:true,create,wait:async()=>{},download:value=>downloads.push(value)});
    await tick(); assert.equal(button.hidden,false);
    await button.onclick();
    assert.deepEqual(downloads,[id]); assert.equal(button.disabled,false);
    assert.equal(button.textContent,'Download ProRes 422');
    const post=calls.find(row=>row.method==='POST');
    assert.deepEqual(JSON.parse(post.body),{id});
    assert.equal(post.headers['X-FreeVideo-Standalone'],'1');
});

test('encoding error permits retry; closing result stops status polling',async()=>{
    const responses=[{available:true,state:'idle'},{available:true,state:'queued'}];
    let calls=0;
    const api={fetchApi:async()=>{calls++;return Response.json(responses.shift());}};
    let button;
    button=proresAction(record,t,api,{enabled:true,create,wait:async()=>{button.isConnected=false;}});
    await tick(); await button.onclick(); assert.equal(calls,2);
    const failed=proresAction(record,t,{fetchApi:async(_,options)=>options.method==='GET'
        ? Response.json({available:true,state:'idle'}) : Response.json({error:'Not enough disk space'},{status:400})}, {enabled:true,create});
    await tick(); await failed.onclick();
    assert.equal(failed.disabled,false); assert.match(failed.textContent,/retry/); assert.match(failed.title,/disk space/);
});
