import test from 'node:test';
import assert from 'node:assert/strict';
import {compileWorkflowDocument, installWorkflowDocumentSaving, workflowDocumentPath, numericWorkflowTemplate} from '../web/workflow_document.js';
function document() {
    const manifest = {id:'test',inputs:{first_frame:{node:'1',input:'image'}},outputs:{video:{node:'2',output:0}},
        profile:{advanced:[{id:'steps',label:'Steps',type:'integer',default:4,min:1,max:100,bindings:[{node:'1',input:'steps'}]}]}};
    const api = {'1':{class_type:'Sampler',inputs:{image:null,steps:4},_meta:{freevideo_progress:{stage:'sampling',label:'Sampler'}}}};
    return {version:.4,nodes:[{id:1,type:'Sampler'},{id:2,type:'SaveVideo'}],links:[],extra:{freevideo:{version:1,manifest,api}}};
}
const compiler = api => doc => compileWorkflowDocument(doc,{createGraph:()=>({configure(){},getNodeById:()=>({inputs:[{name:'image'}]})}),
    graphToPrompt:async()=>({output:api})});

test('native save exports current API and updates Advanced defaults in one visual file',async()=>{
    const original=document(); const api={'1':{class_type:'Sampler',inputs:{steps:8}},'2':{class_type:'SaveVideo',inputs:{}}};
    const output=await compiler(api)(original);
    assert.equal(output.extra.freevideo.api['1'].inputs.steps,8);
    assert.equal(output.extra.freevideo.api['1'].inputs.image,null);
    assert.equal(output.extra.freevideo.manifest.profile.advanced[0].default,8);
    assert.equal(output.extra.freevideo.api['1']._meta.freevideo_progress.stage,'sampling');
    assert.equal(original.extra.freevideo.manifest.profile.advanced[0].default,4);
    assert.deepEqual(output.nodes,original.nodes);
});
test('storeUserData keeps native arguments and unrelated workflows unchanged',async()=>{
    const calls=[]; const api={storeUserData:async(...args)=>{calls.push(args);return 'saved';}};
    installWorkflowDocumentSaving(api,async doc=>({...doc,exported:true}));
    const options={overwrite:true,stringify:false};
    assert.equal(await api.storeUserData('workflows/FreeVideo/test.json',JSON.stringify(document()),options),'saved');
    assert.equal(JSON.parse(calls[0][1]).exported,true); assert.equal(calls[0][2],options);
    const unrelated={nodes:[]}; await api.storeUserData('workflows/other.json',unrelated,options);
    assert.equal(calls[1][1],unrelated);
    await assert.rejects(()=>api.storeUserData('workflows/FreeVideo/wrong.json',document()),/Save this FreeVideo/);
    assert.equal(calls.length,2);
});
test('broken binding stops saving instead of publishing a stale executable graph',async()=>{
    await assert.rejects(()=>compiler({'2':{class_type:'SaveVideo',inputs:{}}})(document()),/removed or disabled/);
    assert.throws(()=>workflowDocumentPath('../escape'),/Invalid/);
});
test('migration keeps numeric editor IDs, API links and profile bindings aligned',()=>{
    const manifest={id:'h3',inputs:{prompt:{node:'encode',input:'text'}},outputs:{video:{node:'video',output:0}},
        profile:{advanced:[{id:'x',bindings:[{node:'encode',input:'text'}]}],loras:[]}};
    const source={encode:{class_type:'Encode',inputs:{text:'hello'}},video:{class_type:'Video',inputs:{frames:['encode',0]}}};
    const out=numericWorkflowTemplate(manifest,source);
    assert.equal(out.manifest.inputs.prompt.node,'1'); assert.equal(out.manifest.outputs.video.node,'2');
    assert.deepEqual(out.api['2'].inputs.frames,['1',0]);
    assert.equal(out.manifest.profile.advanced[0].bindings[0].node,'1');
    assert.equal(manifest.inputs.prompt.node,'encode');
});
