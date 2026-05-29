/* Telegraph — Stage views (Brief, Messages, Memory, Watchtower, Profiles, Day) */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ---------- Search highlight helpers ----------
// Used by both the search-result snippets (MemoryCol) and DayView in-place
// highlighting. Matching is literal (indexOf) and case-insensitive.

function countMatches(text, query) {
  if (!text || !query) return 0;
  const t = String(text).toLowerCase();
  const q = String(query).toLowerCase();
  if (!q) return 0;
  let n = 0, i = 0;
  while ((i = t.indexOf(q, i)) !== -1) { n++; i += q.length; }
  return n;
}

// Collapse markdown into a single tidy line for fallback previews.
// Strips headings, emphasis, code fences, list bullets, hr rules, and
// converts all whitespace runs into single spaces.
function cleanPreview(text, maxLen = 160) {
  if (!text) return "";
  let s = String(text);
  s = s.replace(/```[\s\S]*?```/g, " ");           // fenced code
  s = s.replace(/`([^`]+)`/g, "$1");                // inline code
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");         // headings
  s = s.replace(/^\s*[-*_]{3,}\s*$/gm, " ");        // hr
  s = s.replace(/^\s*[-*+]\s+/gm, "");              // list bullets
  s = s.replace(/^\s*\d+\.\s+/gm, "");              // numbered lists
  s = s.replace(/\*\*([^*]+)\*\*/g, "$1");          // bold
  s = s.replace(/\*([^*]+)\*/g, "$1");              // italic
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, "");       // images
  s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");    // links → text
  s = s.replace(/\s+/g, " ").trim();
  if (s.length > maxLen) s = s.slice(0, maxLen).trimEnd() + "…";
  return s;
}

// Extract up to `max` snippets around each match — used by the search panel
// to show every occurrence as its own clickable preview.
function extractSnippets(text, query, ctx = 36, max = 6) {
  if (!text || !query) return [];
  const t = String(text);
  const lc = t.toLowerCase();
  const q = String(query).toLowerCase();
  if (!q) return [];
  const out = [];
  let i = 0, k = 0;
  while (i < lc.length && out.length < max) {
    const f = lc.indexOf(q, i);
    if (f === -1) break;
    const start = Math.max(0, f - ctx);
    const end = Math.min(t.length, f + q.length + ctx);
    out.push({
      n: k,
      before: t.slice(start, f).replace(/\s+/g, " "),
      match: t.slice(f, f + q.length),
      after: t.slice(f + q.length, end).replace(/\s+/g, " "),
      leading: start > 0,
      trailing: end < t.length,
    });
    i = f + q.length;
    k++;
  }
  return out;
}

// Render a string with every match wrapped in <mark>. `startIdx` is the
// global match offset (so different fields in DayView get unique IDs).
function Highlighted({ text, query, startIdx = 0, activeIdx = -1 }) {
  if (!query || !text) return text || null;
  const t = String(text);
  const lc = t.toLowerCase();
  const q = String(query).toLowerCase();
  if (!q) return t;
  const out = [];
  let i = 0, k = 0;
  while (i < lc.length) {
    const f = lc.indexOf(q, i);
    if (f === -1) { out.push(t.slice(i)); break; }
    if (f > i) out.push(t.slice(i, f));
    const idx = startIdx + k;
    const isActive = idx === activeIdx;
    out.push(
      <mark
        key={`m-${idx}`}
        id={`search-hit-${idx}`}
        className={cls("search-hit", isActive && "active")}
        data-match-idx={idx}
      >
        {t.slice(f, f + q.length)}
      </mark>
    );
    i = f + q.length;
    k++;
  }
  return <>{out}</>;
}

// ---------- Time control ----------
function TimeControl({ hours, setHours }) {
  const presets = [1, 4, 8, 24, 48, 72];
  const [local, setLocal] = useState(hours);
  useEffect(() => { setLocal(hours); }, [hours]);
  const commit = (v) => { if (v !== hours) setHours(v); };
  return (
    <div className="time-control">
      <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
        <span className="time-val">{local}</span>
        <span className="time-label">h</span>
      </div>
      <input
        type="range" className="time-slider"
        min="1" max="168"
        value={local}
        onChange={(e)=>setLocal(Number(e.target.value))}
        onPointerUp={(e)=>commit(Number(e.target.value))}
        onKeyUp={(e)=>commit(Number(e.target.value))}
      />
      <div className="time-presets">
        {presets.map(p => (
          <button key={p} className={local===p ? "active" : ""} onClick={()=>{ setLocal(p); setHours(p); }}>{p}h</button>
        ))}
      </div>
    </div>
  );
}

// ---------- Stage head ----------
function StageHead({ title, meta, right, extra, eyebrow }) {
  return (
    <div className="stage-head">
      <div className="stage-title">
        {eyebrow && <div className="stage-eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        <div className="meta">{meta}</div>
      </div>
      {extra}
      {right}
    </div>
  );
}

// ---------- Empty ----------
function Empty({ mark, title, desc }) {
  return (
    <div className="empty">
      <div className="empty-mark">{mark || "•—"}</div>
      <h3>{title}</h3>
      <p>{desc}</p>
    </div>
  );
}

// ---------- Structured summary panel (renders summary_json if present) ----------
function SummaryJsonPanel({ json }) {
  // json can be a string (from DB) or already-parsed object (from live SSE).
  let data = null;
  if (!json) return null;
  if (typeof json === "string") {
    try { data = JSON.parse(json); } catch { return null; }
  } else if (typeof json === "object") {
    data = json;
  } else {
    return null;
  }
  if (!data) return null;

  const checklist = Array.isArray(data.checklist) ? data.checklist : [];
  const radar = Array.isArray(data.radar) ? data.radar : [];
  const needsContext = Array.isArray(data.needs_context) ? data.needs_context : [];
  const expired = Array.isArray(data.expired) ? data.expired : [];
  const takeaways = Array.isArray(data.key_takeaways) ? data.key_takeaways : [];
  const actionable = Array.isArray(data.actionable) ? data.actionable : [];
  const watchlist = Array.isArray(data.watchlist) ? data.watchlist : [];
  const kolOps = Array.isArray(data.kol_opinions) ? data.kol_opinions : [];
  const sentiment = data.sentiment && typeof data.sentiment === "object" ? data.sentiment : null;

  // If every relevant section is empty, don't render the panel at all —
  // broadcast output with no structured data isn't worth a hollow card.
  if (
    !checklist.length && !radar.length && !needsContext.length && !expired.length &&
    !takeaways.length && !actionable.length && !watchlist.length && !kolOps.length && !sentiment
  ) {
    return null;
  }

  return (
    <div className="summary-json-panel">
      <div className="summary-json-head">
        <span className="summary-json-title">Structured</span>
        <span className="summary-json-sub">summary_json</span>
      </div>

      {checklist.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">今日待查</div>
          <ul className="summary-json-list">
            {checklist.slice(0, 6).map((c, i) => (
              <li key={i}>
                {c.priority && <strong>[{c.priority}] </strong>}
                {c.target && <strong>{c.target}</strong>}
                {c.action && <span> — {c.action}</span>}
                {c.why_now && <span style={{ color: "var(--ink-3)" }}> · {c.why_now}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {radar.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">新項目雷達</div>
          <ul className="summary-json-list">
            {radar.slice(0, 10).map((r, i) => (
              <li key={i}>
                {r.target && <strong>{r.target}</strong>}
                {(r.status || r.strength) && (
                  <span style={{ color: "var(--ink-3)" }}> [{r.status || "—"}/{r.strength || "—"}]</span>
                )}
                {(r.why_now || r.signal) && <span> — {r.why_now || r.signal}</span>}
                {r.next_step && <span style={{ color: "var(--ink-3)" }}> · 下一步:{r.next_step}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {needsContext.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">裸 CA / 缺口</div>
          <ul className="summary-json-list">
            {needsContext.slice(0, 6).map((n, i) => (
              <li key={i}>
                {n.clue && <strong>{n.clue}</strong>}
                {n.missing && <span> — 缺:{n.missing}</span>}
                {n.next_step && <span style={{ color: "var(--ink-3)" }}> · {n.next_step}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {expired.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">過期 / 已追高</div>
          <ul className="summary-json-list">
            {expired.slice(0, 6).map((e, i) => (
              <li key={i}>
                {e.target && <strong>{e.target}</strong>}
                {e.reason && <span> — {e.reason}</span>}
                {e.next_step && <span style={{ color: "var(--ink-3)" }}> · {e.next_step}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {takeaways.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">重點</div>
          <ul className="summary-json-list">
            {takeaways.map((t, i) => (
              <li key={i}>
                {t.title && <strong>{t.title}</strong>}
                {t.title && t.summary && <span style={{ color: "var(--ink-3)" }}> — </span>}
                {t.summary && <span>{t.summary}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {actionable.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">行動</div>
          <ul className="summary-json-list">
            {actionable.map((a, i) => (
              <li key={i}>
                {a.action && <strong>{a.action}</strong>}
                {a.condition && <span style={{ color: "var(--ink-3)" }}> · 條件:{a.condition}</span>}
                {a.stop_loss && <span style={{ color: "var(--ink-3)" }}> · 停損:{a.stop_loss}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {watchlist.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">Watchlist</div>
          <div className="summary-json-chips">
            {watchlist.map((w, i) => (
              <span key={i} className="summary-json-chip">{String(w)}</span>
            ))}
          </div>
        </div>
      )}

      {kolOps.length > 0 && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">KOL 觀點</div>
          <ul className="summary-json-list">
            {kolOps.slice(0, 6).map((k, i) => (
              <li key={i}>
                {k.name && <strong>{k.name}</strong>}
                {k.role && <span style={{ color: "var(--ink-3)" }}> ({k.role})</span>}
                {k.stance && <span> — {k.stance}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {sentiment && (sentiment.overall || sentiment.consensus) && (
        <div className="summary-json-section">
          <div className="summary-json-section-head">情緒</div>
          {sentiment.overall && <div style={{ fontSize: 12 }}>{sentiment.overall}</div>}
          {sentiment.consensus && <div style={{ fontSize: 11, color: "var(--ink-3)" }}>共識:{sentiment.consensus}</div>}
        </div>
      )}
    </div>
  );
}

// ---------- Markdown helpers (lightweight, for AI dispatch output) ----------
// Parse `**bold**` / `*italic*` inside a line — returns array of React nodes.
function mdInline(text) {
  const parts = [];
  const re = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
  let last = 0, m, k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    if (m[1] != null) parts.push(<strong key={k++}>{m[1]}</strong>);
    else parts.push(<em key={k++}>{m[2]}</em>);
    last = re.lastIndex;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length ? parts : [text];
}

// Render an AI-produced markdown paragraph: handles `##`/`###` headings,
// `- ` bullet lists, `---` rule, and inline bold/italic. One paragraph in,
// one React element out (or null if blank).
function MdParagraph({ text }) {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (/^---+\s*$/.test(trimmed)) return <hr className="md-rule" />;
  const lines = trimmed.split(/\n/);
  const first = lines[0];
  const h = first.match(/^(#{1,6})\s+(.*)$/);
  if (h) {
    const level = Math.min(h[1].length, 4);
    const Tag = `h${level + 1}`; // h2 for ##, h3 for ###, etc.
    const rest = lines.slice(1).join("\n").trim();
    return (
      <>
        <Tag className={`md-h md-h${level}`}>{mdInline(h[2])}</Tag>
        {rest && <MdParagraph text={rest} />}
      </>
    );
  }
  // Bullet list — every line starts with `- ` or `• `
  if (lines.every(l => /^\s*[-•]\s+/.test(l))) {
    return (
      <ul className="md-list">
        {lines.map((l, i) => (
          <li key={i}>{mdInline(l.replace(/^\s*[-•]\s+/, ""))}</li>
        ))}
      </ul>
    );
  }
  // Default: paragraph with soft line breaks preserved
  return (
    <p>
      {lines.map((l, i) => (
        <React.Fragment key={i}>
          {i > 0 && <br />}
          {mdInline(l)}
        </React.Fragment>
      ))}
    </p>
  );
}

// Pull a clean headline string out of the first paragraph: strips leading
// `#`/`##` markers and the trailing `(…)` qualifier the prompt template uses.
function extractHeadline(firstPara) {
  if (!firstPara) return "";
  const firstLine = firstPara.split(/\n/)[0].trim();
  const m = firstLine.match(/^#{1,6}\s+(.*)$/);
  const text = m ? m[1] : firstLine;
  return text.replace(/\s*[\(（][^)）]*\d+[^)）]*[\)）]\s*$/, "").trim();
}

// ---------- Dispatch (editorial summary) ----------
function Dispatch({ chatName, hours, summary, events, saved, streaming, streamed, progress, stageTxt, modelLabel, messageCount, profileJson }) {
  const rawSrc = streaming ? (streamed || "") : (summary || "");
  // Defensive: if any legacy `===JSON===` tail leaks into the stream
  // (broadcast moved JSON to a separate Stage-2 call, but other profiles or
  // saved historical summaries may still contain it), hide it from the body.
  const src = rawSrc.split(/\n*===JSON===/)[0];
  const paragraphs = src.split(/\n{2,}/).map(p => p.trim()).filter(Boolean);
  const hasText = paragraphs.length > 0;
  const firstIsHeading = hasText && /^#{1,6}\s+/.test(paragraphs[0].split(/\n/)[0]);
  const headline = hasText ? extractHeadline(paragraphs[0]) : "";
  // If the first paragraph is just the heading, drop it; if it has trailing
  // body lines after the heading, keep those as the lead paragraph.
  let body;
  if (!hasText) {
    body = [];
  } else if (firstIsHeading) {
    const rest = paragraphs[0].split(/\n/).slice(1).join("\n").trim();
    body = (rest ? [rest] : []).concat(paragraphs.slice(1));
  } else {
    body = paragraphs.slice(1);
  }
  // Sweep animation while waiting for the model's first token (progress stays
  // at 65 until tokens start arriving, at which point frontend creep moves it).
  const isPending = streaming && progress === 65 && !(streamed && streamed.length);

  return (
    <div className="dispatch fade-up">
      {streaming && (
        <div className="progress-panel">
          <div className="progress-stage-txt">
            <span className="spinner" />
            {stageTxt || "Working…"}
          </div>
          <div className={"progress-track" + (isPending ? " is-pending" : "")}>
            <div className="progress-fill" style={{ width: (progress || 0) + "%" }} />
          </div>
          <div className="progress-footer">
            <span>Model · Claude {modelLabel || "Sonnet"}</span>
            <span>{isPending ? `${progress || 0}% · 等待模型回應…` : `${progress || 0}%`}</span>
          </div>
        </div>
      )}

      <div className="dispatch-header">
        <div className="dispatch-kicker">
          <span>Dispatch</span>
          <span style={{ color: "var(--ink-3)" }}>
            {new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}
          </span>
        </div>
        <h1 className="dispatch-title">
          {hasText ? headline : (streaming ? "Writing…" : "No summary yet")}
        </h1>
        <div className="dispatch-byline">
          <span>FROM <strong>{chatName || "—"}</strong></span>
          <span>WINDOW <strong>{hours}h</strong></span>
          {messageCount != null && <span>MESSAGES <strong>{messageCount}</strong></span>}
          {events && events.length > 0 && <span>EVENTS <strong>{events.length}</strong></span>}
          {saved && <span style={{ color: "var(--success, #4a6a3a)" }}>✓ saved</span>}
        </div>
      </div>

      <div className={cls("dispatch-body", streaming && "streaming")}>
        {body.length > 0
          ? body.map((p, i) => <MdParagraph key={i} text={p} />)
          : (!hasText && !streaming)
            ? <p style={{ color: "var(--ink-3)" }}>—</p>
            : null}
      </div>

      {!streaming && profileJson && <SummaryJsonPanel json={profileJson} />}

      {!streaming && events && events.length > 0 && (
        <div className="events-block">
          <div className="events-block-head">
            <h3>Pinned events</h3>
            <span className="cnt">{events.length} items{saved ? " · saved to memory" : ""}</span>
          </div>
          {events.map((e, i) => (
            <div className="event" key={e.id != null ? e.id : i}>
              <div className={cls("event-tag", e.importance || "normal")}>
                {e.importance === "high" ? "Priority" : e.importance === "low" ? "Minor" : "Notable"}
              </div>
              <div className="event-body">
                <div className="h">{e.title}</div>
                {(e.description || e.desc) && <div className="d">{e.description || e.desc}</div>}
                {(e.tags || e.source_chat) && <div className="tags">{[e.tags, e.source_chat].filter(Boolean).join(" · ")}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- Messages list ----------
function MessagesList({ messages, watchlist, topicsById }) {
  const [alertIdx, setAlertIdx] = useState(-1);
  const listRef = useRef(null);

  const alerts = useMemo(
    () => messages.map((m, i) => (m.alerts && m.alerts.length) ? i : -1).filter(i => i >= 0),
    [messages]
  );
  const alertCount = alerts.length;
  const kws = useMemo(() => (watchlist || []).map(w => w.keyword).filter(Boolean), [watchlist]);

  useEffect(() => {
    if (alertIdx < 0 || !listRef.current) return;
    const idx = alerts[alertIdx];
    if (idx == null) return;
    const el = listRef.current.querySelector(`[data-msg-idx="${idx}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("msg-flash");
    const t = setTimeout(() => el.classList.remove("msg-flash"), 1500);
    return () => clearTimeout(t);
  }, [alertIdx, alerts]);

  if (!messages || messages.length === 0) {
    return (
      <div style={{ padding: "20px 0", textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
        還沒有訊息 — 點右上 Re-fetch 擷取。
      </div>
    );
  }

  const senders = [...new Set(messages.map(m => m.from))];
  const mediaCount = messages.filter(m => m.media).length;

  return (
    <>
      {alertCount > 0 && (
        <div className="alert-banner">
          <span>⚑</span>
          <span>Detected <strong>{alertCount}</strong> message{alertCount>1?"s":""} matching watchlist keywords</span>
          <div className="alert-nav">
            <span className="mono" style={{fontSize:10}}>{Math.max(0, alertIdx+1)}/{alertCount}</span>
            <button onClick={()=>setAlertIdx(i => (i <= 0 ? alertCount - 1 : i - 1))}>↑</button>
            <button onClick={()=>setAlertIdx(i => (i >= alertCount - 1 ? 0 : i + 1))}>↓</button>
          </div>
        </div>
      )}
      <div className="stats-bar">
        <span className="chip">{messages.length} messages</span>
        <span className="chip">{senders.length} senders</span>
        {mediaCount > 0 && <span className="chip">{mediaCount} media</span>}
      </div>

      <div className="msg-list" ref={listRef}>
        {messages.map((m, idx) => {
          const hasAlert = m.alerts && m.alerts.length;
          const time = formatTime(m.date) || m.time || "";
          const topicTitle = (topicsById && m.topic_id && topicsById[m.topic_id]) || "";
          return (
            <div key={m.id ?? idx} data-msg-idx={idx} className={cls("msg", hasAlert && "alert")}>
              <div className="msg-time-col">{time}</div>
              <div>
                <div className="msg-sender">
                  {m.from}
                  {m.username && <span className="at">@{m.username}</span>}
                  {topicTitle && <span className="at" style={{ color: "var(--accent, #9a5b2a)" }}>📂 {topicTitle}</span>}
                </div>
                <div className="msg-text">{highlightKw(m.text || "", m.alerts && m.alerts.length ? m.alerts : kws)}</div>
                {m.media && <span className="msg-media">▣ {mediaLabel(m.media)}</span>}
              </div>
              <div className="msg-alerts">
                {hasAlert && m.alerts.map((a, i) => <span key={i} className="msg-alert-pill">{a}</span>)}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

// ---------- Coin search result (ticker / CA) ----------
function renderMarkedSnippet(snippet) {
  return String(snippet || "").split(/(«[^»]*»)/g).map((part, i) => {
    if (part.startsWith("«") && part.endsWith("»")) {
      return <mark key={i}>{part.slice(1, -1)}</mark>;
    }
    return <React.Fragment key={i}>{part}</React.Fragment>;
  });
}

function fmtHolderUsd(v) {
  const n = Number(v || 0);
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if (a >= 1_000_000) return `${sign}$${(a / 1_000_000).toFixed(2)}M`;
  if (a >= 1_000) return `${sign}$${(a / 1_000).toFixed(1)}K`;
  return `${sign}$${Math.round(a).toLocaleString()}`;
}

function shortAddr(addr) {
  const s = String(addr || "").trim();
  if (!s) return "no address";
  if (s.length <= 14) return s;
  return `${s.slice(0, 6)}…${s.slice(-4)}`;
}

function txUrlFromSnippet(snippet) {
  const s = String(snippet || "");
  const md = s.match(/\]\((https?:\/\/[^)\s]+\/tx\/[^)\s]+)\)/i);
  if (md) return md[1];
  const raw = s.match(/https?:\/\/[^)\s]+\/tx\/[^)\s]+/i);
  return raw ? raw[0] : "";
}

function fmtPct(v, digits = 0) {
  const n = Number(v || 0);
  if (!Number.isFinite(n) || n === 0) return "";
  return `${n.toFixed(digits)}%`;
}

function holderMetricTone(v) {
  const n = Number(v || 0);
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "";
}

function CopyWalletAddress({ address }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e) => {
    e.stopPropagation();
    const value = String(address || "").trim();
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1100);
    } catch {}
  };
  return (
    <button
      type="button"
      className={cls("holder-address", "mono", copied && "copied")}
      title={address ? `Copy ${address}` : "No address"}
      onClick={copy}
      disabled={!address}
    >
      {copied ? "copied" : shortAddr(address)}
    </button>
  );
}

function HolderRows({ title, rows, tone, onOpenDay }) {
  if (!rows || rows.length === 0) return null;
  return (
    <div className="holder-section">
      <div className="holder-section-title" style={{ color: tone || "var(--ink-2)" }}>
        {title}
      </div>
      {rows.map((w, idx) => (
        <div key={`${w.wallet_addr || w.wallet_name}-${idx}`} className="holder-card">
          <div className="holder-card-head">
            <div className="holder-name-wrap">
              <div className="holder-name">{w.wallet_name || "Unknown wallet"}</div>
              <CopyWalletAddress address={w.wallet_addr} />
            </div>
            <div className={cls("holder-latest", String(w.last_action || "").toLowerCase())}>
              {w.last_action || "?"}
              <span>{w.last_time || compactDateTime(w.last_seen)}</span>
            </div>
          </div>

          <div className="holder-metrics">
            <span>buy <strong>{fmtHolderUsd(w.buy_usd)}</strong></span>
            <span>sell <strong>{fmtHolderUsd(w.sell_usd)}</strong></span>
            <span className={holderMetricTone(w.net_flow_usd)}>net <strong>{fmtHolderUsd(w.net_flow_usd)}</strong></span>
          </div>

          {(w.holds_amount || w.holds_pct || w.sold_pct || w.has_pnl) && (
            <div className="holder-tags">
              {w.holds_amount && (
                <span>holds <strong>{w.holds_amount}</strong>{w.holds_pct ? ` · ${fmtPct(w.holds_pct, 2)}` : ""}</span>
              )}
              {w.sold_pct ? <span>sold <strong>{fmtPct(w.sold_pct)}</strong></span> : null}
              {w.has_pnl ? (
                <span className={holderMetricTone(w.pnl_usd)}>
                  PnL <strong>{fmtHolderUsd(w.pnl_usd)}</strong>{w.pnl_pct ? ` · ${fmtPct(w.pnl_pct, 1)}` : ""}
                </span>
              ) : null}
            </div>
          )}

          <div className="holder-events">
            {(w.events || []).slice(0, 3).map((ev, i) => {
              const txUrl = txUrlFromSnippet(ev.snippet);
              const action = String(ev.action || "").toLowerCase();
              return (
                <div
                  key={i}
                  className="holder-event"
                  onClick={() => ev.date && onOpenDay(ev.date.slice(0, 10))}
                  role={ev.date ? "button" : undefined}
                >
                  <span className={cls("holder-action", action)}>{ev.action || "?"}</span>
                  <span className="mono holder-event-time">{ev.time || compactDateTime(ev.date)}</span>
                  <strong className="holder-event-usd">{fmtHolderUsd(ev.usd_value)}</strong>
                  <span className="holder-event-detail">
                    {ev.holds_amount ? <span>holds {ev.holds_amount}</span> : null}
                    {ev.sold_pct ? <span>sold {fmtPct(ev.sold_pct)}</span> : null}
                    {ev.has_pnl ? <span className={holderMetricTone(ev.pnl_usd)}>PnL {fmtHolderUsd(ev.pnl_usd)}</span> : null}
                    <span className="holder-event-source">{ev.chat_name || ""}</span>
                  </span>
                  {txUrl && (
                    <a
                      href={txUrl}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                    >tx</a>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function HolderBucketTabs({ data, onOpenDay, compact = false }) {
  const groups = useMemo(() => ([
    {
      id: "holding",
      label: "Holding",
      title: `LIKELY HOLDING · ${data?.holder_count || 0}`,
      count: data?.holder_count || 0,
      rows: data?.holders || [],
      tone: "var(--success,#4a6a3a)",
      empty: "目前沒有錢包被推斷為仍持有。",
    },
    {
      id: "exited",
      label: "Exited",
      title: `EXITED / SOLD OUT · ${data?.exited_count || 0}`,
      count: data?.exited_count || 0,
      rows: data?.exited || [],
      tone: "var(--alert)",
      empty: "目前沒有錢包被推斷為已出清。",
    },
    {
      id: "unknown",
      label: "Unclear",
      title: `UNCLEAR · ${data?.unknown_count || 0}`,
      count: data?.unknown_count || 0,
      rows: data?.unknown || [],
      tone: "var(--ink-3)",
      empty: "目前沒有狀態不明的錢包。",
    },
  ]), [data]);

  const defaultTab = groups.find(g => g.count > 0)?.id || "holding";
  const [active, setActive] = useState(defaultTab);

  useEffect(() => {
    setActive(defaultTab);
  }, [data?.ca, defaultTab]);

  const group = groups.find(g => g.id === active) || groups[0];
  const rows = compact && group.id !== "holding" ? group.rows.slice(0, 5) : group.rows;
  return (
    <div className="holder-buckets">
      <div className="holder-tabs" role="tablist" aria-label="Holder status">
        {groups.map(g => (
          <button
            key={g.id}
            className={cls("holder-tab", g.id, active === g.id && "active")}
            onClick={() => setActive(g.id)}
            role="tab"
            aria-selected={active === g.id}
          >
            <span>{g.label}</span>
            <strong>{g.count}</strong>
          </button>
        ))}
      </div>
      {rows.length ? (
        <HolderRows
          title={group.title}
          rows={rows}
          tone={group.tone}
          onOpenDay={onOpenDay}
        />
      ) : (
        <div className="holders-none">{group.empty}</div>
      )}
    </div>
  );
}

function CoinSearchView({ result, onOpenDay, busy }) {
  const q = result.query || "";
  const mode = result.mode || "ticker";
  const perChat = result.per_chat || [];
  const events = result.events || [];
  const notes = result.notes || [];
  const cas = result.ca_candidates || [];
  const holders = result.holders || null;
  const isCA = mode === "ca";
  return (
    <div style={{ padding: "12px 16px" }}>
      <div style={{ fontSize: 11, color: "var(--ink-3)", marginBottom: 4 }}>
        {isCA ? "CA" : "Ticker"}「{q}」{busy ? " · 查詢中..." : ""}
      </div>
      <div style={{ fontSize: 10, color: "var(--ink-3)", marginBottom: 10, fontFamily: "var(--mono)" }}>
        {perChat.length} chats · {result.total_msgs || result.total_hits || 0} mentions · {events.length} events · {notes.length} notes
      </div>

      {isCA && holders && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--accent)", margin: "0 0 6px", letterSpacing: "0.08em" }}>
            SMART MONEY HOLDERS
          </div>
          <div style={{ fontSize: 10, color: "var(--ink-3)", marginBottom: 8, fontFamily: "var(--mono)" }}>
            {holders.symbol ? `$${holders.symbol} · ` : ""}{holders.chain || "chain ?"} · {holders.days}d · {holders.parsed_events} parsed events · {holders.total_wallets} wallets
          </div>
          <HolderBucketTabs data={holders} onOpenDay={onOpenDay} compact />
          {holders.total_wallets === 0 && (
            <div style={{ padding: 12, border: "1px solid var(--rule)", fontSize: 12, color: "var(--ink-3)" }}>
              沒有在已 fetch 的 wallet_log 訊息裡解析到這個 CA 的持倉紀錄。
            </div>
          )}
        </div>
      )}

      {notes.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--accent)", margin: "0 0 6px", letterSpacing: "0.08em" }}>
            ✎ MY REVIEW
          </div>
          {notes.map(n => (
            <div key={"cn"+n.id} className="coin-note" onClick={()=>onOpenDay(n.date)}>
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--ink-3)", marginBottom: 4 }}>
                {n.date}
              </div>
              <div style={{ fontSize: 12, lineHeight: 1.55 }}>{n.content}</div>
              {n.tags && (
                <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--ink-3)", marginTop: 4 }}>
                  {n.tags}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {perChat.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "0 0 6px", letterSpacing: "0.08em" }}>
            💬 CHATS
          </div>
          {perChat.map((c, idx) => (
            <div key={"cc"+idx} className="coin-chat">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
                <div style={{ fontSize: 12, fontWeight: 600 }}>{c.chat_name}</div>
                <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--ink-3)" }}>
                  {c.hit_days ? `${c.hit_days}d · ` : ""}{c.msg_count} msgs
                </div>
              </div>
              <div style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--ink-3)", marginBottom: 4 }}>
                {c.first_date}{c.last_date !== c.first_date ? ` → ${c.last_date}` : ""}
              </div>
              {(c.samples || []).slice(0, 3).map((s, i) => (
                <div key={i}
                     className="coin-sample"
                     onClick={()=>s.date && onOpenDay(s.date.slice(0,10))}
                     style={{ cursor: s.date ? "pointer" : "default" }}>
                  {s.snippet ? (
                    <span>{renderMarkedSnippet(s.snippet)}</span>
                  ) : (
                    <>
                      {s.sender_name && (
                        <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3)", marginRight: 6 }}>
                          {s.sender_name}
                        </span>
                      )}
                      {(s.text || "").slice(0, 140)}
                      {(s.text || "").length > 140 ? "…" : ""}
                    </>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {events.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "0 0 6px", letterSpacing: "0.08em" }}>
            ◉ EVENTS
          </div>
          {events.map(e => (
            <div key={"ce"+e.id} className="timeline-row" onClick={()=>onOpenDay(e.date)}>
              <div className="timeline-date">
                {formatDate(e.date).day}
                <span className="month">{formatDate(e.date).month}</span>
              </div>
              <div>
                <div style={{ fontWeight: 600, fontSize: 12 }}>{e.title}</div>
                <div style={{ fontSize: 11, color: "var(--ink-2)", marginTop: 4 }}>{e.description}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {!isCA && cas.length > 0 && (
        <div>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "0 0 6px", letterSpacing: "0.08em" }}>
            CA CANDIDATES
          </div>
          {cas.slice(0, 8).map((c, i) => (
            <div key={i} style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--ink-2)", padding: "3px 0", wordBreak: "break-all" }}>
              {c.ca} <span style={{ color: "var(--ink-3)" }}>· {c.chat_count} chats · {c.msg_count} msgs</span>
            </div>
          ))}
        </div>
      )}

      {perChat.length === 0 && events.length === 0 && notes.length === 0 && (
        <div style={{ textAlign: "center", padding: 20, color: "var(--ink-3)", fontSize: 12 }}>找不到相關記錄</div>
      )}
    </div>
  );
}

// ---------- Memory search results: per-match clickable snippets ----------
// Each row in the result list shows the date / chat header, then one clickable
// snippet per occurrence of the query. Clicking a snippet opens the day with
// `highlight` + `target` so DayView scrolls to that exact match.
function SnippetRow({ snippet, onClick }) {
  return (
    <div className="search-snippet" onClick={onClick} role="button" tabIndex={0}>
      {snippet.leading && <span className="snippet-ellipsis">…</span>}
      <span>{snippet.before}</span>
      <mark className="search-hit">{snippet.match}</mark>
      <span>{snippet.after}</span>
      {snippet.trailing && <span className="snippet-ellipsis">…</span>}
    </div>
  );
}

function SearchResultsList({ searchQuery, searchBusy, searchResult, onOpenDay }) {
  const summaries = searchResult.summaries || [];
  const events = searchResult.events || [];
  const notes = searchResult.notes || [];

  // Total match count across all rendered fields, for the header chip.
  const totalMatches = useMemo(() => {
    let n = 0;
    summaries.forEach(s => { n += countMatches(s.summary, searchQuery); });
    events.forEach(e => {
      n += countMatches(e.title, searchQuery);
      n += countMatches(e.description, searchQuery);
    });
    notes.forEach(n2 => { n += countMatches(n2.content, searchQuery); });
    return n;
  }, [summaries, events, notes, searchQuery]);

  return (
    <div style={{ padding: "12px 16px" }}>
      <div style={{ fontSize: 11, color: "var(--ink-3)", marginBottom: 8 }}>
        搜尋「{searchQuery}」
        {totalMatches > 0 && <span> · {totalMatches} 個關鍵字</span>}
        {searchBusy ? " · 查詢中..." : ""}
      </div>

      {summaries.length > 0 && (
        <>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "8px 0 4px" }}>SUMMARIES</div>
          {summaries.map(s => {
            const snippets = extractSnippets(s.summary, searchQuery, 36, 6);
            const slot = s.summary_slot || "";
            const open = (target) => onOpenDay(s.date, slot, { highlight: searchQuery, target });
            return (
              <div key={"s"+s.id} className="search-card">
                <div className="search-card-head" onClick={() => open(snippets.length ? { field: `summary-${s.id}`, n: 0 } : null)}>
                  <div className="timeline-date">
                    {formatDate(s.date).day}
                    <span className="month">{formatDate(s.date).month}</span>
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 12 }}>
                      {s.chat_name}
                      {(slot || formatTime(s.created_at)) && (
                        <span className="chip" style={{ marginLeft: 6 }}>
                          {slot || formatTime(s.created_at)}
                        </span>
                      )}
                      {snippets.length > 0 && (
                        <span className="chip accent" style={{ marginLeft: 6 }}>
                          {snippets.length}{snippets.length === 6 ? "+" : ""} hit
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                {snippets.length > 0 ? (
                  <div className="search-snippets">
                    {snippets.map(sn => (
                      <SnippetRow
                        key={sn.n}
                        snippet={sn}
                        onClick={() => open({ field: `summary-${s.id}`, n: sn.n })}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="search-snippets">
                    <div className="search-snippet muted" onClick={() => open(null)}>
                      {cleanPreview(s.summary)}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

      {events.length > 0 && (
        <>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "12px 0 4px" }}>EVENTS</div>
          {events.map(e => {
            const titleSnips = extractSnippets(e.title, searchQuery, 28, 3);
            const descSnips  = extractSnippets(e.description, searchQuery, 36, 6);
            const open = (target) => onOpenDay(e.date, "", { highlight: searchQuery, target });
            const totalHits = titleSnips.length + descSnips.length;
            return (
              <div key={"e"+e.id} className="search-card">
                <div className="search-card-head" onClick={() => open(
                  titleSnips.length ? { field: `event-title-${e.id}`, n: 0 }
                  : descSnips.length ? { field: `event-desc-${e.id}`, n: 0 }
                  : null
                )}>
                  <div className="timeline-date">
                    {formatDate(e.date).day}
                    <span className="month">{formatDate(e.date).month}</span>
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 12 }}>
                      <Highlighted text={e.title} query={searchQuery} />
                      {totalHits > 0 && (
                        <span className="chip accent" style={{ marginLeft: 6 }}>
                          {totalHits} hit
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                {(titleSnips.length > 0 || descSnips.length > 0) ? (
                  <div className="search-snippets">
                    {titleSnips.map(sn => (
                      <SnippetRow
                        key={"t"+sn.n}
                        snippet={sn}
                        onClick={() => open({ field: `event-title-${e.id}`, n: sn.n })}
                      />
                    ))}
                    {descSnips.map(sn => (
                      <SnippetRow
                        key={"d"+sn.n}
                        snippet={sn}
                        onClick={() => open({ field: `event-desc-${e.id}`, n: sn.n })}
                      />
                    ))}
                  </div>
                ) : (
                  e.description && (
                    <div className="search-snippets">
                      <div className="search-snippet muted" onClick={() => open(null)}>
                        {cleanPreview(e.description)}
                      </div>
                    </div>
                  )
                )}
              </div>
            );
          })}
        </>
      )}

      {notes.length > 0 && (
        <>
          <div style={{ fontSize: 10, fontWeight: 600, color: "var(--ink-2)", margin: "12px 0 4px" }}>NOTES</div>
          {notes.map(n => {
            const snippets = extractSnippets(n.content, searchQuery, 36, 6);
            const open = (target) => onOpenDay(n.date, "", { highlight: searchQuery, target });
            return (
              <div key={"n"+n.id} className="search-card">
                <div className="search-card-head" onClick={() => open(snippets.length ? { field: `note-${n.id}`, n: 0 } : null)}>
                  <div className="timeline-date">
                    {formatDate(n.date).day}
                    <span className="month">{formatDate(n.date).month}</span>
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12 }}>
                      Note
                      {snippets.length > 0 && (
                        <span className="chip accent" style={{ marginLeft: 6 }}>
                          {snippets.length}{snippets.length === 6 ? "+" : ""} hit
                        </span>
                      )}
                    </div>
                    {n.tags && <div className="mono" style={{ fontSize: 10, color: "var(--ink-3)", marginTop: 2 }}>{n.tags}</div>}
                  </div>
                </div>
                {snippets.length > 0 ? (
                  <div className="search-snippets">
                    {snippets.map(sn => (
                      <SnippetRow
                        key={sn.n}
                        snippet={sn}
                        onClick={() => open({ field: `note-${n.id}`, n: sn.n })}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="search-snippets">
                    <div className="search-snippet muted" onClick={() => open(null)}>
                      {cleanPreview(n.content)}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

      {!summaries.length && !events.length && !notes.length && (
        <div style={{ textAlign: "center", padding: 20, color: "var(--ink-3)", fontSize: 12 }}>找不到相關記錄</div>
      )}
    </div>
  );
}

// ---------- Memory column ----------
function MemoryCol({
  timeline, activeDate, activeSlot, onOpenDay,
  qaHistory, onAsk, qaLoading,
  onSearch, searchQuery, setSearchQuery, searchResult, searchBusy, onClearSearch,
  onExport, onImport, onDeleteDay,
  hasAI,
}) {
  const [q, setQ] = useState("");
  const progressLabel = (t) => {
    if (!t.run_status || !t.expected_chats) return "";
    if (t.run_status === "running") {
      return `${t.completed_chats}/${t.expected_chats} done`;
    }
    if (t.run_status === "partial" || t.run_status === "failed") {
      return `${t.completed_chats}/${t.expected_chats} done`;
    }
    return "";
  };

  const ask = () => {
    if (!q.trim()) return;
    onAsk(q.trim());
    setQ("");
  };

  const onSearchKey = (e) => {
    if (e.key === "Enter") onSearch();
  };

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 02 · Memory</div>
        <div className="col-title"><em>Archive</em> of days.</div>
        <div className="col-sub">
          {timeline.length} dispatches stored{hasAI ? " · ask anything" : ""}
        </div>
      </div>

      <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--rule)", display: "flex", gap: 6 }}>
        <input
          className="input"
          placeholder="Search memory · try $TICKER or CA…"
          style={{ fontSize: 12 }}
          value={searchQuery}
          onChange={(e)=>setSearchQuery(e.target.value)}
          onKeyDown={onSearchKey}
        />
        {searchQuery ? (
          <button className="btn btn-sm" title="Clear" onClick={onClearSearch}>×</button>
        ) : null}
        <button className="btn btn-sm" title="Export JSON" onClick={onExport}>↑</button>
        <label className="btn btn-sm" title="Import JSON" style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
          ↓
          <input type="file" accept="application/json" style={{ display: "none" }} onChange={(e)=>onImport(e.target.files && e.target.files[0])} />
        </label>
      </div>

      <div className="col-body">
        {searchQuery && searchResult && searchResult._kind === "coin" ? (
          <CoinSearchView result={searchResult} onOpenDay={onOpenDay} busy={searchBusy} />
        ) : searchQuery && searchResult ? (
          <SearchResultsList
            searchQuery={searchQuery}
            searchBusy={searchBusy}
            searchResult={searchResult}
            onOpenDay={onOpenDay}
          />
        ) : (
          <div className="timeline-list">
            {timeline.length === 0 ? (
              <div style={{ textAlign: "center", padding: 24, color: "var(--ink-3)", fontSize: 12 }}>
                還沒有記憶。對訊息按 ✧ Summarize 開始建立。
              </div>
            ) : timeline.map((t) => {
              const { day, month } = formatDate(t.date);
              const slot = (t.summary_slot || "").trim();
              const key = t.timeline_key || `${t.date}::${slot}`;
              const activeKey = activeDate ? `${activeDate}::${activeSlot || ""}` : "";
              const isActive = key === activeKey;
              const headlines = (t.event_titles || []).map(x => x.title).slice(0, 3);
              const highCount = (t.event_titles || []).filter(e => e.importance === "high").length;
              const canDelete = onDeleteDay && t.summaries > 0;
              const progress = progressLabel(t);
              return (
                <div
                  key={key}
                  className={cls("timeline-row", isActive && "active")}
                  onClick={() => onOpenDay(t.date, slot)}
                  style={{ position: "relative" }}
                >
                  <div className="timeline-date">
                    {day}
                    <span className="month">{month}</span>
                    {slot && <span className="slot">{slot}</span>}
                  </div>
                  <div>
                    {headlines.length > 0 && (
                      <div className="timeline-headlines">
                        {headlines.map((h, j) => <span key={j} className="hl">{h}</span>)}
                      </div>
                    )}
                    <div className="timeline-chips">
                      {slot && <span className="chip accent">{slot} UTC+8</span>}
                      {t.run_status === "running" && <span className="chip accent">running</span>}
                      {(t.run_status === "partial" || t.run_status === "failed") && <span className="chip alert">{t.run_status}</span>}
                      {progress && <span className="chip">{progress}</span>}
                      {t.summaries > 0 && <span className="chip">{t.summaries} brief</span>}
                      {t.events > 0 && <span className={cls("chip", highCount > 0 && "alert")}>{t.events} events</span>}
                      {t.notes > 0 && <span className="chip accent">{t.notes} notes</span>}
                      {t.total_msgs > 0 && <span className="chip">{t.total_msgs} msgs</span>}
                    </div>
                  </div>
                  {canDelete && (
                    <button
                      className="kw-remove"
                      onClick={(e) => { e.stopPropagation(); onDeleteDay(t.date, slot); }}
                      title={`刪除 ${t.date} 的 ${t.summaries} 筆 summary(events / notes 保留)`}
                      style={{
                        position: "absolute", top: 6, right: 8,
                        opacity: 0.35, transition: "opacity 120ms",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.opacity = 1; }}
                      onMouseLeave={(e) => { e.currentTarget.style.opacity = 0.35; }}
                    >×</button>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {!searchQuery && (
          <div className="qa-wrap">
            <div className="label">Ask the archive</div>
            <div className="qa-input-row">
              <input
                className="input"
                placeholder={hasAI ? "上週 ETH 有什麼大事？" : "未啟用 AI — 請先設定 CLAUDE_API_KEY"}
                disabled={!hasAI || qaLoading}
                value={q}
                onChange={(e)=>setQ(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && ask()}
              />
              <button className="btn btn-sm btn-accent" onClick={ask} disabled={!hasAI || qaLoading || !q.trim()}>
                {qaLoading ? <span className="spinner" /> : "Ask →"}
              </button>
            </div>
            {qaHistory.map((h, i) => (
              <div key={i} className="qa-bubble">
                <div className="qa-q">{h.q}</div>
                <div className="qa-a">{h.a}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- Shared formatters ----------
function compactDateTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace(/\+\d\d:\d\d$/, "").slice(0, 16);
}

function categoryLabel(value) {
  const s = String(value || "").trim();
  if (!s || /^\?+$/.test(s)) return "匯入";
  return s;
}

// ---------- Smart-money CA holder lookup ----------
function SmartHoldersCol({ query, setQuery, days, setDays, result, busy, onRun }) {
  const onKey = (e) => {
    if (e.key === "Enter") onRun();
  };
  const total = result ? (result.total_wallets || 0) : 0;
  const holderCount = result ? (result.holder_count || 0) : 0;
  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 05 · Smart Money</div>
        <div className="col-title"><em>CA</em> holders.</div>
        <div className="col-sub">從已 fetch 的錢包交易紀錄推斷誰還有持倉</div>
      </div>
      <div className="holders-panel">
        <label className="label">Token contract address</label>
        <textarea
          className="input holders-ca-input mono"
          placeholder="貼上 CA 或 dexscreener / defined 連結..."
          value={query}
          onChange={(e)=>setQuery(e.target.value)}
          onKeyDown={onKey}
          rows={4}
        />
        <div className="holders-controls">
          <select className="input" value={days} onChange={(e)=>setDays(Number(e.target.value))}>
            <option value={30}>30d</option>
            <option value={90}>90d</option>
            <option value={180}>180d</option>
            <option value={365}>365d</option>
            <option value={3650}>all</option>
          </select>
          <button className="btn btn-primary" onClick={onRun} disabled={busy || !String(query || "").trim()}>
            {busy ? <><span className="spinner" /> 查詢中</> : "查持倉"}
          </button>
        </div>
        <div className="holders-hint">
          只會讀取已歸類為 wallet_log 的訊息；沒有 fetch 過的群或頻道不會出現在結果裡。
        </div>
      </div>
      <div className="holders-mini">
        {result ? (
          <>
            <div className="holders-mini-row">
              <span>CA</span>
              <strong className="mono">{result.ca || "-"}</strong>
            </div>
            <div className="holders-mini-row">
              <span>Wallets</span>
              <strong>{holderCount} / {total}</strong>
            </div>
            <div className="holders-mini-row">
              <span>Parsed</span>
              <strong>{result.parsed_events || 0} events</strong>
            </div>
          </>
        ) : (
          <div className="holders-empty-note">貼上 CA 後會在右側看到持倉推斷。</div>
        )}
      </div>
    </div>
  );
}

function SmartHoldersStage({ query, result, busy, error, onOpenDay }) {
  const meta = result
    ? `${result.days}d · ${result.total_wallets || 0} wallets · ${result.parsed_events || 0} parsed events`
    : "Paste CA to infer holders from wallet logs";
  return (
    <div className="stage-body">
      <StageHead title="CA Holders" meta={meta} />
      {busy ? (
        <Empty mark="CA" title="Scanning wallet logs" desc="正在比對已 fetch 的 smart-money 交易紀錄…" />
      ) : error ? (
        <Empty mark="!" title="查詢失敗" desc={error} />
      ) : !result ? (
        <Empty mark="CA" title="Paste a contract address" desc="左邊貼 CA，就會列出可能仍持有、已出清、以及狀態不明的錢包。" />
      ) : (
        <div className="holders-stage">
          <div className="holders-summary">
            <div>
              <div className="label">Token</div>
              <div className="holders-token">
                {result.symbol ? `$${result.symbol}` : "Unknown token"}
                <span>{result.chain || "chain ?"}</span>
              </div>
              <div className="mono holders-ca">{result.ca || query}</div>
            </div>
            <div className="holders-stats">
              <div><strong>{result.holder_count || 0}</strong><span>holding</span></div>
              <div><strong>{result.exited_count || 0}</strong><span>exited</span></div>
              <div><strong>{result.unknown_count || 0}</strong><span>unclear</span></div>
            </div>
          </div>

          <HolderBucketTabs data={result} onOpenDay={onOpenDay} />
          {result.total_wallets === 0 && (
            <div className="holders-none">
              沒有在已 fetch 的 wallet_log 訊息裡解析到這個 CA 的持倉紀錄。
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- Watchtower column (entity radar) ----------
function WatchtowerCol({
  entities, loading, windowDays, setWindowDays,
  kindFilter, setKindFilter, search, setSearch,
  activeKey, onSelect,
}) {
  const filtered = useMemo(() => {
    const q = (search || "").trim().toLowerCase();
    return (entities || []).filter((e) => {
      if (kindFilter && e.kind !== kindFilter) return false;
      if (!q) return true;
      return e.value.toLowerCase().includes(q);
    });
  }, [entities, search, kindFilter]);

  const counts = useMemo(() => {
    const c = { symbol: 0, handle: 0, ca: 0 };
    (entities || []).forEach((e) => { c[e.kind] = (c[e.kind] || 0) + 1; });
    return c;
  }, [entities]);

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 03 · Watchtower</div>
        <div className="col-title"><em>Entity</em> radar</div>
        <div className="col-sub">
          {loading ? "scanning…" : `${entities.length} entities · ${windowDays}d window`}
        </div>
      </div>
      <div className="note-form">
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            className="input"
            placeholder="Filter by symbol / handle / CA…"
            value={search || ""}
            onChange={(e)=>setSearch(e.target.value)}
          />
          <select
            className="input mono"
            value={windowDays}
            onChange={(e)=>setWindowDays(Number(e.target.value))}
            style={{ width: 70, fontSize: 11 }}
          >
            <option value={3}>3d</option>
            <option value={7}>7d</option>
            <option value={14}>14d</option>
            <option value={30}>30d</option>
            <option value={60}>60d</option>
          </select>
        </div>
        <div style={{ display: "flex", gap: 4, marginTop: 8 }}>
          {[
            { id: "", label: "All", n: entities.length },
            { id: "symbol", label: "$Sym", n: counts.symbol },
            { id: "handle", label: "@KOL", n: counts.handle },
            { id: "ca", label: "CA", n: counts.ca },
          ].map((f) => (
            <button
              key={f.id || "all"}
              className={cls("filter-chip", kindFilter === f.id && "active")}
              onClick={() => setKindFilter(f.id)}
            >
              {f.label} {f.n > 0 && <span style={{ opacity: 0.6 }}>{f.n}</span>}
            </button>
          ))}
        </div>
      </div>
      <div className="col-body">
        {loading ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
            <span className="spinner" /> 掃描記憶庫…
          </div>
        ) : filtered.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
            {entities.length === 0
              ? "目前記憶庫沒有可聚合的 entity — 先跑幾份 broadcast 摘要"
              : "沒有符合條件的 entity"}
          </div>
        ) : filtered.map((e) => {
          const key = `${e.kind}:${e.value}`;
          const display = e.kind === "symbol" ? `$${e.value}`
                       : e.kind === "handle" ? `@${e.value}`
                       : e.value.length > 10 ? e.value.slice(0, 6) + "…" + e.value.slice(-4)
                       : e.value;
          return (
            <div
              key={key}
              className={cls("entity-item", activeKey === key && "active")}
              onClick={() => onSelect && onSelect(e)}
              style={{ cursor: "pointer" }}
            >
              <div className="entity-row">
                <span className={cls("entity-tag", `kind-${e.kind}`)}>{display}</span>
                <span className="entity-badges">
                  {e.has_brief && <span className="entity-brief-flag" title="已有 cached brief">✓</span>}
                  {e.has_profile && <span className="entity-pin" title="已建立 profile">★</span>}
                </span>
              </div>
              <div className="entity-meta">
                <span><strong>{e.days_seen}d</strong></span>
                <span>·</span>
                <span><strong>{e.chats_seen}</strong> chats</span>
                <span>·</span>
                <span>{e.last_date}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------- Mention rendering helpers ----------

// Convert `[label](url)` markdown links to plain text (just the label).
// Tracker bots use these heavily; rendering as plain text + collapsing extra
// whitespace makes their messages readable. We don't turn them into real
// <a> tags because (a) the URLs are mostly internal bot routes (rickburpbot,
// dexscreener, etc.) you wouldn't click anyway, and (b) it keeps the row
// height predictable.
function cleanMdLinks(text) {
  if (!text) return "";
  return String(text)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    // Strip bare URLs too (Telegram messages often have them inline).
    .replace(/https?:\/\/\S+/g, "")
    // Collapse runs of whitespace within a single line (preserve newlines).
    .split("\n").map(l => l.replace(/[ \t]+/g, " ").trim()).join("\n")
    .trim();
}

// Wrap occurrences of the entity in <mark> for visual scanning. Case-
// insensitive so `$asteroid` and `Asteroid` both highlight.
function highlightEntity(text, value, kind) {
  if (!text || !value) return text;
  const cleaned = cleanMdLinks(text);
  // Build the matchable form depending on kind.
  const needles = [];
  if (kind === "ca") needles.push(value);
  else if (kind === "handle") needles.push("@" + value);
  else { needles.push("$" + value); needles.push(value); }
  const re = new RegExp("(" + needles.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  const out = [];
  let last = 0, m, k = 0;
  while ((m = re.exec(cleaned)) !== null) {
    if (m.index > last) out.push(cleaned.slice(last, m.index));
    out.push(<mark key={k++}>{m[0]}</mark>);
    last = re.lastIndex;
  }
  if (last < cleaned.length) out.push(cleaned.slice(last));
  return out.length ? out : [cleaned];
}

function MentionContextMsg({ msg }) {
  const txt = cleanMdLinks(msg.text);
  if (!txt) return null;
  const time = (msg.date || "").slice(11, 16);
  return (
    <div className="mention-ctx">
      <span className="mention-ctx-meta">
        {time} · {msg.sender_name || "?"}
      </span>
      <span className="mention-ctx-text">{txt.length > 200 ? txt.slice(0, 200) + "…" : txt}</span>
    </div>
  );
}

function MentionRow({ m, entityValue, entityKind }) {
  const before = m.context_before || [];
  const after = m.context_after || [];
  return (
    <div className="entity-mention">
      <div className="entity-mention-head">
        <span className="entity-mention-time">{compactDateTime(m.date) || m.date}</span>
        <span className="entity-mention-chat">{m.chat_name || "—"}</span>
        <span className="entity-mention-sender">
          {m.sender_name || "?"}
          {m.sender_username && <span className="at"> @{m.sender_username}</span>}
        </span>
      </div>
      {before.map((c, i) => <MentionContextMsg key={"b" + i} msg={c} />)}
      <div className="entity-mention-text hit">
        {m.text ? highlightEntity(m.text, entityValue, entityKind) : (m.media ? `[${m.media}]` : "—")}
      </div>
      {after.map((c, i) => <MentionContextMsg key={"a" + i} msg={c} />)}
    </div>
  );
}

// ---------- Watchtower stage (entity detail) ----------
function WatchtowerStage({ entity, onCreateProfile, onOpenProfile, onOpenDay, windowDays, onBriefGenerated }) {
  // ALL hooks must be called unconditionally on every render — moving them
  // below the `if (!entity) return` early-out broke React when no entity was
  // selected (hook order shifted between renders → component tree blanked).
  const [mentions, setMentions] = useState(null);
  const [mentionsLoading, setMentionsLoading] = useState(false);
  const [mentionsError, setMentionsError] = useState("");
  const [includeBots, setIncludeBots] = useState(false);
  const [botSkipped, setBotSkipped] = useState(0);
  const [contextN, setContextN] = useState(5);
  // AI quick-brief state (replaces the two raw lists as the primary view).
  const [brief, setBrief] = useState("");
  const [briefStreaming, setBriefStreaming] = useState(false);
  const [briefStage, setBriefStage] = useState("");
  const [briefError, setBriefError] = useState("");
  const [briefCounts, setBriefCounts] = useState(null);
  const [briefGeneratedAt, setBriefGeneratedAt] = useState("");
  const [briefFromCache, setBriefFromCache] = useState(false);
  const [briefStale, setBriefStale] = useState(false);
  const [briefAgeHours, setBriefAgeHours] = useState(null);
  // Collapse source data sections by default once brief is ready.
  const [showSources, setShowSources] = useState(false);
  // Track current entity selection — needed by the auto-trigger effect below
  // because runBrief reads `entity` via closure and we need stable identity.
  const runBriefRef = useRef(null);

  // Reset everything when entity selection changes.
  useEffect(() => {
    setMentions(null);
    setMentionsError("");
    setBotSkipped(0);
    setBrief("");
    setBriefStage("");
    setBriefError("");
    setBriefCounts(null);
    setBriefGeneratedAt("");
    setBriefFromCache(false);
    setBriefStale(false);
    setBriefAgeHours(null);
    setShowSources(false);
  }, [entity?.kind, entity?.value]);

  // Auto-load: try cache first, then auto-trigger if missing.
  // 1.5s delay before auto-triggering so quick browsing through entities
  // doesn't fire a brief request for every one.
  useEffect(() => {
    if (!entity) return;
    let cancelled = false;
    let timer = null;

    const params = new URLSearchParams({ value: entity.value, kind: entity.kind });
    apiFetch("/api/watchtower/entity_brief?" + params.toString())
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled || !d) return;
        if (d.brief) {
          setBrief(d.brief.brief_text || "");
          setBriefGeneratedAt(d.brief.generated_at || "");
          setBriefFromCache(true);
          setBriefStale(!!d.brief.is_stale);
          setBriefAgeHours(d.brief.age_hours);
          setBriefCounts({
            summaries: d.brief.summaries_count,
            messages_after_bot_filter: d.brief.messages_count,
          });
        } else {
          // No cache → auto-trigger after short delay (cancellable).
          timer = setTimeout(() => {
            if (!cancelled && runBriefRef.current) runBriefRef.current();
          }, 1500);
        }
      })
      .catch(() => { /* silent — user can still hit Re-run */ });

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [entity?.kind, entity?.value]);

  // Group refs by date so the same date doesn't repeat — show date once with N chats below.
  const refsByDate = useMemo(() => {
    const refs = (entity && entity.summary_refs)
      || (entity && (entity.summary_ids || []).map((id) => ({ id })))
      || [];
    const m = {};
    refs.forEach((r) => {
      const k = r.date || "?";
      if (!m[k]) m[k] = [];
      m[k].push(r);
    });
    return m;
  }, [entity]);

  const loadMentions = async (opts) => {
    if (!entity) return;
    const wantBots = opts && opts.includeBots !== undefined ? opts.includeBots : includeBots;
    const wantCtx = opts && opts.contextN !== undefined ? opts.contextN : contextN;
    setMentionsLoading(true);
    setMentionsError("");
    try {
      const params = new URLSearchParams({
        value: entity.value,
        kind: entity.kind,
        days: String(windowDays || 30),
        limit: "60",
        context: String(wantCtx),
      });
      if (wantBots) params.set("include_bots", "1");
      const r = await apiFetch("/api/watchtower/entity_mentions?" + params.toString());
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setMentions(d.mentions || []);
      setBotSkipped(d.bot_skipped || 0);
    } catch (e) {
      setMentionsError(e.message || String(e));
    } finally {
      setMentionsLoading(false);
    }
  };

  const toggleBots = async () => {
    const next = !includeBots;
    setIncludeBots(next);
    if (mentions != null) {
      await loadMentions({ includeBots: next });
    }
  };

  const changeContext = async (n) => {
    setContextN(n);
    if (mentions != null) {
      await loadMentions({ contextN: n });
    }
  };

  const runBrief = async () => {
    if (briefStreaming || !entity) return;
    setBriefStreaming(true);
    setBrief("");
    setBriefError("");
    setBriefStage("📦 聚合資料...");
    setBriefCounts(null);
    setBriefFromCache(false);
    setBriefGeneratedAt("");
    let acc = "";
    try {
      const r = await apiFetch("/api/watchtower/entity_brief", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          value: entity.value, kind: entity.kind,
          days: windowDays || 14, model: "sonnet",
        }),
      });
      if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buf += dec.decode(chunk, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(5).trim()); } catch { continue; }
          if (ev.type === "token") {
            acc += ev.token || "";
            setBrief(acc);
          } else if (ev.type === "progress") {
            if (ev.msg) setBriefStage(ev.msg);
          } else if (ev.type === "done") {
            setBriefStage("");
            setBriefCounts(ev.context_counts || null);
            setBriefGeneratedAt(ev.generated_at || "");
            // Tell parent to re-fetch entities so the ✓ badge appears in the list.
            if (onBriefGenerated) onBriefGenerated();
          } else if (ev.type === "error") {
            throw new Error(ev.error || "brief failed");
          }
        }
      }
    } catch (e) {
      setBriefError(e.message || String(e));
      setBriefStage("");
    } finally {
      setBriefStreaming(false);
    }
  };

  // Keep latest runBrief reachable from the auto-trigger effect.
  runBriefRef.current = runBrief;

  if (!entity) {
    return (
      <Empty
        mark="◎"
        title="Entity radar"
        desc="從中欄選一個 entity 看細節 — 出現的天數、被哪些 chat / KOL 提到、要不要 promote 為 coin profile。"
      />
    );
  }

  const display = entity.kind === "symbol" ? `$${entity.value}`
               : entity.kind === "handle" ? `@${entity.value}`
               : entity.value;
  const kindLabel = entity.kind === "symbol" ? "Ticker"
                  : entity.kind === "handle" ? "KOL handle"
                  : "Contract address";
  const refs = entity.summary_refs || (entity.summary_ids || []).map((id) => ({ id }));

  return (
    <div className="dispatch fade-up entity-stage">
      <div className="entity-head">
        <div className="entity-head-kicker">Watchtower · {kindLabel}</div>
        <div className="entity-head-row">
          <h1 className="entity-head-title">{display}</h1>
          {entity.has_profile ? (
            <button
              className="btn btn-sm btn-accent"
              onClick={() => onOpenProfile && onOpenProfile(entity.profile_id)}
            >★ Open profile</button>
          ) : entity.kind !== "handle" ? (
            <button
              className="btn btn-sm btn-primary"
              onClick={() => onCreateProfile && onCreateProfile(entity)}
            >+ Create profile</button>
          ) : null}
        </div>
        <div className="entity-stats">
          <span className="entity-stat"><strong>{entity.days_seen}</strong>d seen</span>
          <span className="entity-stat-sep">·</span>
          <span className="entity-stat"><strong>{entity.chats_seen}</strong> chats</span>
          <span className="entity-stat-sep">·</span>
          <span className="entity-stat">first <strong>{entity.first_date}</strong></span>
          <span className="entity-stat-sep">·</span>
          <span className="entity-stat">last <strong>{entity.last_date}</strong></span>
        </div>
      </div>

      <div className="events-block">
        <div className="events-block-head">
          <h3>Quick brief</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: "auto" }}>
            {briefCounts && (
              <span className="cnt">
                {briefCounts.summaries} summaries · {briefCounts.messages_after_bot_filter} msgs
                {typeof briefCounts.tweets === "number" && briefCounts.tweets > 0 && (
                  <> · {briefCounts.tweets} tweets</>
                )}
                {briefCounts.twitter_enabled === false && (
                  <span style={{ color: "var(--ink-3)" }}> · X off</span>
                )}
                {briefGeneratedAt && (
                  <span style={{ color: briefStale ? "var(--alert,#b04226)" : "var(--ink-3)" }}>
                    {" · "}{briefFromCache ? "cached " : ""}{compactDateTime(briefGeneratedAt) || briefGeneratedAt}
                    {briefStale && briefAgeHours != null && ` (${Math.round(briefAgeHours)}h ago, stale)`}
                  </span>
                )}
              </span>
            )}
            {brief && !briefStreaming && (
              <button
                className="btn btn-sm"
                onClick={runBrief}
                title="重新生成(會覆蓋 cache)"
              >↻ Re-generate</button>
            )}
            {briefStreaming && (
              <span className="cnt"><span className="spinner" />drafting…</span>
            )}
          </div>
        </div>
        {briefError && (
          <div className="brief-error">
            {briefError}
            <button className="btn btn-sm" style={{ marginLeft: 8 }} onClick={runBrief}>↻ Retry</button>
          </div>
        )}
        {briefStage && !brief && (
          <div className="brief-stage">{briefStage}</div>
        )}
        {brief && (
          <div className="brief-body">
            {brief.split(/\n{2,}/).filter(Boolean).map((p, i) => <p key={i}>{p}</p>)}
            {briefStreaming && <span className="brief-cursor">▍</span>}
          </div>
        )}
        {!brief && !briefStreaming && !briefError && (
          <div className="brief-stub">
            <span className="spinner" /> 檢查 cache,如無則自動起草…
          </div>
        )}
      </div>

      <div className="events-block sources-toggle">
        <button
          className="sources-toggle-btn"
          onClick={() => setShowSources((v) => !v)}
        >
          {showSources ? "▾" : "▸"} Source data
          <span className="cnt">
            {refs.length} summary refs · {mentions == null ? "raw msgs not loaded" : `${mentions.length} msgs`}
          </span>
        </button>
      </div>

      {showSources && <>
      <div className="events-block">
        <div className="events-block-head">
          <h3>Mentioned in summaries</h3>
          <span className="cnt">{refs.length} refs · click to open day</span>
        </div>
        {refs.length === 0 ? (
          <div style={{ color: "var(--ink-3)", fontSize: 12 }}>(無)</div>
        ) : (
          <div className="entity-refs">
            {Object.entries(refsByDate).map(([date, items]) => (
              <div className="entity-ref-row" key={date} onClick={() => onOpenDay && onOpenDay(date)}>
                <div className="entity-ref-date">{date}</div>
                <div className="entity-ref-chats">
                  {items.map((r, i) => (
                    <span key={r.id ?? i} className="entity-ref-chat">{r.chat_name || `#${r.id}`}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="events-block">
        <div className="events-block-head">
          <h3>Raw mentions</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: "auto" }}>
            {mentions != null && (
              <>
                <span className="cnt">
                  {mentions.length} msgs · {windowDays || 30}d
                  {!includeBots && botSkipped > 0 && <span style={{ color: "var(--ink-3)" }}> · {botSkipped} bot hidden</span>}
                </span>
                <select
                  className="input mono"
                  value={contextN}
                  onChange={(e) => changeContext(Number(e.target.value))}
                  style={{ fontSize: 10, padding: "2px 4px", width: 76 }}
                  title="每則命中前後抓多少則做上下文"
                >
                  <option value={0}>±0 ctx</option>
                  <option value={2}>±2 ctx</option>
                  <option value={5}>±5 ctx</option>
                  <option value={10}>±10 ctx</option>
                </select>
                <button
                  className="btn btn-sm"
                  onClick={toggleBots}
                  title={includeBots ? "Hide tracker bot output" : "Show tracker bot output"}
                >{includeBots ? "Hide bots" : "Show bots"}</button>
              </>
            )}
            {mentions == null && (
              <button
                className="btn btn-sm btn-primary"
                onClick={() => loadMentions()}
                disabled={mentionsLoading}
              >{mentionsLoading ? <><span className="spinner" />loading…</> : "↓ Show messages"}</button>
            )}
          </div>
        </div>
        {mentionsError && (
          <div style={{ color: "var(--alert,#b04226)", fontSize: 12, fontFamily: "var(--mono)" }}>
            {mentionsError}
          </div>
        )}
        {mentions != null && mentions.length === 0 && (
          <div style={{ color: "var(--ink-3)", fontSize: 12 }}>
            {botSkipped > 0
              ? `本時段全是 tracker bot 訊息(${botSkipped} 則),沒有人類發言。按右上「Show bots」可看 bot 內容。`
              : "找不到原始訊息(可能訊息沒被歸檔)。"}
          </div>
        )}
        {mentions != null && mentions.length > 0 && (
          <div className="entity-mentions">
            {mentions.map((m) => (
              <MentionRow key={m.id} m={m} entityValue={entity.value} entityKind={entity.kind} />
            ))}
          </div>
        )}
      </div>
      </>}
    </div>
  );
}

// ---------- Coin profiles column ----------
const PROFILE_STATUSES = [
  { id: "tracking", label: "追蹤", emoji: "👁" },
  { id: "held", label: "持倉", emoji: "💎" },
  { id: "exited", label: "出場", emoji: "✓" },
  { id: "dropped", label: "放棄", emoji: "✕" },
];
const STATUS_BY_ID = Object.fromEntries(PROFILE_STATUSES.map((s) => [s.id, s]));

function ProfilesCol({
  profiles, activeId, onSelect, onCreate, onCreateFromNotes,
  reviewBuild, onDismissReviewBuild,
  statusFilter, setStatusFilter, search, setSearch,
}) {
  const [newSym, setNewSym] = useState("");
  const submit = () => {
    const s = newSym.trim();
    if (!s) return;
    onCreate(s);
    setNewSym("");
  };

  // 貼復盤建檔 — paste a review (with CA), AI extracts symbol/chain/narrative
  // and creates the profile in one shot.
  //
  // Only the typed text + panel-open flag are local to this component (and
  // persisted to localStorage so view-switches don't lose them). The actual
  // build state (busy / stage / error / lastResult) lives in App via the
  // `reviewBuild` prop — that way the SSE keeps running and the user can
  // switch to other views during the build without losing progress / result.
  const _REVIEW_LS_KEY = "coinReview:v1";
  const _initialReview = (() => {
    try {
      const raw = localStorage.getItem(_REVIEW_LS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  })();
  const [reviewOpen, setReviewOpen] = useState(_initialReview?.reviewOpen ?? false);
  const [reviewText, setReviewText] = useState(_initialReview?.reviewText || "");

  // Persist only the user-typed text + open/closed flag.
  useEffect(() => {
    try {
      if (reviewOpen || reviewText) {
        localStorage.setItem(_REVIEW_LS_KEY, JSON.stringify({ reviewOpen, reviewText }));
      } else {
        localStorage.removeItem(_REVIEW_LS_KEY);
      }
    } catch {}
  }, [reviewOpen, reviewText]);

  // Local fallback when reviewBuild prop isn't wired (defensive).
  const build = reviewBuild || { busy: false, stage: "", error: "", lastResult: null };
  const [validationError, setValidationError] = useState("");

  // After a successful build, auto-clear the local textarea so the next paste
  // starts fresh. Trigger on lastResult.ts so each new completion only fires
  // once.
  useEffect(() => {
    if (build.lastResult?.ts) {
      setReviewText("");
      setValidationError("");
    }
  }, [build.lastResult?.ts]);

  const submitReview = async () => {
    const txt = reviewText.trim();
    if (!txt) {
      setValidationError("請先貼一篇含 CA 或 $TICKER 的復盤");
      return;
    }
    if (!onCreateFromNotes) return;
    setValidationError("");
    try {
      await onCreateFromNotes(txt);
      // Success — App-level reviewBuild.lastResult will be set; the useEffect
      // above clears the textarea. No need to touch local state here.
    } catch {
      // App-level reviewBuild.error already populated by createProfileFromNotes.
    }
  };

  const filtered = useMemo(() => {
    const q = (search || "").trim().toLowerCase();
    return (profiles || []).filter((p) => {
      if (statusFilter && p.status !== statusFilter) return false;
      if (!q) return true;
      return (p.symbol || "").toLowerCase().includes(q)
          || (p.ca || "").toLowerCase().includes(q)
          || (p.narrative || "").toLowerCase().includes(q);
    });
  }, [profiles, statusFilter, search]);

  const counts = useMemo(() => {
    const c = {};
    PROFILE_STATUSES.forEach((s) => { c[s.id] = 0; });
    (profiles || []).forEach((p) => { c[p.status] = (c[p.status] || 0) + 1; });
    return c;
  }, [profiles]);

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 04 · Coin Dossier</div>
        <div className="col-title"><em>Coin</em> profiles.</div>
        <div className="col-sub">{profiles.length} profiles · narrative + buys/sells + lessons</div>
      </div>
      <div className="note-form">
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            className="input"
            placeholder="新 symbol (e.g. PEPE)"
            value={newSym}
            onChange={(e)=>setNewSym(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          <button className="btn btn-primary btn-sm" onClick={submit}>+ New</button>
        </div>
        <div style={{ marginTop: 6 }}>
          <button
            className="btn btn-sm"
            onClick={() => { setReviewOpen((v) => !v); setValidationError(""); }}
            style={{ width: "100%", fontSize: 11 }}
            title="貼一篇含 CA 的復盤,AI 自動抽 symbol/chain/敘事 + 上 X 找推文"
          >
            {reviewOpen ? "▾ 收起貼復盤建檔" : "✎ 貼復盤建檔(含 CA / $TICKER)"}
          </button>
        </div>
        {/* Done banner — shows even when panel is collapsed and persists
            across view switches because reviewBuild lives in App. */}
        {build.lastResult && (
          <div style={{ marginTop: 6, padding: "6px 8px", background: "var(--bg-3, #ecead8)", border: "1px solid var(--rule)", borderRadius: 4, fontSize: 11, display: "flex", gap: 6, alignItems: "center" }}>
            <span style={{ color: "var(--success, #4a6a3a)" }}>
              {build.lastResult.merged ? "✓ 已併入現有 profile" : "✓ 建檔完成"}
            </span>
            <button
              className="btn btn-sm"
              style={{ padding: "1px 6px", fontSize: 10 }}
              onClick={() => onSelect && onSelect({ id: build.lastResult.profileId })}
            >${build.lastResult.symbol}</button>
            <span style={{ flex: 1 }} />
            <button
              className="btn btn-sm"
              style={{ padding: "1px 6px", fontSize: 10 }}
              onClick={onDismissReviewBuild}
            >✕</button>
          </div>
        )}
        {/* Error banner — same persistence logic as done banner. */}
        {build.error && (
          <div style={{ marginTop: 6, padding: "6px 8px", background: "var(--bg-3, #ecead8)", border: "1px solid var(--danger, #b00)", borderRadius: 4, fontSize: 11, color: "var(--danger, #b00)", display: "flex", gap: 6, alignItems: "flex-start" }}>
            <span style={{ flex: 1 }}>✗ 建檔失敗:{build.error}</span>
            <button
              className="btn btn-sm"
              style={{ padding: "1px 6px", fontSize: 10 }}
              onClick={onDismissReviewBuild}
            >✕</button>
          </div>
        )}
        {reviewOpen && (
          <div style={{ marginTop: 6, padding: 8, border: "1px solid var(--rule)", borderRadius: 4 }}>
            <textarea
              className="input"
              rows={6}
              placeholder="貼你的復盤(內含 CA 或 $TICKER)…&#10;範例:「BONK 復盤 — DwK4...vAsRr 我在 0.000005 進 0.5 SOL,後來砸到 0.000002 全賣,虧 60%。教訓:Pump 後沒接力就要早跑」"
              value={reviewText}
              onChange={(e) => setReviewText(e.target.value)}
              disabled={build.busy}
              style={{ fontSize: 11, resize: "vertical", width: "100%" }}
            />
            <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center" }}>
              <button
                className={cls("btn btn-primary btn-sm", build.busy && "btn-accent")}
                onClick={submitReview}
                disabled={build.busy || !reviewText.trim()}
              >
                {build.busy ? <><span className="spinner" />建檔中…</> : "建檔"}
              </button>
              <span style={{ fontSize: 10, color: "var(--ink-3)", flex: 1 }}>
                {build.busy ? "切去其他頁也沒關係,建檔會在背景跑完" : "Sonnet · 自動上 X 搜推文 · 原文保存到 my_raw_notes"}
              </span>
            </div>
            {build.busy && build.stage && (
              <div style={{ marginTop: 6, fontSize: 10, color: "var(--ink-2)", fontFamily: "var(--mono)" }}>
                {build.stage}
              </div>
            )}
            {validationError && (
              <div style={{ marginTop: 6, fontSize: 11, color: "var(--danger, #b00)" }}>
                {validationError}
              </div>
            )}
          </div>
        )}
        <input
          className="input"
          placeholder="Search symbol / CA / narrative…"
          value={search || ""}
          onChange={(e)=>setSearch(e.target.value)}
          style={{ marginTop: 8, fontSize: 11 }}
        />
        <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap" }}>
          <button
            className={cls("filter-chip", !statusFilter && "active")}
            onClick={() => setStatusFilter("")}
          >
            All <span style={{ opacity: 0.6 }}>{profiles.length}</span>
          </button>
          {PROFILE_STATUSES.map((s) => (
            <button
              key={s.id}
              className={cls("filter-chip", statusFilter === s.id && "active")}
              onClick={() => setStatusFilter(s.id)}
            >
              {s.emoji} {s.label} <span style={{ opacity: 0.6 }}>{counts[s.id] || 0}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="col-body">
        {filtered.length === 0 ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
            {profiles.length === 0
              ? "還沒有任何 coin profile — 上面打 symbol 新增,或從 Watchtower 一鍵 promote"
              : "沒有符合條件的 profile"}
          </div>
        ) : filtered.map((p) => {
          const status = STATUS_BY_ID[p.status] || STATUS_BY_ID.tracking;
          return (
            <div
              key={p.id}
              className={cls("profile-item", activeId === p.id && "active", p.pinned && "pinned")}
              onClick={() => onSelect && onSelect(p)}
              style={{ cursor: "pointer" }}
            >
              <div className="profile-top">
                <span className="profile-sym">${p.symbol}</span>
                {p.chain && <span className="profile-chain">{p.chain}</span>}
                <span className="profile-status" title={status.label}>{status.emoji}</span>
                {p.pinned ? <span className="profile-pin">★</span> : null}
              </div>
              {p.narrative && (
                <div className="profile-snippet">{String(p.narrative).slice(0, 90)}</div>
              )}
              <div className="profile-meta">
                <span>updated {compactDateTime(p.last_updated) || "—"}</span>
                {p.my_pnl && <span>· PnL {p.my_pnl}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Persist streaming panel state across remounts so switching view mid-stream
// doesn't make typed notes / preview look "lost". React state dies on unmount
// but the SSE generator runs on the server and may still finish after the
// user navigates away — we keep the last visible snapshot per (profile, kind)
// and let the user refresh the list to confirm completion. `kind` is one of
// "fill" (paste & fill), "draft" (AI draft 6-section), "smart" (smart money).
const _STREAM_LS_KEY = (id, kind) => `coinStream:v1:${kind}:${id}`;
const _loadStreamState = (id, kind) => {
  if (!id) return null;
  try {
    const raw = localStorage.getItem(_STREAM_LS_KEY(id, kind));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
};
const _saveStreamState = (id, kind, snap) => {
  if (!id) return;
  try { localStorage.setItem(_STREAM_LS_KEY(id, kind), JSON.stringify(snap)); } catch {}
};
const _clearStreamState = (id, kind) => {
  if (!id) return;
  try { localStorage.removeItem(_STREAM_LS_KEY(id, kind)); } catch {}
};

// ---------- Coin profile stage (right pane: read + edit) ----------
function ProfileStage({
  profile, onUpdate, onDelete, onAfterDraft,
  draftTask, onRunAIDraft, onDismissDraft,
  onDistillRulesDone,
}) {
  const [draft, setDraft] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // AI draft streaming state
  const [drafting, setDrafting] = useState(false);
  const [draftStream, setDraftStream] = useState("");
  const [draftStage, setDraftStage] = useState("");
  const [draftError, setDraftError] = useState("");
  const [smartDrafting, setSmartDrafting] = useState(false);
  const [smartStage, setSmartStage] = useState("");
  const [smartError, setSmartError] = useState("");
  const [smartStream, setSmartStream] = useState("");
  // Paste & fill state — separate panel that takes user-supplied notes
  // and asks Sonnet to extract field values from them.
  const [fillOpen, setFillOpen] = useState(false);
  const [fillNotes, setFillNotes] = useState("");
  const [filling, setFilling] = useState(false);
  const [fillStage, setFillStage] = useState("");
  const [fillError, setFillError] = useState("");
  const [fillStream, setFillStream] = useState("");
  const [fillResult, setFillResult] = useState(null);  // {fields_written: [...]}
  // ✦ Distill trading rules
  const [distilling, setDistilling] = useState(false);
  const [distillStage, setDistillStage] = useState("");
  const [distillError, setDistillError] = useState("");

  // Reset local draft whenever the selected profile changes OR last_updated
  // changes (so AI-draft writes flow into the form without remount). Streaming
  // state (fill / draft / smart) is rehydrated from localStorage instead of
  // being unconditionally cleared — see the helpers above.
  useEffect(() => {
    setDraft(profile ? { ...profile } : null);
    setDirty(false);

    const savedDraft = _loadStreamState(profile?.id, "draft");
    if (savedDraft) {
      setDraftStream(savedDraft.draftStream || "");
      setDraftStage(savedDraft.draftStage || "");
      setDraftError(savedDraft.draftError || "");
    } else {
      setDraftStream(""); setDraftStage(""); setDraftError("");
    }

    const savedSmart = _loadStreamState(profile?.id, "smart");
    if (savedSmart) {
      setSmartStream(savedSmart.smartStream || "");
      setSmartStage(savedSmart.smartStage || "");
      setSmartError(savedSmart.smartError || "");
    } else {
      setSmartStream(""); setSmartStage(""); setSmartError("");
    }

    const savedFill = _loadStreamState(profile?.id, "fill");
    if (savedFill) {
      setFillOpen(savedFill.fillOpen ?? false);
      setFillNotes(savedFill.fillNotes || "");
      setFillStage(savedFill.fillStage || "");
      setFillError(savedFill.fillError || "");
      setFillStream(savedFill.fillStream || "");
      setFillResult(savedFill.fillResult || null);
    } else {
      setFillOpen(false); setFillNotes("");
      setFillStage(""); setFillError("");
      setFillStream(""); setFillResult(null);
    }
  }, [profile?.id, profile?.last_updated]);

  // Auto-save each streaming panel per profile. The active-fetch booleans
  // (filling / drafting / smartDrafting) are intentionally excluded — the
  // SSE reader cannot survive remount, so on rehydrate we treat them as
  // not-running and let the user refresh the list to check backend completion.
  useEffect(() => {
    if (!profile?.id) return;
    if (draftStream || draftStage || draftError) {
      _saveStreamState(profile.id, "draft", { draftStream, draftStage, draftError });
    } else {
      _clearStreamState(profile.id, "draft");
    }
  }, [profile?.id, draftStream, draftStage, draftError]);

  useEffect(() => {
    if (!profile?.id) return;
    if (smartStream || smartStage || smartError) {
      _saveStreamState(profile.id, "smart", { smartStream, smartStage, smartError });
    } else {
      _clearStreamState(profile.id, "smart");
    }
  }, [profile?.id, smartStream, smartStage, smartError]);

  useEffect(() => {
    if (!profile?.id) return;
    const hasAny = fillOpen || fillNotes || fillStage || fillError || fillStream || fillResult;
    if (hasAny) {
      _saveStreamState(profile.id, "fill", {
        fillOpen, fillNotes, fillStage, fillError, fillStream, fillResult,
      });
    } else {
      _clearStreamState(profile.id, "fill");
    }
  }, [profile?.id, fillOpen, fillNotes, fillStage, fillError, fillStream, fillResult]);

  const draftTaskActive = draftTask || null;
  const draftBusy = draftTaskActive ? !!draftTaskActive.busy : drafting;
  const draftPanelStream = draftTaskActive ? (draftTaskActive.stream || "") : draftStream;
  const draftPanelStage = draftTaskActive ? (draftTaskActive.stage || "") : draftStage;
  const draftPanelError = draftTaskActive ? (draftTaskActive.error || "") : draftError;
  const draftPanelDone = draftTaskActive ? !!draftTaskActive.lastResult : (draftStage || "").toLowerCase().includes("done");

  if (!profile || !draft) {
    return (
      <Empty
        mark="◇"
        title="Coin profile"
        desc="從中欄選一個 profile,或新增一筆。每個 profile 是一份持續累積的研究檔案 — 敘事、時間軸、KOL 共識、你的買賣與教訓。"
      />
    );
  }

  const set = (k, v) => {
    setDraft((d) => ({ ...d, [k]: v }));
    setDirty(true);
  };

  const save = async () => {
    if (!dirty) return true;
    setSaving(true);
    try {
      // Only send fields that are user-editable (avoid id/created_at).
      const payload = {};
      [
        "symbol", "chain", "ca", "status",
        "narrative", "timeline_json", "kol_consensus",
        "smart_money_summary", "top_signal", "archetype",
        "my_entry_fdv", "my_entry_size", "my_exit_fdv", "my_exit_size",
        "my_pnl", "my_wallet", "my_verdict", "my_lesson",
        "my_raw_notes",
        "tags", "pinned", "first_seen_date",
      ].forEach((k) => { if (draft[k] !== undefined) payload[k] = draft[k]; });
      const updated = await onUpdate(profile.id, payload);
      if (!updated) return false;
      setDirty(false);
      return true;
    } finally {
      setSaving(false);
    }
  };

  // ✎ Paste & fill — user pastes any text, Sonnet extracts known fields.
  const runFill = async () => {
    if (filling || !profile?.id) return;
    if (!fillNotes.trim()) {
      setFillError("請先貼一些 notes 再按擷取");
      return;
    }
    setFilling(true);
    setFillError("");
    setFillStage("📦 解析筆記...");
    setFillStream("");
    setFillResult(null);
    let acc = "";
    try {
      const r = await apiFetch(`/api/coin_profiles/${profile.id}/fill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: fillNotes, model: "sonnet" }),
      });
      if (!r.ok || !r.body) {
        // Try to surface server error JSON if present.
        try { const j = await r.json(); throw new Error(j.error || `HTTP ${r.status}`); }
        catch { throw new Error(`HTTP ${r.status}`); }
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buf += dec.decode(chunk, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(5).trim()); } catch { continue; }
          if (ev.type === "token") {
            acc += ev.token || "";
            setFillStream(acc);
          } else if (ev.type === "progress") {
            if (ev.msg) setFillStage(ev.msg);
          } else if (ev.type === "done") {
            setFillStage("");
            setFillResult({
              fields_written: ev.fields_written || [],
              tweet_count: ev.tweet_count || 0,
              search_target: ev.search_target || null,
            });
            // Trigger parent refresh so the populated fields flow back.
            if (onAfterDraft) await onAfterDraft();
            // Clear the input box so user knows submission was processed.
            setFillNotes("");
          } else if (ev.type === "error") {
            throw new Error(ev.error || "fill failed");
          }
        }
      }
    } catch (e) {
      setFillError(e.message || String(e));
      setFillStage("");
    } finally {
      setFilling(false);
    }
  };

  // ✦ Distill rules — read this profile's lesson/verdict/notes, ask AI for
  // 1-3 cross-coin reusable trading rules. Candidates flow up to parent which
  // opens the RulesPanel with the drafts pre-seeded for review.
  const runDistillRules = async () => {
    if (distilling || !profile?.id) return;
    setDistilling(true);
    setDistillError("");
    setDistillStage("📦 讀 lesson + notes...");
    try {
      const r = await apiFetch(`/api/coin_profiles/${profile.id}/distill_rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "sonnet" }),
      });
      if (!r.ok || !r.body) {
        try { const j = await r.json(); throw new Error(j.error || `HTTP ${r.status}`); }
        catch { throw new Error(`HTTP ${r.status}`); }
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let candidates = null;
      let sourceSymbol = null;
      for (;;) {
        const { value: chunk, done } = await reader.read();
        if (done) break;
        buf += dec.decode(chunk, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(5).trim()); } catch { continue; }
          if (ev.type === "progress") {
            if (ev.msg) setDistillStage(ev.msg);
          } else if (ev.type === "done") {
            candidates = ev.candidates || [];
            sourceSymbol = ev.source_symbol || profile?.symbol || null;
          } else if (ev.type === "error") {
            throw new Error(ev.error || "distill failed");
          }
          // ignore tokens — RulesPanel shows the parsed candidates only
        }
      }
      setDistillStage("");
      if (!candidates) {
        throw new Error("AI 沒有回傳候選法則");
      }
      if (candidates.length === 0) {
        setDistillError("AI 認為素材不足以提煉(回 [])。先補 my_lesson / my_verdict 再試。");
        return;
      }
      if (onDistillRulesDone) {
        onDistillRulesDone(candidates, sourceSymbol, profile.id);
      }
    } catch (e) {
      setDistillError(e.message || String(e));
      setDistillStage("");
    } finally {
      setDistilling(false);
    }
  };

  // ✧ AI draft — streams Sonnet output into a preview pane, then triggers
  // parent refresh so the populated fields flow back into local draft state.
  const runAIDraft = async () => {
    if (draftBusy || !profile?.id) return;
    if (dirty) {
      if (!confirm("有未儲存改動,會先儲存目前內容,再讓 AI draft 重寫 6 個區塊。要繼續?")) return;
      const saved = await save();
      if (!saved) return;
    }
    if (onRunAIDraft) {
      await onRunAIDraft(profile.id);
      return;
    }
    setDrafting(true);
    setDraftStream("");
    setDraftStage("📦 聚合資料...");
    setDraftError("");
    let acc = "";
    try {
      const r = await apiFetch(`/api/coin_profiles/${profile.id}/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "sonnet", days: 30 }),
      });
      if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(5).trim()); } catch { continue; }
          if (ev.type === "token") {
            acc += ev.token || "";
            setDraftStream(acc);
          } else if (ev.type === "progress") {
            if (ev.msg) setDraftStage(ev.msg);
          } else if (ev.type === "done") {
            const n = (ev.sections_written || []).length;
            setDraftStage(`✓ 完成 — 寫入 ${n} 個區塊`);
            if (onAfterDraft) await onAfterDraft();
          } else if (ev.type === "error") {
            throw new Error(ev.error || "draft failed");
          }
        }
      }
    } catch (e) {
      setDraftError(e.message || String(e));
      setDraftStage("");
    } finally {
      setDrafting(false);
    }
  };

  const runSmartMoneyDraft = async () => {
    if (smartDrafting || !profile?.id) return;
    const hadDirty = dirty;
    if (hadDirty && !confirm("有未儲存改動。Smart refresh 只會寫回 Smart money 這格,其他本地編輯會保留,要繼續?")) return;
    setSmartDrafting(true);
    setSmartStage("聚合 wallet / on-chain 資料...");
    setSmartError("");
    setSmartStream("");
    let acc = "";
    try {
      const r = await apiFetch(`/api/coin_profiles/${profile.id}/draft_smart_money`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "sonnet", days: 30 }),
      });
      if (!r.ok || !r.body) {
        try { const j = await r.json(); throw new Error(j.error || `HTTP ${r.status}`); }
        catch { throw new Error(`HTTP ${r.status}`); }
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(5).trim()); } catch { continue; }
          if (ev.type === "token") {
            acc += ev.token || "";
            setSmartStream(acc);
          } else if (ev.type === "progress") {
            if (ev.msg) setSmartStage(ev.msg);
          } else if (ev.type === "done") {
            const nextText = ev.smart_money_summary || acc;
            setDraft((d) => ({
              ...d,
              smart_money_summary: nextText,
              last_updated: ev.profile?.last_updated || d.last_updated,
            }));
            setDirty(hadDirty);
            setSmartStage("✓ Smart money 已更新");
            if (!hadDirty && onAfterDraft) await onAfterDraft();
          } else if (ev.type === "error") {
            throw new Error(ev.error || "smart refresh failed");
          }
        }
      }
    } catch (e) {
      setSmartError(e.message || String(e));
      setSmartStage("");
    } finally {
      setSmartDrafting(false);
    }
  };

  const status = STATUS_BY_ID[draft.status] || STATUS_BY_ID.tracking;

  return (
    <div className="dispatch fade-up entity-stage profile-stage">
      <div className="entity-head">
        <div className="entity-head-kicker">
          Profile #{profile.id} · {status.emoji} {status.label}
          {draft.pinned ? <span style={{ color: "var(--accent)", marginLeft: 8 }}>★ pinned</span> : null}
        </div>
        <div className="entity-head-row">
          <h1 className="entity-head-title">
            ${draft.symbol}{draft.chain && <em className="entity-head-chain"> [{draft.chain}]</em>}
          </h1>
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <button
              className={cls("btn btn-sm", fillOpen ? "btn-accent" : "btn-moss")}
              onClick={() => setFillOpen((v) => !v)}
              title="貼任何資訊(tweets / 群截錄 / 你的買賣 / 觀察)讓 Sonnet 抽進對應欄位"
            >✎ Paste & fill</button>
            <button
              className={cls("btn btn-sm", draftBusy && "btn-accent")}
              onClick={runAIDraft}
              disabled={draftBusy}
              title="從 messages + summaries + events 用 Sonnet 起草 6 個區塊"
            >{draftBusy ? <><span className="spinner" />drafting…</> : "✧ AI draft"}</button>
            <button
              className="btn btn-sm"
              onClick={() => set("pinned", draft.pinned ? 0 : 1)}
              title="Toggle pin"
            >{draft.pinned ? "★" : "☆"}</button>
            <button
              className={cls("btn btn-sm", dirty && "btn-primary")}
              onClick={save}
              disabled={!dirty || saving}
            >{saving ? "…" : dirty ? "Save" : "Saved"}</button>
            <button
              className="btn btn-sm"
              onClick={() => {
                if (confirm(`刪除 $${draft.symbol} profile?`)) onDelete(profile.id);
              }}
            >Del</button>
          </div>
        </div>
        <div className="entity-stats">
          <span className="entity-stat">created <strong>{compactDateTime(profile.created_at) || "—"}</strong></span>
          <span className="entity-stat-sep">·</span>
          <span className="entity-stat">updated <strong>{compactDateTime(profile.last_updated) || "—"}</strong></span>
          {draft.my_pnl && (<>
            <span className="entity-stat-sep">·</span>
            <span className="entity-stat">PnL <strong>{draft.my_pnl}</strong></span>
          </>)}
        </div>
      </div>

      {(draftBusy || draftPanelStream || draftPanelError) && (() => {
        // runAIDraft sets draftStage to "✓ 完成 — …" on the done event, so
        // a rehydrated snapshot lacking that prefix indicates we left mid-stream.
        const draftDone = draftPanelDone;
        const draftInterrupted = !draftBusy && !draftPanelError && draftPanelStream && !draftDone;
        const draftTitle = draftBusy ? "✧ AI drafting…"
          : draftPanelError ? "✗ Draft failed"
          : draftInterrupted ? "⚠️ AI 上次跑到一半切走"
          : "✓ Draft complete";
        return (
        <div className={cls("draft-panel", draftBusy && "drafting", draftPanelError && "errored")}>
          <div className="draft-panel-head">
            <span className="draft-panel-title">{draftTitle}</span>
            {draftPanelStage && <span className="draft-panel-stage">{draftPanelStage}</span>}
            {!draftBusy && (
              <>
                {draftInterrupted && onAfterDraft && (
                  <button
                    className="btn btn-sm"
                    style={{ marginLeft: "auto" }}
                    onClick={async () => { await onAfterDraft(); }}
                    title="後端可能已寫完;refresh profile 列表確認"
                  >🔄 Refresh 確認</button>
                )}
                <button
                  className="btn btn-sm"
                  style={draftInterrupted ? {} : { marginLeft: "auto" }}
                  onClick={() => {
                    if (onDismissDraft) onDismissDraft(profile.id);
                    setDraftStream(""); setDraftStage(""); setDraftError("");
                  }}
                >Dismiss</button>
              </>
            )}
          </div>
          {draftPanelError && <div className="draft-panel-error">{draftPanelError}</div>}
          {draftPanelStream && (
            <pre className="draft-panel-stream">{draftPanelStream}</pre>
          )}
        </div>
        );
      })()}

      {fillOpen && (
        <div className={cls("draft-panel", filling && "drafting", fillError && "errored")}>
          <div className="draft-panel-head">
            <span className="draft-panel-title">
              ✎ Paste & fill
            </span>
            <span className="draft-panel-stage">
              {fillStage || "貼 tweets / 群截錄 / 自己的買賣紀錄,Sonnet 自動分配欄位"}
            </span>
            <button
              className="btn btn-sm"
              style={{ marginLeft: "auto" }}
              onClick={() => setFillOpen(false)}
              disabled={filling}
            >Close</button>
          </div>
          {fillError && <div className="draft-panel-error">{fillError}</div>}
          {/* Mid-stream recovery hint: we have a partial stream but no result and
              we are not currently filling — most likely the user switched away
              while the SSE was running. The backend may have finished anyway. */}
          {!filling && !fillError && !fillResult && fillStream && (
            <div className="draft-panel-stage" style={{ padding: "8px 12px", color: "var(--ink-3)", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span>⚠️ 之前 AI 跑到一半切走 — 後端可能已寫完。</span>
              <button
                className="btn btn-sm"
                onClick={async () => {
                  if (onAfterDraft) await onAfterDraft();
                  // After parent refresh, profile.last_updated changes →
                  // reset useEffect fires → rehydrate runs and (if backend
                  // completed) my_raw_notes/narrative etc. will be in `profile`.
                }}
              >🔄 Refresh 確認</button>
              <button
                className="btn btn-sm"
                onClick={() => { setFillStream(""); setFillStage(""); }}
              >Dismiss</button>
            </div>
          )}
          {fillResult && (
            <div className="draft-panel-stage" style={{ padding: "8px 12px", color: "var(--success,#4a6a3a)" }}>
              ✓ 寫入 {fillResult.fields_written.length} 個欄位:{fillResult.fields_written.join(", ") || "(無變動)"}
              {fillResult.tweet_count > 0 && (
                <span style={{ color: "var(--ink-3)" }}>
                  {" · "}🐦 {fillResult.tweet_count} tweets({fillResult.search_target})
                </span>
              )}
            </div>
          )}
          <div style={{ padding: "10px 12px" }}>
            <textarea
              className="input"
              value={fillNotes}
              onChange={(e) => setFillNotes(e.target.value)}
              placeholder={"範例:\n- 今天 1.2M 進場 0.5 SOL\n- cobie 推說 generational launch(❤450)\n- 看到 dev 拉砸過一次,已減半倉\n- pnl 大概 +$300\n\n貼任何文字都行,Sonnet 會自己抽。"}
              rows={6}
              disabled={filling}
              style={{ fontFamily: "var(--sans)", fontSize: 13 }}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
              <button
                className={cls("btn btn-sm", filling ? "btn-accent" : "btn-primary")}
                onClick={runFill}
                disabled={filling || !fillNotes.trim()}
              >{filling
                ? <><span className="spinner" />extracting…</>
                : "✎ Extract & fill"}</button>
              <span style={{ fontSize: 10, color: "var(--ink-3)", fontFamily: "var(--mono)" }}>
                {fillNotes.length} chars
              </span>
            </div>
          </div>
          {fillStream && (
            <details style={{ padding: "0 12px 10px" }}>
              <summary style={{ fontSize: 10, color: "var(--ink-3)", cursor: "pointer", fontFamily: "var(--mono)" }}>
                ▸ Raw output
              </summary>
              <pre className="draft-panel-stream" style={{ marginTop: 6 }}>{fillStream}</pre>
            </details>
          )}
        </div>
      )}

      {/* Identity */}
      <ProfileSection title="基本資料">
        <div className="profile-grid">
          <Field label="Symbol" value={draft.symbol} onChange={(v) => set("symbol", v)} />
          <Field label="Chain" value={draft.chain} onChange={(v) => set("chain", v)} placeholder="base / solana / ethereum…" />
          <Field label="CA" value={draft.ca} onChange={(v) => set("ca", v)} placeholder="0x… or base58…" wide />
          <SelectField label="Status" value={draft.status} onChange={(v) => set("status", v)}
                       options={PROFILE_STATUSES.map((s) => ({ value: s.id, label: `${s.emoji} ${s.label}` }))} />
          <Field label="First seen" value={draft.first_seen_date} onChange={(v) => set("first_seen_date", v)} placeholder="YYYY-MM-DD" />
          <Field label="Tags" value={draft.tags} onChange={(v) => set("tags", v)} placeholder="memecoin, AI, …" wide />
        </div>
      </ProfileSection>

      <ProfileSection title="我的筆記原文 (Raw notes)">
        <div className="raw-notes-hint">
          ✎ Paste & fill 累積的原文,最新在最上方。AI 不會改寫這裡;手動修改也不會被覆蓋。
        </div>
        <Textarea value={draft.my_raw_notes} onChange={(v) => set("my_raw_notes", v)}
                  placeholder="(空)" rows={8} />
      </ProfileSection>

      <ProfileSection title="敘事 (Narrative)">
        <Textarea value={draft.narrative} onChange={(v) => set("narrative", v)}
                  placeholder="這顆幣是什麼?故事怎麼起來的?為什麼這群人在乎?(Phase 2 可一鍵 AI 起草)" />
      </ProfileSection>

      <ProfileSection title="時間軸 (Timeline)">
        <Textarea value={draft.timeline_json} onChange={(v) => set("timeline_json", v)}
                  placeholder="HH:MM 或 YYYY-MM-DD - 誰 - 做了什麼 - FDV 多少…&#10;先用自由文字寫,Phase 2 會結構化。" rows={6} />
      </ProfileSection>

      <ProfileSection title="KOL 共識">
        <Textarea value={draft.kol_consensus} onChange={(v) => set("kol_consensus", v)}
                  placeholder="哪些 KOL 在喊?共識多強?有沒有人唱反調?" />
      </ProfileSection>

      <ProfileSection
        title="Smart money 動向"
        action={
          <button
            className={cls("btn btn-sm", smartDrafting && "btn-accent")}
            onClick={runSmartMoneyDraft}
            disabled={smartDrafting || drafting}
            title="只刷新 Smart money 欄位,不重寫整份 profile"
          >
            {smartDrafting ? <><span className="spinner" />smart…</> : "Refresh"}
          </button>
        }
      >
        {(smartDrafting || smartStage || smartError || smartStream) && (() => {
          // runSmartMoneyDraft sets smartStage to "✓ Smart money 已更新" on done.
          const smartDone = (smartStage || "").startsWith("✓");
          const smartInterrupted = !smartDrafting && !smartError && smartStream && !smartDone;
          return (
            <>
              <div className={cls("smart-draft-status", smartError && "errored")}>
                {smartError ? smartError
                  : smartInterrupted ? "⚠️ 上次跑到一半切走 — 後端可能已寫完"
                  : smartStage}
                {smartInterrupted && onAfterDraft && (
                  <button
                    className="btn btn-sm"
                    style={{ marginLeft: 8 }}
                    onClick={async () => { await onAfterDraft(); }}
                    title="refresh profile 列表確認後端是否寫入"
                  >🔄 Refresh 確認</button>
                )}
                {!smartDrafting && (smartStream || smartStage) && (
                  <button
                    className="btn btn-sm"
                    style={{ marginLeft: 8 }}
                    onClick={() => { setSmartStream(""); setSmartStage(""); setSmartError(""); }}
                  >Dismiss</button>
                )}
              </div>
              {smartStream && (
                <pre className="smart-draft-preview">{smartStream}</pre>
              )}
            </>
          );
        })()}
        <Textarea value={draft.smart_money_summary} onChange={(v) => set("smart_money_summary", v)}
                  placeholder="鏈上有哪些 smart money 進?多少 USD 流入流出?" />
      </ProfileSection>

      <ProfileSection title="見頂訊號">
        <Textarea value={draft.top_signal} onChange={(v) => set("top_signal", v)}
                  placeholder="當下可辨認的見頂訊號 / 事後才看清的訊號。" />
      </ProfileSection>

      <ProfileSection title="原型筆記 (Archetype)">
        <Textarea value={draft.archetype} onChange={(v) => set("archetype", v)}
                  placeholder="這顆屬於哪一種可重複的原型?辨識特徵?失效條件?下次看到要怎麼想?" />
      </ProfileSection>

      <ProfileSection title="我的買賣">
        <div className="profile-grid">
          <Field label="進場 FDV" value={draft.my_entry_fdv} onChange={(v) => set("my_entry_fdv", v)} placeholder="e.g. 2M" />
          <Field label="進場倉位" value={draft.my_entry_size} onChange={(v) => set("my_entry_size", v)} placeholder="e.g. 0.5 SOL" />
          <Field label="出場 FDV" value={draft.my_exit_fdv} onChange={(v) => set("my_exit_fdv", v)} placeholder="e.g. 8M" />
          <Field label="出場倉位" value={draft.my_exit_size} onChange={(v) => set("my_exit_size", v)} placeholder="e.g. all / 50%" />
          <Field label="PnL" value={draft.my_pnl} onChange={(v) => set("my_pnl", v)} placeholder="+$1,200 / -50%" />
          <Field label="錢包" value={draft.my_wallet} onChange={(v) => set("my_wallet", v)} placeholder="Phantom / Rabby…" />
        </div>
      </ProfileSection>

      <ProfileSection title="回頭看的判斷">
        <Textarea value={draft.my_verdict} onChange={(v) => set("my_verdict", v)}
                  placeholder="小倉試對了嗎?該重倉嗎?該更早出嗎?" rows={3} />
      </ProfileSection>

      <ProfileSection
        title="教訓 (Lesson)"
        action={
          <button
            className={cls("btn btn-sm", distilling && "btn-accent")}
            onClick={runDistillRules}
            disabled={distilling || !((draft.my_lesson || draft.my_verdict || draft.my_raw_notes || "").trim())}
            title="從 lesson + verdict + notes 提煉跨幣可複用的交易法則"
          >
            {distilling ? <><span className="spinner" />提煉中…</> : "✦ 提煉成法則"}
          </button>
        }
      >
        <Textarea value={draft.my_lesson} onChange={(v) => set("my_lesson", v)}
                  placeholder="下次同型態出現時,要怎麼做不一樣?" rows={3} />
        {(distillStage || distillError) && (
          <div style={{
            marginTop: 6, fontSize: 11,
            color: distillError ? "var(--danger,#b00)" : "var(--ink-3)",
            fontFamily: "var(--mono)",
          }}>
            {distillError || distillStage}
          </div>
        )}
      </ProfileSection>
    </div>
  );
}

function ProfileSection({ title, children, action }) {
  return (
    <div className="profile-section">
      <div className="profile-section-head">
        <span>{title}</span>
        {action && <span className="profile-section-action">{action}</span>}
      </div>
      <div className="profile-section-body">{children}</div>
    </div>
  );
}

function Field({ label, value, onChange, placeholder, wide }) {
  return (
    <label className={cls("profile-field", wide && "wide")}>
      <span className="profile-field-label">{label}</span>
      <input
        className="input"
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || ""}
      />
    </label>
  );
}

function SelectField({ label, value, onChange, options }) {
  return (
    <label className="profile-field">
      <span className="profile-field-label">{label}</span>
      <select
        className="input"
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

function Textarea({ value, onChange, placeholder, rows }) {
  return (
    <textarea
      className="input profile-textarea"
      value={value || ""}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder || ""}
      rows={rows || 4}
    />
  );
}

// (Legacy WatchlistStage / NotebookStage removed — replaced by
// WatchtowerStage and ProfileStage above. The old keyword-driven hits view
// `/api/watchlist/hits` is no longer wired into a dedicated stage; entity
// detail lives in WatchtowerStage and the per-coin dossier replaces field
// notes via the Coin Profile workflow.)

// ---------- Dispatch TOC (jump-to-summary nav under dispatch header) ----------
const AUTO_FALLBACK_PREFIX = "[AI auto-summary fallback:";

function parseAutoFallbackSummary(summary) {
  const text = summary || "";
  if (!text.startsWith(AUTO_FALLBACK_PREFIX)) return null;
  const firstBreak = text.indexOf("\n");
  const head = firstBreak >= 0 ? text.slice(0, firstBreak).trim() : text.trim();
  const body = firstBreak >= 0 ? text.slice(firstBreak).trim() : "";
  const reason = head.replace(/^\[/, "").replace(/\]$/, "");
  return { reason, body };
}

function DispatchToc({ summaries, hasDigest }) {
  const groups = useMemo(() => {
    const order = [];
    const map = new Map();
    (summaries || []).forEach(s => {
      const key = s.category_name || "未分類";
      if (!map.has(key)) {
        map.set(key, { name: key, items: [] });
        order.push(key);
      }
      map.get(key).items.push(s);
    });
    return order.map(k => map.get(k));
  }, [summaries]);

  if (!summaries || summaries.length === 0) return null;

  const jumpTo = (id) => {
    const el = document.getElementById(`dispatch-summary-${id}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const jumpToDigest = () => {
    const el = document.querySelector(".dispatch .events-block");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="dispatch-toc">
      <div className="dispatch-toc-label">JUMP TO</div>
      <div className="dispatch-toc-groups">
        {hasDigest && (
          <div className="dispatch-toc-group">
            <span className="dispatch-toc-group-name">digest</span>
            <div className="dispatch-toc-group-chips">
              <button type="button" className="chip accent dispatch-toc-chip" onClick={jumpToDigest}>
                ✧ Daily digest
              </button>
            </div>
          </div>
        )}
        {groups.map(g => (
          <div key={g.name} className="dispatch-toc-group">
            <span className="dispatch-toc-group-name">{g.name}</span>
            <div className="dispatch-toc-group-chips">
              {g.items.map(s => (
                <button
                  key={s.id}
                  type="button"
                  className="chip dispatch-toc-chip"
                  onClick={() => jumpTo(s.id)}
                  title={s.chat_name}
                >
                  {s.chat_name}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------- Day View (shows all summaries/events/notes for a date) ----------
function DayView({
  date, slot, data, digest, digestBusy, onGenerateDigest,
  onRetryAutoSummary, retryAutoSummaryBusy,
  onDeleteSummary, onDeleteEvent, onDeleteNote, hasAI,
  highlightQuery, highlightTarget,
}) {
  // Compute per-field starting indices so each <mark> across the whole DayView
  // gets a globally unique id (`search-hit-N`). Fields are walked in render
  // order: every summary's `summary`, every event's `title`+`description`,
  // every note's `content`. Skipped when no query.
  const summaries = (data && data.summaries) || [];
  const events = (data && data.events) || [];
  const notes = (data && data.notes) || [];

  const matchInfo = useMemo(() => {
    if (!highlightQuery || !data) return { perField: new Map(), total: 0 };
    const perField = new Map();
    let total = 0;
    const add = (key, text) => {
      const c = countMatches(text, highlightQuery);
      if (c > 0) {
        perField.set(key, total);
        total += c;
      }
    };
    summaries.forEach(s => add(`summary-${s.id}`, s.summary));
    events.forEach(e => {
      add(`event-title-${e.id}`, e.title);
      add(`event-desc-${e.id}`, e.description);
    });
    notes.forEach(n => add(`note-${n.id}`, n.content));
    return { perField, total };
  }, [highlightQuery, data, summaries, events, notes]);

  // Resolve clicked target → global match index (default to 0 if any matches).
  const resolveTarget = useCallback((target) => {
    if (!matchInfo.total) return -1;
    if (target && matchInfo.perField.has(target.field)) {
      return matchInfo.perField.get(target.field) + (target.n || 0);
    }
    return 0;
  }, [matchInfo]);

  const [activeIdx, setActiveIdx] = useState(-1);

  // When a new day loads or the user picks a different snippet, jump to it.
  useEffect(() => {
    setActiveIdx(resolveTarget(highlightTarget));
  }, [highlightQuery, highlightTarget, resolveTarget]);

  // Smooth-scroll to the active match and flash the .active state. We wait
  // a tick so the freshly-rendered <mark> is in the DOM.
  useEffect(() => {
    if (activeIdx < 0) return;
    const id = `search-hit-${activeIdx}`;
    const el = document.getElementById(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [activeIdx, matchInfo.total]);

  const goPrev = useCallback(() => {
    if (!matchInfo.total) return;
    setActiveIdx(i => (i <= 0 ? matchInfo.total - 1 : i - 1));
  }, [matchInfo.total]);
  const goNext = useCallback(() => {
    if (!matchInfo.total) return;
    setActiveIdx(i => (i + 1) % matchInfo.total);
  }, [matchInfo.total]);

  if (!data) {
    return <div style={{ padding: 40, textAlign: "center", color: "var(--ink-3)" }}>載入中...</div>;
  }
  const slotLabel = ((data.summary_slot || slot || "") + "").trim();
  const summaryRun = data.summary_run || null;
  const fieldStart = (key) => matchInfo.perField.has(key) ? matchInfo.perField.get(key) : -1;
  const progressText = summaryRun && summaryRun.expected_chats
    ? `${summaryRun.completed_chats}/${summaryRun.expected_chats}`
    : "";
  const fallbackCount = summaryRun ? (summaryRun.fallback_chats || 0) : 0;
  const showRunNotice = summaryRun && ["running", "partial", "failed"].includes(summaryRun.summary_status);
  const showFallbackNotice = summaryRun && fallbackCount > 0;
  const retryActive = !!retryAutoSummaryBusy;

  return (
    <div className="dispatch fade-up">
      {highlightQuery && matchInfo.total > 0 && (
        <div className="search-nav" role="toolbar" aria-label="Search matches navigator">
          <span className="search-nav-label">「{highlightQuery}」</span>
          <span className="search-nav-count">
            {activeIdx >= 0 ? activeIdx + 1 : 0}/{matchInfo.total}
          </span>
          <button className="btn btn-sm btn-ghost" onClick={goPrev} title="Previous match">↑</button>
          <button className="btn btn-sm btn-ghost" onClick={goNext} title="Next match">↓</button>
        </div>
      )}

      <div className="dispatch-header">
        <div className="dispatch-kicker">
          <span>Archive · {date}</span>
        </div>
        <h1 className="dispatch-title">
          Dispatch for <em>{date}</em>{slotLabel ? <span className="day-slot-inline">{slotLabel}</span> : null}
        </h1>
        <div className="dispatch-byline">
          {slotLabel && <span>SLOT <strong>{slotLabel}</strong></span>}
          <span>SUMMARIES <strong>{summaries.length}</strong></span>
          {summaryRun && summaryRun.summary_status && (
            <span>STATUS <strong>{summaryRun.summary_status}</strong></span>
          )}
          {progressText && <span>PROGRESS <strong>{progressText}</strong></span>}
          <span>EVENTS <strong>{events.length}</strong></span>
          <span>NOTES <strong>{notes.length}</strong></span>
          <button
            className="btn btn-sm btn-accent"
            onClick={onGenerateDigest}
            disabled={!hasAI || digestBusy}
            style={{ marginLeft: "auto" }}
          >
            {digestBusy ? <><span className="spinner" />digest…</> : "✧ Daily digest"}
          </button>
        </div>
        <DispatchToc summaries={summaries} hasDigest={!!digest} />
      </div>

      {showRunNotice && (
        <div className={cls("run-notice", summaryRun.summary_status)}>
          <div className="run-notice-main">
            <span>
              {summaryRun.summary_status === "running" && (
                <>
                  Auto summary is still running for this slot.
                  {progressText ? ` ${summaryRun.completed_chats} of ${summaryRun.expected_chats} tracked chats finished so far.` : ""}
                </>
              )}
              {summaryRun.summary_status === "partial" && (
                <>
                  Auto summary finished partially.
                  {progressText ? ` ${summaryRun.completed_chats} of ${summaryRun.expected_chats} tracked chats produced summaries.` : ""}
                  {summaryRun.failed_chats ? ` ${summaryRun.failed_chats} failed.` : ""}
                </>
              )}
              {summaryRun.summary_status === "failed" && (
                <>
                  Auto summary failed for this slot.
                  {summaryRun.failed_chats ? ` ${summaryRun.failed_chats} chats failed.` : ""}
                </>
              )}
            </span>
            {(summaryRun.summary_status === "partial" || summaryRun.summary_status === "failed") && (
              <button
                className="btn btn-sm"
                onClick={onRetryAutoSummary}
                disabled={!onRetryAutoSummary || retryActive}
                title="只重試這個 slot 尚未完成的 chats"
              >
                {retryActive ? "Retry running..." : "Retry failed"}
              </button>
            )}
          </div>
        </div>
      )}

      {showFallbackNotice && (
        <div className="run-notice fallback">
          <div className="run-notice-main">
            <span>
              {retryActive
                ? `Retry is running for ${fallbackCount} fallback summary${fallbackCount === 1 ? "" : " summaries"} in this slot. This page will refresh while the backend works.`
                : `${fallbackCount} auto summary fell back to raw source coverage in this slot. Re-run fallback summaries to regenerate them with the current compact prefilter.`
              }
            </span>
            <button
              className="btn btn-sm"
              onClick={() => onRetryAutoSummary && onRetryAutoSummary({ retryFallbacks: true })}
              disabled={!onRetryAutoSummary || retryActive}
              title="刪除這個 slot 的 fallback summaries，並用目前的新邏輯重跑"
            >
              {retryActive ? "Retry running..." : "Retry fallback"}
            </button>
          </div>
        </div>
      )}

      {digest && (
        <div className="events-block" style={{ marginTop: 20 }}>
          <div className="events-block-head">
            <h3>Daily digest</h3>
            <span className="cnt">AI rollup</span>
          </div>
          <div className="dispatch-body">
            {digest.split(/\n{2,}/).map(p => p.trim()).filter(Boolean).map((p, i) => (
              <MdParagraph key={i} text={p} />
            ))}
          </div>
        </div>
      )}

      {summaries.length > 0 && (
        <div className="events-block">
          <div className="events-block-head">
            <h3>Summaries</h3>
            <span className="cnt">{summaries.length} summaries</span>
          </div>
          {summaries.map(s => {
            const slot = s.summary_slot || formatTime(s.created_at);
            const startIdx = fieldStart(`summary-${s.id}`);
            const fallback = parseAutoFallbackSummary(s.summary);
            return (
            <div key={s.id} id={`dispatch-summary-${s.id}`} className="event">
              <div className="event-tag normal">{slot || "manual"}</div>
              <div className="event-body">
                <div className="h" style={{ fontStyle: "normal" }}>
                  {s.chat_name}
                  {slot && (
                    <span
                      style={{
                        marginLeft: 8, fontSize: 10, padding: "1px 6px",
                        borderRadius: 3, background: "var(--ink-soft, #eee)",
                        color: "var(--ink-2)", fontWeight: 500,
                        verticalAlign: "middle", fontFamily: "var(--mono)",
                      }}
                      title="Summary slot time (UTC+8)"
                    >
                      {slot} UTC+8
                    </span>
                  )}
                  {s.source === "auto" && (
                    <span
                      style={{
                        marginLeft: 8, fontSize: 10, padding: "1px 6px",
                        borderRadius: 3, background: "var(--ink-soft, #eee)",
                        color: "var(--ink-2)", fontWeight: 500,
                        verticalAlign: "middle", fontFamily: "var(--mono)",
                      }}
                      title="背景自動 summarize 產生(非手動觸發)"
                    >
                      🤖 auto
                    </span>
                  )}
                </div>
                <div className="d" style={{ whiteSpace: "pre-wrap" }}>
                  {fallback ? (
                    <div className="summary-fallback-box">
                      <div className="summary-fallback-head">
                        <div className="summary-fallback-title">Deterministic fallback saved</div>
                        <button
                          className="btn btn-sm"
                          onClick={() => onRetryAutoSummary && onRetryAutoSummary({ retryFallbacks: true })}
                          disabled={!onRetryAutoSummary || retryActive}
                          title="刪除這個 slot 的 fallback summaries，並用目前的新邏輯重跑"
                        >
                          {retryActive ? "Retry running..." : "Retry fallback"}
                        </button>
                      </div>
                      <div className="summary-fallback-reason">{fallback.reason}</div>
                      <details>
                        <summary>Show saved source excerpt</summary>
                        <pre>{fallback.body}</pre>
                      </details>
                    </div>
                  ) : (
                    startIdx >= 0
                      ? <Highlighted text={s.summary} query={highlightQuery} startIdx={startIdx} activeIdx={activeIdx} />
                      : s.summary
                  )}
                </div>
                {s.summary_json && <SummaryJsonPanel json={s.summary_json} />}
                <div className="tags">
                  {s.message_count} msgs · {s.hours}h window{slot ? ` · ${slot} UTC+8` : ""}
                  <button
                    className="kw-remove"
                    style={{ marginLeft: 8 }}
                    onClick={() => onDeleteSummary(s.id)}
                    title="Delete summary"
                  >×</button>
                </div>
              </div>
            </div>
          )})}
        </div>
      )}

      {events.length > 0 && (
        <div className="events-block">
          <div className="events-block-head">
            <h3>Events</h3>
            <span className="cnt">{events.length} pinned</span>
          </div>
          {events.map(e => {
            const titleStart = fieldStart(`event-title-${e.id}`);
            const descStart  = fieldStart(`event-desc-${e.id}`);
            return (
            <div key={e.id} className="event">
              <div className={cls("event-tag", e.importance || "normal")}>
                {e.importance === "high" ? "Priority" : e.importance === "low" ? "Minor" : "Notable"}
              </div>
              <div className="event-body">
                <div className="h">
                  {titleStart >= 0
                    ? <Highlighted text={e.title} query={highlightQuery} startIdx={titleStart} activeIdx={activeIdx} />
                    : e.title}
                </div>
                {e.description && (
                  <div className="d">
                    {descStart >= 0
                      ? <Highlighted text={e.description} query={highlightQuery} startIdx={descStart} activeIdx={activeIdx} />
                      : e.description}
                  </div>
                )}
                <div className="tags">
                  {[e.source_chat, e.tags].filter(Boolean).join(" · ")}
                  <button
                    className="kw-remove"
                    style={{ marginLeft: 8 }}
                    onClick={() => onDeleteEvent(e.id)}
                    title="Delete event"
                  >×</button>
                </div>
              </div>
            </div>
          )})}
        </div>
      )}

      {notes.length > 0 && (
        <div className="events-block">
          <div className="events-block-head">
            <h3>Notes</h3>
            <span className="cnt">{notes.length} entries</span>
          </div>
          {notes.map(n => {
            const startIdx = fieldStart(`note-${n.id}`);
            return (
            <div key={n.id} className="event">
              <div className="event-tag normal">Note</div>
              <div className="event-body">
                <div className="h" style={{ fontStyle: "normal" }}>
                  {startIdx >= 0
                    ? <Highlighted text={n.content} query={highlightQuery} startIdx={startIdx} activeIdx={activeIdx} />
                    : n.content}
                </div>
                <div className="tags">
                  {n.tags || ""}
                  <button
                    className="kw-remove"
                    style={{ marginLeft: 8 }}
                    onClick={() => onDeleteNote(n.id)}
                    title="Delete note"
                  >×</button>
                </div>
              </div>
            </div>
          )})}
        </div>
      )}

      {summaries.length === 0 && events.length === 0 && notes.length === 0 && (
        <div style={{ padding: 40, textAlign: "center", color: "var(--ink-3)" }}>這天還沒有記錄</div>
      )}
    </div>
  );
}

// ---------- Daily Brief aggregate (multi-chat) ----------
function BriefAggregate({ items, running, hours, modelLabel, onClear }) {
  const total = items.length;
  const done = items.filter((x) => x.status === "done").length;
  const errored = items.filter((x) => x.status === "error").length;
  const pending = items.filter((x) => x.status === "pending").length;
  const active = items.filter((x) => x.status === "running" || x.status === "fetching").length;

  return (
    <div className="brief-aggregate fade-up">
      <div
        style={{
          display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
          padding: "12px 0", marginBottom: 12,
          borderBottom: "1px solid var(--rule)",
        }}
      >
        <span className="chip">{total} chats</span>
        {done > 0 && <span className="chip success">{done} done</span>}
        {active > 0 && <span className="chip accent">{active} running</span>}
        {pending > 0 && <span className="chip">{pending} queued</span>}
        {errored > 0 && <span className="chip alert">{errored} errors</span>}
        <div style={{ flex: 1 }} />
        {!running && items.length > 0 && (
          <button className="btn btn-sm btn-ghost" onClick={onClear}>Clear</button>
        )}
      </div>

      {items.map((it, i) => {
        if (it.status === "error") {
          return (
            <div key={i} className="dispatch fade-up" style={{ marginBottom: 28 }}>
              <div className="dispatch-header">
                <div className="dispatch-kicker"><span>Error</span></div>
                <h1 className="dispatch-title" style={{ fontSize: 20 }}>{it.chatName}</h1>
              </div>
              <div style={{ color: "var(--alert, #b04226)", padding: "8px 0" }}>
                ✗ {it.error || "失敗"}
              </div>
            </div>
          );
        }
        if (it.status === "pending") {
          return (
            <div key={i} style={{
              padding: "10px 14px", marginBottom: 8,
              border: "1px dashed var(--rule)", borderRadius: 6,
              color: "var(--ink-3)", fontSize: 12,
              display: "flex", alignItems: "center", gap: 8,
            }}>
              <span style={{ fontFamily: "var(--mono)" }}>⋯</span>
              <strong style={{ color: "var(--ink-2)" }}>{it.chatName}</strong>
              <span>排隊中</span>
            </div>
          );
        }
        const isStreaming = it.status === "running" || it.status === "fetching";
        return (
          <div key={i} style={{ marginBottom: 28 }}>
            <Dispatch
              chatName={it.chatName}
              hours={hours}
              summary={it.summary}
              events={it.events}
              saved={it.status === "done"}
              streaming={isStreaming}
              streamed={it.streamed}
              progress={it.progress}
              stageTxt={it.stageTxt}
              modelLabel={modelLabel}
              messageCount={it.messageCount}
              profileJson={it.profileJson}
            />
          </div>
        );
      })}
    </div>
  );
}

// ---------- Trading Rules side-panel (overlay, callable from any view) ----------
const RULE_SCOPES = [
  { id: "general", label: "general", emoji: "·" },
  { id: "entry",   label: "entry",   emoji: "↗" },
  { id: "exit",    label: "exit",    emoji: "↘" },
  { id: "risk",    label: "risk",    emoji: "⚠" },
  { id: "sizing",  label: "sizing",  emoji: "⚖" },
];
const RULE_STATUSES = [
  { id: "active",   label: "active",   emoji: "●" },
  { id: "draft",    label: "draft",    emoji: "○" },
  { id: "archived", label: "archived", emoji: "✕" },
];

// UTC+8 short formatter for last_hit_at / created_at — `datetime('now',
// 'localtime')` is already Asia/Taipei, so we just trim seconds.
function fmtRuleTime(s) {
  if (!s) return "";
  const t = String(s).trim();
  if (t.length >= 16) return t.slice(0, 16);
  return t;
}

function RulesPanel({
  open, onClose, initialDraft, sourceProfileSymbol, sourceProfileId,
  highlightProfileId,
}) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("active");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState([]); // candidates pending review
  const [editing, setEditing] = useState(null); // {id, ...} | null
  const [creating, setCreating] = useState(null); // {rule_text, reason, scope} | null

  const load = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (statusFilter && statusFilter !== "all") params.set("status", statusFilter);
      if (search.trim()) params.set("q", search.trim());
      if (highlightProfileId) params.set("source_profile_id", String(highlightProfileId));
      const r = await apiFetch(`/api/trading_rules?${params.toString()}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      setRules(j.rules || []);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [open, statusFilter, search, highlightProfileId]);

  useEffect(() => { load(); }, [load]);

  // Seed drafts from props each time the panel opens with new candidates.
  useEffect(() => {
    if (open && Array.isArray(initialDraft) && initialDraft.length) {
      setDrafts(initialDraft.map((c, i) => ({ ...c, _key: `seed-${Date.now()}-${i}` })));
    } else if (!open) {
      setDrafts([]);
      setEditing(null);
      setCreating(null);
    }
  }, [open, initialDraft]);

  const save = async (id, patch) => {
    try {
      const r = await apiFetch(`/api/trading_rules/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      await load();
      return true;
    } catch (e) {
      setError(e.message || String(e));
      return false;
    }
  };

  const create = async (payload) => {
    try {
      const r = await apiFetch(`/api/trading_rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      await load();
      return true;
    } catch (e) {
      setError(e.message || String(e));
      return false;
    }
  };

  const remove = async (id) => {
    if (!confirm("刪除這條法則?(若只是想暫停,改成 archived 即可)")) return;
    try {
      const r = await apiFetch(`/api/trading_rules/${id}`, { method: "DELETE" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      await load();
    } catch (e) {
      setError(e.message || String(e));
    }
  };

  const adoptDraft = async (idx) => {
    const d = drafts[idx];
    const ok = await create({
      rule_text: d.rule_text,
      reason: d.reason || "",
      scope: d.scope || "general",
      source_profile_id: d.source_profile_id || sourceProfileId || null,
      status: "active",
    });
    if (ok) setDrafts((prev) => prev.filter((_, i) => i !== idx));
  };

  const dismissDraft = (idx) => setDrafts((prev) => prev.filter((_, i) => i !== idx));

  if (!open) return null;

  return (
    <div className="cat-modal-backdrop" onClick={onClose}>
      <div
        className="cat-modal"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 520, maxWidth: "96vw",
          height: "calc(100vh - 24px)", maxHeight: "calc(100vh - 24px)",
          marginLeft: "auto", marginRight: 12,
          borderRadius: 4,
        }}
      >
        <div className="cat-modal-head">
          <strong>⚖ 交易法則 · Trading rules</strong>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>×</button>
        </div>

        {/* Filters */}
        <div className="cat-modal-section" style={{ overflowY: "visible" }}>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            {[{ id: "active", label: "active" },
              { id: "draft", label: "draft" },
              { id: "archived", label: "archived" },
              { id: "all", label: "all" }].map((s) => (
              <button
                key={s.id}
                className={cls("btn btn-sm", statusFilter === s.id && "btn-accent")}
                onClick={() => setStatusFilter(s.id)}
                style={{ fontSize: 11 }}
              >{s.label}</button>
            ))}
            <input
              className="input"
              placeholder="搜 rule_text / reason"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ flex: 1, minWidth: 120, fontSize: 11 }}
            />
            <button
              className="btn btn-sm btn-primary"
              onClick={() => setCreating({ rule_text: "", reason: "", scope: "general", status: "active" })}
              style={{ fontSize: 11 }}
            >+ 新增</button>
          </div>
          {highlightProfileId && (
            <div style={{ marginTop: 6, fontSize: 10, color: "var(--ink-3)" }}>
              ✦ 只顯示來自 profile #{highlightProfileId} 的法則
            </div>
          )}
          {error && (
            <div style={{ marginTop: 6, fontSize: 11, color: "var(--danger,#b00)" }}>{error}</div>
          )}
        </div>

        {/* Drafts (AI candidates pending review) */}
        {drafts.length > 0 && (
          <div className="cat-modal-section" style={{ background: "var(--paper-2,#fafaf7)" }}>
            <div style={{ fontSize: 11, color: "var(--ink-2)", marginBottom: 8 }}>
              ✦ AI 提煉的候選法則
              {sourceProfileSymbol && <> · 來源 ${sourceProfileSymbol}</>}
              {" — 採用前可編輯"}
            </div>
            {drafts.map((d, i) => (
              <DraftRuleCard
                key={d._key || i}
                draft={d}
                onChange={(patch) => setDrafts((prev) => {
                  const next = [...prev];
                  next[i] = { ...next[i], ...patch };
                  return next;
                })}
                onAdopt={() => adoptDraft(i)}
                onDismiss={() => dismissDraft(i)}
              />
            ))}
          </div>
        )}

        {/* Create form */}
        {creating && (
          <div className="cat-modal-section" style={{ background: "var(--paper-2,#fafaf7)" }}>
            <div style={{ fontSize: 11, color: "var(--ink-2)", marginBottom: 8 }}>+ 新增法則</div>
            <RuleForm
              draft={creating}
              onChange={(patch) => setCreating((d) => ({ ...d, ...patch }))}
              onSubmit={async () => {
                if (!creating.rule_text.trim()) {
                  setError("rule_text 不可為空"); return;
                }
                const ok = await create({
                  rule_text: creating.rule_text.trim(),
                  reason: creating.reason || "",
                  scope: creating.scope || "general",
                  status: creating.status || "active",
                });
                if (ok) setCreating(null);
              }}
              onCancel={() => setCreating(null)}
              submitLabel="建立"
            />
          </div>
        )}

        {/* List */}
        <div className="cat-modal-section" style={{ flex: 1, overflowY: "auto" }}>
          {loading && <div style={{ fontSize: 11, color: "var(--ink-3)" }}>載入中…</div>}
          {!loading && rules.length === 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-3)", textAlign: "center", padding: "24px 0" }}>
              還沒有法則。試試在 coin profile 詳情頁按「✦ 提煉成法則」,或上方「+ 新增」直接寫一條。
            </div>
          )}
          {rules.map((r) => (
            editing && editing.id === r.id ? (
              <div key={r.id} className="cat-row" style={{ flexDirection: "column", alignItems: "stretch" }}>
                <RuleForm
                  draft={editing}
                  onChange={(patch) => setEditing((d) => ({ ...d, ...patch }))}
                  onSubmit={async () => {
                    const ok = await save(r.id, {
                      rule_text: editing.rule_text,
                      reason: editing.reason,
                      scope: editing.scope,
                      status: editing.status,
                    });
                    if (ok) setEditing(null);
                  }}
                  onCancel={() => setEditing(null)}
                  submitLabel="儲存"
                />
              </div>
            ) : (
              <RuleCard
                key={r.id}
                rule={r}
                onEdit={() => setEditing({ ...r })}
                onTogglePin={() => save(r.id, { pinned: r.pinned ? 0 : 1 })}
                onSetStatus={(status) => save(r.id, { status })}
                onDelete={() => remove(r.id)}
              />
            )
          ))}
        </div>
      </div>
    </div>
  );
}

function RuleCard({ rule, onEdit, onTogglePin, onSetStatus, onDelete }) {
  const scope = RULE_SCOPES.find((s) => s.id === rule.scope) || RULE_SCOPES[0];
  const statusObj = RULE_STATUSES.find((s) => s.id === rule.status) || RULE_STATUSES[0];
  return (
    <div className="cat-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 4, padding: "10px 0" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 6, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3)" }}>
          #{rule.id}
        </span>
        <span style={{ fontSize: 10, color: "var(--ink-3)" }}>
          {scope.emoji} {scope.label}
        </span>
        <span style={{ fontSize: 10, color: "var(--ink-3)" }}>
          · {statusObj.emoji} {statusObj.label}
        </span>
        {rule.pinned ? (
          <span style={{ fontSize: 10, color: "var(--accent)" }}>· ★ pinned</span>
        ) : null}
        <span style={{ fontSize: 10, color: "var(--ink-3)", marginLeft: "auto" }}>
          hit ×{rule.hit_count || 0}
          {rule.last_hit_at ? ` · 最後 ${fmtRuleTime(rule.last_hit_at)}` : ""}
        </span>
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.4 }}>{rule.rule_text}</div>
      {rule.reason && (
        <div style={{ fontSize: 11, color: "var(--ink-2)", lineHeight: 1.4 }}>{rule.reason}</div>
      )}
      <div style={{ display: "flex", gap: 4, marginTop: 4, fontSize: 10, color: "var(--ink-3)", alignItems: "center", flexWrap: "wrap" }}>
        {rule.source_profile_id ? <span>← profile #{rule.source_profile_id}</span> : null}
        <span style={{ marginLeft: rule.source_profile_id ? 6 : 0 }}>
          built {fmtRuleTime(rule.created_at)}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          <button className="btn btn-sm" onClick={onEdit}>edit</button>
          <button className="btn btn-sm" onClick={onTogglePin}>{rule.pinned ? "unpin" : "pin"}</button>
          {rule.status !== "archived" ? (
            <button className="btn btn-sm" onClick={() => onSetStatus("archived")}>archive</button>
          ) : (
            <button className="btn btn-sm" onClick={() => onSetStatus("active")}>activate</button>
          )}
          <button className="btn btn-sm" onClick={onDelete}>del</button>
        </div>
      </div>
    </div>
  );
}

function DraftRuleCard({ draft, onChange, onAdopt, onDismiss }) {
  return (
    <div style={{ borderLeft: "2px solid var(--accent, #888)", padding: "6px 10px", margin: "6px 0" }}>
      <input
        className="input"
        value={draft.rule_text || ""}
        onChange={(e) => onChange({ rule_text: e.target.value })}
        placeholder="rule_text"
        style={{ fontSize: 13, marginBottom: 4 }}
      />
      <textarea
        className="input"
        value={draft.reason || ""}
        onChange={(e) => onChange({ reason: e.target.value })}
        placeholder="reason"
        rows={2}
        style={{ fontSize: 11, marginBottom: 4 }}
      />
      <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
        <select
          className="input"
          value={draft.scope || "general"}
          onChange={(e) => onChange({ scope: e.target.value })}
          style={{ fontSize: 11, width: 100 }}
        >
          {RULE_SCOPES.map((s) => <option key={s.id} value={s.id}>{s.emoji} {s.label}</option>)}
        </select>
        <button
          className="btn btn-sm btn-primary"
          onClick={onAdopt}
          disabled={!(draft.rule_text || "").trim()}
          style={{ marginLeft: "auto" }}
        >✓ 採用</button>
        <button className="btn btn-sm" onClick={onDismiss}>✕ 丟棄</button>
      </div>
    </div>
  );
}

function RuleForm({ draft, onChange, onSubmit, onCancel, submitLabel }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <input
        className="input"
        value={draft.rule_text || ""}
        onChange={(e) => onChange({ rule_text: e.target.value })}
        placeholder="rule_text(一句話 ≤ 30 字)"
        style={{ fontSize: 13 }}
        autoFocus
      />
      <textarea
        className="input"
        value={draft.reason || ""}
        onChange={(e) => onChange({ reason: e.target.value })}
        placeholder="reason(為何成立 / 來自哪次經驗)"
        rows={2}
        style={{ fontSize: 11 }}
      />
      <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
        <select
          className="input"
          value={draft.scope || "general"}
          onChange={(e) => onChange({ scope: e.target.value })}
          style={{ fontSize: 11, width: 100 }}
        >
          {RULE_SCOPES.map((s) => <option key={s.id} value={s.id}>{s.emoji} {s.label}</option>)}
        </select>
        <select
          className="input"
          value={draft.status || "active"}
          onChange={(e) => onChange({ status: e.target.value })}
          style={{ fontSize: 11, width: 110 }}
        >
          {RULE_STATUSES.map((s) => <option key={s.id} value={s.id}>{s.emoji} {s.label}</option>)}
        </select>
        <button className="btn btn-sm btn-primary" onClick={onSubmit} style={{ marginLeft: "auto" }}>
          {submitLabel || "儲存"}
        </button>
        <button className="btn btn-sm" onClick={onCancel}>cancel</button>
      </div>
    </div>
  );
}

const COMPARE_LEADERBOARD_DATA = {
  weekly: [
    ["up", "skills", "+1.5k", "sk"], ["flat", "openhuman", "+1.4k", "oh"],
    ["flat", "hermes-agent", "+990", "ha"], ["flat", "andrej-karpathy-...", "+949", "ak"],
    ["up", "x-algorithm", "+868", "xa"], ["flat", "superpowers", "+851", "sp"],
    ["flat", "everything-claud...", "+683", "ec"], ["flat", "RuView", "+668", "rv"],
    ["up", "agentmemory", "+645", "am"], ["down", "cc-switch", "+575", "cc"],
    ["flat", "DeepSeek-TUI", "+531", "ds"], ["flat", "spec-kit", "+484", "gh"],
    ["flat", "awesome-design...", "+427", "ad"], ["up", "agency-agents", "+420", "aa"],
    ["flat", "gstack", "+398", "gs"], ["down", "skills", "+395", "ai"],
    ["up", "scientific-agent-...", "+382", "sa"], ["down", "financial-services", "+359", "fs"],
    ["new", "opencode", "+308", "op"], ["down", "AiToEarn", "+306", "at"],
  ],
  all: [
    ["flat", "openhuman", "+18.4k", "oh"], ["up", "skills", "+16.9k", "sk"],
    ["flat", "spec-kit", "+13.2k", "gh"], ["up", "DeepSeek-TUI", "+12.6k", "ds"],
    ["down", "cc-switch", "+10.8k", "cc"], ["flat", "x-algorithm", "+9.7k", "xa"],
    ["new", "opencode", "+8.9k", "op"], ["flat", "gstack", "+7.2k", "gs"],
  ],
  random: [
    ["new", "prompt-hub", "+188", "ph"], ["up", "tiny-agents", "+176", "ta"],
    ["down", "benchflow", "+164", "bf"], ["flat", "codecart", "+151", "cc"],
    ["up", "model-router", "+139", "mr"], ["flat", "agentmemory", "+128", "am"],
  ],
  pyramid: [
    ["up", "skills", "Apex", "sk"], ["flat", "openhuman", "Apex", "oh"],
    ["flat", "hermes-agent", "Core", "ha"], ["up", "x-algorithm", "Core", "xa"],
    ["new", "opencode", "Rising", "op"], ["down", "AiToEarn", "Rising", "at"],
  ],
};

const COMPARE_TABS = [
  ["weekly", "Weekly"],
  ["all", "All-time"],
  ["random", "Random"],
  ["pyramid", "Pyramid"],
];

function CompareLeaderboardCol() {
  const [tab, setTab] = useState("weekly");
  const rows = COMPARE_LEADERBOARD_DATA[tab] || COMPARE_LEADERBOARD_DATA.weekly;
  return (
    <div className="col compare-col">
      <div className="col-head">
        <div className="col-eyebrow">Section · Leaderboard</div>
        <div className="col-title"><em>Coding AI</em> ranking</div>
        <div className="col-sub">GitHub repos by recent star momentum</div>
      </div>
      <div className="compare-board">
        <div className="compare-tabs" role="tablist" aria-label="Leaderboard period">
          {COMPARE_TABS.map(([id, label]) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              className="compare-tab"
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="compare-list">
          {rows.map(([trend, name, score, avatar], index) => (
            <button className="compare-row" key={`${tab}-${name}-${index}`}>
              <span className="compare-rank">{index + 1}</span>
              <span className={`compare-trend ${trend}`} aria-label={trend}></span>
              <span className={`compare-avatar compare-avatar-${index % 8}`}>{avatar}</span>
              <span className="compare-name">{name}</span>
              <span className="compare-score">{score}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

const COMPARE_SINCE_OPTIONS = [
  ["daily", "Today"],
  ["weekly", "This week"],
  ["monthly", "This month"],
];

const COMPARE_LANGUAGE_OPTIONS = [
  ["", "All"],
  ["python", "Python"],
  ["typescript", "TypeScript"],
  ["rust", "Rust"],
  ["go", "Go"],
];

function formatRepoCount(n) {
  n = Number(n || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}m`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

function CompareTrendingStage({
  repos, loading, error, since, setSince, language, setLanguage, onRefresh,
}) {
  const top = repos && repos[0];
  const totalPeriodStars = (repos || []).reduce((sum, r) => sum + Number(r.period_stars || 0), 0);
  const periodLabel = since === "daily" ? "stars today" : since === "weekly" ? "stars this week" : "stars this month";
  return (
    <div className="compare-trending">
      <StageHead
        eyebrow="Recent GitHub heat"
        title={top ? `${top.name} leads GitHub Trending` : "GitHub Trending radar"}
        meta="GitHub repositories rising in stars"
        right={
          <button className="btn btn-sm" onClick={onRefresh} disabled={loading}>
            {loading ? <><span className="spinner" />Loading</> : "Refresh"}
          </button>
        }
      />
      <div className="compare-trending-inner">
        <div className="compare-trending-stats">
          <div><span>Tracked repos</span><strong>{repos.length}</strong></div>
          <div><span>Top repo</span><strong>{top ? top.full_name : "-"}</strong></div>
          <div><span>{periodLabel}</span><strong>{formatRepoCount(totalPeriodStars)}</strong></div>
        </div>

        <div className="compare-trending-toolbar">
          <div className="compare-stage-tabs" role="tablist" aria-label="Trending range">
            {COMPARE_SINCE_OPTIONS.map(([id, label]) => (
              <button
                key={id}
                role="tab"
                aria-selected={since === id}
                onClick={() => setSince(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="compare-language-pills" aria-label="Language filter">
            {COMPARE_LANGUAGE_OPTIONS.map(([id, label]) => (
              <button
                key={id || "all"}
                aria-pressed={language === id}
                onClick={() => setLanguage(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {error && <div className="compare-trending-error">{error}</div>}
        {loading && repos.length === 0 ? (
          <div className="compare-trending-loading"><span className="spinner" /> Fetching GitHub Trending...</div>
        ) : (
          <div className="compare-repo-grid">
            {(repos || []).map((repo) => (
              <a className="compare-repo-card" key={repo.full_name} href={repo.url} target="_blank" rel="noreferrer">
                <div className="compare-repo-rank">{repo.rank}</div>
                <div className="compare-repo-main">
                  <div className="compare-repo-name">
                    <img
                      className="compare-repo-avatar"
                      src={`https://github.com/${repo.owner}.png?size=40`}
                      alt=""
                      loading="lazy"
                      referrerPolicy="no-referrer"
                      onError={(e) => { e.currentTarget.style.visibility = "hidden"; }}
                    />
                    <span>{repo.owner}</span> / {repo.name}
                  </div>
                  <p>{repo.description || "No description provided."}</p>
                  <div className="compare-repo-meta">
                    {repo.language && <span className="compare-repo-language">{repo.language}</span>}
                    <span>{formatRepoCount(repo.stars)} stars</span>
                    <span>{formatRepoCount(repo.forks)} forks</span>
                    {repo.period_stars > 0 && <strong>+{formatRepoCount(repo.period_stars)}</strong>}
                  </div>
                </div>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, {
  TimeControl, StageHead, Empty, Dispatch,
  MessagesList, MemoryCol, DayView,
  BriefAggregate, CoinSearchView,
  SmartHoldersCol, SmartHoldersStage,
  WatchtowerCol, WatchtowerStage,
  ProfilesCol, ProfileStage,
  RulesPanel, CompareLeaderboardCol, CompareTrendingStage,
});
