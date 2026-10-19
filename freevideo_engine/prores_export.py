"""CPU-only ProRes 422 export from FreeVideo's retained decoded artifacts.

The retained picture is eight-bit RGB.  ``yuv422p10le`` is the storage format
required by the ProRes encoder; converting to it does not recover ten-bit
precision which was not present in ``rgb.npy``.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import wave

import numpy as np


PROFILE = "422"
_CACHE_SCHEMA = 1
_MAX_JSON_BYTES = 1024 * 1024
_MAX_THREADS = 4
_AUDIO_RATE = 32000


@dataclass(frozen=True)
class _Source:
    work_dir: Path
    artifacts: Path
    rgb_path: Path
    report_path: Path
    video_path: Path | None
    audio_path: Path
    audio_kind: str
    audio_rate: int
    audio_channels: int
    audio_samples: int | None
    frames: int
    height: int
    width: int
    fps: Fraction

    @property
    def output(self) -> Path:
        return self.work_dir / "exports" / "prores-422.mov"

    @property
    def cache_path(self) -> Path:
        return self.output.with_suffix(".json")


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("Missing retained frame-rate report: " + str(path))
    size = path.stat().st_size
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ValueError("Invalid retained frame-rate report size: " + str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid retained frame-rate report: " + str(path)) from error
    if not isinstance(value, dict):
        raise ValueError("Invalid retained frame-rate report: " + str(path))
    return value


def _contained(root: Path, path: Path, label: str, *, must_exist: bool) -> Path:
    """Resolve a plain child without following an owned-work symlink."""
    root = root.resolve(strict=True)
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise ValueError(label + " must stay inside the creation directory") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(label + " must not be a symbolic link: " + str(current))
    try:
        resolved = lexical.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise ValueError(label + " must stay inside the creation directory") from error
    return resolved


def _fps(report: dict, path: Path) -> Fraction:
    geometry = report.get("geometry")
    value = geometry.get("fps") if isinstance(geometry, dict) else report.get("fps")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Retained report has no valid frame rate: " + str(path))
    try:
        result = Fraction(str(value)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Retained report has no valid frame rate: " + str(path)) from error
    if result <= 0 or result > 240 or not math.isfinite(float(result)):
        raise ValueError("Retained report has no valid frame rate: " + str(path))
    return result


def _geometry(report: dict, shape: tuple[int, ...], path: Path) -> None:
    geometry = report.get("geometry")
    if not isinstance(geometry, dict):
        return
    expected = {"frames": shape[0], "height": shape[1], "width": shape[2]}
    for name, actual in expected.items():
        if name in geometry and (type(geometry[name]) not in (int, float) or geometry[name] != actual):
            raise ValueError("Retained RGB does not match report %s: %s" % (name, path))


def _rgb(path: Path) -> np.memmap:
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("Retained RGB is unavailable: " + str(path))
    try:
        pixels = np.load(path, mmap_mode="r", allow_pickle=False, max_header_size=16384)
    except (OSError, ValueError) as error:
        raise ValueError("Invalid retained RGB array: " + str(path)) from error
    if not isinstance(pixels, np.memmap):
        raise ValueError("Retained RGB must be a memory-mapped NPY array")
    if pixels.dtype != np.dtype(np.uint8):
        raise ValueError("Retained RGB must use FreeVideo's uint8 source format")
    if (pixels.ndim != 4 or pixels.shape[0] <= 0 or pixels.shape[1] <= 0
            or pixels.shape[2] <= 0 or pixels.shape[3] != 3):
        raise ValueError("Retained RGB must have shape (frames, height, width, 3)")
    if pixels.shape[2] % 2:
        raise ValueError("ProRes 422 requires an even retained RGB width")
    if not pixels.flags.c_contiguous:
        raise ValueError("Retained RGB must be stored in frame-major C order")
    return pixels


def _retained_audio(npy_path: Path, wav_path: Path) -> tuple[int, int, int]:
    try:
        samples = np.load(npy_path, mmap_mode="r", allow_pickle=False, max_header_size=16384)
    except (OSError, ValueError) as error:
        raise ValueError("Invalid retained audio array: " + str(npy_path)) from error
    try:
        if (not isinstance(samples, np.memmap) or samples.dtype != np.dtype(np.float32)
                or samples.ndim != 2 or samples.shape[0] != 2 or samples.shape[1] <= 0):
            raise ValueError("Retained audio must be float32 stereo")
        for start in range(0, samples.shape[1], _AUDIO_RATE):
            if not np.isfinite(samples[:, start:start + _AUDIO_RATE]).all():
                raise ValueError("Retained audio contains non-finite samples")
        with wave.open(str(wav_path), "rb") as reader:
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()) != (
                    2, 2, _AUDIO_RATE, samples.shape[1]):
                raise ValueError("Retained WAV does not match retained float audio")
            remaining = reader.getnframes()
            while remaining:
                block = reader.readframes(min(_AUDIO_RATE, remaining))
                if not block:
                    raise ValueError("Retained WAV is truncated")
                remaining -= len(block) // (reader.getsampwidth() * reader.getnchannels())
    finally:
        if isinstance(samples, np.memmap) and getattr(samples, "_mmap", None) is not None:
            samples._mmap.close()
    return _AUDIO_RATE, 2, int(samples.shape[1])


def _video_audio(path: Path) -> tuple[int, int, int | None]:
    try:
        import av
        with av.open(str(path)) as container:
            streams = list(container.streams.audio)
            if not streams:
                if not list(container.streams.video):
                    raise ValueError("Saved media has neither video nor audio")
                return 0, 0, 0
            stream = streams[0]
            rate = int(stream.codec_context.sample_rate or stream.rate or 0)
            channels = int(stream.codec_context.channels or 0)
            if rate <= 0 or channels <= 0:
                raise ValueError("Saved video has invalid audio metadata")
            count = (round(stream.duration * stream.time_base * rate)
                     if stream.duration is not None and stream.time_base is not None else None)
            return rate, channels, count
    except ImportError as error:
        raise ValueError("PyAV is required to inspect fallback video audio") from error
    except (OSError, ValueError) as error:
        raise ValueError("Saved video audio is unavailable: " + str(path)) from error


def _locate(work_dir: os.PathLike[str] | str) -> _Source:
    supplied = Path(os.path.abspath(Path(work_dir).expanduser()))
    if supplied.is_symlink():
        raise ValueError("Creation directory must not be a symbolic link")
    if not supplied.exists():
        raise ValueError("Creation directory is unavailable: " + str(supplied))
    requested = supplied.resolve(strict=True)
    if requested.name == "video.artifacts" and requested.is_dir():
        root, artifacts = requested.parent, requested
    else:
        root = requested
        artifacts = root / "video.artifacts" if (root / "video.artifacts").is_dir() else root
    if not root.is_dir():
        raise ValueError("Creation directory is unavailable: " + str(root))
    artifacts = _contained(root, artifacts, "Retained artifacts directory", must_exist=True)
    if not artifacts.is_dir():
        raise ValueError("Retained artifacts directory is unavailable: " + str(artifacts))
    rgb_path = _contained(root, artifacts / "rgb.npy", "Retained RGB", must_exist=True)
    pixels = _rgb(rgb_path)
    try:
        candidates = (artifacts / "video.json", root / "video.request.json", root / "comfy-request.json")
        report_path = next((path for path in candidates if path.is_file()), candidates[0])
        report_path = _contained(root, report_path, "Retained frame-rate report", must_exist=True)
        report = _read_json(report_path)
        fps = _fps(report, report_path)
        _geometry(report, pixels.shape, report_path)
        frames, height, width, _ = pixels.shape
    finally:
        pixels._mmap.close()

    npy_path, wav_path = artifacts / "audio.npy", artifacts / "audio.wav"
    retained = (npy_path.is_file(), wav_path.is_file())
    video_path = root / "video.mp4"
    if retained == (True, True):
        npy_path = _contained(root, npy_path, "Retained float audio", must_exist=True)
        wav_path = _contained(root, wav_path, "Retained WAV", must_exist=True)
        audio_rate, audio_channels, audio_samples = _retained_audio(npy_path, wav_path)
        audio_path, audio_kind = wav_path, "retained"
    elif any(retained):
        raise ValueError("Retained audio is incomplete; audio.npy and audio.wav must both be present")
    elif video_path.is_file():
        video_path = _contained(root, video_path, "Fallback saved video", must_exist=True)
        audio_rate, audio_channels, audio_samples = _video_audio(video_path)
        audio_path, audio_kind = video_path, "video" if audio_channels else "none"
    else:
        raise ValueError("Retained RGB exists but its expected audio is unavailable")
    if (audio_rate and audio_samples is not None
            and abs(audio_samples / audio_rate - frames / fps) > max(1 / fps, Fraction(2048, audio_rate))):
        raise ValueError("Retained audio duration differs from RGB by more than one frame")
    export_dir = _contained(root, root / "exports", "Export directory", must_exist=False)
    _contained(root, export_dir / "prores-422.mov", "ProRes output", must_exist=False)
    _contained(root, export_dir / "prores-422.json", "ProRes cache", must_exist=False)
    _contained(root, export_dir / "prores-422.partial.mov", "Partial ProRes output", must_exist=False)
    _contained(root, export_dir / "prores-422.json.partial", "Partial ProRes cache", must_exist=False)
    return _Source(root, artifacts, rgb_path, report_path,
                   video_path if video_path.is_file() else None,
                   audio_path, audio_kind, audio_rate, audio_channels, audio_samples,
                   frames, height, width, fps)


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _signature(source: _Source) -> dict:
    paths = {"rgb": source.rgb_path, "report": source.report_path, "audio": source.audio_path}
    if source.audio_kind == "retained":
        paths["audio_float"] = source.artifacts / "audio.npy"
    return {"schema": _CACHE_SCHEMA, "profile": PROFILE,
            "inputs": {name: _fingerprint(path) for name, path in paths.items()}}


def _cached(source: _Source) -> bool:
    output, cache = source.output, source.cache_path
    if not output.is_file() or output.stat().st_size <= 0 or not cache.is_file():
        return False
    try:
        if cache.stat().st_size > 64 * 1024:
            return False
        value = json.loads(cache.read_text(encoding="utf-8"))
        return (isinstance(value, dict) and value.get("signature") == _signature(source)
                and value.get("output_size") == output.stat().st_size)
    except (OSError, ValueError, TypeError):
        return False


def inspect_source(work_dir: os.PathLike[str] | str) -> dict:
    """Return export availability and retained-source metadata without ComfyUI."""
    requested = Path(work_dir).expanduser().resolve()
    output = requested.parent / "exports" / "prores-422.mov" if requested.name == "video.artifacts" else requested / "exports" / "prores-422.mov"
    try:
        source = _locate(requested)
        return {"available": True, "profile": PROFILE, "width": source.width,
                "height": source.height, "frames": source.frames, "fps": float(source.fps),
                "fps_fraction": str(source.fps), "dtype": "uint8", "source_bit_depth": 8,
                "output_pixel_format": "yuv422p10le", "audio_available": source.audio_kind != "none",
                "audio_source": None if source.audio_kind == "none" else source.audio_kind,
                "audio_sample_rate": source.audio_rate or None,
                "audio_channels": source.audio_channels or None, "output": str(source.output),
                "cached": _cached(source),
                "precision_note": "ProRes stores yuv422p10le, converted from retained 8-bit RGB; no extra source precision is created."}
    except (OSError, ValueError) as error:
        return {"available": False, "profile": PROFILE, "output": str(output), "cached": False,
                "error": str(error)}


def _notify(callback, phase: str, done: int, total: int) -> None:
    if callback is not None:
        callback({"phase": phase, "done": done, "total": total})


def _audio_frames(av, path: Path, rate: int):
    from fractions import Fraction as _TimeBase

    with av.open(str(path)) as source:
        input_stream = source.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=rate)
        written = 0
        for decoded in source.decode(input_stream):
            for frame in resampler.resample(decoded):
                frame.pts = written
                frame.time_base = _TimeBase(1, rate)
                frame.sample_rate = rate
                written += frame.samples
                yield frame, written
        for frame in resampler.resample(None):
            frame.pts = written
            frame.time_base = _TimeBase(1, rate)
            frame.sample_rate = rate
            written += frame.samples
            yield frame, written


def export_prores(work_dir: os.PathLike[str] | str, on_progress=None) -> Path:
    """Encode retained RGB and audio to CPU ProRes 422, returning the MOV path."""
    source = _locate(work_dir)
    if _cached(source):
        _notify(on_progress, "complete", source.frames, source.frames)
        return source.output
    try:
        import av
        from av.video.reformatter import Colorspace, VideoReformatter
    except ImportError as error:
        raise RuntimeError("PyAV with the prores_ks encoder is required for ProRes export") from error

    output = source.output
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    cache_partial = source.cache_path.with_name(source.cache_path.name + ".partial")
    for stale in (partial, cache_partial):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    pixels = _rgb(source.rgb_path)
    try:
        with av.open(str(partial), "w", format="mov") as container:
            video = container.add_stream("prores_ks", rate=source.fps)
            video.width, video.height = source.width, source.height
            video.pix_fmt = "yuv422p10le"
            video.codec_context.thread_count = max(1, min(_MAX_THREADS, os.cpu_count() or 1))
            video.options = {"profile": "2", "vendor": "apl0"}
            video.codec_context.color_primaries = 1  # AVCOL_PRI_BT709
            video.codec_context.color_trc = 1        # AVCOL_TRC_BT709
            video.codec_context.colorspace = 1       # AVCOL_SPC_BT709
            video.codec_context.color_range = 1      # MPEG/limited range
            video.metadata["encoder"] = "FreeVideo CPU ProRes 422"
            audio = None
            if source.audio_kind != "none":
                audio = container.add_stream("pcm_s16le", rate=source.audio_rate)
                audio.layout = "stereo"
                audio.codec_context.thread_count = 1
            container.metadata["encoding_tool"] = "FreeVideo CPU ProRes exporter"
            reformer = VideoReformatter()
            sounds = iter(_audio_frames(av, source.audio_path, source.audio_rate)) if audio is not None else iter(())
            next_sound = next(sounds, None)

            def audio_until(cutoff=None):
                nonlocal next_sound
                while (next_sound is not None and
                       (cutoff is None or Fraction(next_sound[0].pts, source.audio_rate) < cutoff)):
                    sound, written = next_sound
                    for packet in audio.encode(sound):
                        container.mux(packet)
                    _notify(on_progress, "audio", written, source.audio_samples or written)
                    next_sound = next(sounds, None)

            for index in range(source.frames):
                frame = av.VideoFrame.from_ndarray(np.asarray(pixels[index]), format="rgb24")
                # Retained RGB is full-range.  Supplying the BT.709 transform to
                # swscale prevents its dimension-based BT.601 guess from shifting
                # colours; the ProRes picture itself is tagged limited-range 709.
                frame.color_range = 2               # AVCOL_RANGE_JPEG/full
                frame.color_primaries = 1           # AVCOL_PRI_BT709
                frame.color_trc = 1                 # AVCOL_TRC_BT709
                frame.colorspace = 1                # AVCOL_SPC_BT709
                frame = reformer.reformat(frame, width=source.width, height=source.height,
                                          format="yuv422p10le",
                                          src_colorspace=Colorspace.ITU709,
                                          dst_colorspace=Colorspace.ITU709)
                frame.color_range = 1               # AVCOL_RANGE_MPEG/limited
                frame.color_primaries = 1
                frame.color_trc = 1
                frame.colorspace = 1
                frame.pts, frame.time_base = index, Fraction(source.fps.denominator, source.fps.numerator)
                for packet in video.encode(frame):
                    container.mux(packet)
                _notify(on_progress, "video", index + 1, source.frames)
                audio_until(Fraction((index + 1) * source.fps.denominator, source.fps.numerator))
            for packet in video.encode(None):
                container.mux(packet)
            audio_until()
            if audio is not None:
                for packet in audio.encode(None):
                    container.mux(packet)
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("ProRes encoder produced no output")
        os.replace(partial, output)
        cache_value = {"signature": _signature(source), "output_size": output.stat().st_size}
        cache_partial.write_text(json.dumps(cache_value, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(cache_partial, source.cache_path)
    except BaseException:
        for stale in (partial, cache_partial):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        pixels._mmap.close()
    _notify(on_progress, "complete", source.frames, source.frames)
    return output
