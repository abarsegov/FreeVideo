import test from 'node:test';
import assert from 'node:assert/strict';
import {agentSend, agentUpload} from '../web/agent_session.js';

test('binary upload refreshes an expired page session once and retains the Blob', async () => {
    const blob = new Blob(['test video'], {type:'video/mp4'});
    const uploads = []; let sessions = 0;
    const api = {async fetchApi(path, options) {
        if (path.endsWith('/session')) return Response.json({csrf:`session-${++sessions}`});
        uploads.push(options);
        return uploads.length === 1 ? Response.json({error:{code:'page_session_required'}}, {status:403}) : Response.json({status:'ready'});
    }};
    assert.equal((await agentUpload(api, '/whitebox/test/video', blob)).status, 200);
    assert.equal(sessions, 2);
    assert.deepEqual(uploads.map(row=>row.headers['X-FreeVideo-CSRF']), ['session-1','session-2']);
    for (const row of uploads) { assert.equal(row.body,blob); assert.equal(row.headers['Content-Type'],'video/mp4'); }
});

test('upload does not retry permission refusals or loop on repeated session expiry', async () => {
    for (const code of ['origin_forbidden','local_only','page_session_required']) {
        let calls = 0;
        const api = {async fetchApi(path) {
            if (path.endsWith('/session')) return Response.json({csrf:'test'});
            calls++; return Response.json({error:{code}}, {status:403});
        }};
        assert.equal((await agentUpload(api,'/whitebox/test/video',new Blob(['x']))).status,403);
        assert.equal(calls,code==='page_session_required'?2:1);
    }
});

test('JSON controls keep their existing serialization and content type', async () => {
    let sent;
    const api = {async fetchApi(path, options) {
        if (path.endsWith('/session')) return Response.json({csrf:'test'});
        sent=options; return Response.json({ok:true});
    }};
    await agentSend(api,'POST','/control',{enabled:true});
    assert.equal(sent.body,'{"enabled":true}');
    assert.equal(sent.headers['Content-Type'],'application/json');
});
