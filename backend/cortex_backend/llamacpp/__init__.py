"""Local GGUF model support: a self-managed llama.cpp runtime.

Cortex downloads and runs a pinned ``llama-server`` build on the user's
behalf so a GGUF model works the same as an Ollama model from the user's
point of view -- pick it from the model list and chat. See
``docs`` history / the design plan for the full architecture; in short:

- ``binary_release`` / ``binary_fetcher``: pin, download, and verify the
  llama-server binary.
- ``server_manager``: owns the running llama-server subprocess's lifecycle.
- ``chat_client``: adapts llama-server's HTTP API to the Ollama-shaped
  response :mod:`cortex_backend.services.llm` already expects.
- ``gguf_metadata`` / ``model_directory``: scan a folder of ``.gguf`` files
  into the same ``InstalledModel`` shape Ollama models use.
- ``download``: fetch a GGUF file by URL or Hugging Face repo id.
"""

from __future__ import annotations
