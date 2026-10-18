import test from 'node:test';
import assert from 'node:assert/strict';
import {profileState} from '../web/workflow_profile.js';
import {cleanOptionalPrompt, cleanOptionalWidgets} from '../web/optional_inputs.js';

test('workflow profiles restore independent settings', () => {
    const saved = JSON.stringify({draft: {advanced: {steps: 4}, loras: {motion: {strength: .7, enabled: true}}},
        final: {advanced: {steps: 12}, loras: {motion: {strength: .4, enabled: false}}}});
    assert.equal(profileState(saved, 'draft').current.advanced.steps, 4);
    assert.equal(profileState(saved, 'final').current.loras.motion.enabled, false);
    assert.deepEqual(profileState(saved, 'new').current, {advanced: {}, loras: {}});
    assert.equal(Object.keys(profileState(saved, 'new').all).length, 2);
});

test('malformed profile storage is not silently discarded', () => {
    for (const value of ['[]', 'null', 'false', '{']) assert.throws(() => profileState(value, 'draft'));
});

test('specialized workflow frame counts survive native prompt cleanup', () => {
    const node = {widgets: [{name: 'backend', value: 'workflow'}, {name: 'frames', value: 48}]};
    cleanOptionalWidgets(node); assert.equal(node.widgets[1].value, 48);
    const payload = {output: {'1': {class_type: 'FreeVideoGenerate', inputs: {backend: 'workflow', frames: 48}}}};
    assert.equal(cleanOptionalPrompt(payload).output['1'].inputs.frames, 48);
    payload.output['1'].inputs.backend = 'vdn';
    assert.equal(cleanOptionalPrompt(payload).output['1'].inputs.frames, 0);
});
