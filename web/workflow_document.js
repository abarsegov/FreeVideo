const clone = value => structuredClone(value);
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export function workflowDocumentPath(id) {
    if (!idPattern.test(id)) throw new Error('Invalid FreeVideo workflow ID');
    return `workflows/FreeVideo/${id}.json`;
}
export function isWorkflowDocument(value) {
    return Boolean(value?.extra?.freevideo?.version === 1 && value.extra.freevideo.manifest && Array.isArray(value.nodes));
}
export function numericWorkflowTemplate(manifest, source) {
    const ids = Object.fromEntries(Object.keys(source).map((id, index) => [id, String(index + 1)]));
    const graph = {};
    for (const [id, original] of Object.entries(source)) {
        const node = clone(original);
        for (const [name, value] of Object.entries(node.inputs)) {
            if (Array.isArray(value) && value.length === 2 && typeof value[1] === 'number' && ids[value[0]]) node.inputs[name] = [ids[value[0]], value[1]];
        }
        graph[ids[id]] = node;
    }
    const profile = clone(manifest);
    const remap = target => { if (!ids[target.node]) throw new Error(`Missing workflow node ${target.node}`); target.node = ids[target.node]; };
    for (const target of bindings(profile)) remap(target);
    remap(profile.outputs.video);
    for (const field of profile.profile?.advanced || []) field.bindings.forEach(remap);
    for (const lora of profile.profile?.loras || []) Object.values(lora.bindings).flat().forEach(remap);
    return {manifest: profile, api: graph};
}
function bindings(manifest) {
    return Object.values(manifest.inputs || {}).flatMap(value => value.bindings || [value]);
}
export async function compileWorkflowDocument(document, {createGraph, graphToPrompt}) {
    const result = clone(document), config = result.extra.freevideo;
    const graph = createGraph();
    try {
        graph.configure(clone(result));
        const exported = await graphToPrompt(graph);
        if (!exported?.output || !Object.keys(exported.output).length) throw new Error('The workflow has no executable nodes');
        const api = clone(exported.output);
        for (const target of bindings(config.manifest)) {
            const node = api[target.node];
            if (!node) throw new Error(`FreeVideo input target node ${target.node} was removed or disabled`);
            if (!(target.input in node.inputs)) {
                const visual = graph.getNodeById(target.node) || graph.getNodeById(Number(target.node));
                if (!visual?.inputs?.some(input => input.name === target.input))
                    throw new Error(`FreeVideo input ${target.node}.${target.input} no longer exists`);
                // An unconnected template socket is filled by FreeVideo at run time.
                node.inputs[target.input] = null;
            }
        }
        const values = targets => targets.map(target => {
            const node = api[target.node];
            if (!node || !(target.input in node.inputs)) throw new Error(`FreeVideo setting ${target.node}.${target.input} no longer exists`);
            return node.inputs[target.input];
        });
        for (const field of config.manifest.profile?.advanced || []) {
            const current = values(field.bindings);
            if (current.some(value => JSON.stringify(value) !== JSON.stringify(current[0])))
                throw new Error(`All graph inputs for ${field.label} must have the same default`);
            if (current[0] == null || typeof current[0] === 'object') throw new Error(`${field.label} must remain a literal setting`);
            if (['integer', 'number'].includes(field.type) &&
                (typeof current[0] !== 'number' || !Number.isFinite(current[0]) ||
                    (field.type === 'integer' && !Number.isInteger(current[0])) || current[0] < field.min || current[0] > field.max))
                throw new Error(`${field.label} must stay between ${field.min} and ${field.max}`);
            if (field.type === 'choice' && !field.options.includes(current[0])) throw new Error(`Unsupported ${field.label} value`);
            if (field.type === 'boolean' && typeof current[0] !== 'boolean') throw new Error(`${field.label} must be boolean`);
            if (field.type === 'string' && typeof current[0] !== 'string') throw new Error(`${field.label} must be text`);
            field.default = current[0];
        }
        for (const lora of config.manifest.profile?.loras || []) {
            for (const targets of Object.values(lora.bindings)) values(targets);
        }
        const output = config.manifest.outputs?.video;
        if (!output || !api[output.node]) throw new Error('The FreeVideo VIDEO output node was removed or disabled');
        for (const [id, node] of Object.entries(api)) {
            const previous = config.api?.[id];
            if (node.class_type === previous?.class_type && previous._meta?.freevideo_progress)
                (node._meta ||= {}).freevideo_progress = clone(previous._meta.freevideo_progress);
        }
        config.api = api;
        return result;
    } finally { graph.stop?.(); }
}
export function installWorkflowDocumentSaving(api, compiler) {
    if (api._freevideoDocumentSaving) return;
    const original = api.storeUserData;
    api.storeUserData = async function(path, data, ...rest) {
        const text = typeof data === 'string';
        let document;
        try { document = text ? JSON.parse(data) : data; } catch { return original.call(this, path, data, ...rest); }
        if (!isWorkflowDocument(document) || !String(path).startsWith('workflows/FreeVideo/'))
            return original.call(this, path, data, ...rest);
        const id = document.extra.freevideo.manifest.id;
        if (path !== workflowDocumentPath(id)) throw new Error(`Cannot register ${path}. Save this FreeVideo profile as ${workflowDocumentPath(id)}`);
        const compiled = await compiler(document);
        return original.call(this, path, text ? JSON.stringify(compiled) : compiled, ...rest);
    };
    api._freevideoDocumentSaving = true;
}
