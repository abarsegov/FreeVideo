"""Compile administrator-installed ComfyUI API workflows without importing ComfyUI.

Only files below the registry root supply executable graphs. The request supplies
an adapter ID and ordinary FreeVideo input values, never an executable template.
"""
from copy import deepcopy
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import uuid


class WorkflowAdapterError(ValueError):
    """An adapter or request cannot satisfy the workflow backend contract."""


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_NODE = re.compile(r'[A-Za-z0-9_-]+\Z')
_CANONICAL = {'prompt', 'seed', 'width', 'height', 'seconds', 'frames', 'fps',
              'first_frame', 'last_frame'}
_CAPABILITIES = {'single_shot': True, 'preview': False, 'refine': False,
                 'continuation': False}
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
_MEDIA_INPUTS = {'assets', 'first', 'last', 'reference', 'reference_audio',
                 *(f'reference_{i}' for i in range(2, 9))}


def _fail(message):
    raise WorkflowAdapterError(message)


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(f'{label} must contain 1–64 letters, digits, underscores or hyphens, starting with a letter or digit.')
    return value


def _link(value):
    return (isinstance(value, list) and len(value) == 2
            and isinstance(value[0], str) and type(value[1]) is int and value[1] >= 0)


def _present(value):
    return value is not None and value != '' and value != [] and value != {} and value is not False


def _empty_native_provider(value, kind, prompt, seen=None):
    """Prove an optional native stack empty without running any user code."""
    if not _link(value) or value[1] != 0:
        return False
    seen = set() if seen is None else seen
    if value[0] in seen:
        return False
    seen.add(value[0])
    node = prompt.get(value[0])
    if not isinstance(node, dict) or not isinstance(node.get('inputs'), dict):
        return False
    inputs = node['inputs']
    expected = 'FreeVideoMedia' if kind == 'media' else 'FreeVideoLoRAStack'
    if node.get('class_type') != expected:
        return False
    field = 'assets' if kind == 'media' else 'adapters'
    raw = inputs.get(field, '[]')
    if not isinstance(raw, str):
        return False
    try:
        if json.loads(raw) != []:
            return False
    except ValueError:
        return False
    for name, item in inputs.items():
        if name == field or not _present(item):
            continue
        if kind == 'loras' and name == 'previous' and _empty_native_provider(item, kind, prompt, seen):
            continue
        return False
    return True


def _native_media(inputs, prompt, input_directory, prefix):
    """Lower native keyframes to IMAGE links without executing their provider."""
    value = inputs.get('media')
    if not _present(value):
        return {}
    if not _link(value) or value[1] != 0:
        _fail('Workflow media must link to a native FreeVideoMedia output.')
    node = prompt.get(value[0])
    if not isinstance(node, dict) or node.get('class_type') != 'FreeVideoMedia' or not isinstance(node.get('inputs'), dict):
        _fail('Workflow media must link to a native FreeVideoMedia output.')
    media = node['inputs']
    for name, item in media.items():
        if name not in _MEDIA_INPUTS and _present(item):
            _fail(f'Workflow media has an unsupported input {name!r}.')
        if name not in ('assets', 'first', 'last') and _present(item):
            _fail(f'Workflow media does not support {name}; use first/last frames only.')
    raw = media.get('assets', '[]')
    if not isinstance(raw, str) or len(raw) > 128 * 1024:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    try:
        rows = json.loads(raw)
    except ValueError:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    if not isinstance(rows, list) or len(rows) > 32:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    frames, loaders = {}, {}
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'file', 'role', 'enabled', 'cast', 'early_steps'}:
            _fail('Invalid workflow media item.')
        if type(row.get('enabled', True)) is not bool:
            _fail('Workflow media enabled must be a boolean.')
        cast = row.get('cast')
        if 'cast' in row and (not isinstance(cast, dict) or set(cast) != {'id'}
                              or not isinstance(cast['id'], str) or not 0 < len(cast['id']) <= 128):
            _fail('Invalid workflow media cast metadata.')
        if not row.get('enabled', True):
            continue
        role, name = row.get('role'), row.get('file')
        if role not in ('first', 'last'):
            _fail('Workflow media supports first/last image frames only; references are unsupported.')
        if 'early_steps' in row:
            _fail('Workflow media early_steps conditioning is unsupported.')
        if role in frames:
            _fail(f'Workflow media contains duplicate {role} frames.')
        if input_directory is None:
            _fail('Workflow uploaded media requires a server-configured ComfyUI input directory.')
        if (not isinstance(name, str) or not name or '\x00' in name or '\\' in name
                or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
                or '..' in PurePosixPath(name).parts or ':' in name
                or PurePosixPath(name).suffix.lower() not in _IMAGE_EXTENSIONS):
            _fail('Workflow media frames must be relative image files inside ComfyUI input.')
        path = (input_directory / name).resolve()
        try:
            relative = path.relative_to(input_directory)
        except ValueError:
            _fail('Workflow media link points outside ComfyUI input.')
        if not path.is_file():
            _fail('Upload or select the missing workflow media image: ' + name)
        loader_id = prefix + 'media_' + role
        loaders[loader_id] = {'class_type': 'LoadImage', 'inputs': {'image': relative.as_posix()}}
        frames[role] = [loader_id, 0]
    for role in ('first', 'last'):
        if _present(media.get(role)):
            if role in frames:
                _fail(f'Workflow media has both an uploaded and connected {role} frame; disable the duplicate.')
            frames[role] = media[role]
        if role in frames:
            if _present(inputs.get(role)):
                _fail(f'Workflow has both direct and media {role} frames; remove the duplicate.')
            inputs[role] = frames[role]
    del inputs['media']
    return loaders


def _read(path, directory):
    if path.resolve().parent != directory:
        _fail(f'{path.name} must be a regular file inside its adapter directory.')
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise WorkflowAdapterError(f'Cannot read {path.name}: {error}') from error


def _graph(graph):
    if not isinstance(graph, dict) or not graph:
        _fail('workflow_api.json must be a nonempty ComfyUI API prompt object, not a UI workflow.')
    for node_id, node in graph.items():
        if not isinstance(node_id, str) or not _NODE.fullmatch(node_id):
            _fail('API workflow node IDs may contain only letters, digits, underscores and hyphens.')
        if not isinstance(node, dict) or not isinstance(node.get('class_type'), str) or not node['class_type'].strip():
            _fail(f'Workflow node {node_id!r} needs a class_type string.')
        if node['class_type'] == 'FreeVideoGenerate':
            _fail('Registered workflows cannot contain FreeVideoGenerate (recursive execution).')
        if not isinstance(node.get('inputs'), dict):
            _fail(f'Workflow node {node_id!r} needs an inputs object.')
        for name, value in node['inputs'].items():
            if not isinstance(name, str) or not name:
                _fail(f'Workflow node {node_id!r} has an invalid input name.')
            if isinstance(value, list):
                if not _link(value) or value[0] not in graph:
                    _fail(f'Workflow node {node_id!r} input {name!r} has an invalid internal link.')
    # Cycles cannot execute in an API prompt; report them before queue validation.
    visiting, visited = set(), set()

    def visit(node_id):
        if node_id in visiting:
            _fail(f'Workflow contains a dependency cycle at node {node_id!r}.')
        if node_id in visited:
            return
        visiting.add(node_id)
        for value in graph[node_id]['inputs'].values():
            if _link(value):
                visit(value[0])
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in graph:
        visit(node_id)
    return graph


def _manifest(manifest, graph, adapter_id):
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
        _fail('manifest.json must declare version: 1.')
    if _identifier(manifest.get('id'), 'Adapter ID') != adapter_id:
        _fail('Manifest id must match its registered directory name.')
    if not isinstance(manifest.get('title'), str) or not manifest['title'].strip():
        _fail('Manifest needs a nonempty title.')
    bindings = manifest.get('inputs')
    if not isinstance(bindings, dict) or not set(bindings) <= _CANONICAL:
        _fail('Manifest inputs must map supported canonical input names to bindings.')
    targets = set()
    for name, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) - {'node', 'input', 'required'}:
            _fail(f'Input binding {name!r} needs node, input and optional required fields.')
        node_id, input_name = binding.get('node'), binding.get('input')
        if not isinstance(node_id, str) or node_id not in graph or not isinstance(input_name, str) or input_name not in graph[node_id]['inputs']:
            _fail(f'Input binding {name!r} must target an existing node input.')
        if 'required' in binding and type(binding['required']) is not bool:
            _fail(f'Input binding {name!r} required must be a boolean.')
        target = (node_id, input_name)
        if target in targets:
            _fail(f'Multiple canonical inputs target {node_id!r}.{input_name}.')
        targets.add(target)
    outputs = manifest.get('outputs')
    video = outputs.get('video') if isinstance(outputs, dict) else None
    if (not isinstance(video, dict) or set(video) - {'node', 'output', 'type'}
            or not isinstance(video.get('node'), str) or video['node'] not in graph
            or type(video.get('output')) is not int or video['output'] < 0
            or video.get('type', 'VIDEO') != 'VIDEO'):
        _fail('Manifest outputs.video must declare a VIDEO node/output slot (nonnegative integer).')
    return manifest


class Registry:
    """Read an admin-controlled root containing one directory per adapter ID."""

    def __init__(self, root, *, input_directory=None):
        self.root = Path(root).resolve()
        self.input_directory = Path(input_directory).resolve() if input_directory is not None else None

    def _load(self, adapter_id):
        adapter_id = _identifier(adapter_id, 'Workflow ID')
        directory = self.root / adapter_id
        if directory.resolve().parent != self.root or not directory.is_dir():
            _fail(f'Workflow {adapter_id!r} is not registered. Install its adapter directory on the server.')
        try:
            graph = _graph(_read(directory / 'workflow_api.json', directory.resolve()))
            manifest = _manifest(_read(directory / 'manifest.json', directory.resolve()), graph, adapter_id)
        except WorkflowAdapterError as error:
            raise WorkflowAdapterError(f'Workflow {adapter_id!r}: {error}') from error
        return manifest, graph

    @staticmethod
    def _metadata(manifest):
        return {'id': manifest['id'], 'title': manifest['title'],
                'inputs': sorted(manifest['inputs']), 'capabilities': dict(_CAPABILITIES)}

    def validate(self, workflow_id):
        """Validate one installed adapter and return its public metadata."""
        return self._metadata(self._load(workflow_id)[0])

    def catalog(self):
        """Return metadata only; malformed installed adapters fail visibly."""
        if not self.root.exists():
            return []
        return [self.validate(path.name) for path in sorted(self.root.iterdir())
                if path.is_dir() and not path.name.startswith('.')]

    def expand(self, payload):
        """Return a copied prompt with workflow nodes injected before validation."""
        result = deepcopy(payload)
        prompt = result.get('prompt') if isinstance(result, dict) else None
        if not isinstance(prompt, dict):
            return result
        for owner_id, owner in list(prompt.items()):
            if not isinstance(owner, dict) or owner.get('class_type') != 'FreeVideoGenerate':
                continue
            inputs = owner.get('inputs')
            if not isinstance(inputs, dict):
                continue
            # These fields are server-owned, including on non-workflow nodes.
            if inputs.get('workflow_result') is not None or inputs.get('workflow_run_id') not in (None, ''):
                _fail('workflow_result and workflow_run_id are server-owned; submit the original unexpanded prompt.')
            if inputs.get('backend', 'vdn') != 'workflow':
                continue
            inputs.pop('workflow_error', None)
            run_id = uuid.uuid4().hex
            prefix = f'__freevideo_workflow_{run_id}_'
            loaders = _native_media(inputs, prompt, self.input_directory, prefix)
            if any(node_id in prompt for node_id in loaders):
                _fail('Workflow node namespace collision; submit the original prompt again.')
            prompt.update(loaders)
            for name in ('loras',):
                if _empty_native_provider(inputs.get(name), name, prompt):
                    del inputs[name]
            for name in ('preview', 'refine_from', 'continue_from', 'references', 'media', 'loras',
                         'conditioning', 'first_frame_reference', 'voice_reference', 'hide_tail'):
                if _present(inputs.get(name)):
                    _fail(f'Workflow backend does not support {name}; use a single-shot request without that input.')
            manifest, graph = self._load(inputs.get('workflow_id'))
            values = self._values(inputs, manifest, prompt, str(owner_id))
            names = {node_id: prefix + node_id for node_id in graph}
            if any(node_id in prompt for node_id in names.values()):
                _fail('Workflow node namespace collision; submit the original prompt again.')
            expanded = deepcopy(graph)
            for node in expanded.values():
                for name, value in node['inputs'].items():
                    if _link(value):
                        node['inputs'][name] = [names[value[0]], value[1]]
            for name, binding in manifest['inputs'].items():
                if name in values:
                    # Inject after rewriting: external IMAGE links belong to the
                    # caller prompt and must never receive the adapter namespace.
                    expanded[binding['node']]['inputs'][binding['input']] = deepcopy(values[name])
            prompt.update({names[node_id]: node for node_id, node in expanded.items()})
            video = manifest['outputs']['video']
            inputs['workflow_result'] = [names[video['node']], video['output']]
            inputs['workflow_run_id'] = run_id
        return result

    @staticmethod
    def _values(inputs, manifest, prompt, owner_id):
        values = {}
        bindings = manifest['inputs']
        for source, target in (('text', 'prompt'), ('seed', 'seed'), ('width', 'width'),
                               ('height', 'height'), ('seconds', 'seconds'),
                               ('first', 'first_frame'), ('last', 'last_frame')):
            if source in inputs and (inputs[source] is not None):
                value = inputs[source]
                if target in ('first_frame', 'last_frame'):
                    if not _present(value):
                        continue
                    if target not in bindings:
                        _fail(f'Workflow {manifest["id"]!r} does not support {source} frame input; remove its connection.')
                    if not _link(value) or value[0] not in prompt or value[0] == owner_id:
                        _fail(f'Workflow {source} frame must link to an IMAGE output in this prompt.')
                elif isinstance(value, (list, dict)):
                    _fail(f'Workflow input {source!r} must be a literal value; linked scalar inputs are unsupported.')
                values[target] = value
        for name in ('width', 'height', 'seed'):
            if name in values and (type(values[name]) is not int or values[name] < (0 if name == 'seed' else 1)):
                _fail(f'Workflow input {name!r} must be a {"nonnegative" if name == "seed" else "positive"} integer.')
        if 'prompt' in values and not isinstance(values['prompt'], str):
            _fail('Workflow prompt must be text.')
        if 'seconds' in values and (type(values['seconds']) not in (int, float)
                                    or not math.isfinite(values['seconds']) or values['seconds'] <= 0):
            _fail('Workflow duration must be a finite positive number of seconds.')
        frames = inputs.get('frames', 0)
        if frames is None or frames == '':
            frames = 0
        if type(frames) is not int or frames < 0:
            _fail('Workflow frames must be a nonnegative integer; use 0 to follow duration.')
        if frames > 0:
            values['frames'] = frames
        elif 'seconds' in values:
            values['frames'] = int(round(values['seconds'] * 24))
            if values['frames'] < 1:
                _fail('Workflow duration must produce at least one frame at 24 fps.')
        values['fps'] = 24
        for name, binding in bindings.items():
            if binding.get('required', False) and (name not in values or not _present(values[name])):
                _fail(f'Workflow {manifest["id"]!r} requires {name!r}; supply that FreeVideo input.')
        return values
