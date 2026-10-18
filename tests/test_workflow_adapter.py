"""Pure compiler tests: no ComfyUI, Torch, models or server required."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from freevideo_engine.workflow_adapter import Registry, WorkflowAdapterError, load_workflow_document


class WorkflowAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'test-video'
        self.directory.mkdir()
        self.manifest = {
            'version': 1, 'id': 'test-video', 'title': 'Test video',
            'inputs': {
                'width': {'node': 'image', 'input': 'width', 'required': True},
                'height': {'node': 'image', 'input': 'height'},
                'frames': {'node': 'image', 'input': 'batch_size'},
                'fps': {'node': 'video', 'input': 'fps'},
                'first_frame': {'node': 'video', 'input': 'images'},
            },
            'outputs': {'video': {'node': 'video', 'output': 0}},
        }
        self.graph = {
            'image': {'class_type': 'EmptyImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1, 'color': 0}},
            'video': {'class_type': 'CreateVideo', 'inputs': {'images': ['image', 0], 'fps': 1}},
        }
        self.registry = Registry(self.root)
        self.write()

    def write(self):
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        (self.directory / 'workflow_api.json').write_text(json.dumps(self.graph), encoding='utf-8')

    def write_document(self, document=None):
        self.editor_directory = self.root / '.editor'
        self.editor_directory.mkdir(exist_ok=True)
        self.registry = Registry(self.root, editor_directory=self.editor_directory)
        document = document if document is not None else {
            'version': 0.4, 'nodes': [
                {'id': node_id, 'type': node['class_type'], 'widgets_values': []}
                for node_id, node in self.graph.items()], 'links': [],
            'extra': {'freevideo': {'version': 1, 'manifest': deepcopy(self.manifest), 'api': deepcopy(self.graph)}},
        }
        (self.editor_directory / 'test-video.json').write_text(json.dumps(document), encoding='utf-8')
        return document

    def test_native_document_compiles_and_overrides_legacy(self):
        document = self.write_document()
        document['extra']['freevideo']['manifest']['title'] = 'Native document'
        document['extra']['freevideo']['api']['image']['inputs']['color'] = 1234
        self.write_document(document)
        catalog = self.registry.catalog()
        self.assertEqual([item['title'] for item in catalog], ['Native document'])
        result, _, video = self.expanded_parts(self.payload())
        self.assertEqual(result['prompt'][video['inputs']['images'][0]]['inputs']['color'], 1234)
        self.assertNotIn('api', catalog[0])
        self.assertNotIn('manifest', catalog[0])

    def test_native_document_without_legacy_root_and_foreign_documents(self):
        self.write_document()
        self.registry = Registry(self.root / 'missing', editor_directory=self.editor_directory)
        (self.editor_directory / 'foreign.json').write_text(json.dumps({'version': 0.4, 'nodes': [], 'links': []}))
        self.assertEqual([item['id'] for item in self.registry.catalog()], ['test-video'])
        self.assertEqual(self.registry.validate('test-video')['id'], 'test-video')

    def test_invalid_native_document_never_falls_back_to_legacy(self):
        document = self.write_document()
        for invalid in [dict(document, links={}), dict(document, version=9),
                        dict(document, extra={}), dict(document, nodes=[]),
                        dict(document, extra={'freevideo': {'version': 2}})]:
            self.write_document(invalid)
            with self.subTest(document=invalid), self.assertRaises(WorkflowAdapterError):
                self.registry.validate('test-video')
        (self.editor_directory / 'test-video.json').write_text('{broken')
        with self.assertRaisesRegex(WorkflowAdapterError, 'test-video'):
            self.registry.catalog()

    def test_document_manifest_must_match_filename(self):
        document = self.write_document()
        document['extra']['freevideo']['manifest']['id'] = 'different'
        self.write_document(document)
        with self.assertRaisesRegex(WorkflowAdapterError, 'Manifest id'):
            self.registry.validate('test-video')
        with self.assertRaises(WorkflowAdapterError):
            self.registry.catalog()

    def test_document_helper_checks_api_visual_node_identity(self):
        document = self.write_document()
        manifest, graph = load_workflow_document(document)
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(graph, self.graph)
        document['nodes'].append({'id': 'note', 'type': 'Note'})
        load_workflow_document(document)  # Editor-only nodes need no executable API node.
        document['nodes'][0]['type'] = 'WrongClass'
        with self.assertRaisesRegex(WorkflowAdapterError, 'visual node ID and type'):
            load_workflow_document(document)

    def test_document_accepts_numeric_visual_ids_and_rejects_duplicates(self):
        document = self.write_document()
        envelope = document['extra']['freevideo']
        envelope['api'] = {'1': envelope['api'].pop('image'), '2': envelope['api'].pop('video')}
        envelope['api']['2']['inputs']['images'] = ['1', 0]
        for binding in envelope['manifest']['inputs'].values():
            binding['node'] = '1' if binding['node'] == 'image' else '2'
        envelope['manifest']['outputs']['video']['node'] = '2'
        document['nodes'] = [{'id': 1, 'type': 'EmptyImage'}, {'id': 2, 'type': 'CreateVideo'}]
        load_workflow_document(document)
        document['nodes'].append({'id': '1', 'type': 'EmptyImage'})
        with self.assertRaisesRegex(WorkflowAdapterError, 'unique IDs'):
            load_workflow_document(document)

    def test_native_document_size_limit_and_path_validation(self):
        self.write_document()
        path = self.editor_directory / 'test-video.json'
        path.write_bytes(b' ' * (8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(WorkflowAdapterError, '8 MiB'):
            self.registry.validate('test-video')
        for workflow_id in ('../test-video', 'test-video.json', '/test-video'):
            with self.subTest(workflow_id=workflow_id), self.assertRaises(WorkflowAdapterError):
                self.registry.validate(workflow_id)

    def test_native_document_symlink_cannot_escape_directory(self):
        self.write_document()
        external = self.root / 'external.json'
        path = self.editor_directory / 'test-video.json'
        external.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(external)
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        with self.assertRaisesRegex(WorkflowAdapterError, 'inside its adapter directory'):
            self.registry.validate('test-video')

    def payload(self, **inputs):
        return {'prompt': {'7': {'class_type': 'FreeVideoGenerate', 'inputs': {
            'backend': 'workflow', 'workflow_id': 'test-video', 'text': 'a scene',
            'width': 640, 'height': 360, 'seed': 0, 'seconds': 2.5, 'frames': 0,
            **inputs}}, '8': {'class_type': 'SaveVideo', 'inputs': {'video': ['7', 0]}}},
            'extra_data': {'extra_pnginfo': {'workflow': {'title': 'unchanged'}}}}

    def expanded_parts(self, payload):
        result = self.registry.expand(payload)
        owner = result['prompt']['7']
        video_id = owner['inputs']['workflow_result'][0]
        video = result['prompt'][video_id]
        return result, owner, video

    def test_compile_namespaces_links_preserves_owner_and_payload(self):
        payload = self.payload()
        original = deepcopy(payload)
        result, owner, video = self.expanded_parts(payload)
        self.assertEqual(payload, original)
        self.assertEqual(owner['class_type'], 'FreeVideoGenerate')
        self.assertEqual(result['prompt']['8']['inputs']['video'], ['7', 0])
        image_id = video['inputs']['images'][0]
        self.assertTrue(image_id.startswith('__freevideo_workflow_'))
        self.assertEqual(result['prompt'][image_id]['inputs'], {'width': 640, 'height': 360, 'batch_size': 60, 'color': 0})
        self.assertEqual(video['inputs']['fps'], 24)
        self.assertEqual(len(owner['inputs']['workflow_run_id']), 32)
        self.assertEqual(result['extra_data'], payload['extra_data'])
        _, second_owner, _ = self.expanded_parts(payload)
        self.assertNotEqual(owner['inputs']['workflow_run_id'], second_owner['inputs']['workflow_run_id'])

    def test_requested_frames_are_arbitrary_counts(self):
        result, _, video = self.expanded_parts(self.payload(frames=17))
        self.assertEqual(result['prompt'][video['inputs']['images'][0]]['inputs']['batch_size'], 17)

    def test_image_binding_keeps_external_namespace(self):
        payload = self.payload(first=['source', 0])
        payload['prompt']['source'] = {'class_type': 'LoadImage', 'inputs': {'image': 'test.png'}}
        _, owner, video = self.expanded_parts(payload)
        self.assertEqual(video['inputs']['images'], ['source', 0])
        self.assertEqual(owner['inputs']['first'], ['source', 0])

    def test_missing_required_and_unsupported_image(self):
        payload = self.payload()
        del payload['prompt']['7']['inputs']['width']
        with self.assertRaisesRegex(WorkflowAdapterError, "requires 'width'"):
            self.registry.expand(payload)
        with self.assertRaisesRegex(WorkflowAdapterError, 'does not support last'):
            self.registry.expand(self.payload(last=['source', 0]))

    def test_required_zero_seed_is_valid(self):
        self.manifest['inputs']['seed'] = {'node': 'image', 'input': 'color', 'required': True}
        self.write()
        result, _, video = self.expanded_parts(self.payload(seed=0))
        self.assertEqual(result['prompt'][video['inputs']['images'][0]]['inputs']['color'], 0)

    def test_rejects_preexpanded_client_fields(self):
        result = self.registry.expand(self.payload())
        with self.assertRaisesRegex(WorkflowAdapterError, 'server-owned'):
            self.registry.expand(result)
        with self.assertRaisesRegex(WorkflowAdapterError, 'server-owned'):
            self.registry.expand(self.payload(backend='vdn', workflow_run_id='spoof'))

    def test_empty_serialized_server_fields_are_allowed(self):
        result, owner, _ = self.expanded_parts(self.payload(workflow_run_id='', workflow_result=None, workflow_error='old error'))
        self.assertEqual(len(owner['inputs']['workflow_run_id']), 32)
        self.assertIsInstance(owner['inputs']['workflow_result'], list)
        self.assertNotIn('workflow_error', owner['inputs'])
        native = self.payload(backend='vdn', workflow_run_id='', workflow_result=None)
        self.assertEqual(self.registry.expand(native), native)

    def test_native_prompt_is_unchanged_and_copied(self):
        payload = self.payload(backend='vdn', frames='')
        result = self.registry.expand(payload)
        self.assertEqual(result, payload)
        self.assertIsNot(result['prompt'], payload['prompt'])

    def test_rejects_unsupported_features_and_links(self):
        for name, value in [('preview', True), ('refine_from', 'clip.mp4'), ('continue_from', 'clip.mp4'),
                            ('references', ['other', 0]), ('media', ['other', 0]),
                            ('conditioning', ['other', 0]), ('hide_tail', True)]:
            with self.subTest(name=name), self.assertRaisesRegex(WorkflowAdapterError, name):
                self.registry.expand(self.payload(**{name: value}))

    def test_provably_empty_native_media_and_lora_stacks(self):
        payload = self.payload(media=['media', 0], loras=['loras', 0])
        payload['prompt'].update({
            'media': {'class_type': 'FreeVideoMedia', 'inputs': {'assets': ' [] ', 'first': None}},
            'loras': {'class_type': 'FreeVideoLoRAStack', 'inputs': {'adapters': '[]', 'previous': ['old', 0]}},
            'old': {'class_type': 'FreeVideoLoRAStack', 'inputs': {'adapters': '[]'}},
        })
        result = self.registry.expand(payload)
        self.assertNotIn('media', result['prompt']['7']['inputs'])
        self.assertNotIn('loras', result['prompt']['7']['inputs'])
        self.assertIn('media', payload['prompt']['7']['inputs'])
        payload['prompt']['media']['inputs']['reference'] = ['other', 0]
        with self.assertRaisesRegex(WorkflowAdapterError, 'media'):
            self.registry.expand(payload)

    def media_payload(self, assets='[]', **connections):
        payload = self.payload(media=['media', 0])
        payload['prompt']['media'] = {'class_type': 'FreeVideoMedia', 'inputs': {'assets': assets, **connections}}
        return payload

    def test_native_connected_keyframes_are_forwarded(self):
        payload = self.media_payload(first=['source', 0])
        payload['prompt']['source'] = {'class_type': 'LoadImage', 'inputs': {'image': 'test.png'}}
        result, owner, video = self.expanded_parts(payload)
        self.assertEqual(video['inputs']['images'], ['source', 0])
        self.assertNotIn('media', owner['inputs'])
        self.assertEqual(owner['inputs']['first'], ['source', 0])
        self.assertIn('media', payload['prompt']['7']['inputs'])

    def test_uploaded_native_keyframes_use_core_loader_and_manifest_bindings(self):
        input_directory = self.root / 'input'
        (input_directory / 'nested').mkdir(parents=True)
        for name in ('first.png', 'last.jpg'):
            (input_directory / 'nested' / name).write_bytes(b'validated by ComfyUI LoadImage')
        self.registry = Registry(self.root, input_directory=input_directory)
        self.graph['last'] = {'class_type': 'SomeImageConsumer', 'inputs': {'image': ['image', 0]}}
        self.manifest['inputs']['last_frame'] = {'node': 'last', 'input': 'image'}
        self.write()
        payload = self.media_payload(json.dumps([
            {'file': 'nested/first.png', 'role': 'first'},
            {'file': 'nested/last.jpg', 'role': 'last', 'enabled': True},
        ]))
        result, owner, video = self.expanded_parts(payload)
        first_id = video['inputs']['images'][0]
        last_id = owner['inputs']['last'][0]
        self.assertEqual(result['prompt'][first_id], {'class_type': 'LoadImage', 'inputs': {'image': 'nested/first.png'}})
        self.assertEqual(result['prompt'][last_id]['inputs']['image'], 'nested/last.jpg')
        last_consumer = next(node for node in result['prompt'].values() if node['class_type'] == 'SomeImageConsumer')
        self.assertEqual(last_consumer['inputs']['image'], [last_id, 0])
        self.assertNotIn('media', owner['inputs'])

    def test_disabled_native_rows_are_empty_without_file_access(self):
        payload = self.media_payload(json.dumps([
            {'file': 'missing.mp4', 'role': 'reference', 'enabled': False, 'cast': {'id': 'person'}},
        ]), first=None, reference_audio=None)
        _, owner, _ = self.expanded_parts(payload)
        self.assertNotIn('media', owner['inputs'])

    def test_native_media_rejects_unsupported_conditioning_and_bad_rows(self):
        for rows in ([{'file': 'test.png', 'role': 'reference'}],
                     [{'file': 'test.png', 'role': 'first', 'early_steps': 2}],
                     [{'file': 'test.png', 'role': 'first', 'enabled': 0}],
                     [{'enabled': False, 'unexpected': True}], {}, [None]):
            with self.subTest(rows=rows), self.assertRaisesRegex(WorkflowAdapterError, 'media'):
                self.registry.expand(self.media_payload(json.dumps(rows)))
        for name in ('reference', 'reference_8', 'reference_audio', 'unknown'):
            with self.subTest(name=name), self.assertRaisesRegex(WorkflowAdapterError, 'media|does not support'):
                self.registry.expand(self.media_payload(**{name: ['source', 0]}))
        with self.assertRaisesRegex(WorkflowAdapterError, 'input directory'):
            self.registry.expand(self.media_payload('[{"file":"test.png","role":"first"}]'))

    def test_native_media_rejects_unsafe_paths_and_duplicate_frames(self):
        input_directory = self.root / 'input'
        input_directory.mkdir()
        (input_directory / 'test.png').write_bytes(b'image')
        self.registry = Registry(self.root, input_directory=input_directory)
        for name in ('../outside.png', '/outside.png', 'C:/outside.png', 'sub\\test.png',
                     'test.png [output]', 'movie.mp4', 'missing.png'):
            with self.subTest(name=name), self.assertRaisesRegex(WorkflowAdapterError, 'media'):
                self.registry.expand(self.media_payload(json.dumps([{'file': name, 'role': 'first'}])))
        row = {'file': 'test.png', 'role': 'first'}
        for payload in (self.media_payload(json.dumps([row, row])),
                        self.media_payload(json.dumps([row]), first=['source', 0])):
            with self.assertRaisesRegex(WorkflowAdapterError, 'duplicate'):
                self.registry.expand(payload)
        payload = self.media_payload(first=['source', 0])
        payload['prompt']['7']['inputs']['first'] = ['other', 0]
        with self.assertRaisesRegex(WorkflowAdapterError, 'duplicate'):
            self.registry.expand(payload)

    def test_native_media_requires_supported_frame_binding(self):
        payload = self.media_payload(last=['source', 0])
        payload['prompt']['source'] = {'class_type': 'LoadImage', 'inputs': {'image': 'test.png'}}
        with self.assertRaisesRegex(WorkflowAdapterError, 'does not support last'):
            self.registry.expand(payload)

    def test_ui_empty_frame_overrides_follow_duration(self):
        for frames in ('', None):
            with self.subTest(frames=frames):
                result, _, video = self.expanded_parts(self.payload(frames=frames))
                self.assertEqual(result['prompt'][video['inputs']['images'][0]]['inputs']['batch_size'], 60)

    def test_invalid_ids_and_unregistered_workflows(self):
        for value in ['../test-video', '/test-video', 'a/b', 'a\\b', '.', '', None]:
            with self.subTest(value=value), self.assertRaises(WorkflowAdapterError):
                self.registry.expand(self.payload(workflow_id=value))
        with self.assertRaisesRegex(WorkflowAdapterError, 'not registered'):
            self.registry.expand(self.payload(workflow_id='missing'))

    def test_invalid_api_graphs(self):
        for invalid in [[], {'nodes': []}, {'1': {'class_type': 'EmptyImage'}},
                        {'1': {'class_type': 'FreeVideoGenerate', 'inputs': {}}},
                        {'1': {'class_type': 'CreateVideo', 'inputs': {'images': ['missing', 0]}}},
                        {'1': {'class_type': 'CreateVideo', 'inputs': {'images': ['1', -1]}}},
                        {'1': {'class_type': 'CreateVideo', 'inputs': {'images': ['1', 0]}}}]:
            self.graph = invalid
            self.write()
            with self.subTest(graph=invalid), self.assertRaises(WorkflowAdapterError):
                self.registry.validate('test-video')

    def test_invalid_manifest_bindings_and_output(self):
        original = deepcopy(self.manifest)
        invalids = [dict(original, version=2), dict(original, id='../escape'),
                    dict(original, inputs={'unknown': {'node': 'image', 'input': 'width'}}),
                    dict(original, inputs={'width': {'node': 'missing', 'input': 'width'}}),
                    dict(original, outputs={'video': {'node': 'video', 'output': 0, 'type': 'IMAGE'}}),
                    dict(original, outputs={'video': {'node': 'video', 'output': True}})]
        for invalid in invalids:
            self.manifest = invalid
            self.write()
            with self.subTest(manifest=invalid), self.assertRaises(WorkflowAdapterError):
                self.registry.validate('test-video')

    def test_numeric_inputs_are_validated(self):
        for inputs in [{'seconds': float('nan')}, {'seconds': 0}, {'seconds': float('inf')},
                       {'frames': -1}, {'frames': ['source', 0]}, {'width': True}, {'seed': -1}]:
            with self.subTest(inputs=inputs), self.assertRaises(WorkflowAdapterError):
                self.registry.expand(self.payload(**inputs))

    def test_catalog_has_only_public_metadata(self):
        catalog = self.registry.catalog()
        self.assertEqual(catalog[0]['id'], 'test-video')
        self.assertEqual(catalog[0]['capabilities']['single_shot'], True)
        self.assertEqual(catalog[0]['capabilities']['continuation'], False)
        self.assertNotIn('graph', catalog[0])
        self.assertEqual(Registry(self.root / 'missing').catalog(), [])

    def profile(self):
        self.graph['settings'] = {'class_type': 'TestSettings', 'inputs': {
            'steps': 10, 'steps2': 10, 'gain': 1.0, 'enabled': True, 'text': 'old', 'mode': 'fast',
            'lora_name': 'fixed.safetensors', 'strength': 0.75, 'strength_clip': 0.75, 'lora_enabled': True}}
        fields = [('steps', 'integer', 12, {'min': 1, 'max': 40, 'step': 1}),
                  ('gain', 'number', 1.5, {'min': 0, 'max': 3}),
                  ('enabled', 'boolean', False, {}), ('text', 'string', 'default', {}),
                  ('mode', 'choice', 'slow', {'options': ['fast', 'slow']})]
        self.manifest['profile'] = {'advanced': [
            {'id': key, 'label': key.title(), 'type': kind, 'default': default, **extra,
             'bindings': [{'node': 'settings', 'input': key}]} for key, kind, default, extra in fields],
            'loras': [{'id': 'style', 'label': 'Fixed style', 'min': -2, 'max': 2,
                       'bindings': {'name': [{'node': 'settings', 'input': 'lora_name'}],
                                    'strength': [{'node': 'settings', 'input': 'strength'},
                                                 {'node': 'settings', 'input': 'strength_clip'}],
                                    'enabled': [{'node': 'settings', 'input': 'lora_enabled'}]}}]}
        self.write()

    def settings_node(self, payload):
        return next(node['inputs'] for node in self.registry.expand(payload)['prompt'].values()
                    if node['class_type'] == 'TestSettings')

    def test_profile_defaults_and_public_catalog(self):
        self.profile()
        original = deepcopy(self.graph)
        values = self.settings_node(self.payload())
        self.assertEqual((values['steps'], values['gain'], values['enabled'], values['text'], values['mode']),
                         (12, 1.5, False, 'default', 'slow'))
        self.assertEqual(values['strength'], 0.75)
        metadata = self.registry.validate('test-video')['profile']
        self.assertEqual(metadata['loras'][0]['name'], 'fixed.safetensors')
        self.assertEqual(metadata['loras'][0]['strength'], 0.75)
        self.assertNotIn('bindings', json.dumps(metadata))
        self.assertEqual(self.graph, original)

    def test_profile_overrides_multibind_and_disabled_lora(self):
        self.profile()
        self.manifest['profile']['advanced'][0]['bindings'].append({'node': 'settings', 'input': 'steps2'})
        self.write()
        raw = json.dumps({'test-video': {'advanced': {'steps': 21},
                                       'loras': {'style': {'strength': 1.25, 'enabled': False}}}})
        values = self.settings_node(self.payload(workflow_settings=raw))
        self.assertEqual((values['steps'], values['steps2']), (21, 21))
        self.assertEqual((values['strength'], values['strength_clip'], values['lora_enabled']), (0, 0, False))
        self.assertEqual(values['lora_name'], 'fixed.safetensors')

    def test_profile_rejects_unknown_ids_types_ranges_and_graph_tampering(self):
        self.profile()
        invalids = [{'advanced': {'unknown': 1}}, {'advanced': {'steps': True}},
                    {'advanced': {'steps': 1.2}}, {'advanced': {'steps': 41}},
                    {'advanced': {'gain': float('nan')}}, {'advanced': {'enabled': 0}},
                    {'advanced': {'mode': 'unknown'}}, {'advanced': {'text': ['node', 0]}},
                    {'loras': {'unknown': {}}}, {'loras': {'style': {'strength': 3}}},
                    {'loras': {'style': {'enabled': 1}}}, {'loras': {'style': {'name': 'evil.safetensors'}}},
                    {'graph': {}}, {'advanced': []}]
        for value in invalids:
            with self.subTest(value=value), self.assertRaises(WorkflowAdapterError):
                self.registry.expand(self.payload(workflow_settings=json.dumps({'test-video': value})))
        for raw in ('[]', '{', {}, 'null'):
            with self.subTest(raw=raw), self.assertRaises(WorkflowAdapterError):
                self.registry.expand(self.payload(workflow_settings=raw))

    def test_profile_other_workflow_settings_are_isolated_and_native_stack_detached(self):
        self.profile()
        raw = json.dumps({'another-workflow': {'advanced': {'steps': 999}},
                          'test-video': {'advanced': {'steps': 19}}})
        payload = self.payload(workflow_settings=raw, loras=['old', 0])
        payload['prompt']['old'] = {'class_type': 'FreeVideoLoRAStack', 'inputs': {'adapters': '[{"name":"stale"}]'}}
        result = self.registry.expand(payload)
        self.assertNotIn('loras', result['prompt']['7']['inputs'])
        self.assertEqual(next(node['inputs']['steps'] for node in result['prompt'].values()
                              if node['class_type'] == 'TestSettings'), 19)
        self.assertEqual(self.settings_node(self.payload())['steps'], 12)

    def test_profile_manifest_rejects_conflicts_invalid_defaults_and_lora_links(self):
        self.profile()
        original = deepcopy(self.manifest)
        mutations = [lambda p: p['advanced'][0].update(default=50),
                     lambda p: p['advanced'][0].update(min=True),
                     lambda p: p['advanced'][0].update(step=0),
                     lambda p: p['advanced'][0].update(bindings=[{'node': 'image', 'input': 'width'}]),
                     lambda p: p['advanced'].append(deepcopy(p['advanced'][0])),
                     lambda p: p['loras'][0]['bindings'].update(name=[{'node': 'video', 'input': 'images'}])]
        for mutate in mutations:
            self.manifest = deepcopy(original)
            mutate(self.manifest['profile'])
            self.write()
            with self.subTest(manifest=self.manifest), self.assertRaises(WorkflowAdapterError):
                self.registry.validate('test-video')

    def test_canonical_multibind(self):
        self.graph['second'] = deepcopy(self.graph['image'])
        self.manifest['inputs']['width'] = {'required': True, 'bindings': [
            {'node': 'image', 'input': 'width'}, {'node': 'second', 'input': 'width'}]}
        self.write()
        result = self.registry.expand(self.payload(width=768))
        self.assertEqual([node['inputs']['width'] for node in result['prompt'].values()
                          if node['class_type'] == 'EmptyImage'], [768, 768])

    def reference_bindings(self):
        self.graph['references'] = {'class_type': 'TestReferences', 'inputs': {
            'images': ['image', 0], 'audio': 'template', 'video': 'template'}}
        self.manifest['inputs'].update({name: {'node': 'references', 'input': target}
                                      for name, target in [('reference_images', 'images'),
                                                           ('reference_audio', 'audio'), ('reference_video', 'video')]})
        self.write()

    def test_native_uploaded_references_and_connected_image_batch(self):
        self.reference_bindings()
        input_directory = self.root / 'input'
        input_directory.mkdir()
        for name in ('a.png', 'b.jpg', 'voice.wav', 'clip.mp4'):
            (input_directory / name).write_bytes(b'validated by core Comfy loaders')
        self.registry = Registry(self.root, input_directory=input_directory)
        rows = [{'file': name, 'role': 'reference'} for name in ('a.png', 'b.jpg', 'voice.wav', 'clip.mp4')]
        payload = self.media_payload(json.dumps(rows), reference=['source', 0])
        payload['prompt']['source'] = {'class_type': 'LoadImage', 'inputs': {'image': 'existing.png'}}
        result = self.registry.expand(payload)
        refs = next(node['inputs'] for node in result['prompt'].values() if node['class_type'] == 'TestReferences')
        self.assertEqual(result['prompt'][refs['audio'][0]], {'class_type': 'LoadAudio', 'inputs': {'audio': 'voice.wav'}})
        self.assertEqual(result['prompt'][refs['video'][0]], {'class_type': 'LoadVideo', 'inputs': {'file': 'clip.mp4'}})
        batches = [node for node in result['prompt'].values() if node['class_type'] == 'ImageBatch']
        self.assertEqual(len(batches), 2)
        self.assertEqual(result['prompt'][refs['images'][0]]['inputs']['image2'], ['source', 0])
        self.assertNotIn('media', result['prompt']['7']['inputs'])

    def test_references_require_bindings_and_reject_duplicate_audio_video(self):
        self.reference_bindings()
        input_directory = self.root / 'input'
        input_directory.mkdir()
        self.registry = Registry(self.root, input_directory=input_directory)
        for name in ('voice.wav', 'clip.mp4'):
            (input_directory / name).write_bytes(b'media')
            row = {'file': name, 'role': 'reference'}
            with self.subTest(name=name), self.assertRaisesRegex(WorkflowAdapterError, 'duplicate'):
                self.registry.expand(self.media_payload(json.dumps([row, row])))
        del self.manifest['inputs']['reference_audio']
        self.write()
        payload = self.media_payload(reference_audio=['source', 0])
        payload['prompt']['source'] = {'class_type': 'TestAudio', 'inputs': {}}
        with self.assertRaisesRegex(WorkflowAdapterError, 'does not support reference_audio'):
            self.registry.expand(payload)

    def test_example_is_valid_and_compiles(self):
        registry = Registry(Path(__file__).resolve().parents[1] / 'example_workflows' / 'adapters')
        for workflow_id in ('empty-video', 'synthetic-scaled-video'):
            with self.subTest(workflow_id=workflow_id):
                metadata = registry.validate(workflow_id)
                self.assertEqual(metadata['id'], workflow_id)
                self.assertTrue(metadata['profile']['advanced'])
                self.assertEqual(metadata['profile']['loras'], [])
                result = registry.expand(self.payload(workflow_id=workflow_id))
                self.assertEqual(result['prompt']['7']['class_type'], 'FreeVideoGenerate')


if __name__ == '__main__':
    unittest.main()
