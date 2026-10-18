# H3 reference-to-video (native 4-step)

This specialized first-clip workflow generates video with native audio from 1–9 reference pictures and an optional voice/audio reference. It uses the installed MiniMax H3 ref2va model and its installed ref2v turbo 4-step LoRA. It is an adaptation of `.artifacts/selflift-h3-workflow.json`, not the SelfLift sampler.

## Inputs

- `reference_images` is required: connect an IMAGE batch of 1–9 pictures. `FreeVideoH3ReferenceImages` splits the batch into H3's nine autogrow sockets without dropping pictures. H3's native node otherwise reads only the first image of each socket. Unused helper outputs are `None` and H3 explicitly skips them. Tag pictures as `<Picture 1>` through `<Picture 9>` in batch order.
- `reference_audio` is optional AUDIO. The template's `null` is skipped by H3; a supplied reference becomes `<Audio 1>`.
- `prompt`, `seed`, `width`, `height`, `frames` and `fps` use the common FreeVideo inputs. FreeVideo converts seconds to frames at 24 fps; the supported `ComfyMathExpression` node rounds upward to H3's `5 + 17*n` frame count. Five seconds requests 120 frames and produces 124 frames (~5.17 seconds). Width and height must be multiples of 32.
- This profile does not bind first/last-frame locks or reference video. Picture references guide identity and appearance rather than locking the first frame. It contains no previous-clip input, continuation loop, color-transfer branch, saved-media dependency, or UI primitive.

The output is a VIDEO from `CreateVideo`, with 24 fps and both the generated IMAGE decode and generated AUDIO decode connected. FreeVideo's outer output node performs saving.

## Profile

Advanced settings expose 4 steps, CFG 1, beta scheduling, Euler sampling, denoise 1, reference sizing `match`, and Comfy Kitchen attention. Seed is the common input (default 42 in the API template). The fixed model path and LoRA name come from the API graph; the UI exposes LoRA strength/enabled, not arbitrary model replacement.

Installed selections verified from HellEva's individual `/object_info/<class>` schemas on 2026-10-18:

- `minimax_h3_ref2va_pruned_int8_convrot.safetensors`
- `H3\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` (default strength 1)
- `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
- `minimax_h3_video_vae_int8_convrot.safetensors`
- `minimax_h3_audio_vae_fp32.safetensors`

## Difference from the supplied SelfLift graph

Production did not provide `SelfLiftH3Sampler`, `DasiwaMinimaxH3_dasiwaREF2VAHybridV1.safetensors`, the source turbo 8-step v1.0 / 4-step v1.2 LoRAs, or a verified SelfLift upscaler. This adaptation uses `SamplerCustomAdvanced` and the installed matching ref2va model/ref2v four-step LoRA. It therefore does not expose SelfLift transition step, low-resolution scale, rho, w_min/w_max or upscaler settings. The original used 8 beta/Euler steps, CFG 1, seed 42, transition 6, low-resolution scale 0.5, and LoRA strengths 0.75/0.70. The new single-stage recipe uses 4 steps and strength 1, with a 768×1344 template canvas. It does not claim equivalent SelfLift results.

## Verification

The adapter registry and expansion pass locally. A three-picture external IMAGE link remains in the caller namespace and fans out to all nine helper outputs. All installed node classes, links, required inputs, enum selections, numeric bounds and selected model names passed a static check against the production node schemas. Optional null AUDIO handling was checked against the installed `execution.py` validator and H3 `execute` implementation. The added FreeVideo helper must be present in the installed FreeVideo version.

No `/prompt` request or GPU generation was submitted. Generation quality, peak memory, and runtime compatibility of this new four-step recipe have not been measured.
