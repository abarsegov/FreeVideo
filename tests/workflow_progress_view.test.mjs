import test from 'node:test';
import assert from 'node:assert/strict';
import {workflowProgressView} from '../web/workflow_progress_view.js';

test('workflow bar counts graph nodes and sampler steps without inventing time estimates', () => {
    const message = {phase: 'sampling', done: 2, total: 4,
        workflow: {nodes_done: 3, nodes_total: 10, node_label: 'H3 sampler'}, overall: {status: 'running'}};
    const view = workflowProgressView(message);
    assert.equal(view.fraction, .35);
    assert.equal(view.count, 'Workflow nodes 3 / 10');
    assert.match(view.text, /H3 sampler.*step 2 \/ 4/);
    assert.equal(workflowProgressView({...message, done: 4}).fraction, .4);
    assert.equal(workflowProgressView({...message, phase: 'save', workflow: {nodes_done: 10, nodes_total: 10}}).fraction, .99);
    assert.equal(workflowProgressView({...message, phase: 'complete'}).fraction, 1);
});

test('loading and failed workflows keep honest partial progress', () => {
    const value = {phase: 'load', workflow: {node_label: 'Diffusion model', nodes_done: 0, nodes_total: 8}};
    assert.equal(workflowProgressView(value).fraction, 0);
    assert.equal(workflowProgressView(value).text, 'Diffusion model');
    assert.equal(workflowProgressView({...value, phase: 'failed'}).fraction, 0);
    assert.equal(workflowProgressView({phase: 'sampling'}), null);
    assert.equal(workflowProgressView({phase: 'load', workflow: {}}).counted, false);
});
