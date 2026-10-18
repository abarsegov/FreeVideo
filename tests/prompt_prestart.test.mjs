import test from 'node:test';
import assert from 'node:assert/strict';
import {PromptVersions, needsPromptPrestart, promptPrestart} from '../web/prompt_enhance.js';

test('repeated generation and focus do not reload enhancement for a fresh rewrite', async () => {
    const scene = {seconds: 5, media: []};
    const versions = new PromptVersions(null, 'A blue cloud');
    versions.value.enabled = true;
    assert.equal(needsPromptPrestart(versions, () => scene), true);
    assert.equal(versions.accept(versions.key(scene), scene, 'A softly moving blue cloud'), true);
    const events = new Map(), calls = [];
    const api = {addEventListener: (name, fn) => events.set(name, fn), removeEventListener() {},
        fetchApi: async path => { calls.push(path); return {ok: true, json: async () => ({token: 'test'})}; }};
    const warm = promptPrestart(api, '1', () => needsPromptPrestart(versions, () => scene));
    for (let run = 0; run < 3; run++) {
        events.get('executed')({detail: {node: '1'}});
        events.get('status')({detail: {exec_info: {queue_remaining: 0}}});
        await warm.request();
    }
    assert.deepEqual(calls, []);
    assert.equal(needsPromptPrestart(versions, () => ({...scene, seconds: 6})), true);
    versions.select('original');
    assert.equal(needsPromptPrestart(versions, () => scene), false);
    versions.value.useOriginal = false; versions.edit('A new scene');
    await warm.request();
    assert.deepEqual(calls, ['/freevideo/prompt-vlm', '/freevideo/prompt-vlm/prestart']);
    warm.dispose();
});

test('empty or unsupported scenes do not trigger speculative model loading', () => {
    const versions = new PromptVersions(null, ''); versions.value.enabled = true;
    assert.equal(needsPromptPrestart(versions, () => ({})), false);
    versions.edit('A scene');
    assert.equal(needsPromptPrestart(versions, () => {throw new Error('unsupported_media');}), false);
});
