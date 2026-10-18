// The optional advanced inputs were appended after the first public workflow
// shape. A workflow restored from an older version, or a widget list that moved,
// can leave an empty or missing value in them, and Comfy then rejects the whole
// request. Keep each of them to a value the server accepts.
const DEFAULTS = {refine_from: '', continue_from: '', frames: 0, first_frame_reference: false, step_cache: 'none', upscale_factor: '2'};
// A workflow saved before Step cache existed holds '' where it now sits.
const STEP_CACHE_OPTIONS = ['none', 'easycache-av', 'fbcache'];
// Likewise for Upscale factor, appended after Step cache.
const UPSCALE_OPTIONS = ['2', '1.5', '1.2'];

// The words a script, an older workflow or String(null) leaves behind for "not given".
const EMPTY_WORDS = new Set(['none', 'null', 'undefined', 'nan']);

// Text of an optional input, or '' when it was not given.
export function optionalText(value) {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    return !text || EMPTY_WORDS.has(text.toLowerCase()) ? '' : value;
}

// Total frames is 0 (follow Duration) or 5 + 17n from 39 on, as the engine reads it (node_frames).
// Any other number came from somewhere else, such as a moved widget, and is not a requested length.
export function frameCount(value) {
    return Number.isSafeInteger(value) && value >= 39 && (value - 5) % 17 === 0 ? value : 0;
}

export function optionalInputValue(name, value) {
    if (name === 'frames') return frameCount(value);
    if (name === 'refine_from' || name === 'continue_from') return optionalText(value);
    if (name === 'first_frame_reference') return value === true;
    if (name === 'step_cache') return STEP_CACHE_OPTIONS.includes(value) ? value : 'none';
    if (name === 'upscale_factor') return UPSCALE_OPTIONS.includes(value) ? value : '2';
    return value;
}

export function cleanOptionalWidgets(node) {
    for (const widget of node?.widgets || []) {
        if (!(widget.name in DEFAULTS)) continue;
        const workflowFrames = widget.name === 'frames' && node.widgets?.find(w => w.name === 'backend')?.value === 'workflow';
        const value = workflowFrames ? (Number.isSafeInteger(widget.value) && widget.value >= 0 ? widget.value : 0) : optionalInputValue(widget.name, widget.value);
        if (value !== widget.value) widget.value = value;
    }
}

export function cleanOptionalPrompt(prompt) {
    for (const entry of Object.values(prompt?.output || {})) {
        if (entry?.class_type !== 'FreeVideoGenerate' || !entry.inputs) continue;
        for (const name of Object.keys(DEFAULTS)) {
            if (name === 'frames' && entry.inputs.backend === 'workflow') {
                entry.inputs.frames = Number.isSafeInteger(entry.inputs.frames) && entry.inputs.frames >= 0 ? entry.inputs.frames : 0;
                continue;
            }
            if (name in entry.inputs) entry.inputs[name] = optionalInputValue(name, entry.inputs[name]);
        }
    }
    return prompt;
}
