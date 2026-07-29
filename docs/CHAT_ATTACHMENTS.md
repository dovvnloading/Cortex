# Chat attachments

Chat attachments let a user add local images or text-oriented documents to one
conversation without placing the file contents in the composer textarea.

## User flow

1. The paperclip control inside the composer opens the native file picker.
2. Cortex validates and stages each file locally, then returns opaque metadata:
   filename, MIME type, byte size, SHA-256 digest, kind, and expiry.
3. The composer renders that metadata as a compact file chip. File bytes are
   never copied into the draft, browser storage, or the chat transcript.
4. On send, the metadata is submitted with the generation request. The backend
   resolves the bytes only after checking the installation owner, expiry,
   digest, size, and kind.
5. Persisted chat messages retain metadata only, so reloading or forking a chat
   keeps the attachment reference without duplicating file contents.

## Supported input

- Images are verified with Pillow before they are accepted. PNG, JPEG, GIF,
  BMP, WEBP, and TIFF are supported.
- Documents are decoded as UTF-8/UTF-16 reference text. The allowlist covers
  common Markdown, plain-text, CSV, JSON, YAML, TOML, XML, HTML, CSS, shell,
  PowerShell, and programming-language files, plus common config and diff
  files. SVG is intentionally treated as text, not as an image input.
- A file is at most 10 MiB; one message may contain at most eight files and a
  combined 24 MiB. Document text is bounded before prompting and is truncated
  with an explicit marker when the selected model context cannot hold it.

Binary office archives, executables, and arbitrary archives are not accepted as
chat documents. They remain outside the chat attachment boundary and cannot be
executed by the code/worker system.

## Model capability behavior

Cortex probes Ollama's model details endpoint (`POST /api/show`) and exposes its
`capabilities` list in `GET /api/v1/models`. `supports_vision` is true only when
the advertised list contains `vision`; if Ollama cannot answer the capability
probe, Cortex reports the value as unknown rather than guessing.

When an image is attached to a known non-vision model, the composer explains
the mismatch and disables send. The backend repeats the check at generation
start, so a crafted request cannot bypass the UI. Unknown capability state is
allowed and relies on Ollama's own response, preserving compatibility with
older gateways.

For a vision-capable model, Cortex sends document text in the user message and
image bytes as the Ollama chat message's `images` array. Document text is
explicitly marked as untrusted reference data so instructions inside an upload
are not treated as system instructions.

References: [Ollama vision](https://docs.ollama.com/capabilities/vision),
[Ollama model details and capabilities](https://docs.ollama.com/api-reference/show-model-details),
and [Ollama chat](https://docs.ollama.com/api/chat).

## Storage and lifecycle

The native runtime uses the existing owner-scoped execution repository only as
a durable local artifact store. Chat staging uses its own `chat.attachment.v1`
profile and does not enter the executable worker input path. In-memory demo
dependencies use an equivalent process-local store. Artifacts expire after 30
days, and every generation re-checks ownership and integrity before resolving
them.

The UI stores only draft metadata in `sessionStorage` to survive a route change.
The bytes remain in the backend store and are never written to browser storage.
