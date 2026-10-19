// ComfyUI host-module doubles shared by the synchronous and Node 22.11 loaders.
export function resolve(specifier, context, nextResolve) {
    if (specifier === '../../scripts/app.js') return {url: 'data:text/javascript,export const app={graph:{_nodes:[]}};', shortCircuit: true};
    if (specifier === '../../scripts/api.js') return {url: 'data:text/javascript,export const api={};', shortCircuit: true};
    return nextResolve(specifier, context);
}
