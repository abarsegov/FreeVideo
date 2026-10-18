"""Workflow finalization and dispatch contracts, without ComfyUI or a GPU."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from freevideo_engine import comfy_workflows


class Video:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def save_to(self, path, format='auto', codec='auto', metadata=None):
        self.calls.append(dict(path=path, format=format, codec=codec, metadata=metadata))
        Path(path).write_bytes(b'stub video')
        if self.fail:
            raise RuntimeError('save interrupted')

    def get_stream_source(self):
        raise AssertionError('must preserve VIDEO trims and audio through save_to')


def api_stubs():
    class Port:
        def __init__(self, name=None, **kwargs):
            self.id = name
            self.options = kwargs

    class Type:
        Input = Output = Port

    class Output:
        def __init__(self, *args, ui=None):
            self.result, self.ui = args, ui

    io = SimpleNamespace(ComfyNode=type('Node', (), {}), Schema=lambda **kwargs: SimpleNamespace(**kwargs),
                         Custom=lambda name: Type, Hidden=SimpleNamespace(unique_id='unique_id', prompt='prompt',
                                                                         extra_pnginfo='extra_pnginfo'),
                         NodeOutput=Output)
    for name in ('String', 'Int', 'Float', 'Image', 'Video', 'Conditioning', 'Boolean', 'Combo', 'Audio'):
        setattr(io, name, Type)
    api = ModuleType('comfy_api.latest')
    api.ComfyExtension, api.io = type('Extension', (), {}), io
    api.InputImpl = SimpleNamespace(VideoFromFile=lambda path: SimpleNamespace(path=path))
    api.ui = SimpleNamespace(PreviewVideo=lambda value: SimpleNamespace(as_dict=lambda: {'videos': value}))
    return {'comfy_api': ModuleType('comfy_api'), 'comfy_api.latest': api}


class WorkflowTests(unittest.TestCase):
    def save(self, video, root, **kwargs):
        return comfy_workflows.save_result(video, output_directory=root, workflow_id='test',
                                          workflow_run_id='nonce', text='prompt', width=640,
                                          height=480, seconds=3., seed=42, **kwargs)

    def test_save_report_uses_full_execution_measurement_after_saving(self):
        now = [10.]
        timing = Mock(side_effect=lambda: dict(request_seconds=now[0], sample_seconds=4.,
                                               cached_nodes=2, timing_scope='execution'))
        class TimedVideo(Video):
            def save_to(self, *args, **kwargs):
                super().save_to(*args, **kwargs)
                now[0] += 7.
        with tempfile.TemporaryDirectory() as root, patch.object(comfy_workflows, '_probe',
                return_value=({'width': 320}, {'present': False})):
            output, serialized, report = self.save(TimedVideo(), root, timing_source=timing)
            self.assertEqual(report['request_seconds'], 17.)
            self.assertEqual(report['video']['sample_seconds'], 4.)
            self.assertEqual(report['workflow']['cached_nodes'], 2)
            self.assertEqual(report['workflow']['timing_scope'], 'execution')
            self.assertEqual(json.loads(output.with_suffix('.request.json').read_text())['request_seconds'], 17.)
            self.assertEqual(json.loads(serialized)['request_seconds'], 17.)
        timing.assert_called_once_with()

    def test_save_report_does_not_claim_execution_timing_without_observer(self):
        with tempfile.TemporaryDirectory() as root, patch.object(comfy_workflows, '_probe',
                return_value=({'width': 320}, {'present': False})):
            _, _, report = self.save(Video(), root, timing_source=lambda: None)
            self.assertEqual(report['workflow']['timing_scope'], 'save')
            self.assertIsNone(report['request_seconds'])
            self.assertGreaterEqual(report['workflow']['save_seconds'], 0.)
            self.assertNotIn('sample_seconds', report['video'])

    def test_save_preserves_video_api_and_writes_library_report(self):
        video = Video()
        actual = dict(width=320, height=240, frames=48, fps=24., seconds=2.)
        metadata = {'workflow': {'nodes': []}, 'prompt': {'1': {}}}
        with tempfile.TemporaryDirectory() as root, patch.object(comfy_workflows, '_probe', return_value=(actual, {'present': True})):
            output, serialized, report = self.save(video, root, metadata=metadata)
            self.assertRegex(output.relative_to(root).as_posix(), r'^FreeVideo/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}/video.mp4$')
            self.assertEqual(video.calls, [dict(path=str(output), format='auto', codec='auto', metadata=metadata)])
            self.assertEqual(report['geometry'], actual)
            self.assertEqual(report['parameters']['width'], 640)
            self.assertEqual(report['audio'], {'present': True})
            self.assertEqual(report['workflow']['run_id'], 'nonce')
            self.assertEqual(json.loads(output.with_suffix('.request.json').read_text(encoding='utf-8')), report)
            self.assertEqual(json.loads(serialized), report)
            self.assertEqual(json.loads((output.parent / 'workflow.json').read_text()), metadata)
            self.assertTrue(report['success'])

    def test_save_failure_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, 'save interrupted'):
                self.save(Video(fail=True), root)
            self.assertEqual(list(Path(root).rglob('video.mp4')), [])
            self.assertEqual(list(Path(root).rglob('*.request.json')), [])

    def test_saved_report_works_with_real_output_summary_and_library(self):
        from freevideo_engine.comfy_assets import output_summary
        from freevideo_engine.comfy_library import list_videos
        actual = dict(width=320, height=240, frames=48, fps=24., seconds=2.)
        with tempfile.TemporaryDirectory() as root, patch.object(comfy_workflows, '_probe', return_value=(actual, {'present': True})):
            output, _, report = self.save(Video(), root)
            relative = output.relative_to(root)
            summary = output_summary(report, relative)
            self.assertEqual(summary['geometry'], actual)
            self.assertEqual(summary['report'], relative.with_suffix('.request.json').as_posix())
            rows = list_videos(root)['items']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['video'], relative.as_posix())
            self.assertEqual(rows[0]['geometry'], actual)

    def test_probe_failure_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as root, patch.object(comfy_workflows, '_probe', side_effect=ValueError('no video')):
            with self.assertRaisesRegex(ValueError, 'no video'):
                self.save(Video(), root)
            self.assertEqual(list(Path(root).rglob('video.mp4')), [])

    def test_missing_video_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'Workflow result is missing'):
                self.save(None, root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_stream_headers_are_used_without_decoding(self):
        video = SimpleNamespace(width=320, height=240, average_rate=24, frames=48, duration=48, time_base=1 / 24)
        audio = SimpleNamespace(sample_rate=48000, layout=SimpleNamespace(channels=['L', 'R']))
        container = SimpleNamespace(streams=SimpleNamespace(video=[video], audio=[audio]), duration=None)
        context = Mock()
        context.__enter__ = Mock(return_value=container)
        context.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {'av': SimpleNamespace(open=lambda path: context)}):
            geometry, sound = comfy_workflows._probe('video.mp4')
        self.assertEqual(geometry, dict(width=320, height=240, frames=48, fps=24., seconds=2.))
        self.assertEqual(sound, dict(present=True, sample_rate=48000, channels=2))

    def test_finalize_publishes_preview_and_completed_result(self):
        modules = api_stubs()
        server = SimpleNamespace()
        modules['server'] = SimpleNamespace(PromptServer=SimpleNamespace(instance=server))
        progress = Mock()
        modules['freevideo_engine.comfy_progress'] = SimpleNamespace(publish=progress)
        # The actual output_summary contract is exercised separately below.
        modules['freevideo_engine.comfy_assets'] = SimpleNamespace(
            output_summary=lambda report, relative: dict(geometry=report['geometry'], video=relative.as_posix()),
            saved_video=lambda output, root: {'filename': output.name})
        hidden = SimpleNamespace(unique_id='node', extra_pnginfo={}, prompt={})
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules, modules), \
                patch.object(comfy_workflows, 'registry', return_value=SimpleNamespace(validate=lambda name: {'title': 'Test'})), \
                patch.object(comfy_workflows, '_probe', return_value=({'width': 320}, {'present': False})):
            modules['folder_paths'] = SimpleNamespace(get_output_directory=lambda: root)
            with patch.dict(sys.modules, {'folder_paths': modules['folder_paths']}):
                result = comfy_workflows.finalize(Video(), workflow_id='test', text='prompt', width=640,
                                                 height=480, seconds=3, seed=42, frames=0, hidden=hidden)
            self.assertTrue(Path(result.result[0].path).is_file())
            summary = result.ui['freevideo_summary'][0]
            self.assertEqual(summary['backend'], 'workflow')
            self.assertEqual(progress.call_args.args[2]['result'], summary)
            self.assertEqual(progress.call_args.args[2]['phase'], 'complete')

    def test_finalize_missing_video_fails_closed_and_publishes_failure(self):
        modules = api_stubs()
        modules.update(server=SimpleNamespace(PromptServer=SimpleNamespace(instance=None)),
                       folder_paths=SimpleNamespace(get_output_directory=Mock()))
        publish = Mock()
        modules['freevideo_engine.comfy_progress'] = SimpleNamespace(publish=publish)
        modules['freevideo_engine.comfy_assets'] = SimpleNamespace(output_summary=Mock(), saved_video=Mock())
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(ValueError, 'Workflow result is missing'):
                comfy_workflows.finalize(None, workflow_id='test', text='text', width=320, height=240,
                                         seconds=3, seed=1, frames=0, hidden=SimpleNamespace(unique_id='node'))
        self.assertEqual(publish.call_args.args[2]['phase'], 'failed')
        modules['folder_paths'].get_output_directory.assert_not_called()

    def test_prepare_releases_resources_and_only_stops_idle_services(self):
        actions = Mock()
        owner = SimpleNamespace(mutex=threading.RLock(), active=False, release=actions.release)
        server = SimpleNamespace(_freevideo_prompt_vlm=SimpleNamespace(stop_prestart=actions.prompt_stop),
                                 _freevideo_image_gen=SimpleNamespace(stop_for_generation=actions.image_stop))
        modules = {'server': SimpleNamespace(PromptServer=SimpleNamespace(instance=server)),
                   'freevideo_engine.background_compile': SimpleNamespace(COMPILE=SimpleNamespace(stop=actions.compile_stop)),
                   'freevideo_engine.encoder_prewarm': SimpleNamespace(IDLE=SimpleNamespace(stop=actions.idle_stop)),
                   'freevideo_engine.resident_process': SimpleNamespace(OWNER=owner)}
        with patch.dict(sys.modules, modules):
            comfy_workflows.prepare()
        self.assertEqual([call[0] for call in actions.mock_calls],
                         ['compile_stop', 'idle_stop', 'release', 'prompt_stop', 'image_stop'])
        actions.compile_stop.assert_called_once_with(final=True)
        actions.release.assert_called_once_with('workflow backend')
        actions.prompt_stop.assert_called_once_with('workflow backend', wait=True)
        actions.image_stop.assert_called_once_with(only_idle=True)

    def test_prepare_failure_does_not_release_active_generation(self):
        owner = SimpleNamespace(mutex=threading.RLock(), active=True, release=Mock())
        modules = {'server': SimpleNamespace(PromptServer=SimpleNamespace(instance=None)),
                   'freevideo_engine.background_compile': SimpleNamespace(COMPILE=SimpleNamespace(stop=Mock())),
                   'freevideo_engine.encoder_prewarm': SimpleNamespace(IDLE=SimpleNamespace(stop=Mock())),
                   'freevideo_engine.resident_process': SimpleNamespace(OWNER=owner)}
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'still generating'):
                comfy_workflows.prepare()
        owner.release.assert_not_called()


class NodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = api_stubs()
        cls.modules['freevideo_engine.comfy_bridge'] = Mock()
        # Real Comfy node import needs only these CPU helpers; prevent optional
        # VDN dependencies from becoming requirements of this isolated test.
        cls.modules['freevideo_engine.comfy_continuation'] = SimpleNamespace(
            node_frames=lambda value: value if type(value) is int else 0,
            optional_text=lambda value: value or '', part_steps=Mock(), request_options=Mock())
        with patch.dict(sys.modules, cls.modules):
            sys.modules.pop('freevideo_engine.comfy_nodes', None)
            cls.nodes = importlib.import_module('freevideo_engine.comfy_nodes')

    def test_workflow_validation_skips_vdn(self):
        selected = Mock(return_value={'id': 'test'})
        with patch.object(comfy_workflows, 'registry', return_value=SimpleNamespace(validate=selected)):
            self.assertIs(self.nodes.FreeVideoGenerate.validate_inputs('text', 321, 123, .1, 1,
                                                                       backend='workflow', workflow_id='test'), True)
        selected.assert_called_once_with('test')

    def test_unknown_backend_rejected_before_vdn(self):
        self.assertIn('Unknown', self.nodes.FreeVideoGenerate.validate_inputs('text', 320, 240, 3, 1, backend='bad'))
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.nodes.FreeVideoGenerate.execute('text', 320, 240, 3, 1, backend='bad')

    def test_workflow_execute_dispatches_before_vdn_imports(self):
        self.nodes.FreeVideoGenerate.hidden = SimpleNamespace(unique_id='node')
        with patch.object(comfy_workflows, 'finalize', return_value='done') as finalize:
            self.assertEqual(self.nodes.FreeVideoGenerate.execute('text', 320, 240, 3, 1,
                                                                 backend='workflow', workflow_id='test',
                                                                 workflow_result='VIDEO'), 'done')
        self.assertEqual(finalize.call_args.args, ('VIDEO',))

    def test_prompt_cleanup_expands_and_preserves_errors(self):
        payload = {'prompt': {'1': {'class_type': 'FreeVideoGenerate', 'inputs': {'frames': ''}}}}
        expand = Mock(side_effect=ValueError('adapter binding is missing'))
        with patch.object(comfy_workflows, 'registry', return_value=SimpleNamespace(expand=expand)):
            self.nodes.clean_prompt(payload)
        self.assertEqual(payload['prompt']['1']['inputs']['frames'], 0)

    def test_workflow_frames_skip_vdn_frame_grid_cleanup(self):
        payload = {'prompt': {'1': {'class_type': 'FreeVideoGenerate',
                                   'inputs': {'backend': 'workflow', 'frames': 24}}}}
        with patch.object(self.nodes, 'node_frames', return_value=0) as normalize, \
                patch.object(comfy_workflows, 'registry', return_value=SimpleNamespace(expand=lambda value: value)):
            result = self.nodes.clean_prompt(payload)
        normalize.assert_not_called()
        self.assertEqual(result['prompt']['1']['inputs']['frames'], 24)

    def test_lazy_workflow_prepares_before_requesting_video_dependency(self):
        with patch.object(comfy_workflows, 'prepare') as prepare:
            self.assertEqual(self.nodes.FreeVideoGenerate.check_lazy_status(backend='workflow', workflow_run_id='server-id'), ['workflow_result'])
            prepare.assert_called_once_with()
            prepare.reset_mock()
            self.assertEqual(self.nodes.FreeVideoGenerate.check_lazy_status(backend='workflow', workflow_result='VIDEO'), [])
            self.assertEqual(self.nodes.FreeVideoGenerate.check_lazy_status(), [])
            prepare.assert_not_called()

    def test_lazy_prepare_failure_does_not_request_dependency(self):
        with patch.object(comfy_workflows, 'prepare', side_effect=RuntimeError('worker did not release')):
            with self.assertRaisesRegex(RuntimeError, 'worker did not release'):
                self.nodes.FreeVideoGenerate.check_lazy_status(backend='workflow', workflow_run_id='server-id')

    def test_unexpanded_workflow_fails_before_requesting_missing_link(self):
        with patch.object(comfy_workflows, 'prepare') as prepare:
            with self.assertRaisesRegex(ValueError, 'not expanded'):
                self.nodes.FreeVideoGenerate.check_lazy_status(backend='workflow')
            prepare.assert_not_called()

    def test_workflow_hook_failure_is_a_queue_validation_error(self):
        payload = {'prompt': {'1': {'class_type': 'FreeVideoGenerate', 'inputs': {'backend': 'workflow', 'frames': ''}}}}
        with patch.object(comfy_workflows, 'registry', return_value=SimpleNamespace(expand=Mock(side_effect=ValueError('Unsupported media')))):
            result = self.nodes.clean_prompt(payload)
        self.assertEqual(result['prompt']['1']['inputs']['workflow_error'], 'Unsupported media')
        self.assertEqual(result['prompt']['1']['inputs']['frames'], 0)
        self.assertEqual(self.nodes.FreeVideoGenerate.validate_inputs(text='', width=256, height=256,
            seconds=2, seed=1, backend='workflow', workflow_error='Unsupported media'), 'Unsupported media')

    def test_backend_schema_appended_after_existing_widgets(self):
        schema = self.nodes.FreeVideoGenerate.define_schema()
        names = [row.id for row in schema.inputs]
        self.assertEqual(names[-6:], ['backend', 'workflow_id', 'workflow_result', 'workflow_run_id', 'workflow_error', 'workflow_settings'])
        self.assertEqual(schema.inputs[-6].options['default'], 'vdn')
        self.assertTrue(schema.inputs[-4].options['lazy'])

    def test_h3_reference_batch_preserves_all_pictures_and_empty_sockets(self):
        result = self.nodes.FreeVideoH3ReferenceImages.execute(['picture-1', 'picture-2']).result
        self.assertEqual(result[:2], (['picture-1'], ['picture-2']))
        self.assertEqual(result[2:], (None,) * 7)
        self.assertEqual(len(self.nodes.FreeVideoH3ReferenceImages.define_schema().outputs), 9)
        for batch in ([], list(range(10))):
            with self.assertRaisesRegex(ValueError, '1 and 9'):
                self.nodes.FreeVideoH3ReferenceImages.execute(batch)


if __name__ == '__main__':
    unittest.main()
