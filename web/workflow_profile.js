// A profile is authored with our workflow; the browser only edits declared values.
export function profileState(serialized, id) {
    const all = JSON.parse(serialized || '{}');
    if (!all || Array.isArray(all) || typeof all !== 'object') throw new Error('Invalid workflow settings');
    return {all, current: all[id] || {advanced: {}, loras: {}}};
}

export function createWorkflowProfileControls({read, write, t}) {
    const advanced = document.createElement('div'); advanced.className = 'fv-workflow-advanced fv-fields';
    const loras = document.createElement('div'); loras.className = 'fv-workflow-loras';
    let selected = null;
    function save(group, id, value) {
        const {all, current} = profileState(read(), selected.id);
        all[selected.id] = {...current, [group]: {...current[group], [id]: value}};
        write(JSON.stringify(all));
    }
    function field(title, input) {
        const label = document.createElement('label'); label.className = 'fv-backend-field';
        const text = document.createElement('span'); text.textContent = title;
        input.setAttribute('aria-label', title); label.append(text, input); return label;
    }
    function render(profile) {
        selected = profile; advanced.replaceChildren(); loras.replaceChildren();
        if (!profile) return;
        const {current} = profileState(read(), profile.id);
        for (const item of profile.profile?.advanced || []) {
            const input = document.createElement(item.type === 'choice' ? 'select' : 'input');
            if (item.type === 'choice') {
                for (const value of item.options) { const option = document.createElement('option'); option.value = value; option.textContent = value; input.append(option); }
            } else input.type = item.type === 'boolean' ? 'checkbox' : ['integer', 'number'].includes(item.type) ? 'number' : 'text';
            const value = current.advanced?.[item.id] ?? item.default;
            if (item.type === 'boolean') input.checked = value; else input.value = value;
            if (item.min != null) input.min = item.min;
            if (item.max != null) input.max = item.max;
            input.step = item.step ?? (item.type === 'integer' ? 1 : 'any');
            if (input.type !== 'checkbox') input.required = true;
            input.onchange = () => {
                if (!input.reportValidity()) return;
                save('advanced', item.id, item.type === 'boolean' ? input.checked
                    : ['integer', 'number'].includes(item.type) ? Number(input.value) : input.value);
            };
            advanced.append(field(item.label, input));
        }
        for (const item of profile.profile?.loras || []) {
            const row = document.createElement('div'); row.className = 'fv-workflow-lora';
            const setting = {...item, ...current.loras?.[item.id]};
            const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.checked = setting.enabled;
            const title = document.createElement('span'); title.textContent = item.label; title.title = item.name;
            const name = document.createElement('small'); name.textContent = item.name;
            const caption = document.createElement('label'); caption.append(enabled, title, name);
            enabled.setAttribute('aria-label', item.label);
            const strength = document.createElement('input'); strength.type = 'number'; strength.value = setting.strength;
            strength.min = item.min; strength.max = item.max; strength.step = item.step; strength.required = true;
            strength.disabled = !enabled.checked;
            strength.setAttribute('aria-label', item.label + ' · ' + t('Strength', '强度'));
            const update = () => { strength.disabled = !enabled.checked; if (strength.reportValidity()) save('loras', item.id, {enabled: enabled.checked, strength: Number(strength.value)}); };
            enabled.onchange = update; strength.onchange = update;
            row.append(caption, strength); loras.append(row);
        }
        if (!loras.children.length) { const note = document.createElement('p'); note.className = 'fv-muted'; note.textContent = t('This workflow has no configurable LoRAs.', '此工作流没有可配置的 LoRA。'); loras.append(note); }
    }
    return {advanced, loras, render, validate() {
        return [...advanced.querySelectorAll('input,select'), ...loras.querySelectorAll('input')].every(input => input.reportValidity());
    }};
}
