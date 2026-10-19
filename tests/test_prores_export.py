"""Tests for retained RGB to CPU ProRes export."""
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

from freevideo_engine import prores_export


class ProResExportTests(unittest.TestCase):
    def retained(self, root, *, frames=3, height=16, width=16, fps=24, dtype=np.uint8):
        work = Path(root)
        artifacts = work / "video.artifacts"
        artifacts.mkdir()
        pixels = np.zeros((frames, height, width, 3), dtype=dtype)
        if dtype == np.uint8:
            pixels[0, :, :, 0] = 255
            pixels[-1, :, :, 2] = 255
        np.save(artifacts / "rgb.npy", pixels, allow_pickle=False)
        (artifacts / "video.json").write_text(json.dumps({"geometry": {
            "frames": frames, "height": height, "width": width, "fps": fps}}), encoding="utf-8")
        count = round(frames / fps * 32000)
        timeline = np.arange(count, dtype=np.float32) / 32000
        mono = (np.sin(timeline * 2 * np.pi * 440) * .1).astype(np.float32)
        audio = np.stack((mono, mono))
        np.save(artifacts / "audio.npy", audio, allow_pickle=False)
        pcm = (audio.T * 32767).round().astype("<i2")
        with wave.open(str(artifacts / "audio.wav"), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(32000)
            output.writeframes(pcm.tobytes())
        return work

    def test_inspect_reports_actual_uint8_source_and_fixed_profile(self):
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root, frames=2, height=12, width=18, fps=25)
            result = prores_export.inspect_source(work)
        self.assertTrue(result["available"], result)
        self.assertEqual((result["frames"], result["height"], result["width"]), (2, 12, 18))
        self.assertEqual(result["fps"], 25.0)
        self.assertEqual(result["fps_fraction"], "25")
        self.assertEqual(result["profile"], "422")
        self.assertEqual(result["source_bit_depth"], 8)
        self.assertEqual(result["output_pixel_format"], "yuv422p10le")
        self.assertEqual(result["audio_source"], "retained")
        self.assertIn("no extra source precision", result["precision_note"])
        self.assertEqual(list(inspect.signature(prores_export.export_prores).parameters),
                         ["work_dir", "on_progress"])

    def test_direct_artifacts_directory_uses_creation_exports_folder(self):
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            result = prores_export.inspect_source(work / "video.artifacts")
            self.assertTrue(result["available"], result)
            self.assertEqual(Path(result["output"]), work / "exports" / "prores-422.mov")

    def test_rejects_non_uint8_or_wrong_report_geometry(self):
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root, dtype=np.float32)
            result = prores_export.inspect_source(work)
            self.assertFalse(result["available"])
            self.assertIn("uint8", result["error"])
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            report = work / "video.artifacts" / "video.json"
            report.write_text(json.dumps({"geometry": {"frames": 4, "height": 16,
                                                        "width": 16, "fps": 24}}), encoding="utf-8")
            result = prores_export.inspect_source(work)
            self.assertFalse(result["available"])
            self.assertIn("frames", result["error"])

    def test_rejects_partial_or_wrong_duration_audio_instead_of_silent_drop(self):
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            (work / "video.artifacts" / "audio.wav").unlink()
            result = prores_export.inspect_source(work)
            self.assertFalse(result["available"])
            self.assertIn("incomplete", result["error"])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                prores_export.export_prores(work)
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            artifacts = work / "video.artifacts"
            short = np.zeros((2, 10), dtype=np.float32)
            np.save(artifacts / "audio.npy", short, allow_pickle=False)
            with wave.open(str(artifacts / "audio.wav"), "wb") as output:
                output.setnchannels(2); output.setsampwidth(2); output.setframerate(32000)
                output.writeframes(np.zeros((10, 2), dtype="<i2").tobytes())
            result = prores_export.inspect_source(work)
            self.assertFalse(result["available"])
            self.assertIn("duration", result["error"])

    def test_cache_signature_invalidates_when_retained_input_changes(self):
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            source = prores_export._locate(work)
            source.output.parent.mkdir()
            source.output.write_bytes(b"mov")
            source.cache_path.write_text(json.dumps({
                "signature": prores_export._signature(source), "output_size": 3}), encoding="utf-8")
            self.assertTrue(prores_export.inspect_source(work)["cached"])
            report = work / "video.artifacts" / "video.json"
            report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
            self.assertFalse(prores_export.inspect_source(work)["cached"])

    def test_rejects_retained_source_symlink_outside_creation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            work = self.retained(root)
            rgb = work / "video.artifacts" / "rgb.npy"
            other = Path(outside) / "rgb.npy"
            other.write_bytes(rgb.read_bytes())
            rgb.unlink()
            try:
                os.symlink(other, rgb)
            except OSError as error:
                self.skipTest("symlink creation unavailable: " + str(error))
            result = prores_export.inspect_source(work)
            self.assertFalse(result["available"])
            self.assertIn("symbolic link", result["error"])

    def test_valid_silent_saved_video_exports_without_inventing_audio(self):
        try:
            import av
            av.codec.Codec("mpeg4", "w")
            av.codec.Codec("prores_ks", "w")
        except Exception as error:
            self.skipTest("local PyAV encoders unavailable: " + str(error))
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            artifacts = work / "video.artifacts"
            (artifacts / "audio.npy").unlink()
            (artifacts / "audio.wav").unlink()
            with av.open(str(work / "video.mp4"), "w") as container:
                stream = container.add_stream("mpeg4", rate=24)
                stream.width = stream.height = 16
                stream.pix_fmt = "yuv420p"
                for _ in range(3):
                    frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24")
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode(None):
                    container.mux(packet)
            metadata = prores_export.inspect_source(work)
            self.assertTrue(metadata["available"], metadata)
            self.assertFalse(metadata["audio_available"])
            self.assertIsNone(metadata["audio_source"])
            output = prores_export.export_prores(work)
            with av.open(str(output)) as container:
                self.assertEqual(len(container.streams.audio), 0)
                self.assertEqual(sum(1 for _ in container.decode(container.streams.video[0])), 3)

    def test_mini_export_decodes_as_prores_422_with_audio_when_pyav_available(self):
        try:
            import av
            av.codec.Codec("prores_ks", "w")
            av.codec.Codec("pcm_s16le", "w")
        except Exception as error:
            self.skipTest("local PyAV ProRes encoder unavailable: " + str(error))
        with tempfile.TemporaryDirectory() as root:
            work = self.retained(root)
            progress = []
            output = prores_export.export_prores(work, progress.append)
            self.assertEqual(output, work / "exports" / "prores-422.mov")
            self.assertTrue(output.is_file())
            with av.open(str(output)) as container:
                video = container.streams.video[0]
                self.assertEqual(video.codec_context.name, "prores")
                self.assertEqual(video.codec_context.format.name, "yuv422p10le")
                self.assertEqual(getattr(video.codec_context.color_primaries, "value",
                                         video.codec_context.color_primaries), 1)
                self.assertEqual(getattr(video.codec_context.colorspace, "value",
                                         video.codec_context.colorspace), 1)
                self.assertEqual(str(video.average_rate), "24")
                decoded = [frame.to_ndarray(format="rgb24") for frame in container.decode(video)]
                self.assertEqual(len(decoded), 3)
                self.assertGreater(decoded[0][0, 0, 0], 220)
                self.assertLess(decoded[0][0, 0, 1], 40)
                self.assertLess(decoded[0][0, 0, 2], 40)
            with av.open(str(output)) as container:
                self.assertEqual(len(container.streams.audio), 1)
                self.assertGreater(sum(frame.samples for frame in container.decode(container.streams.audio[0])), 0)
            self.assertEqual(progress[-1], {"phase": "complete", "done": 3, "total": 3})
            again = []
            self.assertEqual(prores_export.export_prores(work, again.append), output)
            self.assertEqual(again, [{"phase": "complete", "done": 3, "total": 3}])


if __name__ == "__main__":
    unittest.main()
