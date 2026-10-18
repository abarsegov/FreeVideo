# Registered workflow adapters

The workflow backend runs an administrator-installed ComfyUI API prompt behind
the existing `FreeVideoGenerate` node. The original node ID, VIDEO output and
library ownership remain intact. The server expands the registered graph before
ComfyUI prompt validation. It never accepts an executable adapter graph from the
client.

Each adapter is a directory below ComfyUI's registry root
`<user-directory>/default/freevideo/workflows`:

```text
<registry-root>/<adapter-id>/manifest.json
<registry-root>/<adapter-id>/workflow_api.json
```

The server administrator installs and reviews these files. Installing an adapter
grants its graph the same execution privileges as other ComfyUI workflows.
Custom node dependencies and model files must already exist on that server.
Export `workflow_api.json` using ComfyUI's API prompt export; a UI workflow with
`nodes` and `links` arrays is not accepted. IDs use 1–64 letters, digits,
underscores or hyphens, starting with a letter or digit. The manifest ID must
match its directory. File and directory links cannot escape that directory/root.

## Manifest version 1

```json
{
  "version": 1,
  "id": "my-video",
  "title": "My installed video workflow",
  "inputs": {
    "prompt": {"node": "6", "input": "text", "required": true},
    "seed": {"node": "3", "input": "seed"},
    "first_frame": {"node": "12", "input": "image"}
  },
  "outputs": {"video": {"node": "20", "output": 0}}
}
```

Every binding must target an existing API node and input. Supported canonical
names are `prompt`, `seed`, `width`, `height`, `seconds`, `frames`, `fps`,
`first_frame`, `last_frame`, `reference_images`, `reference_audio`, and
`reference_video`. A canonical name can target several inputs using
`{"bindings":[{"node":"3","input":"seed"},{"node":"8","input":"noise_seed"}],"required":true}`.
The original single-target form remains supported. No two canonical or profile
settings may overwrite the same input. `required` defaults to false. Missing optional
values retain the template's input. Missing required values produce an actionable
request error. A zero seed is a valid supplied value.

The VIDEO output declaration uses a node ID and zero-based output slot. It is a
contract that the selected socket returns ComfyUI `VIDEO`; `IMAGE` batches and
filesystem paths are not VIDEO. An optional `type` field must equal `VIDEO`.
The pure compiler checks the declaration, links and node shapes; ComfyUI validates
the actual installed node classes and socket types before execution. The adapter
author must select the correct socket. A `CreateVideo` node can convert images
into VIDEO. Do not use a save node's file list as the VIDEO output.

## Request mapping and scope

Set `backend="workflow"` and `workflow_id="my-video"` on the original
`FreeVideoGenerate`. `text` maps to `prompt`, `first` maps to `first_frame`, and
`last` maps to `last_frame`. IMAGE links stay connected to the caller's original
nodes. Supplying either frame when its binding is absent fails explicitly.
Other mapped scalar inputs must be literal values; scalar input links are outside
this first contract.

`fps` is 24. A positive `frames` integer is used exactly; otherwise `frames` is
`int(round(seconds * 24))`. There is no native H3 frame grid. Width and height are
positive integers, seed is a nonnegative integer, and seconds is finite and
positive. An adapter can use either duration or frames, or both, through its
bindings. Unbound scalar values do not alter the installed template.

This version supports single-shot generation, including native Studio first/last
frame media. Connected `FreeVideoMedia.first`/`last` IMAGE links are forwarded to
the corresponding canonical binding. Enabled uploaded image rows with role
`first` or `last` become core `LoadImage` nodes. The server must configure the
registry with its trusted ComfyUI input directory; filenames must be relative
image paths that resolve to existing files inside it. Absolute paths, traversal,
links escaping input, output/temp annotations and nonimage files are refused.
ComfyUI validates the installed loader, image contents and IMAGE socket types.
Uploaded reference images and connected `reference` through `reference_8` IMAGE
links combine in row/socket order using core `ImageBatch`; this batch maps to
`reference_images`. ComfyUI resizes later batches to the first batch's dimensions.
Uploaded reference audio uses `LoadAudio.audio` and maps to `reference_audio`;
uploaded reference video uses `LoadVideo.file` and maps to `reference_video`.
Connected `reference_audio` AUDIO and `reference_video` VIDEO sockets can supply
those bindings directly. Only one audio and one video reference are allowed.
The installed graph decides how each reference conditions its model. ComfyUI
validates loader and socket types. File suffixes identify loader types; all uploads
require the trusted input-directory/path checks described above.
The adapter must declare every supplied media binding. Duplicate uploaded,
connected or direct frames produce an error rather than choosing one silently.

Empty native media and media containing only disabled rows are disconnected
without executing the provider. Disabled rows retain validated native metadata,
including optional character IDs, but require no file access. Native media's
schema has no scalar metadata defaults. Invalid JSON, unknown providers and
nonempty unknown inputs are refused. Preview, refinement, continuation,
`early_steps` and native conditioning remain unsupported and fail before queueing.
Workflow profiles own LoRAs: native `FreeVideoLoRAStack` connections are detached
from the workflow owner, preserving independent VDN choices without applying them
to the registered graph. Profile LoRA controls configure the installed graph.
Native sampling controls are ignored unless the manifest explicitly binds
`two_pass`, `base_steps`, `refine_steps`, `step_cache`, or `upscale_factor`;
profile advanced controls are the preferred visible workflow configuration.
Studio chain execution is outside this contract.

An empty or null frame-count override follows duration, matching Studio's optional
widget serialization; other frame-count values must be nonnegative integers.

Each expansion creates a fresh UUID, namespaces the registered nodes, rewrites
their internal links and binds canonical values. The original node receives
server-owned `workflow_result` (a VIDEO link) and `workflow_run_id`. Requests
already containing a nonempty run ID or a non-null result are rejected, including repeated expansion, so
clients cannot bypass registry validation. Submit the original request again for
a new run. Ordinary native-backend requests are copied without graph changes.
Registered graphs may not contain `FreeVideoGenerate`, which would recurse.

The Create panel offers **Generation engine** and an installed workflow selector.
This version does not route image generation, the agent API or Edit exports
through adapters. Workflow model/sampling settings come from the installed
profile. Returning to VDN restores its independent sampling controls.

## Specialized profile controls

Manifest version 1 optionally declares `profile`. Legacy manifests work unchanged.
The catalog returns `profile.advanced` and `profile.loras` arrays with their display
and validation metadata; graph bindings remain private.

```json
{
  "profile": {
    "advanced": [
      {"id":"steps","label":"Sampling steps","type":"integer","default":20,
       "min":1,"max":60,"step":1,"bindings":[{"node":"3","input":"steps"}]},
      {"id":"sampler","label":"Sampler","type":"choice","default":"euler",
       "options":["euler","heun"],"bindings":[{"node":"3","input":"sampler_name"}]}
    ],
    "loras": [
      {"id":"style","label":"Installed style","min":-2,"max":2,"step":0.05,
       "bindings":{"name":[{"node":"4","input":"lora_name"}],
                   "strength":[{"node":"4","input":"strength_model"},
                               {"node":"4","input":"strength_clip"}]}}
    ]
  }
}
```

This excerpt illustrates authoring; it does not assert that a model or LoRA is
installed. Advanced types are `number`, `integer`, `boolean`, `string`, and
`choice`. Every field needs an ID, label, default, and nonempty bindings array.
Numeric fields require finite inclusive `min`/`max`; optional `step` is a positive
UI increment (values need not lie on a step grid). Integer bounds/steps must be
integers. Choices require unique string `options`. Booleans require JSON booleans;
integer fields reject booleans/fractional numbers. Strings are limited to 16384
characters. Each group permits at most 64 controls.

LoRA `bindings.name` and `bindings.strength` are required nonempty arrays. Names
and default strengths come from installed graph literals; multiple targets must
agree. Names are read-only. Optional `bindings.enabled` targets literal booleans.
An optional manifest `enabled` sets the default; otherwise the template enabled
value is used, or true without an enabled binding. Catalog LoRA items expose
`id`, `label`, `name`, `strength`, `enabled`, `min`, `max`, and `step`. Default
strength bounds are -20 through 20 and the default step is 0.05. Disabling writes
zero to strength bindings and false to any enabled bindings. Enabling restores
the selected/default strength. Clients never insert loader nodes or model names.

`FreeVideoGenerate.workflow_settings` is a JSON string storing independent maps:

```json
{"my-video":{"advanced":{"steps":28,"sampler":"heun"},"loras":{"style":{"strength":0.6,"enabled":true}}}}
```

Omitted settings initialize from installed profile defaults. The selected workflow
rejects unknown IDs/keys, invalid types, nonfinite numbers, and out-of-range values
before queueing. Other workflow entries stay isolated, allowing switching without
losing settings. Clients never provide node IDs, bindings, executable graphs, or
LoRA names. Settings JSON is limited to 256 KiB. Stale settings referring to a
removed selected-profile field produce an explicit error; reset that workflow's
settings to use its updated defaults.

## Pure compiler API and example

```python
from freevideo_engine.workflow_adapter import Registry, WorkflowAdapterError

registry = Registry(admin_configured_root, input_directory=comfy_input_directory)
metadata = registry.catalog()
metadata = registry.validate("my-video")
expanded_payload = registry.expand(original_prompt_payload)
```

`input_directory` is optional for text-only requests and connected IMAGE inputs.
It must come from server configuration, never a client request. Without it,
uploaded media receives a configuration error.

Catalog metadata exposes `id`, `title`, supported input names and capabilities
(`single_shot`, `preview`, `refine`, `continuation`), without executable graph
contents or profile bindings. Missing registry roots catalog as empty. Invalid installed adapters
raise `WorkflowAdapterError` with their ID rather than appearing runnable.

`example_workflows/adapters/empty-video` is a synthetic smoke test using core
`EmptyImage` → `CreateVideo` nodes. Copy its directory into the configured root
to register it. It generates black frames and ignores the text prompt; it is not
a generative model. Its optional first-frame binding replaces the black image
batch, so an attached image batch determines output length in that mode.
Its profile exposes a solid RGB color integer. The second CPU-only example,
`synthetic-scaled-video`, exposes solid color, image resize method, and playback
FPS using core `EmptyImage` → `ImageScale` → `CreateVideo`. Its frame count still
uses duration at 24 fps; changing playback FPS changes playback duration. Both
are synthetic demonstrations with empty LoRA lists and no generative models.

Run the compiler checks without ComfyUI or models:

```console
python -m unittest discover -s tests -p test_workflow_adapter.py
```
