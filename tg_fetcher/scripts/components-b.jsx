/* Telegraph — Stage views (Brief, Messages, Memory, etc.) */

// ---------- Time control (reusable) ----------
function TimeControl({ hours, setHours }) {
  const presets = [1, 4, 8, 24, 48, 72];
  return (
    <div className="time-control">
      <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
        <span className="time-val">{hours}</span>
        <span className="time-label">h</span>
      </div>
      <input type="range" className="time-slider" min="1" max="72" value={hours} onChange={(e)=>setHours(Number(e.target.value))} />
      <div className="time-presets">
        {presets.map(p => (
          <button key={p} className={hours===p ? "active" : ""} onClick={()=>setHours(p)}>{p}h</button>
        ))}
      </div>
    </div>
  );
}

// ---------- Stage head ----------
function StageHead({ title, meta, right, extra }) {
  return (
    <div className="stage-head">
      <div className="stage-title">
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

// ---------- Dispatch (the AI summary as an editorial article) ----------
function Dispatch({ chat, hours, summary, events, streamed, streaming, progress, stageTxt }) {
  return (
    <div className="dispatch fade-up">
      {streaming && (
        <div className="progress-panel">
          <div className="progress-stage-txt">
            <span className="spinner" />
            {stageTxt}
          </div>
          <div className="progress-track"><div className="progress-fill" style={{ width: progress + "%" }} /></div>
          <div className="progress-footer">
            <span>Model · Claude Opus 4</span>
            <span>{progress}%</span>
          </div>
        </div>
      )}

      <div className="dispatch-header">
        <div className="dispatch-kicker">
          <span>Dispatch №47</span>
          <span style={{ color: "var(--ink-3)" }}>{new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}</span>
        </div>
        <h1 className="dispatch-title">
          BTC reclaims <em>$104k</em> as ETF inflows mark their seventh consecutive day.
        </h1>
        <div className="dispatch-byline">
          <span>FROM <strong>{chat}</strong></span>
          <span>WINDOW <strong>{hours}h</strong></span>
          <span>MESSAGES <strong>11</strong></span>
          <span>EVENTS <strong>{events.length}</strong></span>
        </div>
      </div>

      <div className={cls("dispatch-body", streaming && "streaming")}>
        {streaming ? streamed : summary.split("\n\n").map((p, i) => <p key={i}>{p}</p>)}
      </div>

      {!streaming && events.length > 0 && (
        <div className="events-block">
          <div className="events-block-head">
            <h3>Pinned events</h3>
            <span className="cnt">{events.length} items · saved to memory</span>
          </div>
          {events.map((e, i) => (
            <div className="event" key={i}>
              <div className={cls("event-tag", e.importance)}>{e.importance === "high" ? "Priority" : e.importance === "normal" ? "Notable" : "Minor"}</div>
              <div className="event-body">
                <div className="h">{e.title}</div>
                <div className="d">{e.desc}</div>
                <div className="tags">{e.tags}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- Messages list ----------
function MessagesList({ messages, watchlist }) {
  const [alertIdx, setAlertIdx] = useState(-1);
  const alertCount = messages.filter(m => m.alerts && m.alerts.length).length;
  const kws = watchlist.map(w => w.keyword);
  return (
    <>
      {alertCount > 0 && (
        <div className="alert-banner">
          <span>⚑</span>
          <span>Detected <strong>{alertCount}</strong> message{alertCount>1?"s":""} matching watchlist keywords</span>
          <div className="alert-nav">
            <span className="mono" style={{fontSize:10}}>{alertIdx+1}/{alertCount}</span>
            <button onClick={()=>setAlertIdx(i => Math.max(0, i-1))}>↑</button>
            <button onClick={()=>setAlertIdx(i => Math.min(alertCount-1, i+1))}>↓</button>
          </div>
        </div>
      )}
      <div className="stats-bar">
        <span className="chip">{messages.length} messages</span>
        <span className="chip">{[...new Set(messages.map(m => m.from))].length} senders</span>
        <span className="chip">{messages.filter(m=>m.media).length} media</span>
      </div>

      <div className="msg-list">
        {messages.map(m => {
          const hasAlert = m.alerts && m.alerts.length;
          return (
            <div key={m.id} className={cls("msg", hasAlert && "alert")}>
              <div className="msg-time-col">{m.time}</div>
              <div>
                <div className="msg-sender">
                  {m.from}
                  {m.username && <span className="at">@{m.username}</span>}
                </div>
                <div className="msg-text">{highlightKw(m.text, m.alerts || [])}</div>
                {m.media && <span className="msg-media">▣ {m.media}</span>}
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

// ---------- Fetch panel (for raw messages view) ----------
function FetchPanel({ chatName, hours, setHours, onFetch, onSummarize, hasMessages, topics, selectedTopics, onToggleTopic }) {
  return (
    <div className="fetch-panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2>Fetch window</h2>
        <span className="mono" style={{ fontSize: 10, color: "var(--ink-3)", letterSpacing: ".08em", textTransform: "uppercase" }}>
          {chatName}
        </span>
      </div>
      <div className="fetch-row">
        <TimeControl hours={hours} setHours={setHours} />
        <button className="btn btn-primary" onClick={onFetch}>
          ↙ Fetch messages
        </button>
        {hasMessages && (
          <button className="btn btn-accent" onClick={onSummarize}>
            ✧ Summarize with AI
          </button>
        )}
        <div style={{ flex: 1 }} />
        <button className="btn btn-sm">JSON</button>
        <button className="btn btn-sm">CSV</button>
      </div>
      {topics && topics.length > 0 && (
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
          <span className="topics-label">Topics</span>
          <button className={cls("topic-pill", selectedTopics.size===0 && "active")} onClick={()=>onToggleTopic(null)}>All</button>
          {topics.map(t => (
            <button key={t.id} className={cls("topic-pill", selectedTopics.has(t.id) && "active")} onClick={()=>onToggleTopic(t.id)}>
              {t.title}{t.unread ? ` · ${t.unread}` : ""}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- Memory column (timeline + Q&A) ----------
function MemoryCol({ timeline, onOpenDay, activeDate, qa, onAsk }) {
  const [q, setQ] = useState("");
  const [history, setHistory] = useState(qa);
  const [loading, setLoading] = useState(false);

  const ask = () => {
    if (!q.trim()) return;
    setLoading(true);
    const qt = q;
    setQ("");
    setTimeout(() => {
      setHistory([{ q: qt, a: "Searching 347 stored summaries for relevant context… Found 3 matching entries from the past 14 days. Based on them: the most consistent theme is ETF flow momentum, which inflected positive on Dec 8 and has continued. ETH/BTC ratio bottomed around the same day." }, ...history]);
      setLoading(false);
    }, 1400);
  };

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 02 · Memory</div>
        <div className="col-title"><em>Archive</em> of days.</div>
        <div className="col-sub">{timeline.length} dispatches stored · ask anything, <span className="mono">⌘K</span> to search</div>
      </div>

      <div style={{ padding: "10px 16px", borderBottom: "1px solid var(--rule)", display: "flex", gap: 6 }}>
        <input className="input" placeholder="Search memory…" style={{ fontSize: 12 }} />
        <button className="btn btn-sm" title="Export">↑</button>
        <button className="btn btn-sm" title="Import">↓</button>
      </div>

      <div className="col-body">
        <div className="timeline-list">
          {timeline.map((t, i) => {
            const { day, month } = formatDate(t.date);
            const isActive = t.date === activeDate;
            return (
              <div key={t.date} className={cls("timeline-row", isActive && "active")} onClick={() => onOpenDay(t.date)}>
                <div className="timeline-date">
                  {day}
                  <span className="month">{month}</span>
                </div>
                <div>
                  <div className="timeline-headlines">
                    {[t.headlines].flat().slice(0,3).map((h, j) => (
                      <span key={j} className="hl">{h}</span>
                    ))}
                  </div>
                  <div className="timeline-chips">
                    {t.summaries > 0 && <span className="chip">{t.summaries} brief</span>}
                    {t.events > 0 && <span className={cls("chip", t.high>0 && "alert")}>{t.events} events</span>}
                    {t.notes > 0 && <span className="chip accent">{t.notes} notes</span>}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="qa-wrap">
          <div className="label">Ask the archive</div>
          <div className="qa-input-row">
            <input
              className="input"
              placeholder="上週 ETH 有什麼大事？"
              value={q}
              onChange={(e)=>setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && ask()}
            />
            <button className="btn btn-sm btn-accent" onClick={ask} disabled={loading}>
              {loading ? <span className="spinner" /> : "Ask →"}
            </button>
          </div>
          {history.map((h, i) => (
            <div key={i} className="qa-bubble">
              <div className="qa-q">{h.q}</div>
              <div className="qa-a">{h.a}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------- Alerts column ----------
function AlertsCol({ watchlist, onAdd, onRemove }) {
  const [kw, setKw] = useState("");
  const [cat, setCat] = useState("主流");
  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 03 · Watchlist</div>
        <div className="col-title"><em>Keywords</em> &amp; alerts</div>
        <div className="col-sub">{watchlist.length} keywords · highlighted across all fetches</div>
      </div>
      <div className="note-form">
        <div className="label" style={{marginBottom:8}}>Add keyword</div>
        <div style={{ display: "flex", gap: 6 }}>
          <input className="input" placeholder="BTC, 爆倉, Coinbase…" value={kw} onChange={(e)=>setKw(e.target.value)} onKeyDown={(e)=>e.key==="Enter" && (onAdd(kw, cat), setKw(""))} />
          <button className="btn btn-primary btn-sm" onClick={() => { if(kw.trim()){onAdd(kw, cat); setKw("");} }}>Add</button>
        </div>
        <select className="input mono" value={cat} onChange={(e)=>setCat(e.target.value)} style={{ fontSize: 11, marginTop: 6 }}>
          <option>主流</option><option>市場結構</option><option>資金</option><option>穩定幣</option><option>監管</option><option>交易所</option><option>其他</option>
        </select>
      </div>
      <div className="col-body">
        {watchlist.map((w, i) => (
          <div className="kw-item" key={i}>
            <span className="kw-tag">{w.keyword}</span>
            <span className="kw-cat">{w.category}</span>
            <button className="kw-remove" onClick={()=>onRemove(i)}>×</button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------- Notes column ----------
function NotesCol({ notes, onAdd }) {
  const [text, setText] = useState("");
  const [tags, setTags] = useState("");
  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 04 · Notebook</div>
        <div className="col-title"><em>Field</em> notes.</div>
        <div className="col-sub">Record strategy, theses, observations — merged into daily dispatches.</div>
      </div>
      <div className="note-form">
        <div className="label">New note</div>
        <textarea className="input" placeholder="Observation, strategy, thesis…" value={text} onChange={(e)=>setText(e.target.value)} rows={3} />
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <input className="input" placeholder="Tags (comma-separated)" value={tags} onChange={(e)=>setTags(e.target.value)} style={{ fontSize: 11 }} />
          <button className="btn btn-primary btn-sm" onClick={()=>{ if(text.trim()){ onAdd(text, tags); setText(""); setTags(""); } }}>Save</button>
        </div>
      </div>
      <div className="col-body">
        {notes.map(n => (
          <div key={n.id} className="note">
            <div className="note-top">
              <span>{n.date}</span>
              <span>#{n.id}</span>
            </div>
            <div className="note-body">{n.text}</div>
            <div className="note-tags">
              {n.tags.map((t, i) => <span key={i} className="chip">{t}</span>)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { TimeControl, StageHead, Empty, Dispatch, MessagesList, FetchPanel, MemoryCol, AlertsCol, NotesCol });
