// The Boardroom — front end logic.
// Single-page vanilla JS. Same-origin REST + WebSocket.

const API_BASE = (() => {
  const fromQuery = new URLSearchParams(location.search).get("api");
  return (fromQuery || "").replace(/\/$/, "");
})();
const WS_BASE = (() => {
  if (API_BASE) return API_BASE.replace(/^http/, "ws");
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}`;
})();

const $ = (sel) => document.querySelector(sel);
const directorMeta = {
  bull: { name: "The Bull", subtitle: "Growth-first" },
  bear: { name: "The Bear", subtitle: "Risk-first" },
  quant: { name: "The Quant", subtitle: "Numbers-only" },
  strategist: { name: "The Strategist", subtitle: "Macro frame" },
};

const state = {
  audioId: null,
  ticker: null,
  sourceLabel: null,
  dashboardWS: null,
  voiceWS: null,
  latestByDirectorTopic: new Map(), // key=`${director}|${topic}` → block
  argumentsList: [],
  verdict: null,
  impacts: [],
};

// ----- DOM hooks ----------------------------------------------------------

function setStatus(label, color) {
  $("#status-label").textContent = label;
  const dot = $("#status-dot");
  dot.className = `w-2.5 h-2.5 rounded-full inline-block ${color}`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ----- Start / end session ------------------------------------------------

$("#start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#yt-url").value.trim();
  const ticker = $("#ticker").value.trim().toUpperCase();
  if (!url) return;
  setStatus("starting…", "bg-amber-500");
  try {
    const r = await fetch(`${API_BASE}/api/sources/youtube_live`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, ticker, label: ticker ? `${ticker} live audio` : "Live audio" }),
    });
    if (!r.ok) throw new Error(await r.text());
    const body = await r.json();
    enterLiveStage(body.audio_id, ticker, url);
  } catch (err) {
    setStatus(`error: ${err.message.slice(0, 80)}`, "bg-rose-500");
    console.error(err);
  }
});

$("#end-session").addEventListener("click", endSession);
$("#verdict-now").addEventListener("click", verdictNow);

function enterLiveStage(audioId, ticker, url) {
  state.audioId = audioId;
  state.ticker = ticker || null;
  state.sourceLabel = url;
  $("#setup-card").classList.add("hidden");
  $("#live-stage").classList.remove("hidden");
  $("#voice-bar").classList.remove("hidden");
  $("#source-label").textContent = url;
  $("#ticker-label").textContent = ticker ? `Ticker: ${ticker}` : "(no ticker)";
  renderDirectorColumns();
  openDashboardWS();
  setupShareButton();
  loadDecisionDiary();
  setStatus("listening", "bg-emerald-500");
}

async function endSession() {
  if (!state.audioId) return;
  setStatus("synthesizing verdict…", "bg-amber-500");
  try {
    const r = await fetch(`${API_BASE}/api/sources/end`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_id: state.audioId }),
    });
    const body = await r.json();
    if (body.verdict) renderVerdict(body.verdict);
  } catch (err) {
    console.error(err);
  } finally {
    setStatus("session ended", "bg-slate-500");
  }
}

async function verdictNow() {
  if (!state.audioId || !state.ticker) return;
  setStatus("synthesizing…", "bg-amber-500");
  try {
    const r = await fetch(`${API_BASE}/api/verdict/synthesize_now`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audio_id: state.audioId,
        ticker: state.ticker,
        source_label: state.sourceLabel,
      }),
    });
    const body = await r.json();
    if (body.verdict) renderVerdict(body.verdict);
  } catch (err) {
    console.error(err);
  } finally {
    setStatus("listening", "bg-emerald-500");
  }
}

// ----- Dashboard WebSocket ------------------------------------------------

function openDashboardWS() {
  const url = `${WS_BASE}/ws/dashboard/${state.audioId}`;
  state.dashboardWS = new WebSocket(url);
  state.dashboardWS.onopen = () => setStatus("listening", "bg-emerald-500");
  state.dashboardWS.onclose = () => setStatus("disconnected", "bg-slate-500");
  state.dashboardWS.onerror = () => setStatus("ws error", "bg-rose-500");
  state.dashboardWS.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleEvent(msg);
    } catch (err) {
      console.warn("bad ws msg", err);
    }
  };
}

function handleEvent(msg) {
  const { type, data } = msg;
  if (type === "ping") return;
  if (type === "transcript_line") return appendTranscriptLine(data);
  if (type === "score_block") return updateScoreBlock(data);
  if (type === "argument") return appendArgument(data);
  if (type === "verdict") return renderVerdict(data);
  if (type === "portfolio_impact") return appendImpact(data);
  if (type === "trade_executed") return markImpactExecuted(data);
}

// ----- Transcript stream --------------------------------------------------

const TRANSCRIPT_MAX_LINES = 80;
function appendTranscriptLine(line) {
  const feed = $("#transcript-feed");
  const row = document.createElement("div");
  row.className = "transcript-line";
  const speaker = line.speaker_role || line.speaker_raw || "?";
  row.innerHTML = `<span class="speaker">${escapeHtml(speaker)}</span><span>${escapeHtml(line.text)}</span>`;
  feed.appendChild(row);
  while (feed.childElementCount > TRANSCRIPT_MAX_LINES) feed.firstChild.remove();
  feed.scrollTop = feed.scrollHeight;
}

// ----- Director columns ---------------------------------------------------

function renderDirectorColumns() {
  for (const id of ["bull", "bear", "quant", "strategist"]) {
    const col = document.querySelector(`.director-col[data-director="${id}"]`);
    const meta = directorMeta[id];
    col.innerHTML = `
      <div class="director-header">
        <div class="director-name">${meta.name}</div>
        <div class="director-score" data-role="score">—</div>
      </div>
      <div class="director-subtitle">${meta.subtitle}</div>
      <div class="director-blocks" data-role="blocks"></div>
    `;
  }
}

function updateScoreBlock(sb) {
  const key = `${sb.director}|${sb.topic}`;
  state.latestByDirectorTopic.set(key, sb);
  const col = document.querySelector(`.director-col[data-director="${sb.director}"]`);
  if (!col) return;

  // Update top score = average across topics for this director.
  const blocksForDirector = [...state.latestByDirectorTopic.entries()]
    .filter(([k]) => k.startsWith(sb.director + "|"))
    .map(([, b]) => b);
  const avg = Math.round(
    blocksForDirector.reduce((s, b) => s + (b.score || 50), 0) / Math.max(1, blocksForDirector.length)
  );
  col.querySelector('[data-role="score"]').textContent = `${avg}/100`;

  // Render per-topic latest blocks (most recent up top).
  const blocksEl = col.querySelector('[data-role="blocks"]');
  blocksEl.innerHTML = "";
  blocksForDirector
    .slice(-4)
    .reverse()
    .forEach((b) => {
      const row = document.createElement("div");
      row.className = "topic-block";
      row.innerHTML = `
        <div class="topic-tag">${escapeHtml(b.topic)} · ${b.score}/100 · ${b.label}</div>
        <div>${escapeHtml((b.reason || "").slice(0, 140))}</div>
      `;
      blocksEl.appendChild(row);
    });

  // Update the disagreement heatmap too.
  renderHeatmap();
}

// ----- Disagreement heatmap (4 directors × N topics) ----------------------

function renderHeatmap() {
  const all = [...state.latestByDirectorTopic.values()];
  if (!all.length) return;
  $("#heatmap-empty").classList.add("hidden");
  const heat = $("#heatmap");
  heat.classList.remove("hidden");

  const directors = ["bull", "bear", "quant", "strategist"];
  const topics = [...new Set(all.map((b) => b.topic))].sort();
  const map = new Map();
  for (const b of all) map.set(`${b.director}|${b.topic}`, b);

  // Build a tiny HTML table.
  let html = '<table class="heatmap-table"><tr><th></th>';
  for (const t of topics) {
    html += `<th title="${escapeHtml(t)}">${escapeHtml(t.slice(0, 8))}</th>`;
  }
  html += "</tr>";
  for (const d of directors) {
    html += `<tr><th class="text-left pr-2">${escapeHtml(directorMeta[d].name.replace("The ", ""))}</th>`;
    for (const t of topics) {
      const b = map.get(`${d}|${t}`);
      if (!b) {
        html += '<td class="heat-empty">·</td>';
      } else {
        const s = Math.max(0, Math.min(100, b.score));
        // Bear directions get rose; bull directions get emerald; neutral grey.
        let bg;
        if (s >= 60) bg = `rgba(52,211,153,${0.25 + (s - 60) / 100})`;       // emerald
        else if (s <= 40) bg = `rgba(251,113,133,${0.25 + (40 - s) / 100})`; // rose
        else bg = `rgba(148,163,184,0.25)`;                                  // slate
        html += `<td class="heatcell" style="background:${bg}" title="${escapeHtml(d)} · ${escapeHtml(t)} · ${escapeHtml(b.reason || "")}">${s}</td>`;
      }
    }
    html += "</tr>";
  }
  html += "</table>";
  heat.innerHTML = html;
}

// ----- Shareable verdict card ---------------------------------------------

function setupShareButton() {
  const btn = document.getElementById("share-verdict");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "rendering…";
    try {
      const node = document.getElementById("verdict-printable");
      const canvas = await html2canvas(node, {
        backgroundColor: "#0f172a",
        scale: 2,
        useCORS: true,
      });
      const url = canvas.toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url;
      a.download = `boardroom-verdict-${state.ticker || "card"}.png`;
      a.click();
    } catch (e) {
      console.warn(e);
      btn.textContent = "render failed";
    } finally {
      setTimeout(() => { btn.disabled = false; btn.textContent = "📷 Save card"; }, 1500);
    }
  });
}

// ----- Decision diary -----------------------------------------------------

async function loadDecisionDiary() {
  try {
    const r = await fetch(`${API_BASE}/api/diary/recent`);
    if (!r.ok) return;
    const rows = await r.json();
    if (!rows.length) return;
    document.getElementById("diary-card").classList.remove("hidden");
    const list = document.getElementById("diary-list");
    list.innerHTML = "";
    for (const row of rows.slice(0, 12)) {
      const div = document.createElement("div");
      div.className = "flex items-center justify-between border-b border-slate-800/60 py-1.5";
      const outcome7 = row.outcome_7d_pct;
      const outcome30 = row.outcome_30d_pct;
      const outcomeHtml = (val, lbl) =>
        val == null
          ? `<span class="text-slate-500">${lbl} pending</span>`
          : `<span class="${val >= 0 ? "text-emerald-300" : "text-rose-300"} font-mono">${lbl} ${val >= 0 ? "+" : ""}${Number(val).toFixed(1)}%</span>`;
      div.innerHTML = `
        <div>
          <span class="font-mono mr-2">${escapeHtml(row.ticker || "?")}</span>
          <span class="text-slate-300">${escapeHtml(row.action_taken || "")}</span>
          <span class="text-slate-500"> · qty ${row.qty_delta || 0}</span>
        </div>
        <div class="flex gap-2 text-xs">${outcomeHtml(outcome7, "7d")} ${outcomeHtml(outcome30, "30d")}</div>
      `;
      list.appendChild(div);
    }
  } catch (e) { /* silent */ }
}

// ----- Argument bubbles ---------------------------------------------------

function appendArgument(arg) {
  state.argumentsList.push(arg);
  const feed = $("#arguments-feed");
  const row = document.createElement("div");
  row.className = "argument-row";
  const from = directorMeta[arg.from_director]?.name || arg.from_director;
  const to = directorMeta[arg.to_director]?.name || arg.to_director;
  row.innerHTML = `
    <div class="argument-bubble">
      <div class="text-xs uppercase tracking-wider mb-1 text-slate-400">
        ${escapeHtml(from)} → ${escapeHtml(to)} · ${escapeHtml(arg.topic)}
      </div>
      <div>${escapeHtml(arg.claim)}</div>
    </div>
  `;
  feed.appendChild(row);
  feed.scrollTop = feed.scrollHeight;
}

// ----- Verdict + portfolio impact ----------------------------------------

function renderVerdict(verdict) {
  state.verdict = verdict;
  $("#verdict-card").classList.remove("hidden");
  $("#verdict-action").textContent = verdict.final_action || "—";
  const colorByAction = {
    "Add Aggressively": "text-emerald-300",
    "Add": "text-emerald-300",
    "Hold": "text-slate-200",
    "Trim": "text-rose-300",
    "Trim Aggressively": "text-rose-400",
  };
  $("#verdict-action").className = `text-2xl font-bold ${colorByAction[verdict.final_action] || "text-slate-200"}`;
  $("#verdict-score").textContent = `${verdict.final_score ?? "?"}/100`;
  $("#verdict-confidence").textContent = `${verdict.confidence || "?"} CONFIDENCE`;
  $("#verdict-thesis").textContent = verdict.thesis || "";

  const dissent = verdict.named_dissent;
  if (dissent && dissent.director) {
    $("#verdict-dissent").classList.remove("hidden");
    $("#verdict-dissent-body").textContent =
      `${(dissent.director || "").toUpperCase()} (${dissent.score}/100): ${dissent.thesis || ""}`;
  } else {
    $("#verdict-dissent").classList.add("hidden");
  }

  const patternText = verdict.pattern_callout_narrative;
  if (patternText) {
    $("#verdict-patterns").classList.remove("hidden");
    $("#verdict-patterns-body").textContent = patternText;
  } else {
    $("#verdict-patterns").classList.add("hidden");
  }

  // Populate impacts panel if returned with the verdict.
  if (Array.isArray(verdict.portfolio_impacts)) {
    state.impacts = verdict.portfolio_impacts;
    renderImpacts();
  }
}

function appendImpact(imp) {
  state.impacts.push(imp);
  renderImpacts();
}

function renderImpacts() {
  if (!state.impacts.length) return;
  $("#impacts-card").classList.remove("hidden");
  const list = $("#impacts-list");
  list.innerHTML = "";
  for (const imp of state.impacts) {
    const severityColor = {
      urgent: "text-rose-400",
      meaningful: "text-amber-300",
      minor: "text-slate-400",
    }[imp.severity] || "text-slate-400";
    const card = document.createElement("div");
    card.className = "bg-slate-950/60 border border-slate-800 rounded-lg p-3";
    const sideLabel = imp.side === "buy" ? "BUY" : imp.side === "sell" ? "SELL" : "HOLD";
    const qty = Math.abs(imp.qty_delta || 0);
    const showApprove = qty > 0;
    card.innerHTML = `
      <div class="flex items-baseline justify-between gap-2 flex-wrap">
        <div>
          <div class="text-sm font-mono">${escapeHtml(imp.ticker)}</div>
          <div class="text-xs text-slate-400">
            ${Math.round((imp.current_position_pct || 0) * 100)}% →
            ${Math.round((imp.target_position_pct || 0) * 100)}% · qty ${imp.qty_delta || 0}
          </div>
        </div>
        <div class="text-xs uppercase tracking-wider ${severityColor}">${imp.severity || "minor"}</div>
      </div>
      <p class="text-xs text-slate-300 mt-2">${escapeHtml((imp.rationale || "").slice(0, 240))}</p>
      ${showApprove ? `
        <div class="mt-3 flex gap-2">
          <button class="approve-btn flex-1 bg-emerald-600 hover:bg-emerald-500 text-slate-950 font-semibold rounded-lg py-2 text-xs"
            data-ticker="${escapeHtml(imp.ticker)}"
            data-side="${escapeHtml(imp.side)}"
            data-qty="${qty}">
            Approve ${sideLabel} ${qty}
          </button>
          <button class="reject-btn flex-1 bg-slate-800 hover:bg-slate-700 text-slate-100 rounded-lg py-2 text-xs">
            Skip
          </button>
        </div>
      ` : `<div class="text-xs text-slate-500 mt-2">No action recommended.</div>`}
    `;
    list.appendChild(card);
  }
  list.querySelectorAll(".approve-btn").forEach((btn) => {
    btn.addEventListener("click", () => approveTrade(btn));
  });
  list.querySelectorAll(".reject-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest("div.bg-slate-950\\/60").style.opacity = "0.4";
    });
  });
}

function markImpactExecuted(data) {
  // Visually grey out the matching impact card.
  document.querySelectorAll("#impacts-list .approve-btn").forEach((btn) => {
    if (btn.dataset.ticker === data.ticker && btn.dataset.side === data.side) {
      btn.textContent = `Executed · ${data.alpaca_order_id || ""}`;
      btn.disabled = true;
      btn.classList.add("bg-slate-700", "text-slate-300");
      btn.classList.remove("bg-emerald-600", "hover:bg-emerald-500");
    }
  });
}

async function approveTrade(btn) {
  const { ticker, side, qty } = btn.dataset;
  btn.disabled = true;
  btn.textContent = "submitting…";
  try {
    const r = await fetch(`${API_BASE}/api/orders/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audio_id: state.audioId,
        ticker, side,
        qty: Number(qty),
      }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || "submit failed");
  } catch (err) {
    btn.textContent = `error: ${err.message.slice(0, 40)}`;
    btn.classList.add("bg-rose-600");
  }
}

// ----- Voice Q&A ----------------------------------------------------------

let voiceCapture = null;

$("#voice-btn").addEventListener("click", async () => {
  if (voiceCapture) {
    await stopVoice();
  } else {
    await startVoice();
  }
});

async function startVoice() {
  try {
    voiceCapture = await VoiceCapture.start(state.audioId, (resp) => {
      // Optional callback per audio chunk back. For now just animate the dot.
    });
    $("#voice-btn").classList.add("voice-listening");
    $("#voice-label").textContent = "Listening…  (tap to stop)";
  } catch (err) {
    alert(`mic error: ${err.message}`);
  }
}

async function stopVoice() {
  try { await voiceCapture?.stop(); } catch {}
  voiceCapture = null;
  $("#voice-btn").classList.remove("voice-listening");
  $("#voice-label").textContent = "Ask the Chairman";
}

// ----- Voice capture (simple AudioWorklet stub) --------------------------
// Full voice Q&A round-trip is wired in voice.js (lazy-loaded).

const VoiceCapture = {
  async start(audioId, onResponseFrame) {
    const ws = new WebSocket(`${WS_BASE}/ws/voice_qa/${audioId}`);
    ws.binaryType = "arraybuffer";
    const ready = await new Promise((res, rej) => {
      ws.onopen = () => res(true);
      ws.onerror = (e) => rej(new Error("voice ws failed"));
    });
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      ws.close();
      throw new Error("mic access denied");
    }
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(2048, 1, 1);
    proc.onaudioprocess = (e) => {
      const ch = e.inputBuffer.getChannelData(0);
      const int16 = new Int16Array(ch.length);
      for (let i = 0; i < ch.length; i++) {
        const s = Math.max(-1, Math.min(1, ch[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      if (ws.readyState === 1) ws.send(int16.buffer);
    };
    src.connect(proc); proc.connect(ctx.destination);
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "chairman_text") {
          // Append the Chairman's text answer to the arguments feed as a special bubble.
          const feed = document.getElementById("arguments-feed");
          const row = document.createElement("div");
          row.className = "argument-row";
          row.innerHTML = `
            <div class="argument-bubble" style="border-color:#06b6d4">
              <div class="text-xs uppercase tracking-wider mb-1 text-cyan-300">CHAIRMAN · response</div>
              <div>${escapeHtml(msg.text || "")}</div>
            </div>
          `;
          feed.appendChild(row);
          feed.scrollTop = feed.scrollHeight;
        }
      } catch {}
    };
    return {
      async stop() {
        try { proc.disconnect(); src.disconnect(); ctx.close(); } catch {}
        try { stream.getTracks().forEach((t) => t.stop()); } catch {}
        try { ws.close(); } catch {}
      },
    };
  },
};
