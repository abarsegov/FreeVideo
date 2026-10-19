const ID = /^\d{4}-\d{2}-\d{2}\/[a-f0-9]{32}$/;
const PATH = '/freevideo/_owned/prores';
export function proresIdentity(record) {
    if (ID.test(record?.id || '')) return record.id;
    const match = String(record?.video || '').match(/(?:^|\/)FreeVideo\/(\d{4}-\d{2}-\d{2}\/[a-f0-9]{32})\/video\.mp4$/);
    return match?.[1] || null;
}

export function proresAction(record, t, api, {enabled = Boolean(globalThis.window?.FreeVideoStandalone?.installed),
    create = tag => document.createElement(tag), wait = ms => new Promise(resolve => setTimeout(resolve, ms)),
    download = id => {
        const link = document.createElement('a');
        link.href = api.apiURL(`${PATH}/download?id=${encodeURIComponent(id)}`);
        link.download = ''; document.body.append(link); link.click(); link.remove();
    }} = {}) {
    const id = proresIdentity(record);
    if (!enabled || !id) return null;
    const button = create('button');
    button.type = 'button'; button.className = 'fv-action fv-action-secondary fv-action-prores';
    button.hidden = true;
    let state, polling = false;
    const title = t('RGB → ProRes 422', 'RGB → ProRes 422');
    const ready = t('Download ProRes 422', '下载 ProRes 422');
    const failure = t('ProRes export failed. Click to retry.', 'ProRes 导出失败，点击重试。');
    async function request(method = 'GET') {
        const response = await api.fetchApi(PATH + (method === 'GET' ? `?id=${encodeURIComponent(id)}` : ''), {
            method, credentials: 'same-origin', cache: 'no-store',
            headers: {'X-FreeVideo-Standalone': '1', ...(method === 'POST' ? {'Content-Type':'application/json'} : {})},
            ...(method === 'POST' ? {body:JSON.stringify({id})} : {}),
        });
        const value = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof value.error === 'string' ? value.error : failure);
        return value;
    }
    function render(value) {
        state = value;
        button.hidden = !value.available;
        const busy = ['queued','encoding'].includes(value.state);
        button.disabled = busy;
        button.setAttribute('aria-busy', String(busy));
        const progress = value.progress;
        const percent = progress?.total > 0 ? Math.min(100, Math.round(100 * progress.done / progress.total)) : null;
        button.textContent = value.state === 'ready' ? ready : value.state === 'queued'
            ? t('ProRes 422 · queued', 'ProRes 422 · 排队中')
            : value.state === 'encoding' ? progress?.phase === 'audio'
                ? t('ProRes 422 · audio…', 'ProRes 422 · 音频…')
                : `ProRes 422${percent === null ? '…' : ` · ${percent}%`}`
            : value.state === 'error' ? t('ProRes 422 · retry', 'ProRes 422 · 重试') : title;
        button.title = value.state === 'error' ? value.error || failure
            : t('Encode retained RGB frames to a MOV file without running the model again.', '将保留的 RGB 帧导出为 MOV，无需重新生成。');
        return busy;
    }
    async function poll(autoDownload = false) {
        if (polling) return;
        polling = true;
        try {
            while (button.isConnected && ['queued','encoding'].includes(state?.state)) {
                await wait(1000);
                if (!button.isConnected) break;
                const value = await request(); render(value);
                if (value.state === 'ready' && autoDownload) download(id);
            }
        } catch (error) { render({available:true,state:'error',error:error.message || failure}); }
        finally { polling = false; }
    }
    button.onclick = async () => {
        if (button.disabled) return;
        if (state?.state === 'ready') { download(id); return; }
        button.disabled = true; button.textContent = t('Preparing ProRes 422…', '正在准备 ProRes 422…');
        try {
            const value = await request('POST'); render(value);
            if (value.state === 'ready') download(id);
            else await poll(true);
        } catch (error) { render({available:true,state:'error',error:error.message || failure}); }
    };
    request().then(value => { if (render(value)) void poll(); }).catch(() => { button.hidden = true; });
    return button;
}
