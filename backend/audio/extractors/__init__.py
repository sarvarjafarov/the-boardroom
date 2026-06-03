"""Per-source-type audio extractors.

Each extractor implements the same interface:
  - async start(): bring up whatever process/state is needed to produce PCM
  - pcm_frames(): async iterator yielding 16 kHz mono Int16 PCM frames
  - async push_pcm(chunk): optional — for sources where PCM is pushed in
    from outside (tab share over WebSocket) rather than pulled by us
  - async stop(): tear down cleanly

The choice of which concrete extractor to use is made by `make_extractor()`
based on the AudioSession's source_type.
"""
from .base import make_extractor  # noqa: F401
