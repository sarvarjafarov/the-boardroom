# Devpost Submission Package

Everything you need to copy into the Devpost form, plus the 3-minute demo
video script.

## Submission fields

### Project name
**The Boardroom**

### Tagline (≤200 chars)
Always listening. Taps you when it matters. Debates it live. Four AI director personas with persistent personalities debate any live financial audio in real time against your portfolio.

### Hosted URL
https://the-boardroom-760978959766.us-central1.run.app

### Code repository
https://github.com/sarvarjafarov/the-boardroom (public, MIT-licensed)

### Partner track
**MongoDB** (12 collections, real schema, every read/write through the official `mongodb-mcp-server`)

### Built with (tags)
- Gemini 3
- Google Agent Development Kit (ADK)
- Vertex AI Agent Engine
- MongoDB Atlas
- MongoDB MCP Server
- Python · FastAPI · WebSockets · Tailwind CSS
- Alpaca Markets · yt-dlp · ffmpeg

---

## Long description (1500-word narrative)

### The problem

Retail investors lose money in the 15 hours between a CEO speaking and the next market open. They doom-scroll Twitter, read clickbait, form opinions in 90 seconds at 7 AM, and panic-trade at the 9:30 bell. By then, every algorithm on Wall Street has digested the transcript. There is no second opinion in their pocket.

### The product

**The Boardroom** is an always-listening AI investment committee. Four director personas with persistent personalities and track records — Bull, Bear, Quant, Strategist — debate any live financial audio in real time against the user's actual portfolio. A Chairman synthesizes the disagreement into a verdict with named dissent, pattern callouts from a historical pattern library, and specific portfolio actions. The user can ask follow-up questions by voice, then approve a recommended Alpaca paper trade with one tap.

The product solves the after-hours retail panic problem head-on. The four-director persona structure is the differentiator: they don't just summarize — they *disagree*. The "approximately $29 billion in cash" line that the Bull cites as fortress liquidity is the same line the Bear flags as "uncharacteristic balance-sheet imprecision." That kind of dissent surfaces exactly what an investment committee would catch.

### Architecture

```
LIVE AUDIO IN (browser-tab share, YouTube Live URL, X Spaces, file upload)
    │
    ▼
Gemini Live transcription (gemini-3.1-flash-live-preview)
    │
    ▼
Topic Router (gemini-3.5-flash) — classifies each line into one of 12
    financial topics: guidance, revenue, margins, capital_allocation,
    strategy, risk, qa, macro, M&A, regulatory, competitive, crisis
    │
    ▼
4 Director Agents reasoning concurrently (gemini-3.5-flash, JSON mode)
    Bull / Bear / Quant / Strategist — each with a distinct persona
    prompt + persona-specific tool grounding:
      Bull         → search_pattern_library, get_analyst_consensus
      Bear         → search_pattern_library, search_track_records
      Quant        → uses numbers from transcript itself
      Strategist   → get_macro_snapshot, get_peer_comp
    Each emits per-topic score_block (0-100 + label + confidence +
    drivers + supporting_line_indices) every ~8 seconds.
    │
    ▼
Debate Engine (every 15 s)
    Pairwise disagreement (max-min ≥ 25 points)
    Top 2 contested topics → highest + lowest scorers prompted to write
    directed rebuttals citing each other's evidence + transcript lines.
    │
    ▼
Cross-call pattern detector (substring match seeded pattern library)
    │
    ▼
Chairman synthesis (gemini-3.5-flash)
    Boardroom committee math with topic-weighted per-director composites
    Named dissent identification (director furthest from committee)
    Pattern callout narrative with historical drawdown citations
    Portfolio Impact Engine (severity + recommended action + draft trade)
    │
    ▼
Live UI (WebSocket-fan-out to browser)
    4-column director stream / argument bubbles / disagreement heatmap /
    verdict card with shareable PNG / portfolio impact panel with
    one-tap Alpaca approval / voice Q&A loop
```

### How we use the mandatory stack

| Mandatory ingredient | How it lives in the codebase |
|---|---|
| **Gemini 3** | `agents/director_agent.py` uses `gemini-3.5-flash` for every director scoring call. `audio/gemini_live.py` uses `gemini-3.1-flash-live-preview` for the bidirectional transcription session (the only Gemini-3 family member that supports `bidiGenerateContent`). `chairman_synthesis.py` uses `gemini-3.5-flash` for verdict prose. |
| **Google Cloud Agent Builder** | `agents/root_agent.py` defines a `google.adk.agents.LlmAgent` (the Chairman) with 16 ADK-registered tools. `agents/tools/synthesis.py` exposes `synthesize_verdict` so voice Q&A can trigger the synthesis pipeline through the LlmAgent's tool-call mechanism. The agent is deployable to Vertex AI Agent Engine via `agent_engines.create`. |
| **MongoDB MCP** (partner track) | `agents/tools/_mcp_client.py` uses the official `mcp` Python SDK over Streamable HTTP to talk to `mongodb-mcp-server@1.11.0` (running in the same Cloud Run container, bound to loopback). 12 collections: `users`, `portfolios`, `audio_sources`, `transcripts`, `transcript_lines`, `director_personas`, `director_score_blocks`, `arguments`, `portfolio_impacts`, `verdicts`, `track_records`, `patterns`, `decision_diary`. |

### How we adapted to Atlas reality

Atlas M0 free-tier primary shards intermittently fail SSL handshakes. We built `atlas_writer.py`: a durable in-memory write queue with exponential backoff retry. The caller's hot path tries inline first (zero latency on the healthy path); failures are silently deferred to the queue and retried until Atlas recovers. The UI never blocks on persistence. Read paths use `readPreference=secondaryPreferred` so the healthy secondaries continue serving even when the primary is in TLS distress.

### What's novel

1. **Persistent persona structure**: each director has a system prompt + biases + sector weights + voice profile + characteristic style phrases stored in MongoDB. The personas are loaded at agent construction time and used across every audio session. After many sessions, each director's MongoDB-stored track record updates the committee's effective weights.

2. **Per-topic score grid**: rather than one verdict per audio, the system maintains a 4 × N grid of per-(director, topic) score_blocks. The disagreement heatmap visualizes this grid directly. The Chairman composite is a topic-weighted aggregation.

3. **Directed rebuttals with citations**: the Debate Engine doesn't just collect opinions — it prompts the most-disagreed directors to argue *with each other*, citing each other's verbatim driver evidence and grounding the rebuttal in a specific transcript line index. The "I've seen this before" pattern callouts use real historical instance outcomes (e.g., "compute_capacity_promise: median 7d drawdown -8.1% over 3 prior instances").

4. **Atlas-resilient by design**: the synthesis pipeline persists score_blocks, arguments, impacts, and verdicts through a durable write queue; the in-memory result is always returned to the caller regardless of MongoDB write state. The demo survives multi-minute Atlas blackouts.

5. **Voice Q&A loop**: the Chairman is an actual ADK `LlmAgent` with 16 tools registered. When the user asks a question by voice (mic → Gemini Live → text), the agent routes the question through tool calls and produces a text answer that streams back through the WebSocket.

### What we built on

This project sits on top of ~7,700 lines of opinionated fintech-AI infrastructure developed for a previous project ("EarningsEdge", a real-time earnings call cockpit). We re-used the standardized `score_block` envelope, the weighted-committee aggregation engine (with hysteresis and disagreement spread), the Gemini Live transcription pattern (with the production-tested VAD config), the 6-dimension highlight-lexicon analyst taxonomy that informed our 12-topic router, the 44-function data adapter library, and the Alpaca paper trade executor.

The *new* work for The Boardroom is: persistent-persona directors, the topic-router + diarizer + line-processor pipeline, the debate engine, the boardroom committee math layered on top of EarningsEdge's committee.py, the Chairman synthesis, the Portfolio Impact Engine, the cross-call pattern memory, the multi-source audio ingestion, the live dashboard WebSocket bus, the live UI, the voice Q&A loop, the atlas_writer durable queue, the disagreement heatmap, and the shareable verdict card. Each of these is a significant codebase on its own.

### What we'd build next (V2 roadmap, hinted in the UI)

- X Spaces direct ingest (currently uses the tab-share fallback)
- File upload (MP3/MP4)
- Famous-investor persona marketplace (Carl Icahn-style activism, Buffett-style value, Druckenmiller-style macro)
- A "Your Style" personal director trained on the user's past trades
- Brokerage integration beyond Alpaca (Schwab, Fidelity, IBKR)
- Native iOS / Android app with push notifications
- Group Boardroom (multi-user real-time debate)
- 5 earnings calls in parallel during earnings season

---

## 3-minute demo video script

**Setting**: laptop, Chrome browser, screen-recorded. Use a real YouTube earnings replay or a public Bloomberg/CNBC clip with clear financial content.

### Beat sheet

```
0:00–0:15  Setup voice-over
    "It's 6 PM on a Wednesday. Tesla just reported. Markets are closed.
    You own TSLA. Tomorrow at 9:30, you'll have 90 seconds to decide
    before the open. What do you do tonight?"
    [show The Boardroom landing page on phone-width Chrome]

0:15–0:30  Pin the source
    [paste a Tesla earnings call YouTube URL, type TSLA, click "Listen live"]
    Voice-over: "Pin any market-moving audio. The Boardroom debates it
    live against your portfolio."

0:30–1:15  Live debate in action
    [point at 4 director columns lighting up in real time]
    Voice-over: "Four director personas with persistent personalities.
    The Bull, the Bear, the Quant, the Strategist — each scoring topics
    as the call unfolds."
    [point at argument bubble appearing]
    Voice-over: "When they disagree, they argue. The Bear flags the
    'approximately twenty-nine billion in cash' phrasing as uncharacteristic
    balance-sheet imprecision — quoting the Bull's own evidence."
    [point at heatmap]
    Voice-over: "The disagreement heatmap visualizes where the boardroom
    is fighting hardest."

1:15–1:45  Verdict card materializes
    [click "Verdict now" → card animates in]
    Voice-over: "When the call ends — or any time the user asks — the
    Chairman synthesizes. Verdict: Hold, 42 out of 100, low confidence
    because the boardroom is genuinely split. Named dissent: the Bull,
    quoted verbatim. Pattern callouts cite real historical drawdowns
    from the boardroom's memory of prior calls."

1:45–2:15  Portfolio impact + one-tap Alpaca
    [scroll to portfolio impact card]
    Voice-over: "Against the user's actual twelve-percent TSLA position,
    the Portfolio Impact Engine recommends Hold — minor severity, no
    action. When the boardroom does recommend a trim, one tap submits
    to Alpaca paper."

2:15–2:45  Voice Q&A
    [tap "Ask the Chairman" → speak: "What did the Bear say about Dojo?"]
    Voice-over: "Voice in, voice or text out. The Chairman is an actual
    Vertex-AI-Agent-Engine LlmAgent — it routes the question to the
    relevant director."
    [show Chairman's response bubble appearing]

2:45–3:00  Closing
    [click "Save card" → PNG downloads]
    Voice-over: "Share the verdict. Track your decisions in the diary.
    The Boardroom: always listening. Taps you when it matters.
    Debates it live."
    [end card with hosted URL + GitHub URL]
```

### Recording checklist

- [ ] Chrome window resized to phone width (390 × 844 dev tools device toolbar)
- [ ] Paste real YouTube earnings call URL (Tesla Q4 2023 or similar; pick something with clear management quotes)
- [ ] Run for ~60 s so directors have material to score
- [ ] Trigger "Verdict now" mid-recording so card animates
- [ ] Show share button, downloads bar with the PNG file
- [ ] Voice over from a separate take, layered in editing
- [ ] End screen: GitHub URL + hosted URL + "MIT licensed · Gemini 3 · MongoDB MCP · Alpaca paper"

### Backup if live audio is flaky

If Gemini Live transcription is having a slow day during recording, fall back to the **staged Tesla Q4 2023** smoke test from `scripts/smoke/` (Day 5 isolated synthesis test). It produces the exact same verdict in <30 seconds without depending on live Gemini Live throughput.

---

## Devpost answers — short fields

### What inspired you
The chronic 15-hour gap between an earnings call ending and the next market open. Retail investors form opinions in 90 seconds at 7 AM and panic-trade at 9:30. We wanted to put an actual investment committee in their pocket.

### What it does
Listens to any live financial audio — earnings, news, Fed pressers, conference keynotes. Four AI director personas with persistent personalities debate the implications against the user's portfolio in real time. Surfaces the named dissent + cross-call pattern callouts. Drafts one-tap Alpaca paper trades. Voice Q&A throughout.

### How we built it
Python FastAPI backend, Gemini 3.5 Flash for director reasoning + Chairman synthesis, Gemini 3.1 Flash Live for transcription, Google Agent Development Kit (ADK) for the Chairman LlmAgent, MongoDB Atlas + the official `mongodb-mcp-server` for persistence (12 collections), Alpaca Markets API for paper trades, yt-dlp + ffmpeg for YouTube Live audio extraction, Tailwind CSS + vanilla JS for the live UI, Cloud Run for hosting. Single-container deployment with a tiny entrypoint script that bootstraps the MCP server before uvicorn.

### Challenges
1. Gemini Live's `1000 None` rapid-close pattern (solved by tuning `silence_duration_ms=30000` and dropping the `response_modalities=TEXT` config that the model rejects).
2. Atlas M0 SSL handshake flakiness (solved by `readPreference=secondaryPreferred` + a durable in-memory write queue with exponential backoff retry).
3. Asyncio subprocess piping for yt-dlp → ffmpeg (solved by switching from `create_subprocess_exec` chained streams to `create_subprocess_shell` with quoted args).
4. ADK callback signature mismatch (solved by registering the async wrapper instead of the sync method).

### Accomplishments
- Four persistent personas that genuinely disagree and quote each other verbatim
- Real-time multi-agent debate with directed rebuttals citing transcript lines
- Live UI with 4-column director stream + heatmap + verdict card
- One-tap Alpaca paper-trade execution end-to-end
- Voice Q&A loop closing on the same Chairman LlmAgent
- Atlas-resilient persistence (the demo survives Atlas blackouts)

### What we learned
The most novel part wasn't the audio pipeline or the synthesis — it was the *debate engine*. Getting persistent personas to actually engage with each other's specific evidence (rather than emit independent monologues) is what makes the product feel like a boardroom rather than four parallel summaries.

### What's next
Famous-investor persona marketplace · "Your Style" personal director · brokerage integration beyond Alpaca · native mobile · group boardroom (multi-user real-time) · earnings-season multi-call orchestration.
