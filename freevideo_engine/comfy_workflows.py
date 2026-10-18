"""ComfyUI workflow catalog and saving the VIDEO produced by an expanded graph.

Only administrators install templates, directly in ComfyUI's user directory.
Generation stays in the current Comfy prompt, with no second queue or HTTP call.
"""
import json
from datetime import datetime
from pathlib import Path
import shutil
import time
from uuid import uuid4

_registry = None


def registry():
    global _registry
    if _registry is None:
        import folder_paths
        from .workflow_adapter import Registry
        root = Path(folder_paths.get_user_directory()) / 'default' / 'freevideo' / 'workflows'
        _registry = Registry(root, input_directory=folder_paths.get_input_directory())
    return _registry


def register():
    from aiohttp import web
    from server import PromptServer
    server = PromptServer.instance
    if server is None:
        return
    from .workflow_progress import register as register_progress
    register_progress(server)
    if getattr(server, '_freevideo_workflows', False):
        return

    @server.routes.get('/freevideo/workflows')
    async def workflows(request):
        try:
            return web.json_response(dict(backends=[dict(id='vdn', title='FreeVideo / VDN'),
                                                    dict(id='workflow', title='ComfyUI workflow')],
                                          workflows=registry().catalog()))
        except (OSError, ValueError) as error:
            return web.json_response(dict(error=str(error)), status=400)

    server._freevideo_workflows = True


def prepare():
    """Yield FreeVideo's retained resources before evaluating native models.

    Called by Comfy's lazy evaluation at execution time, never while admitting
    a queued prompt. Active independent jobs are not cancelled here.
    """
    from server import PromptServer
    from .background_compile import COMPILE
    from .encoder_prewarm import IDLE
    from .resident_process import OWNER
    COMPILE.stop(final=True)
    IDLE.stop()
    with OWNER.mutex:
        if OWNER.active:
            raise RuntimeError('FreeVideo is still generating. Wait for the active video before running a workflow.')
        OWNER.release('workflow backend')
    server = PromptServer.instance
    prompt_service = getattr(server, '_freevideo_prompt_vlm', None)
    if prompt_service is not None:
        # Wait for the prestart process to exit before native Comfy allocations.
        # This method leaves the service's active generation worker untouched.
        prompt_service.stop_prestart('workflow backend', wait=True)
    image_service = getattr(server, '_freevideo_image_gen', None)
    if image_service is not None:
        image_service.stop_for_generation(only_idle=True)


def _probe(path):
    """Read saved stream headers, without materializing the generated frames."""
    import av
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError('The selected workflow did not produce a video stream')
        stream = container.streams.video[0]
        geometry = dict(width=stream.width, height=stream.height)
        rate = stream.average_rate
        if rate:
            geometry['fps'] = float(rate)
        if stream.frames:
            geometry['frames'] = stream.frames
        if stream.duration is not None and stream.time_base is not None:
            geometry['seconds'] = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            geometry['seconds'] = container.duration / av.time_base
        audio = dict(present=bool(container.streams.audio))
        if container.streams.audio:
            audio_stream = container.streams.audio[0]
            audio.update(sample_rate=audio_stream.sample_rate,
                         channels=len(audio_stream.layout.channels) if audio_stream.layout else None)
        return geometry, audio


def _embed_metadata(hidden):
    try:
        from comfy.cli_args import args
        if args.disable_metadata:
            return None
    except (ImportError, AttributeError):
        pass
    metadata = dict(getattr(hidden, 'extra_pnginfo', None) or {})
    prompt = getattr(hidden, 'prompt', None)
    if prompt is not None:
        metadata['prompt'] = prompt
    return metadata


def save_result(video, *, output_directory, workflow_id, workflow_run_id='', text,
                width, height, seconds, seed, frames=0, metadata=None, workflow=None, workflow_settings=None,
                timing_source=None):
    """Save the Comfy VIDEO API object, then commit its library-compatible report.

    A VIDEO can carry trims/crops and audio, so its save_to API is authoritative.
    Never copy get_stream_source() or rebuild it from only the image tensor.
    """
    if video is None or not callable(getattr(video, 'save_to', None)):
        raise ValueError('Workflow result is missing. Select an installed workflow and queue the FreeVideo prompt again.')
    root = Path(output_directory).resolve()
    folder = root / 'FreeVideo' / datetime.now().strftime('%Y-%m-%d') / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    output = folder / 'video.mp4'
    started = time.monotonic()
    try:
        video.save_to(str(output), metadata=metadata)
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError('The selected workflow did not save a video')
        geometry, audio = _probe(output)
        if metadata is not None:
            (folder / 'workflow.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        (folder / 'prompt.txt').write_text(text, encoding='utf-8')
        timing = timing_source() if timing_source is not None else None
        save_seconds = time.monotonic() - started
        # Without execution_start, only the save is measured. Do not present
        # that smaller interval as the request's total runtime.
        timing = timing or dict(request_seconds=None, timing_scope='save')
        report = dict(success=True, backend='workflow', workflow=dict(id=workflow_id, run_id=workflow_run_id),
                      prompt=text, seed=seed, parameters=dict(width=width, height=height, seconds=seconds,
                                                             frames=frames, seed=seed),
                      geometry=geometry, audio=audio, request_seconds=timing['request_seconds'],
                      video={}, encoding={})
        report['workflow']['timing_scope'] = timing['timing_scope']
        report['workflow']['save_seconds'] = save_seconds
        if 'sample_seconds' in timing:
            report['video']['sample_seconds'] = timing['sample_seconds']
        if 'cached_nodes' in timing:
            report['workflow']['cached_nodes'] = timing['cached_nodes']
        if workflow:
            report['workflow'].update(title=workflow.get('title'), capabilities=workflow.get('capabilities', {}))
        if workflow_settings is not None:
            report['workflow']['settings'] = workflow_settings
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        output.with_suffix('.request.json').write_text(serialized + '\n', encoding='utf-8')
        return output, serialized, report
    except BaseException:
        # This directory was created exclusively for this request. Partial saves
        # must never appear as a completed library entry or survive as an MP4.
        shutil.rmtree(folder)
        raise


def finalize(video, *, workflow_id, workflow_run_id='', text, width, height, seconds, seed, frames, hidden, workflow_settings='{}'):
    from comfy_api.latest import InputImpl, io, ui
    import folder_paths
    from server import PromptServer
    from .comfy_assets import output_summary, saved_video
    from .comfy_progress import publish
    from .workflow_progress import get as get_progress
    node_id = hidden.unique_id
    server = PromptServer.instance
    progress = get_progress(server)
    try:
        if video is None:
            raise ValueError('Workflow result is missing. Select an installed workflow and queue the FreeVideo prompt again.')
        workflow = registry().validate(workflow_id)
        if progress is not None:
            progress.begin_save(workflow_run_id)
        root = Path(folder_paths.get_output_directory()).resolve()
        output, serialized, report = save_result(
            video, output_directory=root, workflow_id=workflow_id, workflow_run_id=workflow_run_id,
            text=text, width=width, height=height, seconds=seconds, seed=seed, frames=frames,
            metadata=_embed_metadata(hidden), workflow=workflow,
            workflow_settings=json.loads(workflow_settings or '{}').get(workflow_id, {}),
            timing_source=(lambda: progress.timing(workflow_run_id)) if progress is not None else None)
        preview = ui.PreviewVideo([saved_video(output, root)]).as_dict()
        summary = output_summary(report, output.relative_to(root))
        summary.update(backend='workflow', workflow_id=workflow_id, result_cache_hit=False)
        preview['freevideo_summary'] = [summary]
    except BaseException as error:
        cancelled = isinstance(error, KeyboardInterrupt) or type(error).__name__ in ('InterruptProcessingException', 'CancelledError')
        phase = 'cancelled' if cancelled else 'failed'
        handled = progress.finish(workflow_run_id, phase, publish=True) if progress is not None else False
        if not handled:
            publish(server, node_id, dict(label='Generation cancelled' if cancelled else 'Generation stopped',
                                         phase=phase, overall=dict(status=phase)))
        raise
    handled = progress.finish(workflow_run_id, publish=True, result=summary) if progress is not None else False
    if not handled:
        publish(server, node_id, dict(label='Video saved', phase='complete',
                                     overall=dict(status='complete', fraction=1.), result=summary))
    return io.NodeOutput(InputImpl.VideoFromFile(str(output)), serialized, ui=preview)
