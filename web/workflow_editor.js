import {app} from '../../scripts/app.js';
import {api} from '../../scripts/api.js';
import {compileWorkflowDocument, installWorkflowDocumentSaving, workflowDocumentPath, numericWorkflowTemplate} from './workflow_document.js';

const compile = document => compileWorkflowDocument(document, {
    createGraph: () => new app.rootGraph.constructor(), graphToPrompt: graph => app.graphToPrompt(graph),
});
async function chooseWorkflow() {
    const response = await api.fetchApi('/freevideo/workflows');
    if (!response.ok) throw new Error('Cannot read the FreeVideo workflow catalog');
    const catalog = await response.json();
    return new Promise(resolve => {
        const dialog = document.createElement('dialog'); dialog.setAttribute('aria-label', 'Open FreeVideo workflow');
        dialog.style.cssText = 'background:#141b24;color:#edf2f9;border:1px solid #344052;border-radius:12px;padding:24px;min-width:340px;font:14px system-ui';
        const form = document.createElement('form'); form.method = 'dialog';
        const label = document.createElement('label'); label.textContent = 'FreeVideo workflow';
        const select = document.createElement('select'); select.setAttribute('aria-label', 'FreeVideo workflow');
        select.style.cssText = 'display:block;width:100%;margin:12px 0 20px;padding:10px;background:#202a38;color:inherit;border:1px solid #45536a;border-radius:6px';
        for (const row of catalog.workflows || []) { const option = document.createElement('option'); option.value = row.id; option.textContent = row.title; select.append(option); }
        if ([...select.options].some(option => option.value === 'h3-reference-4step')) select.value = 'h3-reference-4step';
        const cancel = document.createElement('button'); cancel.textContent = 'Cancel'; cancel.value = 'cancel';
        const open = document.createElement('button'); open.textContent = 'Open'; open.value = 'open'; open.disabled = !select.options.length;
        open.style.marginLeft = '12px';
        label.append(select); form.append(label, cancel, open); dialog.append(form); document.body.append(dialog);
        dialog.addEventListener('close', () => { const id = dialog.returnValue === 'open' ? select.value : null; dialog.remove(); resolve(id); }, {once:true});
        dialog.showModal();
    });
}
async function createEditableWorkflow() {
    if (window.FreeVideoStandalone?.installed) throw new Error('Open the full ComfyUI node editor to edit workflows');
    const id = await chooseWorkflow();
    if (!id) return;
    const path = workflowDocumentPath(id);
    const existing = await api.getUserData(path);
    if (existing.ok) {
        await app.loadGraphData(await existing.json(), true, true, `FreeVideo/${id}.json`);
        return;
    }
    if (existing.status !== 404) throw new Error(`Cannot read workflow (${existing.status})`);
    const read = async name => {
        const response = await api.getUserData(`freevideo/workflows/${id}/${name}`);
        if (!response.ok) throw new Error(`Cannot read ${name} (${response.status})`);
        return response.json();
    };
    const originalManifest = await read('manifest.json'), originalSource = await read('workflow_api.json');
    // A visible, model-free placeholder makes the external reference input clear.
    // FreeVideo replaces it with the required reference batch at generation time.
    if (id === 'h3-reference-4step' && originalSource.references?.inputs.images == null) {
        originalSource.reference_placeholder = {class_type: 'EmptyImage', inputs: {width: 256, height: 256, batch_size: 1, color: 3368652},
            _meta: {title: 'Reference placeholder · replace with your image for node-editor previews'}};
        originalSource.references.inputs.images = ['reference_placeholder', 0];
    }
    const {manifest, api: source} = numericWorkflowTemplate(originalManifest, originalSource);
    // Import into a separate temporary tab: reserving the canonical path here
    // would make the native workflow store treat the newly written file as unsaved.
    await app.loadApiJson(source, `${id}-template.json`);
    const graph = app.rootGraph;
    graph.extra ||= {};
    graph.extra.freevideo = {version: 1, manifest, api: source};
    // Keep the live graph marked too, so subsequent native Ctrl+S saves retain it.
    const document = await compile(graph.serialize());
    graph.extra.freevideo = structuredClone(document.extra.freevideo);
    graph.change(); graph.setDirtyCanvas(true, true);
    await api.storeUserData(path, document);
    app.extensionManager?.toast?.add({severity: 'success', summary: 'FreeVideo',
        detail: `Created FreeVideo/${id}.json. Open it from Workflows; Ctrl+S also updates FreeVideo.`, life: 12000});
}
app.registerExtension({name: 'FreeVideo.WorkflowDocument',
    commands: [{id: 'FreeVideo.EditWorkflow', label: 'FreeVideo: open workflow editor', function: createEditableWorkflow}],
    menuCommands: [{path: ['Extensions'], commands: ['FreeVideo.EditWorkflow']}],
    setup() { if (!window.FreeVideoStandalone?.installed) installWorkflowDocumentSaving(api, compile); },
});
