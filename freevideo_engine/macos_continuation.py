"""Native process wiring for the shared continuation contract (no tensor imports)."""
from pathlib import Path

from . import continuation as chain
from . import continuation_tail as encoded


def prepare(args, canvas, media, artifacts):
    """Admit source context and optional film-wide references before encoding."""
    source = getattr(args, 'continue_from', None)
    anchor = getattr(args, 'continue_anchor', None)
    if anchor is not None and source is None:
        raise ValueError('--continue-anchor requires --continue-from')
    if source is None:
        return None, media, {}
    if anchor is not None:
        media = chain.anchor_media(media, anchor)
    pictures = chain.continuation_pictures(media) if anchor is None else []
    anchored = anchor is not None or bool(pictures)
    def validate():
        chain.validate_route(task=args.task or ('ref2va' if anchored else 't2va'), backend='mps',
            anchor=anchored, two_pass=args.two_pass, preview=getattr(args, 'preview', False),
            refine_from=getattr(args, 'refine_from', None), media=media)
    validate()
    if getattr(args, 'continuation_snapshot', None):
        request = chain.restore_snapshot(args.continuation_snapshot,
            getattr(args, 'continuation_snapshot_sha256', None), canvas)
    else:
        try:
            request = chain.prepare(source, canvas)
        except ValueError as error:
            if str(error) != chain.MISSING:
                raise
            request = encoded.pending(source, canvas)
    request = dict(request, audio_context=chain.audio_context(), source_video=str(source))
    details = {}
    if getattr(args, 'first_frame_reference', False):
        if anchor is not None:
            raise ValueError('Choose --first-frame-reference or --continue-anchor, not both')
        first = details['first_frame_reference'] = dict(requested=True, used=False)
        if pictures:
            first['reason'] = 'pictures'
        else:
            try:
                kept = None if 'tail' in request else chain.chain_first_frame_picture(request, source)
            except ValueError:
                kept = None
                first['reason'] = 'unreadable'
            if kept is None:
                first.setdefault('reason', 'source_predates')
            else:
                raw, width, height = kept
                anchor = artifacts / 'first-frame.png'
                anchor.write_bytes(raw)
                media = chain.anchor_media(media, anchor)
                anchored = True
                validate()
                first.update(used=True, width=width, height=height)
    if anchored:
        request['anchor'] = media['references'][0]
    if pictures:
        request['pictures'] = len(pictures)
        details['continue_pictures'] = len(pictures)
    if anchor is not None:
        details['continue_anchor'] = request['anchor']
    if getattr(args, 'voice_reference', False):
        from .generate import dialogue_reason
        reason = ('first_frame' if 'first_frame_reference' in details and not pictures else
                  'anchor' if anchor is not None else 'conditioning' if args.conditioning else
                  dialogue_reason(Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else None))
        details['voice_reference'] = dict(requested=True, used=False, reason=reason)
    return request, media, details


def freeze(request, canvas, sampling, cache, base, artifacts):
    """Check final weights/first-pass size, freeze fallback files, then persist once."""
    if request is None:
        return None
    import json
    manifest = json.loads((Path(cache) / 'manifest.json').read_text(encoding='utf-8'))
    if 'tail' not in request:
        try:
            request = chain.validate_snapshot(request, canvas, manifest, sampling)
        except ValueError as error:
            if str(error) != chain.MISSING:
                raise
            request = dict(encoded.pending(request['source_video'], canvas),
                **{key: request[key] for key in ('audio_context', 'source_video', 'anchor', 'pictures', 'sound') if key in request})
    if 'tail' in request:
        return encoded.freeze(request, canvas, sampling['first'], artifacts, base)
    if 'snapshot' not in request:
        return chain.persist_snapshot(request, artifacts / 'continuation-source.safetensors')
    return request


def voice(request, media, details, artifacts):
    """Use the film's opening sound only after final context compatibility checks."""
    report = details.get('voice_reference')
    if report is None or report['reason'] is not None:
        return media
    if 'tail' in request:
        report['reason'] = 'source_predates'
        return media
    try:
        kept = chain.chain_first_sound(request, request.get('source_video'))
        raw = chain.unpack_first_sound(kept) if kept is not None else None
    except ValueError:
        raw = None
    if raw is None:
        report['reason'] = 'unreadable'
        return media
    from .media_request import digest, task_for
    path = artifacts / 'first-sound.wav'
    path.write_bytes(raw)
    row = dict(kind='audio', path=str(path), sha256=digest(path), bytes=path.stat().st_size)
    result = dict(media, version=1, references=[*media.get('references', []), row])
    chain.validate_route(task=task_for(result), backend='mps', anchor=bool(request.get('anchor')),
                         sound=True, media=result)
    request['sound'] = row
    report['used'] = True
    return result


def store(output, data, request, result):
    """Carry film metadata and save the same bounded record the CUDA worker writes."""
    if request is not None:
        for key, read in (('first_frame', chain.chain_first_frame), ('first_sound', chain.chain_first_sound)):
            try:
                kept = read(request, request.get('source_video'))
                if kept is not None:
                    data['metadata'][key] = kept
            except Exception as error:
                storage_failure(error, result, key + '_kept')
    try:
        from .continuation_store import save
        result['continuation_store'] = dict(save(output, data), saved=True)
        if request is not None and 'audio_lead_in' in data:
            result['continuation']['audio_lead_in_samples'] = chain.LEAD_IN_SAMPLES
    except Exception as error:
        storage_failure(error, result)


def storage_failure(error, result, field='continuation_store'):
    # Optional storage failures may not swallow memory exhaustion or cancellation.
    from .adaptive import classify_failure
    if (isinstance(error, MemoryError) or type(error).__name__ in
            ('OutOfMemoryError', 'CancelledError', 'InterruptProcessingException')
            or classify_failure(error)['kind'] in ('gpu_oom', 'cancelled')
            or 'MPS backend out of memory' in str(error)):
        raise error
    result[field] = dict(saved=False, error=type(error).__name__)
