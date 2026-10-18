"""Observe native Comfy execution without replacing its queue or progress events.

Only node identifiers, presentation metadata and measured times are retained.
Node counts describe graph work, never a prediction of remaining runtime.
"""
from collections import OrderedDict
from functools import wraps
import threading
import time


LABELS = dict(prepare='Preparing workflow', model_load='Loading models',
              text_encode='Encoding text', reference='Preparing references',
              sampling='Sampling video', video_decode='Decoding video',
              audio_decode='Decoding audio', saving='Saving video')
PHASES = dict(prepare='prepare', model_load='load', text_encode='encode', reference='encode',
              sampling='sampling', video_decode='decode', audio_decode='decode', saving='save')


def _presentation(node):
    metadata = node.get('_meta')
    meta = metadata.get('freevideo_progress', {}) if isinstance(metadata, dict) else {}
    kind = str(node.get('class_type', '')).lower().replace('_', '')
    stage = meta.get('stage') if isinstance(meta, dict) else None
    if stage not in LABELS:
        if 'decode' in kind and 'audio' in kind:
            stage = 'audio_decode'
        elif 'decode' in kind:
            stage = 'video_decode'
        elif ('sampler' in kind or 'sampling' in kind) and not any(
                word in kind for word in ('samplerselect', 'scheduler', 'samplingconfig', 'modelsampling')):
            stage = 'sampling'
        elif 'textencode' in kind or 'cliptext' in kind:
            stage = 'text_encode'
        elif 'loader' in kind and not any(word in kind for word in ('image', 'audio', 'video')):
            stage = 'model_load'
        elif any(word in kind for word in ('image', 'reference', 'resize', 'loadvideo', 'loadaudio')):
            stage = 'reference'
        else:
            stage = 'prepare'
    label = meta.get('label') if isinstance(meta, dict) else None
    # Labels are administrator-authored metadata, never prompt inputs or paths.
    if not isinstance(label, str) or '/' in label or '\\' in label:
        label = LABELS[stage]
    label = ' '.join(label.split())[:100] or LABELS[stage]
    return stage, label


def _default_publish(server, owner, message):
    from .comfy_progress import publish
    publish(server, owner, message)


class WorkflowProgress:
    def __init__(self, server, *, clock=time.monotonic, publish=_default_publish, limit=64):
        self.server, self.clock, self.publish = server, clock, publish
        self.limit = limit
        self.prompts = OrderedDict()
        self.runs = OrderedDict()
        self.lock = threading.RLock()

    def _graph(self, prompt_id):
        queue = getattr(self.server, 'prompt_queue', None)
        if queue is None:
            return None
        # Comfy's accessor takes the queue mutex and copies the running rows.
        if callable(getattr(queue, 'get_current_queue', None)):
            running, _ = queue.get_current_queue()
        else:
            running = list(getattr(queue, 'currently_running', {}).values())
        return next((row for row in running if len(row) > 3 and str(row[1]) == prompt_id), None)

    def _start(self, prompt_id):
        if prompt_id in self.prompts:
            return
        started = self.clock()
        row = self._graph(prompt_id)
        if row is None or not isinstance(row[2], dict):
            return
        graph = row[2]
        run_ids = []
        for owner, node in graph.items():
            inputs = node.get('inputs', {})
            run_id = inputs.get('workflow_run_id')
            if node.get('class_type') != 'FreeVideoGenerate' or inputs.get('backend') != 'workflow' or not run_id:
                continue
            prefix = f'__freevideo_workflow_{run_id}_'
            nodes = {str(key): _presentation(value) for key, value in graph.items() if str(key).startswith(prefix)}
            if not nodes:
                continue
            # A deterministic dependency order is useful in receipts. Actual
            # execution and cache events alone advance the completed node set.
            ordered, visited = [], set()
            def visit(key):
                if key in visited or key not in nodes:
                    return
                visited.add(key)
                for value in graph[key].get('inputs', {}).values():
                    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                        visit(value[0])
                ordered.append(key)
            result = inputs.get('workflow_result')
            if isinstance(result, list) and len(result) == 2 and isinstance(result[0], str):
                visit(result[0])
            nodes = {key: nodes[key] for key in ordered}
            if not nodes:
                continue
            run = dict(id=str(run_id), owner=str(owner), workflow_id=str(inputs.get('workflow_id', '')),
                       prompt_id=prompt_id, sid=row[3].get('client_id') if isinstance(row[3], dict) else None,
                       nodes=nodes, order=ordered, done=set(), cached=set(), active=None,
                       started=started, ended=None, sampling_started=None, sample_seconds=0., status='running')
            self.runs[str(run_id)] = run
            run_ids.append(str(run_id))
            self._emit(run, new_request=True)
        if run_ids:
            self.prompts[prompt_id] = run_ids
        while len(self.prompts) > self.limit:
            _, expired = self.prompts.popitem(last=False)
            for run_id in expired:
                self.runs.pop(run_id, None)
        while len(self.runs) > self.limit:
            self.runs.popitem(last=False)

    def _close_node(self, run, now, *, completed=True):
        if run['sampling_started'] is not None:
            run['sample_seconds'] += max(0., now - run['sampling_started'])
            run['sampling_started'] = None
        if completed and run['active'] is not None:
            run['done'].add(run['active'])
        run['active'] = None

    def _emit(self, run, *, stage='prepare', label=None, **extra):
        node = run['active']
        envelope = dict(id=run['workflow_id'], node_label=label or LABELS[stage],
                        node_index=run['order'].index(node) + 1 if node in run['order'] else None,
                        nodes_total=len(run['nodes']), nodes_done=len(run['done']),
                        cached_nodes=len(run['cached']))
        elapsed = max(0., (run['ended'] if run['ended'] is not None else self.clock()) - run['started'])
        phase = extra.pop('phase', PHASES[stage])
        overall = dict(status=run['status'], elapsed_seconds=elapsed)
        if phase == 'complete':
            overall['fraction'] = 1.
        value = dict(label=label or LABELS[stage], phase=phase, stage=stage,
                     elapsed_seconds=elapsed, workflow=envelope,
                     overall=overall, **extra)
        self.publish(self.server, run['owner'], value)

    def observe(self, event, data, sid=None):
        if event not in ('execution_start', 'executing', 'progress', 'execution_cached',
                         'execution_success', 'execution_error', 'execution_interrupted') or not isinstance(data, dict):
            return
        prompt_id = data.get('prompt_id')
        if prompt_id is None:
            return
        prompt_id = str(prompt_id)
        with self.lock:
            if event == 'execution_start':
                self._start(prompt_id)
                return
            runs = [self.runs[key] for key in self.prompts.get(prompt_id, []) if key in self.runs]
            now = self.clock()
            node = str(data['node']) if data.get('node') is not None else None
            display_node = str(data['display_node']) if data.get('display_node') is not None else None
            for run in runs:
                if run['status'] != 'running':
                    continue
                current = node if node in run['nodes'] else display_node
                if event == 'executing':
                    if current != run['active']:
                        self._close_node(run, now)
                    if current in run['nodes']:
                        run['active'] = current
                        stage, label = run['nodes'][current]
                        if stage == 'sampling' and run['sampling_started'] is None:
                            run['sampling_started'] = now
                        self._emit(run, stage=stage, label=label)
                elif event == 'progress' and current in run['nodes'] and current == run['active']:
                    total, done = data.get('max'), data.get('value')
                    if isinstance(total, (int, float)) and isinstance(done, (int, float)) and total > 0:
                        stage, label = run['nodes'][current]
                        self._emit(run, stage=stage, label=label, done=max(0, min(done, total)), total=total, unit='steps')
                elif event == 'execution_cached':
                    cached = {str(key) for key in data.get('nodes', [])} & run['nodes'].keys()
                    run['cached'].update(cached)
                    run['done'].update(cached)
                    if cached:
                        self._emit(run, detail=f'{len(cached)} workflow nodes reused from Comfy cache')
                elif event in ('execution_error', 'execution_interrupted'):
                    status = 'cancelled' if event == 'execution_interrupted' else 'failed'
                    self._finish(run, status, now)
                    self._emit(run, label='Generation cancelled' if status == 'cancelled' else 'Generation stopped', phase=status)
                elif event == 'execution_success':
                    # Only finalize can declare the VIDEO saved. A successful
                    # prompt that never evaluated this owner has no new result.
                    self._finish(run, 'finished', now)

    def _finish(self, run, status, now):
        self._close_node(run, now, completed=status not in ('failed', 'cancelled'))
        run['ended'], run['status'] = now, status

    def begin_save(self, run_id):
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is not None and run['status'] == 'running':
                self._close_node(run, self.clock())
                self._emit(run, stage='saving')

    def timing(self, run_id):
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                return None
            now = run['ended'] if run['ended'] is not None else self.clock()
            sample = run['sample_seconds']
            if run['sampling_started'] is not None:
                sample += max(0., now - run['sampling_started'])
            return dict(request_seconds=max(0., now - run['started']), sample_seconds=sample,
                        cached_nodes=len(run['cached']), timing_scope='execution')

    def finish(self, run_id, status='complete', *, publish=False, result=None):
        with self.lock:
            run = self.runs.get(str(run_id))
            if run is None:
                return False
            if run['status'] == 'running':
                self._finish(run, status, self.clock())
            if publish:
                label = {'complete': 'Video saved', 'cancelled': 'Generation cancelled',
                         'failed': 'Generation stopped'}.get(status, 'Workflow finished')
                self._emit(run, stage='saving', label=label, phase=status,
                           **(dict(result=result) if result is not None else {}))
            return True


def register(server):
    """Install one fail-open observer; preserve native arguments and delivery."""
    existing = getattr(server, '_freevideo_workflow_progress', None)
    if existing is not None:
        return existing
    bridge = WorkflowProgress(server)
    original = server.send_sync

    @wraps(original)
    def send_sync(event, data, *args, **kwargs):
        result = original(event, data, *args, **kwargs)
        try:
            bridge.observe(event, data, kwargs.get('sid', args[0] if args else None))
        except Exception:
            # A presentation observer must never break Comfy execution.
            pass
        return result

    server.send_sync = send_sync
    server._freevideo_workflow_progress = bridge
    return bridge


def get(server):
    return getattr(server, '_freevideo_workflow_progress', None) if server is not None else None
