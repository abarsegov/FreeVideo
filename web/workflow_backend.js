// Registered workflows execute in the normal Comfy queue. Graphs stay on the server.
export function workflowChoice(catalog, id) {
    return catalog.find(row => row.id === id) || null;
}

export function workflowRequestError({backend, workflow, parts = 1, continuing = false, preview = false}) {
    if (backend !== 'workflow') return null;
    if (!workflow) return 'Select an installed ComfyUI workflow.';
    if (parts > 1 || continuing) return 'This workflow adapter supports a single clip. Automatic parts and continuation require VDN.';
    if (preview) return 'Preview-first is a VDN feature. Select a full workflow run.';
    return null;
}

export function createWorkflowBackendPicker({api, read, write, t, onChange = () => {}}) {
    const element = document.createElement('section'); element.className = 'fv-section fv-workflow-picker';
    const label = document.createElement('div'); label.className = 'fv-label';
    label.textContent = t('Generation engine', '生成引擎');
    const engine = document.createElement('select'); engine.setAttribute('aria-label', label.textContent);
    for (const [id, name] of [['vdn', 'FreeVideo / VDN'], ['workflow', 'ComfyUI workflow']]) {
        const option = document.createElement('option'); option.value = id; option.textContent = name; engine.append(option);
    }
    engine.value = read('backend') || 'vdn';
    const workflows = document.createElement('select'); workflows.setAttribute('aria-label', t('Workflow', '工作流'));
    const engineField = document.createElement('label'); engineField.className = 'fv-backend-field';
    const engineTitle = document.createElement('span'); engineTitle.textContent = t('Engine', '引擎');
    engineField.append(engineTitle, engine);
    const workflowField = document.createElement('label'); workflowField.className = 'fv-backend-field';
    const workflowTitle = document.createElement('span'); workflowTitle.textContent = t('Workflow', '工作流');
    workflowField.append(workflowTitle, workflows);
    const note = document.createElement('p'); note.className = 'fv-muted'; note.setAttribute('role', 'status');
    element.append(label, engineField, workflowField, note);
    let catalog = [], loading = true, failure = '', disposed = false;
    function render() {
        const custom = engine.value === 'workflow'; workflowField.hidden = !custom;
        workflows.disabled = loading || !catalog.length;
        note.hidden = !custom;
        const selected = workflowChoice(catalog, workflows.value);
        workflows.title = selected?.title || '';
        note.textContent = failure || (loading ? t('Loading workflows…', '正在加载工作流…') : !catalog.length
            ? t('No workflows installed. An administrator must register a workflow on this server.', '没有已安装的工作流，请管理员在服务器上注册。')
            : t('Model and sampling settings are defined by the workflow.', '模型和采样设置由工作流决定。'));
        onChange(custom, selected);
    }
    engine.onchange = () => { write('backend', engine.value); render(); };
    workflows.onchange = () => { failure = ''; write('workflow_id', workflows.value); render(); };
    const ready = (async () => {
        try {
            const response = await api.fetchApi('/freevideo/workflows');
            if (!response.ok) throw new Error('Workflow catalog unavailable (' + response.status + ')');
            const value = await response.json();
            if (!Array.isArray(value.workflows)) throw new Error('Invalid workflow catalog');
            if (disposed) return;
            catalog = value.workflows;
            workflows.replaceChildren();
            for (const row of catalog) {
                const option = document.createElement('option'); option.value = row.id; option.textContent = row.title; workflows.append(option);
            }
            const saved = read('workflow_id');
            if (saved && workflowChoice(catalog, saved)) workflows.value = saved;
            else if (!saved && catalog.length) write('workflow_id', workflows.value);
            else if (saved) { workflows.selectedIndex = -1; failure = t('The saved workflow is no longer installed. Choose another workflow.', '原工作流已不存在，请重新选择。'); }
        } catch (error) { failure = error.message; }
        finally { loading = false; if (!disposed) render(); }
    })();
    render();
    return {element, ready, custom: () => engine.value === 'workflow',
        selected: () => workflowChoice(catalog, read('workflow_id')),
        error(options) { return loading ? 'Wait for the workflow catalog.' : failure || workflowRequestError({backend: engine.value, workflow: this.selected(), ...options}); },
        dispose() { disposed = true; }};
}
