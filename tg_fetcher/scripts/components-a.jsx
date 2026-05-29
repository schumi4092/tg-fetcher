/* Telegraph app logic — single-file React prototype */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ---------- utilities ----------
const esc = (s) => (s == null ? "" : String(s));
const cls = (...a) => a.filter(Boolean).join(" ");

function highlightKw(text, keywords) {
  if (!keywords || !keywords.length) return text;
  const parts = [text];
  keywords.forEach((kw) => {
    for (let i = 0; i < parts.length; i++) {
      if (typeof parts[i] !== "string") continue;
      const re = new RegExp(`(${kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
      const split = parts[i].split(re);
      parts.splice(
        i,
        1,
        ...split.map((p, idx) => (idx % 2 ? <span key={`${kw}-${i}-${idx}`} className="kw-hit">{p}</span> : p))
      );
      i += split.length - 1;
    }
  });
  return parts;
}

function formatDate(dateStr) {
  const d = new Date(dateStr);
  const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  return { day: d.getDate(), month: months[d.getMonth()], year: d.getFullYear() };
}

// ---------- Rail ----------
function Rail({ view, setView, alertCount, onAvatar }) {
  const items = [
    { id: "inbox", icon: "◧", label: "Inbox" },
    { id: "brief", icon: "◉", label: "Daily Brief" },
    { id: "memory", icon: "⏳", label: "Memory" },
    { id: "alerts", icon: "!", label: "Alerts", badge: alertCount > 0 },
    { id: "notes", icon: "✎", label: "Notes" },
  ];
  return (
    <nav className="rail" aria-label="Main navigation">
      <div className="mark" title="Telegraph">
        <span className="dash">•—</span>
      </div>
      {items.map((it) => (
        <button
          key={it.id}
          className="rail-btn"
          aria-pressed={view === it.id}
          aria-label={it.label}
          onClick={() => setView(it.id)}
          title={it.label}
        >
          <span style={{ fontFamily: "var(--mono)", fontSize: it.id === "alerts" ? 16 : 18, fontWeight: 600 }}>
            {it.icon}
          </span>
          {it.badge && <span className="dot" />}
        </button>
      ))}
      <div className="rail-spacer" />
      <button className="rail-btn" aria-label="Settings" title="Settings">
        <span style={{ fontFamily: "var(--mono)", fontSize: 18 }}>⚙</span>
      </button>
      <div className="rail-avatar" title="Account" onClick={onAvatar}>W</div>
    </nav>
  );
}

// ---------- Login ----------
function LoginView({ onLogin }) {
  const [phase, setPhase] = useState("phone");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [pw, setPw] = useState("");
  const [loading, setLoading] = useState(false);
  const next = () => {
    setLoading(true);
    setTimeout(() => {
      if (phase === "phone") { setPhase("code"); setLoading(false); }
      else if (phase === "code") { setPhase("pw"); setLoading(false); }
      else { onLogin({ name: "Demo", username: "demo", phone }); }
    }, 700);
  };
  return (
    <div className="stage-body" style={{ maxWidth: "unset" }}>
      <div className="login-card">
        <div className="login-eyebrow">Telegraph — Sign dispatch access</div>
        <h1 className="login-title">Fetch <em>signal</em> from the wire.</h1>
        <p className="login-sub">Telegraph connects to your Telegram account to fetch channel messages, summarize them into daily dispatches, and build a searchable memory.</p>

        <div className="login-step">
          <span className="num">{phase === "phone" ? 1 : phase === "code" ? 2 : 3}</span>
          <span>{phase === "phone" ? "Phone number" : phase === "code" ? "Verification code" : "Two-step password"}</span>
        </div>

        {phase === "phone" && (
          <div>
            <label className="label">Phone (with country code)</label>
            <input className="input mono" value={phone} onChange={(e)=>setPhone(e.target.value)} placeholder="+886 9xx xxx xxx" />
          </div>
        )}
        {phase === "code" && (
          <div>
            <label className="label">Enter the 5-digit code sent to {phone}</label>
            <input className="input mono" value={code} onChange={(e)=>setCode(e.target.value)} placeholder="12345" autoFocus />
          </div>
        )}
        {phase === "pw" && (
          <div>
            <label className="label">Cloud password (two-step)</label>
            <input className="input mono" type="password" value={pw} onChange={(e)=>setPw(e.target.value)} placeholder="••••••••" autoFocus />
          </div>
        )}

        <div style={{ marginTop: 20, display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={next} disabled={loading} style={{ flex: 1 }}>
            {loading ? <><span className="spinner" />sending…</> : phase === "phone" ? "Send code →" : phase === "code" ? "Verify →" : "Sign in →"}
          </button>
          {phase !== "phone" && (
            <button className="btn" onClick={() => setPhase("phone")}>Back</button>
          )}
        </div>

        <div className="login-hr" />
        <p className="muted" style={{ fontSize: 11, lineHeight: 1.5 }}>
          Your session stays on this device. Telegraph never stores your phone number or cloud password on any remote server.
        </p>
      </div>
    </div>
  );
}

// ---------- Inbox (chat list) ----------
const TAG_FILTERS = [
  { id: "all", label: "All" },
  { id: "tier-1", label: "Tier 1" },
  { id: "alpha", label: "Alpha" },
  { id: "market", label: "Market" },
  { id: "research", label: "Research" },
  { id: "tech", label: "Tech" },
  { id: "community", label: "Community" },
  { id: "unread", label: "Unread" },
];

function Inbox({ chats, selectedChat, onSelect, batchMode, setBatchMode, batchSet, onBatch, onBatchExport }) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("all");

  const visible = useMemo(() => {
    let list = chats;
    if (filter === "unread") list = list.filter(c => c.unread > 0);
    else if (filter !== "all") list = list.filter(c => c.tag === filter);
    if (q) {
      const lc = q.toLowerCase();
      list = list.filter(c => c.name.toLowerCase().includes(lc) || (c.username && c.username.toLowerCase().includes(lc)));
    }
    return list;
  }, [chats, q, filter]);

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 01 · Inbox</div>
        <div className="col-title"><em>Channels</em> <span style={{ fontStyle: "italic" }}>&amp;</span> groups</div>
        <div className="col-sub">{chats.length} subscribed · {chats.filter(c=>c.unread>0).length} with unread · last sync <span className="mono">4s ago</span></div>
      </div>

      <div className="inbox-search">
        <input className="input" placeholder="Filter channels…" value={q} onChange={(e)=>setQ(e.target.value)} />
        <button
          className={cls("btn btn-sm", batchMode && "btn-accent")}
          onClick={() => setBatchMode(!batchMode)}
          title="Batch export mode"
        >
          {batchMode ? `☑ ${batchSet.size}` : "☐"}
        </button>
      </div>

      <div className="filter-row">
        {TAG_FILTERS.map(f => (
          <button key={f.id} className={cls("filter-chip", filter===f.id && "active")} onClick={()=>setFilter(f.id)}>
            {f.label}
          </button>
        ))}
      </div>

      <div className="col-body">
        <div className="chat-list">
          {visible.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>No channels match.</div>
          ) : visible.map(c => {
            const isActive = selectedChat === c.id;
            const isBatch = batchSet.has(c.id);
            const initial = (c.name || "#").charAt(0).toUpperCase();
            return (
              <div
                key={c.id}
                className={cls("chat-item", isActive && "active", isBatch && "batch-checked")}
                onClick={() => batchMode ? onBatch(c.id) : onSelect(c.id)}
              >
                {batchMode && <span className="batch-checkbox">{isBatch ? "✓" : ""}</span>}
                <div className={cls("chat-avatar", c.type)}>{initial}</div>
                <div className="chat-body">
                  <div className="chat-row-top">
                    <div className="chat-name">
                      {c.name}
                      {c.is_forum && <span className="chat-forum-badge">Forum</span>}
                    </div>
                    <span className="chat-time">{c.last}</span>
                  </div>
                  <div className="chat-preview">{c.preview}</div>
                </div>
                {c.unread > 0 && !batchMode && <span className="chat-unread">{c.unread > 99 ? "99+" : c.unread}</span>}
              </div>
            );
          })}
        </div>
        {batchMode && (
          <div className="batch-bar">
            <span className="batch-count">{batchSet.size} selected</span>
            <select defaultValue="24">
              <option value="4">4h</option><option value="8">8h</option>
              <option value="24">24h</option><option value="48">48h</option>
              <option value="72">72h</option><option value="168">7d</option>
            </select>
            <button className="btn btn-sm btn-primary" disabled={batchSet.size===0} onClick={onBatchExport}>Export CSV</button>
            <button className="btn btn-sm btn-ghost" onClick={() => onBatch("__clear")}>Clear</button>
            <div className="summary">
              {batchSet.size === 0 ? "Click channels to add to export queue"
                : `Queued: ${Array.from(batchSet).slice(0,3).map(id => chats.find(c=>c.id===id)?.name || id).join(" · ")}${batchSet.size>3?` +${batchSet.size-3}`:""}`}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { Rail, LoginView, Inbox, esc, cls, highlightKw, formatDate });
