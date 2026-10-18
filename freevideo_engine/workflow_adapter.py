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
              'first_frame', 'last_frame', 'reference_images', 'reference_audio', 'reference_video',
              'two_pass', 'base_steps', 'refine_steps', 'step_cache', 'upscale_factor'}
_CAPABILITIES = {'single_shot': True, 'preview': False, 'refine': False,
                 'continuation': False}
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
_AUDIO_EXTENSIONS = {'.wav', '.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac'}
_VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v'}
_MEDIA_INPUTS = {'assets', 'first', 'last', 'reference', 'reference_audio',
                 'reference_video', *(f'reference_{i}' for i in range(2, 9))}


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


def _bindings(binding):
    return binding['bindings'] if 'bindings' in binding else [
        {key: binding[key] for key in ('node', 'input')}]


def _targets(bindings, graph, used, label):
    if not isinstance(bindings, list) or not bindings or len(bindings) > 64:
        _fail(f'{label} bindings must be a nonempty array of at most 64 targets.')
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {'node', 'input'}:
            _fail(f'{label} binding needs node and input fields.')
        node, name = binding['node'], binding['input']
        if not isinstance(node, str) or node not in graph or not isinstance(name, str) or name not in graph[node]['inputs']:
            _fail(f'{label} binding must target an existing node input.')
        target = (node, name)
        if target in used:
            _fail(f'Multiple settings target {node!r}.{name}.')
        used.add(target)


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _field_value(field, value, label):
    kind = field['type']
    valid = ((kind == 'number' and _number(value)) or
             (kind == 'integer' and type(value) is int) or
             (kind == 'boolean' and type(value) is bool) or
             (kind == 'string' and isinstance(value, str) and len(value) <= 16384) or
             (kind == 'choice' and isinstance(value, str) and value in field['options']))
    if not valid:
        _fail(f'{label} must be a valid {kind} value.')
    if kind in ('number', 'integer') and not field['min'] <= value <= field['max']:
        _fail(f'{label} must be between {field["min"]} and {field["max"]}.')
    return value


def _bounds(field, label, *, integer=False):
    if (not _number(field.get('min')) or not _number(field.get('max')) or field['min'] > field['max']
            or integer and (type(field['min']) is not int or type(field['max']) is not int)):
        _fail(f'{label} needs finite min/max bounds in ascending order.')
    if 'step' in field and (not _number(field['step']) or field['step'] <= 0
                            or integer and type(field['step']) is not int):
        _fail(f'{label} step must be a positive {"integer" if integer else "number"}.')


def _same_template_value(bindings, graph, label):
    values = [graph[binding['node']]['inputs'][binding['input']] for binding in bindings]
    if any(type(value) is not type(values[0]) or value != values[0] for value in values[1:]):
        _fail(f'{label} bindings must have matching template values.')
    return values[0]


def _profile(manifest, graph, used):
    profile = manifest.get('profile', {})
    if not isinstance(profile, dict) or set(profile) - {'advanced', 'loras'}:
        _fail('Manifest profile supports advanced and loras arrays only.')
    public = {'advanced': [], 'loras': []}
    for group in public:
        rows = profile.get(group, [])
        if not isinstance(rows, list) or len(rows) > 64:
            _fail(f'Profile {group} must be an array of at most 64 items.')
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                _fail(f'Profile {group} items must be objects.')
            item_id = _identifier(row.get('id'), f'Profile {group} ID')
            if item_id in ids:
                _fail(f'Duplicate profile {group} ID {item_id!r}.')
            ids.add(item_id)
            label = f'Profile {group} {item_id!r}'
            if not isinstance(row.get('label'), str) or not row['label'].strip():
                _fail(f'{label} needs a label.')
            if group == 'advanced':
                if set(row) - {'id', 'label', 'type', 'default', 'min', 'max', 'step', 'options', 'bindings'}:
                    _fail(f'{label} contains unknown fields.')
                kind = row.get('type')
                if kind not in ('number', 'integer', 'boolean', 'string', 'choice') or 'default' not in row:
                    _fail(f'{label} needs a supported type and default.')
                if kind in ('number', 'integer'):
                    _bounds(row, label, integer=kind == 'integer')
                elif set(row) & {'min', 'max', 'step'}:
                    _fail(f'{label} supports numeric bounds only for numeric fields.')
                if kind == 'choice':
                    options = row.get('options')
                    if (not isinstance(options, list) or not options or len(options) > 256
                            or any(not isinstance(option, str) or len(option) > 16384 for option in options)
                            or len(options) != len(set(options))):
                        _fail(f'{label} options must be unique strings.')
                elif 'options' in row:
                    _fail(f'{label} options require type choice.')
                _field_value(row, row['default'], label)
                _targets(row.get('bindings'), graph, used, label)
                # Replacing linked inputs with scalar settings usually indicates a
                # mistaken authoring target and can leave a disconnected graph.
                if any(_link(graph[b['node']]['inputs'][b['input']]) for b in row['bindings']):
                    _fail(f'{label} must target literal scalar template inputs.')
                public[group].append({key: deepcopy(value) for key, value in row.items() if key != 'bindings'})
            else:
                if set(row) - {'id', 'label', 'min', 'max', 'step', 'enabled', 'bindings'}:
                    _fail(f'{label} contains unknown fields.')
                bindings = row.get('bindings')
                if (not isinstance(bindings, dict) or set(bindings) - {'name', 'strength', 'enabled'}
                        or not {'name', 'strength'} <= set(bindings)):
                    _fail(f'{label} needs name/strength binding arrays and optional enabled bindings.')
                for key, targets in bindings.items():
                    _targets(targets, graph, used, label + ' ' + key)
                name = _same_template_value(bindings['name'], graph, label + ' name')
                strength = _same_template_value(bindings['strength'], graph, label + ' strength')
                if not isinstance(name, str) or not name or not _number(strength):
                    _fail(f'{label} name/strength must be literal template text/number.')
                enabled = _same_template_value(bindings['enabled'], graph, label + ' enabled') if 'enabled' in bindings else True
                if type(enabled) is not bool or 'enabled' in row and type(row['enabled']) is not bool:
                    _fail(f'{label} enabled must be a boolean.')
                item = {'id': item_id, 'label': row['label'], 'name': name, 'strength': strength,
                        'enabled': row.get('enabled', enabled), 'min': row.get('min', -20),
                        'max': row.get('max', 20), 'step': row.get('step', 0.05)}
                _bounds(item, label)
                if not item['min'] <= strength <= item['max']:
                    _fail(f'{label} template strength is outside its bounds.')
                public[group].append(item)
    return public


def _settings(raw, manifest, public):
    if raw is None or raw == '':
        raw = '{}'
    if not isinstance(raw, str) or len(raw) > 256 * 1024:
        _fail('workflow_settings must be a JSON object of settings per workflow.')
    try:
        settings = json.loads(raw)
    except ValueError:
        _fail('workflow_settings must be valid JSON.')
    if not isinstance(settings, dict) or len(settings) > 256:
        _fail('workflow_settings must be an object of settings per workflow.')
    # Retain other workflow settings in the client; validate this selected
    # workflow's IDs against the current installed manifest on every run.
    for key, value in settings.items():
        _identifier(key, 'workflow_settings workflow ID')
        if not isinstance(value, dict) or set(value) - {'advanced', 'loras'}:
            _fail('workflow_settings entries support advanced and loras objects only.')
        if any(not isinstance(group, dict) for group in value.values()):
            _fail('workflow_settings advanced and loras must be objects.')
    selected = settings.get(manifest['id'], {})
    result = {'advanced': {}, 'loras': {}}
    for group, fields in public.items():
        supplied = selected.get(group, {})
        known = {field['id'] for field in fields}
        if set(supplied) - known:
            _fail(f'Unknown workflow {group} setting: {sorted(set(supplied) - known)[0]}.')
        for field in fields:
            key = field['id']
            if group == 'advanced':
                result[group][key] = _field_value(field, supplied.get(key, field['default']), f'Workflow setting {key!r}')
            else:
                value = supplied.get(key, {})
                if not isinstance(value, dict) or set(value) - {'strength', 'enabled'}:
                    _fail(f'Workflow LoRA {key!r} accepts strength and enabled only.')
                strength, enabled = value.get('strength', field['strength']), value.get('enabled', field['enabled'])
                if not _number(strength) or not field['min'] <= strength <= field['max']:
                    _fail(f'Workflow LoRA {key!r} strength must be between {field["min"]} and {field["max"]}.')
                if type(enabled) is not bool:
                    _fail(f'Workflow LoRA {key!r} enabled must be a boolean.')
                result[group][key] = {'strength': strength, 'enabled': enabled}
    return result


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


def _native_media(inputs, prompt, input_directory, prefix, owner_id):
    """Lower native media to explicit IMAGE/AUDIO/VIDEO links."""
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
    raw = media.get('assets', '[]')
    if not isinstance(raw, str) or len(raw) > 128 * 1024:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    try:
        rows = json.loads(raw)
    except ValueError:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    if not isinstance(rows, list) or len(rows) > 32:
        _fail('Workflow media assets must be a JSON array of at most 32 items.')
    frames, loaders, references = {}, {}, []
    for index, row in enumerate(rows):
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
        if role not in ('first', 'last', 'reference'):
            _fail('Workflow media role must be first, last or reference.')
        if 'early_steps' in row:
            _fail('Workflow media early_steps conditioning is unsupported.')
        if input_directory is None:
            _fail('Workflow uploaded media requires a server-configured ComfyUI input directory.')
        if (not isinstance(name, str) or not name or '\x00' in name or '\\' in name
                or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
                or '..' in PurePosixPath(name).parts or ':' in name):
            _fail('Workflow media must use relative files inside ComfyUI input.')
        extension = PurePosixPath(name).suffix.lower()
        kind = ('image' if extension in _IMAGE_EXTENSIONS else 'audio' if extension in _AUDIO_EXTENSIONS
                else 'video' if extension in _VIDEO_EXTENSIONS else None)
        if kind is None or role in ('first', 'last') and kind != 'image':
            _fail('Workflow media frames must be images; reference files must be image, audio or video.')
        path = (input_directory / name).resolve()
        try:
            relative = path.relative_to(input_directory)
        except ValueError:
            _fail('Workflow media link points outside ComfyUI input.')
        if not path.is_file():
            _fail('Upload or select the missing workflow media file: ' + name)
        loader_id = prefix + 'media_' + str(index)
        loader, field = {'image': ('LoadImage', 'image'), 'audio': ('LoadAudio', 'audio'),
                         'video': ('LoadVideo', 'file')}[kind]
        loaders[loader_id] = {'class_type': loader, 'inputs': {field: relative.as_posix()}}
        if role == 'reference' and kind == 'image':
            references.append([loader_id, 0])
        else:
            target = role if role in ('first', 'last') else 'reference_' + kind
            if target in frames:
                _fail(f'Workflow media contains duplicate {target} inputs.')
            frames[target] = [loader_id, 0]
    for name in ('reference', *(f'reference_{i}' for i in range(2, 9))):
        if _present(media.get(name)):
            if not _link(media[name]) or media[name][0] not in prompt or media[name][0] == owner_id:
                _fail(f'Workflow media {name} must link to an IMAGE output in this prompt.')
            references.append(media[name])
    if references:
        # ImageBatch preserves row/socket order and resizes later images to the
        # first batch's dimensions according to ComfyUI's core implementation.
        batch = references[0]
        for index, reference in enumerate(references[1:], 1):
            batch_id = prefix + 'media_batch_' + str(index)
            loaders[batch_id] = {'class_type': 'ImageBatch', 'inputs': {'image1': batch, 'image2': reference}}
            batch = [batch_id, 0]
        frames['reference_images'] = batch
    for role in ('first', 'last', 'reference_audio', 'reference_video', 'reference_images'):
        if _present(media.get(role)):
            if role in frames:
                _fail(f'Workflow media has duplicate uploaded/connected {role} inputs; disable the duplicate.')
            frames[role] = media[role]
        if role in frames:
            if _present(inputs.get(role)):
                _fail(f'Workflow has duplicate direct/media {role} inputs; remove the duplicate.')
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
        if (not isinstance(binding, dict) or set(binding) - {'node', 'input', 'bindings', 'required'}
                or ('bindings' in binding and set(binding) & {'node', 'input'})
                or ('bindings' not in binding and not {'node', 'input'} <= set(binding))):
            _fail(f'Input binding {name!r} needs node/input or a bindings array, plus optional required.')
        if 'required' in binding and type(binding['required']) is not bool:
            _fail(f'Input binding {name!r} required must be a boolean.')
        _targets(_bindings(binding), graph, targets, f'Input {name!r}')
    _profile(manifest, graph, targets)
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
    def _metadata(manifest, graph):
        return {'id': manifest['id'], 'title': manifest['title'],
                'inputs': sorted(manifest['inputs']), 'capabilities': dict(_CAPABILITIES),
                'profile': _profile(manifest, graph, set())}

    def validate(self, workflow_id):
        """Validate one installed adapter and return its public metadata."""
        return self._metadata(*self._load(workflow_id))

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
            loaders = _native_media(inputs, prompt, self.input_directory, prefix, str(owner_id))
            if any(node_id in prompt for node_id in loaders):
                _fail('Workflow node namespace collision; submit the original prompt again.')
            prompt.update(loaders)
            # Workflow profiles own LoRA configuration. Stale native Studio
            # connections are detached and never executed by the owner.
            inputs.pop('loras', None)
            for name in ('preview', 'refine_from', 'continue_from', 'references', 'media', 'loras',
                         'conditioning', 'first_frame_reference', 'voice_reference', 'hide_tail'):
                if _present(inputs.get(name)):
                    _fail(f'Workflow backend does not support {name}; use a single-shot request without that input.')
            manifest, graph = self._load(inputs.get('workflow_id'))
            values = self._values(inputs, manifest, prompt, str(owner_id))
            settings = _settings(inputs.get('workflow_settings', '{}'), manifest, _profile(manifest, graph, set()))
            # Persist the effective selected profile, including defaults, with
            # the queued graph/report. The browser keeps other profiles locally.
            inputs['workflow_settings'] = json.dumps({manifest['id']: settings}, ensure_ascii=False)
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
                    for target in _bindings(binding):
                        expanded[target['node']]['inputs'][target['input']] = deepcopy(values[name])
            for field in manifest.get('profile', {}).get('advanced', []):
                for binding in field['bindings']:
                    expanded[binding['node']]['inputs'][binding['input']] = deepcopy(settings['advanced'][field['id']])
            for field in manifest.get('profile', {}).get('loras', []):
                value = settings['loras'][field['id']]
                for binding in field['bindings']['strength']:
                    expanded[binding['node']]['inputs'][binding['input']] = value['strength'] if value['enabled'] else 0
                for binding in field['bindings'].get('enabled', []):
                    expanded[binding['node']]['inputs'][binding['input']] = value['enabled']
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
                               ('first', 'first_frame'), ('last', 'last_frame'),
                               ('reference_images', 'reference_images'), ('reference_audio', 'reference_audio'),
                               ('reference_video', 'reference_video'),
                               *((name, name) for name in ('two_pass', 'base_steps', 'refine_steps', 'step_cache', 'upscale_factor')
                                 if name in bindings)):
            if source in inputs and (inputs[source] is not None):
                value = inputs[source]
                if target in ('first_frame', 'last_frame', 'reference_images', 'reference_audio', 'reference_video'):
                    if not _present(value):
                        continue
                    if target not in bindings:
                        _fail(f'Workflow {manifest["id"]!r} does not support {source} input; declare its canonical binding or remove it.')
                    if not _link(value) or value[0] not in prompt or value[0] == owner_id:
                        socket = 'AUDIO' if target == 'reference_audio' else 'VIDEO' if target == 'reference_video' else 'IMAGE'
                        _fail(f'Workflow {source} must link to an {socket} output in this prompt.')
                elif isinstance(value, (list, dict)):
                    _fail(f'Workflow input {source!r} must be a literal value; linked scalar inputs are unsupported.')
                values[target] = value
        for name in ('width', 'height', 'seed', 'base_steps', 'refine_steps'):
            if name in values and (type(values[name]) is not int or values[name] < (0 if name == 'seed' else 1)):
                _fail(f'Workflow input {name!r} must be a {"nonnegative" if name == "seed" else "positive"} integer.')
        if 'prompt' in values and not isinstance(values['prompt'], str):
            _fail('Workflow prompt must be text.')
        if 'two_pass' in values and type(values['two_pass']) is not bool:
            _fail('Workflow two_pass must be a boolean.')
        if 'step_cache' in values and not isinstance(values['step_cache'], str):
            _fail('Workflow step_cache must be text.')
        if 'upscale_factor' in values:
            value = values['upscale_factor']
            if value not in ('2', '1.5', '1.2', 2, 1.5, 1.2) or type(value) is bool:
                _fail('Workflow upscale_factor must be 2, 1.5 or 1.2.')
            values['upscale_factor'] = float(value)
        if 'seconds' in values and (not _number(values['seconds']) or values['seconds'] <= 0):
            _fail('Workflow duration must be a finite positive number of seconds.')
        frames = inputs.get('frames', 0)
        if frames is None or frames == '':
            frames = 0
        if type(frames) is not int or frames < 0:
            _fail('Workflow frames must be a nonnegative integer; use 0 to follow duration.')
        if frames > 0:
            values['frames'] = frames
        elif 'seconds' in values:
            if not _number(values['seconds'] * 24):
                _fail('Workflow duration is too large to produce a finite frame count.')
            values['frames'] = int(round(values['seconds'] * 24))
            if values['frames'] < 1:
                _fail('Workflow duration must produce at least one frame at 24 fps.')
        values['fps'] = 24
        for name, binding in bindings.items():
            if binding.get('required', False) and (name not in values or values[name] is None or values[name] == ''):
                _fail(f'Workflow {manifest["id"]!r} requires {name!r}; supply that FreeVideo input.')
        return values
