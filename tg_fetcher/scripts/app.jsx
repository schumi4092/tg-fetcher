/* Telegraph main app */
const { useState: uS, useEffect: uE, useRef: uR, useMemo: uM } = React;

const SAMPLE_TOPICS = [
  { id: 1, title: "General", unread: 12 },
  { id: 2, title: "Alpha calls", unread: 45 },
  { id: 3, title: "Research", unread: 3 },
  { id: 4, title: "Macro", unread: 0 },
  { id: 5, title: "Jokes", unread: 151 },
];

function App() {
  const [loggedIn, setLoggedIn] = uS(true);
  const [user, setUser] = uS({ name: "Demo", username: "demo", phone: "" });
  const [view, setView] = uS("brief"); // inbox | brief | memory | alerts | notes

  // Inbox state
  const [selectedChat, setSelectedChat] = uS("1001");
  const [batchMode, setBatchMode] = uS(false);
  const [batchSet, setBatchSet] = uS(new Set());

  // Fetch / summary state
  const [hours, setHours] = uS(8);
  const [messages, setMessages] = uS(MOCK.messages);
  const [selectedTopics, setSelectedTopics] = uS(new Set());
  const [summaryState, setSummaryState] = uS({ streaming: false, progress: 0, stageTxt: "", streamed: "" });
  const [hasSummary, setHasSummary] = uS(true);

  // Memory
  const [activeDate, setActiveDate] = uS(null);

  // Watchlist / notes
  const [watchlist, setWatchlist] = uS(MOCK.watchlist);
  const [notes, setNotes] = uS(MOCK.notes);

  // Tweaks
  const DEFAULTS = /*EDITMODE-BEGIN*/{
    "skin": "editorial",
    "theme": "light",
    "density": "normal",
    "accent": "ochre",
    "typeScale": 17,
    "serifHeads": true
  }/*EDITMODE-END*/;
  const [tweaks, setTweaks] = uS(DEFAULTS);
  const [tweaksOpen, setTweaksOpen] = uS(false);

  // Apply theme
  uE(() => {
    // 'editorial' is the default (no skin attr). Others: matrix, obsidian, press.
    if (tweaks.skin && tweaks.skin !== "editorial") {
      document.documentElement.dataset.skin = tweaks.skin;
    } else {
      delete document.documentElement.dataset.skin;
    }
    document.documentElement.dataset.theme = tweaks.theme;
    document.body.dataset.density = tweaks.density;
    // Only apply the Accent tweak in Editorial skin — other skins own their palette.
    const isEditorial = !tweaks.skin || tweaks.skin === "editorial";
    if (isEditorial) {
      const accents = {
        ochre: ["#9a5b2a", "#b04226", "#4a6a3a"],
        moss: ["#3d5a4a", "#b04226", "#6c5425"],
        cobalt: ["#3b5b8c", "#b04226", "#4a6a3a"],
        rose: ["#a4415f", "#b04226", "#4a6a3a"],
      };
      const a = accents[tweaks.accent] || accents.ochre;
      document.documentElement.style.setProperty("--accent", a[0]);
      document.documentElement.style.setProperty("--alert", a[1]);
      document.documentElement.style.setProperty("--success", a[2]);
    } else {
      document.documentElement.style.removeProperty("--accent");
      document.documentElement.style.removeProperty("--alert");
      document.documentElement.style.removeProperty("--success");
    }
  }, [tweaks]);

  // Tweaks bridge
  uE(() => {
    const onMsg = (e) => {
      if (e.data?.type === "__activate_edit_mode") setTweaksOpen(true);
      else if (e.data?.type === "__deactivate_edit_mode") setTweaksOpen(false);
    };
    window.addEventListener("message", onMsg);
    window.parent.postMessage({ type: "__edit_mode_available" }, "*");
    return () => window.removeEventListener("message", onMsg);
  }, []);

  const updateTweak = (k, v) => {
    const next = { ...tweaks, [k]: v };
    setTweaks(next);
    window.parent.postMessage({ type: "__edit_mode_set_keys", edits: { [k]: v } }, "*");
  };

  // Batch toggle
  const onBatch = (id) => {
    if (id === "__clear") return setBatchSet(new Set());
    setBatchSet(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };
  const onBatchExport = () => {
    alert(`[Prototype] Would export ${batchSet.size} chats as CSV for the past ${hours}h window.`);
  };

  // Summarize (simulated stream)
  const runSummarize = () => {
    setHasSummary(false);
    setSummaryState({ streaming: true, progress: 5, stageTxt: "Preparing prompt…", streamed: "" });
    const stages = [
      { pct: 18, txt: "Fetching context…" },
      { pct: 36, txt: "Scoring relevance…" },
      { pct: 55, txt: "Extracting events…" },
      { pct: 68, txt: "Opus is writing…" },
    ];
    let i = 0;
    const t1 = setInterval(() => {
      if (i >= stages.length) { clearInterval(t1); return; }
      setSummaryState(s => ({ ...s, progress: stages[i].pct, stageTxt: stages[i].txt }));
      i++;
    }, 600);

    const fullText = MOCK.aiSummary.summary;
    setTimeout(() => {
      clearInterval(t1);
      let idx = 0;
      const t2 = setInterval(() => {
        idx += Math.floor(Math.random() * 4) + 2;
        if (idx >= fullText.length) {
          clearInterval(t2);
          setSummaryState({ streaming: false, progress: 100, stageTxt: "", streamed: "" });
          setHasSummary(true);
          return;
        }
        setSummaryState(s => ({ ...s, streamed: fullText.slice(0, idx), progress: Math.min(98, 70 + Math.floor(idx/fullText.length * 28)) }));
      }, 35);
    }, 2400);
  };

  const runFetch = () => {
    setMessages([]);
    setTimeout(() => setMessages(MOCK.messages), 500);
  };

  // Chat meta
  const activeChat = MOCK.chats.find(c => c.id === selectedChat) || MOCK.chats[0];
  const chatTypeLabel = { channel: "Channel", supergroup: "Supergroup", group: "Group", private: "Direct" }[activeChat?.type] || "";

  if (!loggedIn) {
    return (
      <div className="shell" style={{ gridTemplateColumns: "1fr" }}>
        <main className="stage">
          <LoginView onLogin={(u) => { setUser(u); setLoggedIn(true); }} />
        </main>
      </div>
    );
  }

  // Middle column
  let middleCol = null;
  if (view === "inbox" || view === "brief") {
    middleCol = (
      <Inbox
        chats={MOCK.chats}
        selectedChat={selectedChat}
        onSelect={(id) => { setSelectedChat(id); setView("brief"); }}
        batchMode={batchMode}
        setBatchMode={setBatchMode}
        batchSet={batchSet}
        onBatch={onBatch}
        onBatchExport={onBatchExport}
      />
    );
  } else if (view === "memory") {
    middleCol = (
      <MemoryCol
        timeline={MOCK.timeline}
        onOpenDay={(d) => { setActiveDate(d); setView("memory"); }}
        activeDate={activeDate}
        qa={MOCK.qa}
      />
    );
  } else if (view === "alerts") {
    middleCol = (
      <AlertsCol
        watchlist={watchlist}
        onAdd={(kw, cat) => setWatchlist([...watchlist, { keyword: kw, category: cat }])}
        onRemove={(i) => setWatchlist(watchlist.filter((_, j) => j !== i))}
      />
    );
  } else if (view === "notes") {
    middleCol = (
      <NotesCol
        notes={notes}
        onAdd={(text, tags) => setNotes([{ id: Date.now(), date: "just now", tags: tags.split(",").map(t=>t.trim()).filter(Boolean), text }, ...notes])}
      />
    );
  }

  // Stage content
  let stage = null;
  if (view === "brief" && selectedChat) {
    stage = (
      <>
        <StageHead
          title={activeChat.name}
          meta={`${chatTypeLabel} · ${activeChat.username ? "@"+activeChat.username : "ID "+activeChat.id} · window ${hours}h`}
          extra={<TimeControl hours={hours} setHours={setHours} />}
          right={
            <div style={{ display: "flex", gap: 6 }}>
              <button className="btn btn-ghost btn-sm" onClick={runFetch}>Re-fetch</button>
              <button className="btn btn-accent btn-sm" onClick={runSummarize} disabled={summaryState.streaming}>
                {summaryState.streaming ? "…writing" : "✧ Summarize"}
              </button>
            </div>
          }
        />
        {activeChat.is_forum && (
          <div className="topics-row">
            <span className="topics-label">Topics</span>
            <button className={cls("topic-pill", selectedTopics.size===0 && "active")} onClick={()=>setSelectedTopics(new Set())}>All</button>
            {SAMPLE_TOPICS.map(t => (
              <button key={t.id} className={cls("topic-pill", selectedTopics.has(t.id) && "active")}
                onClick={()=> {
                  const ns = new Set(selectedTopics);
                  ns.has(t.id) ? ns.delete(t.id) : ns.add(t.id);
                  setSelectedTopics(ns);
                }}>
                {t.title}{t.unread?` · ${t.unread}`:""}
              </button>
            ))}
          </div>
        )}
        <div className="stage-body reading">
          {hasSummary || summaryState.streaming ? (
            <Dispatch
              chat={activeChat.name}
              hours={hours}
              summary={MOCK.aiSummary.summary}
              events={MOCK.aiSummary.events}
              streaming={summaryState.streaming}
              streamed={summaryState.streamed}
              progress={summaryState.progress}
              stageTxt={summaryState.stageTxt}
            />
          ) : (
            <Empty mark="◧" title="Ready to summarize" desc="Adjust the time window and click Summarize to have Opus turn this channel's last few hours into a readable dispatch." />
          )}
          <div style={{ marginTop: 60, paddingTop: 24, borderTop: "1px solid var(--rule)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 16 }}>
              <h3 style={{ fontFamily: "var(--serif)", fontSize: 20, fontWeight: 500, fontStyle: "italic", letterSpacing: "-0.01em" }}>
                Source messages
              </h3>
              <span className="mono" style={{ fontSize: 10, color: "var(--ink-3)", letterSpacing: ".08em", textTransform: "uppercase" }}>
                {messages.length} raw · past {hours}h
              </span>
            </div>
            <MessagesList messages={messages} watchlist={watchlist} />
          </div>
        </div>
      </>
    );
  } else if (view === "inbox") {
    stage = (
      <>
        <StageHead title="Inbox" meta="Select a channel to read or press ⌘F to search" />
        <div className="stage-body">
          <Empty mark="◧" title="Pick a channel" desc="Select any channel on the left to generate a fresh dispatch, browse raw messages, or export to CSV." />
        </div>
      </>
    );
  } else if (view === "memory") {
    const activeDay = activeDate ? MOCK.timeline.find(t => t.date === activeDate) : null;
    stage = (
      <>
        <StageHead
          title={activeDay ? `Dispatch for ${activeDay.date}` : "Memory"}
          meta={activeDay ? `${activeDay.summaries} brief · ${activeDay.events} events · ${activeDay.notes} notes` : "Browse the archive or ask a question"}
          right={activeDay && <button className="btn btn-sm" onClick={()=>setActiveDate(null)}>← Back</button>}
        />
        <div className="stage-body reading">
          {activeDay ? (
            <Dispatch
              chat={"Daily rollup"}
              hours={24}
              summary={MOCK.aiSummary.summary}
              events={MOCK.aiSummary.events.slice(0, Math.max(1, activeDay.events))}
              streaming={false}
              streamed=""
              progress={100}
              stageTxt=""
            />
          ) : (
            <Empty mark="⏳" title="Archive of days" desc="Every daily brief is stored locally with its events and notes. Pick a date on the left, or ask the archive a question." />
          )}
        </div>
      </>
    );
  } else if (view === "alerts") {
    stage = (
      <>
        <StageHead title="Watchlist" meta="Keywords are matched across every fetch and highlighted inline" />
        <div className="stage-body reading">
          <div className="dispatch-header">
            <div className="dispatch-kicker"><span>How alerts work</span></div>
            <h1 className="dispatch-title">Turn noisy channels into <em>signal</em>.</h1>
          </div>
          <div className="dispatch-body">
            <p>Add terms you actually care about — ticker symbols, project names, macroeconomic keywords, or Chinese words like <span className="mono">爆倉</span>. Every time Telegraph fetches a channel, matches are pulled to the top and marked inline.</p>
            <p>You can also set <em>sound alerts</em> and <em>digest frequency</em> per keyword (coming soon in the Pro tier).</p>
          </div>
          <div className="events-block">
            <div className="events-block-head">
              <h3>Recent keyword hits</h3>
              <span className="cnt">last 24h · 3 channels</span>
            </div>
            {MOCK.aiSummary.events.map((e,i) => (
              <div className="event" key={i}>
                <div className={cls("event-tag", e.importance)}>Matched</div>
                <div className="event-body">
                  <div className="h">{e.title}</div>
                  <div className="d">{e.desc}</div>
                  <div className="tags">{e.tags}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </>
    );
  } else if (view === "notes") {
    stage = (
      <>
        <StageHead title="Notebook" meta={`${notes.length} entries · synced into daily briefs`} />
        <div className="stage-body reading">
          <div className="dispatch-header">
            <div className="dispatch-kicker"><span>Field journal</span></div>
            <h1 className="dispatch-title">Your own <em>annotations</em>, layered onto the wire.</h1>
          </div>
          <div className="dispatch-body">
            <p>Notes are merged into each daily dispatch — the AI references your own observations when summarizing. Use tags like <span className="mono">#策略</span>, <span className="mono">#研究</span>, <span className="mono">#觀察</span> to group them.</p>
          </div>

          <div className="events-block">
            <div className="events-block-head">
              <h3>All notes</h3>
              <span className="cnt">{notes.length} entries</span>
            </div>
            {notes.map(n => (
              <div className="event" key={n.id}>
                <div className="event-tag normal">{n.date}</div>
                <div className="event-body">
                  <div className="h" style={{ fontStyle: "normal" }}>{n.text}</div>
                  <div className="tags">{n.tags.map(t => "#"+t).join(" · ")}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </>
    );
  }

  const unreadAlerts = watchlist.length;

  return (
    <>
      <div className="shell">
        <Rail view={view} setView={setView} alertCount={unreadAlerts} onAvatar={() => setLoggedIn(false)} />
        {middleCol}
        <main className="stage">{stage}</main>
      </div>
      <button
        onClick={() => setTweaksOpen(o => !o)}
        style={{
          position: "fixed", right: 16, bottom: 16, zIndex: 180,
          width: 36, height: 36, borderRadius: "50%",
          background: "var(--ink)", color: "var(--paper)",
          border: "0", cursor: "pointer",
          fontFamily: "var(--mono)", fontSize: 14,
          boxShadow: "var(--shadow-md)",
        }}
        title="Tweaks"
      >✦</button>
      {tweaksOpen && <TweaksPanel tweaks={tweaks} update={updateTweak} onClose={()=>setTweaksOpen(false)} />}
    </>
  );
}

function TweaksPanel({ tweaks, update, onClose }) {
  return (
    <div className="tweaks">
      <div className="tweaks-head">
        <h3>Tweaks</h3>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>×</button>
      </div>
      <div className="tweaks-body">
        <div className="tweak-row">
          <div className="label">Style</div>
          <div className="tweak-opts">
            {[
              {id:"editorial", label:"Editorial"},
              {id:"aesop", label:"Aesop"},
              {id:"monocle", label:"Monocle"},
              {id:"gallery", label:"Gallery"},
              {id:"nordic", label:"Nordic"},
              {id:"noir", label:"Noir"},
              {id:"atelier", label:"Atelier"},
              {id:"hermes", label:"Hermès"},
              {id:"kinfolk", label:"Kinfolk"},
            ].map(v => (
              <button key={v.id} className={cls("tweak-opt", (tweaks.skin||"editorial")===v.id && "active")} onClick={()=>update("skin", v.id)}>{v.label}</button>
            ))}
          </div>
        </div>
        <div className="tweak-row">
          <div className="label">Theme</div>
          <div className="tweak-opts">
            {["light","dark"].map(v => (
              <button key={v} className={cls("tweak-opt", tweaks.theme===v && "active")} onClick={()=>update("theme", v)}>{v}</button>
            ))}
          </div>
        </div>
        <div className="tweak-row">
          <div className="label">Accent</div>
          <div className="tweak-opts">
            {["ochre","moss","cobalt","rose"].map(v => (
              <button key={v} className={cls("tweak-opt", tweaks.accent===v && "active")} onClick={()=>update("accent", v)}>{v}</button>
            ))}
          </div>
        </div>
        <div className="tweak-row">
          <div className="label">Density</div>
          <div className="tweak-opts">
            {["tight","normal","loose"].map(v => (
              <button key={v} className={cls("tweak-opt", tweaks.density===v && "active")} onClick={()=>update("density", v)}>{v}</button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { App, TweaksPanel });

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
