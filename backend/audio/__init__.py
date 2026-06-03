"""Audio pipeline — multi-source ingestion into a unified Gemini Live transcript stream.

Public surface:
  - AudioSession: one live audio source bound to one Gemini Live transcription
    session; consumers subscribe to its transcript_queue for line events.
  - SourceType: enum of the four real source types we support.
  - SessionRegistry: process-wide registry of active sessions, keyed by audio_id.

Architecture in one paragraph:
  Each pinned source spawns one AudioSession. Inside, a per-source Extractor
  produces PCM frames (browser tab share over WebSocket, yt-dlp+ffmpeg
  pipeline for YouTube Live, headless browser tab for X Spaces, or ffmpeg
  on an uploaded file). The PCM stream is forwarded to a single Gemini Live
  bidirectional session whose `input_audio_transcription` deltas surface as
  transcript lines. Each line is then routed through Speaker Diarization +
  Topic Routing (Day 2) and persisted to MongoDB via the `transcripts`
  collection. Days 3-5 attach the director loops, debate engine, and
  portfolio impact engine off the same transcript_queue.
"""

from .pipeline import AudioSession, SourceType, SessionRegistry, registry  # noqa: F401
