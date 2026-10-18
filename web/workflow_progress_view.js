// This bar counts graph work, not time. A slow model load has no invented ETA.
export function workflowProgressView(message, t = english => english) {
    const workflow = message?.workflow;
    if (!workflow || typeof workflow !== 'object') return null;
    const total = workflow.nodes_total, done = workflow.nodes_done;
    const counted = Number.isInteger(total) && total > 0 && Number.isInteger(done) && done >= 0 && done <= total;
    const complete = message.phase === 'complete' || message.overall?.status === 'complete';
    const sampling = message.phase === 'sampling';
    const steps = sampling && Number.isInteger(message.done) && Number.isInteger(message.total)
        && message.total > 0 && message.done >= 0 && message.done <= message.total;
    const local = steps ? message.done / message.total : 0;
    const labels = {
        starting: ['Starting workflow', '正在启动工作流'], prepare: ['Preparing workflow inputs', '正在准备输入'],
        load: ['Loading models', '正在加载模型'], encode: ['Encoding prompt and references', '正在编码提示词和参考素材'],
        sampling: ['Sampling', '正在采样'], decode: ['Decoding media', '正在解码媒体'],
        save: ['Saving video', '正在保存视频'], complete: ['Video saved', '视频已保存'],
        failed: ['Workflow failed', '工作流失败'], cancelled: ['Generation cancelled', '生成已取消'],
    };
    const label = labels[message.phase] || labels.prepare;
    const named = typeof workflow.node_label === 'string' ? workflow.node_label.slice(0, 160) : '';
    const text = (named || t(...label))
        + (steps ? t(` · step ${message.done} / ${message.total}`, ` · 第 ${message.done} / ${message.total} 步`) : '');
    return {text, key: sampling ? 'pass1' : ['load', 'encode', 'decode', 'save'].includes(message.phase) ? message.phase : 'prepare',
        counted, done: complete && counted ? total : done, total,
        fraction: counted ? complete ? 1 : Math.min(.99, (done + local) / total) : null,
        count: counted ? t(`Workflow nodes ${complete ? total : done} / ${total}`, `工作流节点 ${complete ? total : done} / ${total}`) : ''};
}
