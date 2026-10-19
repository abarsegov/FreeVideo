# Retained RGB export

The standalone wrapper adds **RGB → ProRes 422** beside a saved video's download
action. The action is hidden when retained RGB is unavailable. It runs a CPU
export, reports progress, and downloads a MOV; it never runs the model or decodes
MP4 video as a substitute for missing RGB.

`freevideo_engine.prores_export` memory-maps `video.artifacts/rgb.npy`, preserves
the saved FPS and audio, and writes `exports/prores-422.mov` atomically. The codec
is `prores_ks`, profile 2, `yuv422p10le`, with explicit BT.709 conversion. An
8-bit RGB source retains its original precision; the 10-bit encoding does not
recover additional source detail. A valid silent source remains silent.

Source-signature metadata lets subsequent downloads reuse the finished export.
The wrapper owns the asynchronous single-worker queue and checks browser
ownership on status, submission, and download. Existing source files are retained.
