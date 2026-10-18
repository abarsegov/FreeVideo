"""Pure compiler tests: no ComfyUI, Torch, models or server required."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from freevideo_engine.workflow_adapter import Registry, WorkflowAdapterError


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
        result, owner, _ = self.expanded_parts(self.payload(workflow_run_id='', workflow_result=None))
        self.assertEqual(len(owner['inputs']['workflow_run_id']), 32)
        self.assertIsInstance(owner['inputs']['workflow_result'], list)
        native = self.payload(backend='vdn', workflow_run_id='', workflow_result=None)
        self.assertEqual(self.registry.expand(native), native)

    def test_native_prompt_is_unchanged_and_copied(self):
        payload = self.payload(backend='vdn', frames='')
        result = self.registry.expand(payload)
        self.assertEqual(result, payload)
        self.assertIsNot(result['prompt'], payload['prompt'])

    def test_rejects_unsupported_features_and_links(self):
        for name, value in [('preview', True), ('refine_from', 'clip.mp4'), ('continue_from', 'clip.mp4'),
                            ('references', ['other', 0]), ('media', ['other', 0]), ('loras', ['other', 0]),
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
        payload['prompt']['media']['inputs']['first'] = ['other', 0]
        with self.assertRaisesRegex(WorkflowAdapterError, 'media'):
            self.registry.expand(payload)

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

    def test_example_is_valid_and_compiles(self):
        registry = Registry(Path(__file__).resolve().parents[1] / 'example_workflows' / 'adapters')
        self.assertEqual(registry.validate('empty-video')['id'], 'empty-video')
        payload = self.payload(workflow_id='empty-video')
        result = registry.expand(payload)
        self.assertEqual(result['prompt']['7']['class_type'], 'FreeVideoGenerate')


if __name__ == '__main__':
    unittest.main()
