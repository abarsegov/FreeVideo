"""Native workflow progress uses measured events, with no GPU or Comfy install."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from freevideo_engine.workflow_progress import WorkflowProgress, _presentation, register


def graph(run='run', owner='owner'):
    prefix = f'__freevideo_workflow_{run}_'
    nodes = {}
    previous = None
    for key, kind in [('model', 'CheckpointLoaderSimple'), ('text', 'CLIPTextEncode'),
                      ('sample', 'KSampler'), ('decode', 'VAEDecode'), ('audio', 'AudioVAEDecode')]:
        inputs = {'source': [previous, 0]} if previous else {}
        previous = prefix + key
        nodes[previous] = dict(class_type=kind, inputs=inputs)
    nodes[owner] = dict(class_type='FreeVideoGenerate', inputs=dict(backend='workflow',
        workflow_id='test', workflow_run_id=run, workflow_result=[previous, 0]))
    return nodes


class Clock:
    now = 10.

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.rows = [(0, 'prompt', graph(), {'client_id': 'client'}, ['owner'])]
        self.server = SimpleNamespace(prompt_queue=SimpleNamespace(get_current_queue=lambda: (self.rows, [])))
        self.bridge = WorkflowProgress(self.server, clock=self.clock,
            publish=lambda server, owner, value: self.events.append((owner, value)))

    def event(self, event, **data):
        self.bridge.observe(event, dict(prompt_id='prompt', **data), 'client')

    def execute(self, node):
        self.event('executing', node=f'__freevideo_workflow_run_{node}')

    def test_complete_lifecycle_keeps_local_progress_separate_from_graph(self):
        self.event('execution_start')
        self.assertTrue(self.events[-1][1]['new_request'])
        self.assertEqual(self.events[-1][1]['workflow']['nodes_total'], 5)
        self.clock.now = 12
        self.execute('model')
        self.assertEqual(self.events[-1][1]['stage'], 'model_load')
        self.clock.now = 16
        self.execute('text')
        self.clock.now = 18
        self.execute('sample')
        self.clock.now = 28
        self.event('progress', node='__freevideo_workflow_run_sample', value=20, max=20)
        message = self.events[-1][1]
        self.assertEqual((message['done'], message['total']), (20, 20))
        self.assertEqual(message['workflow']['nodes_done'], 2)
        self.assertNotIn('fraction', message['overall'])
        self.assertEqual(message['overall']['status'], 'running')
        self.clock.now = 30
        self.execute('decode')
        self.assertEqual(self.events[-1][1]['stage'], 'video_decode')
        self.assertNotIn('done', self.events[-1][1])
        self.clock.now = 32
        self.execute('audio')
        self.assertEqual(self.events[-1][1]['stage'], 'audio_decode')
        self.clock.now = 34
        self.bridge.begin_save('run')
        self.assertEqual(self.events[-1][1]['stage'], 'saving')
        self.clock.now = 37
        self.assertEqual(self.bridge.timing('run')['request_seconds'], 27)
        self.assertEqual(self.bridge.timing('run')['sample_seconds'], 12)
        self.bridge.finish('run')
        self.clock.now = 100
        self.event('execution_success')
        self.assertEqual(self.bridge.timing('run')['request_seconds'], 27)
        self.assertTrue(all(owner == 'owner' for owner, _ in self.events))

    def test_queue_time_is_excluded_and_cached_nodes_are_counted(self):
        self.clock.now = 1000
        self.assertIsNone(self.bridge.timing('run'))
        self.event('execution_start')
        self.clock.now = 1003
        self.event('execution_cached', nodes=['__freevideo_workflow_run_model',
                                             '__freevideo_workflow_run_text', 'unrelated'])
        message = self.events[-1][1]
        self.assertEqual(message['workflow']['cached_nodes'], 2)
        self.assertEqual(message['workflow']['nodes_done'], 2)
        self.assertEqual(self.bridge.timing('run')['request_seconds'], 3)
        self.assertEqual(self.bridge.timing('run')['sample_seconds'], 0)

    def test_failed_and_interrupted_runs_stop_and_do_not_complete_active_node(self):
        for event, phase in [('execution_error', 'failed'), ('execution_interrupted', 'cancelled')]:
            with self.subTest(event=event):
                self.setUp()
                self.event('execution_start')
                self.clock.now = 12
                self.execute('sample')
                self.clock.now = 17
                self.event(event, node_id='__freevideo_workflow_run_sample', exception_message='secret path')
                message = self.events[-1][1]
                self.assertEqual(message['phase'], phase)
                self.assertEqual(message['workflow']['nodes_done'], 0)
                self.assertNotIn('secret path', str(message))
                self.clock.now = 100
                self.assertEqual(self.bridge.timing('run')['request_seconds'], 7)
                self.assertEqual(self.bridge.timing('run')['sample_seconds'], 5)
                count = len(self.events)
                self.execute('decode')
                self.assertEqual(len(self.events), count)

    def test_overlapping_prompts_and_multiple_owners_do_not_cross_route(self):
        self.rows[0][2].update(graph('second', 'owner2'))
        self.rows.append((1, 'other', graph('third', 'owner3'), {'client_id': 'other-client'}, ['owner3']))
        self.event('execution_start')
        self.bridge.observe('execution_start', {'prompt_id': 'other'}, 'other-client')
        self.clock.now = 13
        self.execute('sample')
        self.assertEqual(self.events[-1][0], 'owner')
        self.assertEqual(self.events[-1][1]['workflow']['id'], 'test')
        self.bridge.observe('executing', {'prompt_id': 'other', 'node': '__freevideo_workflow_third_sample'}, 'other-client')
        self.assertEqual(self.events[-1][0], 'owner3')
        self.event('progress', node='__freevideo_workflow_second_sample', value=1, max=2)
        self.assertEqual(self.events[-1][0], 'owner3')
        self.assertEqual(self.bridge.timing('run')['sample_seconds'], 0)
        self.clock.now = 18
        self.event('execution_error')
        self.assertEqual(self.bridge.runs['third']['status'], 'running')
        self.assertEqual(self.bridge.runs['second']['status'], 'failed')
        self.assertEqual(self.bridge.timing('third')['sample_seconds'], 5)

    def test_duplicate_and_unrelated_events_do_not_create_runs(self):
        self.event('progress', node='unknown', value=1, max=2)
        self.assertEqual(self.events, [])
        self.event('execution_start')
        count = len(self.events)
        self.event('execution_start')
        self.bridge.observe('execution_start', {'prompt_id': 'missing'})
        self.bridge.observe(1, b'binary')
        self.assertEqual(len(self.events), count)

    def test_dynamic_display_node_and_duplicate_execution_keep_sampling_interval(self):
        self.event('execution_start')
        self.clock.now = 12
        self.event('executing', node='dynamic-child', display_node='__freevideo_workflow_run_sample')
        self.clock.now = 14
        self.event('executing', node='dynamic-child', display_node='__freevideo_workflow_run_sample')
        self.event('progress', node='dynamic-child', display_node='__freevideo_workflow_run_sample', value=1, max=2)
        self.assertEqual(self.events[-1][1]['phase'], 'sampling')
        self.assertEqual(self.events[-1][1]['done'], 1)
        self.clock.now = 17
        self.execute('decode')
        self.assertEqual(self.bridge.timing('run')['sample_seconds'], 5.)

    def test_cache_is_bounded_and_retains_no_prompt_inputs(self):
        self.bridge.limit = 2
        for i in range(4):
            self.rows[:] = [(i, str(i), graph(str(i)), {'client_id': 'client'}, ['owner'])]
            self.bridge.observe('execution_start', {'prompt_id': str(i)})
        self.assertEqual(list(self.bridge.runs), ['2', '3'])
        self.assertEqual(len(self.bridge.prompts), 2)
        self.assertNotIn('inputs', str(self.bridge.runs))

    def test_metadata_can_override_unknown_nodes_but_paths_are_not_labels(self):
        self.assertEqual(_presentation(dict(class_type='Custom', _meta=dict(
            freevideo_progress=dict(stage='sampling', label='Second sampler')))), ('sampling', 'Second sampler'))
        self.assertEqual(_presentation(dict(class_type='Custom', _meta=dict(
            freevideo_progress=dict(stage='sampling', label='/private/model')))), ('sampling', 'Sampling video'))
        self.assertEqual(_presentation(dict(class_type='LoadImage')), ('reference', 'Preparing references'))
        self.assertEqual(_presentation(dict(class_type='KSamplerSelect')), ('prepare', 'Preparing workflow'))

    def test_unused_graph_nodes_are_not_counted_as_required_work(self):
        self.rows[0][2]['__freevideo_workflow_run_unused'] = dict(class_type='KSampler', inputs={})
        self.event('execution_start')
        self.assertEqual(self.events[-1][1]['workflow']['nodes_total'], 5)

    def test_completion_keeps_workflow_receipt_and_original_owner(self):
        self.event('execution_start')
        self.execute('sample')
        self.clock.now = 14
        self.bridge.begin_save('run')
        self.clock.now = 16
        summary = {'request_seconds': 6.}
        self.assertTrue(self.bridge.finish('run', publish=True, result=summary))
        owner, message = self.events[-1]
        self.assertEqual(owner, 'owner')
        self.assertEqual(message['phase'], 'complete')
        self.assertEqual(message['overall']['fraction'], 1.)
        self.assertEqual(message['result'], summary)
        self.assertEqual(message['workflow']['nodes_total'], 5)
        self.assertEqual(message['elapsed_seconds'], 6)

    def test_registration_preserves_native_calls_sid_return_and_is_idempotent(self):
        native = Mock(return_value='native-return')
        self.server.send_sync = native
        bridge = register(self.server)
        bridge.observe = Mock()
        self.assertIs(register(self.server), bridge)
        self.assertEqual(self.server.send_sync('progress', {'x': 1}, 'sid'), 'native-return')
        self.server.send_sync('executing', {'x': 2}, sid='private')
        self.assertEqual(native.call_args_list[0].args, ('progress', {'x': 1}, 'sid'))
        self.assertEqual(native.call_args_list[1].kwargs, {'sid': 'private'})
        self.assertEqual(bridge.observe.call_args.args[-1], 'private')
        bridge.observe.side_effect = RuntimeError('presentation failure')
        self.assertEqual(self.server.send_sync('execution_start', {}), 'native-return')


if __name__ == '__main__':
    unittest.main()
