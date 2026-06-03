# 🏛 The Boardroom

> **Always listening. Taps you when it matters. Debates it live.**

Your AI investment committee listens to live financial audio across browser tabs, YouTube Live streams, X Spaces, and uploaded files. Four director personas with persistent personalities and track records debate the implications for your portfolio in real time. Voice-ask follow-ups. Execute trades through Alpaca with one tap.

**Built for the Google Cloud Rapid Agent Hackathon** — Financial Services track · MongoDB partner.

## Why this exists

Retail investors lose money in the 15 hours between a CEO speaking and the next market open. They doom-scroll Twitter. They read clickbait. They wake up at 7 AM and form opinions in 90 seconds. They panic-trade at the 9:30 open.

The Boardroom is the AI in your pocket that listens to the audio, holds the debate, lets you sleep on it, lets you ask follow-up questions in voice, and lets you execute disciplined trades when you're ready.

## What it does

1. **Pin a source.** Browser tab, YouTube Live URL, X Spaces URL, or upload an MP3.
2. **Listen in real time.** Gemini Live transcribes; the Topic Router classifies each line into 12 financial topics (guidance, revenue, margins, capital allocation, strategy, risk, Q&A, macro, M&A, regulatory, competitive, crisis).
3. **Four directors debate.** Bull / Bear / Quant / Strategist — each with a persistent persona, accumulated track record, and characteristic voice. They emit standardized score_blocks per topic every ~8 seconds.
4. **Disagreement surfaces.** The Debate Engine finds the most-divergent topics and prompts the highest and lowest scorers for directed rebuttals. The arguments thread to MongoDB.
5. **Cross-call patterns flag.** "Same compute-capacity language as MSFT Q3 2024 — that one dropped 12% in 5 days." Pulled from the boardroom's persistent memory.
6. **Portfolio Impact Engine.** Reads your holdings, scores each director's view against your positions, generates specific actions ("trim TSLA from 12% to 8%").
7. **Chairman synthesizes.** Final verdict with citations, named dissent, recommended action, confidence level.
8. **Voice Q&A.** Ask follow-ups via mic; the Chairman routes to the relevant director; replies stream as TTS audio.
9. **One-tap execution.** Approve the recommended trade; Alpaca paper executes.
10. **Decision diary.** What you did and why is logged; 7-day and 30-day outcomes are scored. Your personal alpha tracker.

## Mandatory hackathon stack

| Requirement | Implementation |
|---|---|
| **Gemini 3** | `gemini-3.5-flash` for director reasoning + Chairman synthesis; `gemini-3.5-flash-live-preview` for live transcription; `gemini-2.5-flash-preview-tts` for voice replies |
| **Google Cloud Agent Builder** | Agent Development Kit (`google-adk`) defines the agent + tool surface; Vertex AI Agent Engine for managed deployment |
| **MongoDB MCP** (chosen partner) | All reads/writes through the official `mongodb-mcp-server` over Streamable HTTP — 12 collections, real schema design |
| **Public repo + OSS license** | MIT, this repo |
| **Newly created in contest period** | First commit 2026-06-03 (window: 2026-05-05 – 2026-06-11) |

## Architecture (one picture)

```
   LIVE AUDIO INGESTION (four real source types)
        │
        ├── Browser tab share (Gemini Live tab capture)
        ├── YouTube Live URL (yt-dlp → ffmpeg → PCM)
        ├── X Spaces URL (headless browser → tab share)
        └── File upload (MP3/MP4 → ffmpeg)
        │
   [Gemini Live Streaming Transcription + Speaker Diarization]
        │
   [Topic Router — Gemini 3.5 Flash, 12 financial topics]
        │
   ┌────┴────┬──────────┬───────────────┐
   ▼         ▼          ▼               ▼
 BULL      BEAR       QUANT         STRATEGIST
   │         │          │               │
   │  Each director reads only topic-relevant lines.
   │  Each emits per-topic score_block every ~8s.
   │  Persists to MongoDB → director_score_blocks.
   └──────────┬──────────┘
              │
   [Debate Engine — pairwise disagreement → directed rebuttals]
              │
   [Cross-Call Pattern Matching — query MongoDB pattern library]
              │
   [Portfolio Impact Engine — read user portfolio, score impact]
              │
   [Chairman Synthesis — runs EarningsEdge committee engine
    with track-record-weighted votes; writes verdict, dissent,
    citations, recommended action]
              │
   ┌──────────┼──────────┐
   ▼          ▼          ▼
 LIVE UI    VOICE Q&A   ALPACA PAPER
 (4-col)    (TTS replies)   (one-tap execute)
              │
   [MongoDB MCP — 12 collections, persistent boardroom memory]
```

## MongoDB schema (12 collections — the partner story)

| Collection | Purpose |
|---|---|
| `users` | Demo user + timezone + market clock |
| `portfolios` | Real positions, target allocation, decision log |
| `audio_sources` | Every pinned source (type, URL, ticker, status) |
| `transcripts` | Per-source transcript lines + topics |
| `director_personas` | The 4 personas with their system prompts, biases, voice profiles |
| `director_score_blocks` | Per-source × per-director × per-topic scores over time |
| `arguments` | Debate threads — who rebutted whom on what |
| `portfolio_impacts` | Per-source × per-position recommended actions + severity |
| `verdicts` | Chairman's final synthesis per source |
| `track_records` | Director predictions vs actual outcomes |
| `patterns` | Cross-call recurring patterns + hit rate |
| `decision_diary` | User actions + outcome scoring at 7d / 30d |

## Setup

See **[docs/SETUP.md](docs/SETUP.md)** for click-by-click. Summary:

```bash
git clone https://github.com/sarvarjafarov/the-boardroom.git
cd the-boardroom
cp .env.example .env
# Fill in keys (see docs/SETUP.md)

# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# Open frontend/index.html in browser, or:
cd frontend && python3 -m http.server 3000
# → http://localhost:3000
```

Live demo: see Devpost submission for hosted URL.

## Hackathon roadmap

- [x] **Day 1** — Foundation: repo, LICENSE, Atlas seeded, director personas, track-record seed
- [ ] **Day 2** — Audio pipeline: tab share + YouTube Live ingestion + Topic Router
- [ ] **Day 3** — Director engine: 4 personas emitting score_blocks
- [ ] **Day 4** — Debate Engine: pairwise disagreement + directed rebuttals
- [ ] **Day 5** — Portfolio Impact Engine + Chairman synthesis + pattern matching
- [ ] **Day 6** — Live debate UI + Voice Q&A + Alpaca paper trade
- [ ] **Day 7** — X Spaces ingestion + polish (cards, heatmap, decision diary)
- [ ] **Day 8** — Cloud Run deploy + 3-min demo + Devpost submission

## Documentation

- [docs/SETUP.md](docs/SETUP.md) — click-by-click setup
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — agent + tool deep dive
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md) — hackathon rule mapping

## Pedigree

The Boardroom is built on top of **7,700 lines of opinionated fintech-AI infrastructure** developed for an earlier project. We reuse the standardized score_block envelope, the weighted-committee aggregation engine (with hysteresis and disagreement spread), the Gemini Live transcription pattern, the highlight-lexicon analyst taxonomy, the 44-function data adapter library, and the Alpaca paper trade executor. The new work is the persistent-persona directors, the debate engine, the portfolio impact engine, the cross-call pattern memory, and the multi-source audio ingestion.

## License

MIT — see [LICENSE](LICENSE).
