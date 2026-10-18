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
`first_frame` and `last_frame`. One canonical name binds one input; multiple names
cannot overwrite the same input. `required` defaults to false. Missing optional
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
The adapter must declare each supplied frame binding. Duplicate uploaded,
connected or direct frames produce an error rather than choosing one silently.

Empty native media and media containing only disabled rows are disconnected
without executing the provider. Disabled rows retain validated native metadata,
including optional character IDs, but require no file access. Native media's
schema has no scalar metadata defaults. Invalid JSON, unknown providers and
nonempty unknown inputs are refused. Preview, refinement, continuation,
references (image/video/audio), LoRAs, `early_steps` and native conditioning remain
unsupported and fail before queueing. Empty native `FreeVideoLoRAStack`
connections are removed only when the compiler can prove the whole chain empty.
Native two-pass and sampling controls do not configure the registered graph;
configure that graph on disk. Studio chain execution is outside this contract.

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
This first version does not route image generation, the agent API or Edit exports
through adapters. Workflow model/sampling settings remain in the registered API
graph. Returning to VDN restores its sampling controls.

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
contents. Missing registry roots catalog as empty. Invalid installed adapters
raise `WorkflowAdapterError` with their ID rather than appearing runnable.

`example_workflows/adapters/empty-video` is a synthetic smoke test using core
`EmptyImage` → `CreateVideo` nodes. Copy its directory into the configured root
to register it. It generates black frames and ignores the text prompt; it is not
a generative model. Its optional first-frame binding replaces the black image
batch, so an attached image batch determines output length in that mode.

Run the compiler checks without ComfyUI or models:

```console
python -m unittest discover -s tests -p test_workflow_adapter.py
```
