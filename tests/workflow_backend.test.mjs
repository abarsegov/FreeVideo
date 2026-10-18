import {test} from 'node:test';
import assert from 'node:assert/strict';
import {workflowChoice, workflowRequestError} from '../web/workflow_backend.js';

const workflow = {id: 'selflift', title: 'SelfLift', inputs: ['prompt', 'first_frame']};
test('registered backend selection never silently substitutes a missing workflow', () => {
    assert.equal(workflowChoice([workflow], 'selflift'), workflow);
    assert.equal(workflowChoice([workflow], 'removed'), null);
});
test('VDN retains its preview and continuation behavior', () => {
    assert.equal(workflowRequestError({backend:'vdn', workflow:null, parts:5, continuing:true, preview:true}), null);
});
test('workflow mode supports full single clips and explains unsupported operations', () => {
    assert.equal(workflowRequestError({backend:'workflow', workflow}), null);
    for (const options of [{workflow:null},{parts:2},{continuing:true},{preview:true}]) {
        assert.equal(typeof workflowRequestError({backend:'workflow',workflow,...options}), 'string');
    }
});
