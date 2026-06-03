"""Audio constants for the EarningsEdge agents."""
from __future__ import annotations

# 16 kHz mono 16-bit PCM. 100 ms of silence = 1600 samples * 2 bytes = 3200 bytes.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_MS = 100
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * FRAME_MS // 1000
SILENT_FRAME = b"\x00" * FRAME_BYTES
