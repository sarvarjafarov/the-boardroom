# MongoDB Schema — The Boardroom

12 collections. Every read/write flows through the `mongodb-mcp-server` (Streamable HTTP). pymongo is used only by the one-time seed script and as a Cloud Run reachability fallback.

## Collections

### `users`
```js
{
  _id: "demo",                                // single-user demo mode for hackathon
  display_name: "Demo Investor",
  timezone: "America/New_York",
  market_clock: { open: "09:30", close: "16:00" },
  default_risk_tolerance: "moderate",
  created_at: <iso>,
}
```

### `portfolios`
```js
{
  _id: ObjectId,
  user_id: "demo",
  positions: [
    { ticker: "TSLA", qty: 100, avg_cost: 198.5, weight_pct: 0.12 },
    { ticker: "NVDA", qty: 50, avg_cost: 412.3, weight_pct: 0.08 },
    ...
  ],
  cash_usd: 25000,
  total_value: 412000,
  last_updated: <iso>,
}
```

### `audio_sources`
```js
{
  _id: ObjectId,
  user_id: "demo",
  type: "tab_share" | "youtube_live" | "x_spaces" | "file_upload",
  label: "Tesla Q3 2024 Earnings Call",
  source_url: "https://www.youtube.com/watch?v=...",
  ticker: "TSLA",                             // resolved if applicable
  status: "pending" | "listening" | "paused" | "ended",
  started_at: <iso>,
  ended_at: <iso>,
  pinned: true,
  created_at: <iso>,
}
```

### `transcripts`
```js
{
  _id: ObjectId,
  audio_id: ObjectId,
  lines: [
    {
      idx: 0,
      ts: 0.0,                                 // seconds from audio start
      speaker: "CEO" | "CFO" | "ANCHOR" | "GUEST" | "ANALYST" | "UNKNOWN",
      speaker_name: "Elon Musk",               // when identifiable
      text: "...",
      topics: ["guidance", "strategy"],
    },
    ...
  ],
  total_seconds: 3450.2,
  finalized: false,
}
```

### `director_personas`
Loaded once from `seed_data/director_personas.json`. Each director has a persistent persona used across every audio session.

```js
{
  _id: "bull" | "bear" | "quant" | "strategist",
  display_name: "The Bull",
  subtitle: "Growth-first capital allocator",
  voice_profile: "Charon",
  color_token: "emerald",
  icon: "trending-up",
  system_prompt: "...",
  biases: [...],
  style_phrases: [...],
  sector_weights: {...},
  default_weight: 0.25,
}
```

### `director_score_blocks`
The atomic unit of director reasoning. One row per (audio × director × topic × timestamp).

```js
{
  _id: ObjectId,
  audio_id: ObjectId,
  director: "bull" | "bear" | "quant" | "strategist",
  topic: "guidance",
  ts_from_start: 92.4,
  score: 68,                                   // 0-100
  label: "bullish" | "neutral" | "bearish",
  confidence: "LOW" | "MEDIUM" | "HIGH",
  reason: "Reaffirmed Q1 guidance with no hedging escalation.",
  drivers: [
    { evidence: "We remain on track for our full-year operating margin target", direction: "bullish", weight: 0.6 },
    ...
  ],
  supporting_line_indices: [142, 148, 151],
  sample_size: 3,
  freshness: "fresh",
  created_at: <iso>,
}
```

### `arguments`
Debate threads — who rebutted whom on what, with citations.

```js
{
  _id: ObjectId,
  audio_id: ObjectId,
  topic: "AI strategy",
  ts_from_start: 215.8,
  from_director: "bear",
  to_director: "bull",
  claim: "Compute capacity language echoes Q4 2023 promises that slipped 18 months.",
  rebuttal_target_evidence: "We have committed 25,000 H100 GPUs of compute capacity.",
  supports_line_idx: 218,
  pattern_id: "compute_capacity_promise",      // if pattern-matched
  created_at: <iso>,
}
```

### `portfolio_impacts`
Per-position recommended actions derived from the current set of score blocks.

```js
{
  _id: ObjectId,
  audio_id: ObjectId,
  user_id: "demo",
  ticker: "TSLA",
  current_position_pct: 0.12,
  severity: "minor" | "meaningful" | "urgent",
  recommended_action: {
    side: "trim",
    target_weight_pct: 0.08,
    qty_delta: -33,
    rationale: "Bear's compute thesis backed by Quant's margin compression data.",
  },
  contributing_directors: ["bear", "quant"],
  created_at: <iso>,
}
```

### `verdicts`
Chairman's final synthesis per audio source.

```js
{
  _id: ObjectId,
  audio_id: ObjectId,
  ticker: "TSLA",
  final_action: "Trim",
  final_score: 42,
  confidence: "MEDIUM",
  thesis: "Mixed signal driven by reaffirmed guidance offset by compute-promise pattern match.",
  named_dissent: {
    director: "bull",
    score: 67,
    thesis: "Energy storage business growing 75%+, capacity announcement verifiable."
  },
  pattern_callouts: ["compute_capacity_promise"],
  recommended_actions: [<portfolio_impact_id>, ...],
  citation_line_indices: [142, 218, 305],
  created_at: <iso>,
}
```

### `track_records`
Director predictions vs actual outcomes. Seeded with 12 real records; the system accumulates more after each audio session is post-scored at +7 days.

```js
{
  _id: ObjectId,
  director: "bear",
  ticker: "TSLA",
  call_id: "TSLA-2024Q4-call",
  audio_date: "2024-01-24",
  prediction: "bearish",
  score: 28,
  thesis: "...",
  evidence_quote: "...",
  actual_outcome_7d_relative_to_spx: -12.4,
  hit: true,
  scored_at: "2024-01-31",
}
```

### `patterns`
Cross-call recurring patterns the boardroom recognizes. Seeded with 8 real patterns from actual historical calls.

```js
{
  _id: "compute_capacity_promise",
  name: "Compute capacity promise without verifiable supply chain disclosure",
  owned_by_director: "bear",
  description: "...",
  trigger_keywords: [...],
  instances: [
    { ticker, call_id, audio_date, outcome_7d_relative_to_spx },
    ...
  ],
  hit_rate: 1.0,
  sample_size: 3,
  median_drawdown_7d: -8.1,
  callout_template: "Same compute-capacity language as {prior_ticker} {prior_quarter} — that one dropped {drawdown}% in 7 days.",
}
```

### `decision_diary`
User actions logged after each verdict, with outcome scoring at +7d / +30d.

```js
{
  _id: ObjectId,
  user_id: "demo",
  verdict_id: ObjectId,
  audio_id: ObjectId,
  ticker: "TSLA",
  action_taken: "trimmed" | "added" | "held" | "ignored",
  qty_delta: -33,
  executed_at: <iso>,
  execution_method: "alpaca_paper" | "manual",
  outcome_7d_pct: null,                        // populated at +7d
  outcome_30d_pct: null,                       // populated at +30d
  followed_recommendation: true,
}
```

## Indexes

```js
db.audio_sources.createIndex({ user_id: 1, status: 1 })
db.audio_sources.createIndex({ pinned: 1, status: 1 })
db.transcripts.createIndex({ audio_id: 1 }, { unique: true })
db.director_score_blocks.createIndex({ audio_id: 1, director: 1, topic: 1, ts_from_start: 1 })
db.director_score_blocks.createIndex({ audio_id: 1, director: 1 })
db.arguments.createIndex({ audio_id: 1, topic: 1 })
db.portfolio_impacts.createIndex({ audio_id: 1, user_id: 1 })
db.verdicts.createIndex({ audio_id: 1 }, { unique: true })
db.verdicts.createIndex({ ticker: 1, created_at: -1 })
db.track_records.createIndex({ director: 1, ticker: 1 })
db.patterns.createIndex({ owned_by_director: 1 })
db.decision_diary.createIndex({ user_id: 1, executed_at: -1 })
```
