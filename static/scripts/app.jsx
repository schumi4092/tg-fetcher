/* Telegraph — main app (live Flask backend) */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ---------- Tweaks defaults (EDITMODE block read by external design tool) ----------
const DEFAULTS = /*EDITMODE-BEGIN*/{
  "skin": "editorial",
  "theme": "light",
  "density": "normal",
  "accent": "ochre",
  "typeScale": 17,
  "serifHeads": true
}/*EDITMODE-END*/;

const ACCENT_COLORS = {
  ochre:    "#9a5b2a",
  ink:      "#2a3d52",
  olive:    "#5a6b3a",
  rust:     "#8a3a2a",
  slate:    "#3a4a5a",
  forest:   "#3a5a4a",
  burgundy: "#6a2a3a",
};

const SKIN_LIST = ["editorial","aesop","monocle","gallery","nordic","noir","atelier","hermes","kinfolk"];

// ---------- Tweaks panel (floating; toggle with T key or rail gear) ----------
function TweaksPanel({ tweaks, setTweaks, open, onClose }) {
  if (!open) return null;
  const set = (k, v) => setTweaks({ ...tweaks, [k]: v });
  return (
    <div
      style={{
        position: "fixed", right: 20, bottom: 20, zIndex: 999,
        background: "var(--paper)", border: "1px solid var(--rule)",
        padding: 14, width: 280, fontSize: 12,
        boxShadow: "0 8px 32px rgba(0,0,0,0.15)", borderRadius: 6,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <strong style={{ textTransform: "uppercase", letterSpacing: 1, fontSize: 11 }}>Tweaks</strong>
        <button className="btn btn-sm btn-ghost" onClick={onClose}>×</button>
      </div>

      <div className="label">Skin</div>
      <select className="input" value={tweaks.skin} onChange={(e) => set("skin", e.target.value)} style={{ marginBottom: 8 }}>
        {SKIN_LIST.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>

      <div className="label">Theme</div>
      <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
        {["light", "dark"].map((t) => (
          <button key={t} className={cls("btn btn-sm", tweaks.theme === t && "btn-accent")} onClick={() => set("theme", t)} style={{ flex: 1 }}>{t}</button>
        ))}
      </div>

      <div className="label">Density</div>
      <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
        {["tight", "normal", "loose"].map((d) => (
          <button key={d} className={cls("btn btn-sm", tweaks.density === d && "btn-accent")} onClick={() => set("density", d)} style={{ flex: 1 }}>{d}</button>
        ))}
      </div>

      <div className="label">Accent</div>
      <div style={{ display: "flex", gap: 4, marginBottom: 8, flexWrap: "wrap" }}>
        {Object.keys(ACCENT_COLORS).map((a) => (
          <button
            key={a}
            onClick={() => set("accent", a)}
            title={a}
            style={{
              width: 22, height: 22, borderRadius: "50%",
              background: ACCENT_COLORS[a], cursor: "pointer",
              border: tweaks.accent === a ? "2px solid var(--ink-1)" : "1px solid var(--rule)",
              padding: 0,
            }}
          />
        ))}
      </div>

      <div className="label">Type scale · {tweaks.typeScale}px</div>
      <input
        type="range" min="14" max="20" value={tweaks.typeScale}
        onChange={(e) => set("typeScale", Number(e.target.value))}
        style={{ width: "100%", marginBottom: 8 }}
      />

      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, cursor: "pointer" }}>
        <input type="checkbox" checked={tweaks.serifHeads} onChange={(e) => set("serifHeads", e.target.checked)} />
        Serif headings
      </label>

      <button className="btn btn-sm btn-ghost" onClick={() => setTweaks({ ...DEFAULTS })} style={{ marginTop: 10, width: "100%" }}>
        Reset to defaults
      </button>
    </div>
  );
}

// ---------- Model/hours sub-head for Brief view ----------
function BriefHeadRight({ model, setModel, onRun, running, disabled }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div style={{ display: "flex", gap: 4 }}>
        {[["sonnet", "Sonnet"], ["opus", "Opus"]].map(([k, l]) => (
          <button key={k} className={cls("btn btn-sm", model === k && "btn-accent")} onClick={() => setModel(k)}>
            {l}
          </button>
        ))}
      </div>
      <button className="btn btn-primary btn-sm" onClick={onRun} disabled={disabled || running}>
        {running ? <><span className="spinner" />Summarize…</> : "✧ Summarize"}
      </button>
    </div>
  );
}

// ---------- App ----------
function App() {
  // ---- session / boot ----
  const [booted, setBooted] = useState(false);
  const [connected, setConnected] = useState(false);
  const [user, setUser] = useState(null);
  const [hasAI, setHasAI] = useState(false);
  const [hasEmb, setHasEmb] = useState(false);
  const [banner, setBanner] = useState(null);
  const bannerTimer = useRef(null);

  const showBanner = useCallback((type, text, ttl = 3000) => {
    if (bannerTimer.current) clearTimeout(bannerTimer.current);
    setBanner({ type, text });
    if (ttl > 0) {
      bannerTimer.current = setTimeout(() => setBanner(null), ttl);
    }
  }, []);

  // ---- nav ----
  const [view, setView] = useState("brief");

  // ---- inbox / chats ----
  const [chats, setChats] = useState([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [lastSyncTs, setLastSyncTs] = useState(0);
  const [selectedChat, setSelectedChat] = useState(null);
  const [batchMode, setBatchMode] = useState(false);
  const [batchSet, setBatchSet] = useState(() => new Set());
  const [batchHours, setBatchHours] = useState(24);
  const [batchExportBusy, setBatchExportBusy] = useState(false);

  // ---- messages / topics ----
  const [messages, setMessages] = useState([]);
  const [msgLoading, setMsgLoading] = useState(false);
  const [topics, setTopics] = useState([]);
  const [selectedTopics, setSelectedTopics] = useState(() => new Set());
  const topicsById = useMemo(() => {
    const m = {};
    topics.forEach((t) => { m[t.id] = t.title; });
    return m;
  }, [topics]);

  // ---- brief / summarize ----
  const [hours, setHours] = useState(8);
  const [model, setModel] = useState("sonnet");
  const [summary, setSummary] = useState("");
  const [events, setEvents] = useState([]);
  const [saved, setSaved] = useState(false);
  const [profileJson, setProfileJson] = useState(null);
  const [streaming, setStreaming] = useState(false);
  const [streamed, setStreamed] = useState("");
  const [progress, setProgress] = useState(0);
  const [stageTxt, setStageTxt] = useState("");
  const streamedRef = useRef("");

  // ---- Daily Brief aggregate (multi-chat) ----
  const [briefMode, setBriefMode] = useState("single"); // "single" | "aggregate"
  const [briefItems, setBriefItems] = useState([]);
  const [briefRunning, setBriefRunning] = useState(false);
  const briefItemsRef = useRef([]);

  // ---- memory ----
  const [timeline, setTimeline] = useState([]);
  const [activeDate, setActiveDate] = useState(null);
  const [activeSlot, setActiveSlot] = useState("");
  const [dayData, setDayData] = useState(null);
  const [digest, setDigest] = useState("");
  const [digestBusy, setDigestBusy] = useState(false);
  const [retryAutoSummaryBusy, setRetryAutoSummaryBusy] = useState(false);
  const [retryAutoSummaryQueued, setRetryAutoSummaryQueued] = useState(null);
  const [qaHistory, setQaHistory] = useState([]);
  const [qaLoading, setQaLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState(null);
  const [searchBusy, setSearchBusy] = useState(false);
  // ---- Search highlight (carried into DayView when user clicks a result) ----
  // highlightQuery: the term to <mark>; highlightTarget: { field, n } picking
  // the specific occurrence to scroll to first.
  const [highlightQuery, setHighlightQuery] = useState("");
  const [highlightTarget, setHighlightTarget] = useState(null);

  // ---- watchlist (legacy: still drives MessagesList keyword highlighting) ----
  const [watchlist, setWatchlist] = useState([]);
  // ---- field notes (legacy: surfaced inside Memory > Day view) ----
  const [notes, setNotes] = useState([]);

  // ---- Compare / GitHub Trending ----
  const [githubSince, setGithubSince] = useState("daily");
  const [githubLanguage, setGithubLanguage] = useState("");
  const [githubTrending, setGithubTrending] = useState([]);
  const [githubTrendingBusy, setGithubTrendingBusy] = useState(false);
  const [githubTrendingError, setGithubTrendingError] = useState("");

  // ---- Watchtower (Section 03 — entity radar from harvested summary_json) ----
  const [entities, setEntities] = useState([]);
  const [entitiesLoading, setEntitiesLoading] = useState(false);
  const [entityWindow, setEntityWindow] = useState(14);
  const [entityKindFilter, setEntityKindFilter] = useState("");
  const [entitySearch, setEntitySearch] = useState("");
  const [selectedEntityKey, setSelectedEntityKey] = useState(null);

  // ---- Smart-money CA holder lookup ----
  const [holderQuery, setHolderQuery] = useState("");
  const [holderDays, setHolderDays] = useState(180);
  const [holderResult, setHolderResult] = useState(null);
  const [holderBusy, setHolderBusy] = useState(false);
  const [holderError, setHolderError] = useState("");

  // ---- Coin profiles (Section 04 — per-asset research dossiers) ----
  const [profiles, setProfiles] = useState([]);
  const [profilesStatus, setProfilesStatus] = useState("");
  const [profilesSearch, setProfilesSearch] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState(null);
  // Build-from-notes state lives here (not in ProfilesCol) so the SSE keeps
  // running and the user can navigate to other views while it builds. The
  // panel reads this state via prop and re-renders the busy / done indicator
  // whenever they return to the profiles view.
  const [reviewBuild, setReviewBuild] = useState({
    busy: false, stage: "", error: "", lastResult: null,
  });
  const [profileDraftTasks, setProfileDraftTasks] = useState({});
  const profileDraftTasksRef = useRef({});

  // ---- Trading rules side-panel (cross-coin guardrails, callable from any view) ----
  const [rulesPanelOpen, setRulesPanelOpen] = useState(false);
  const [rulesPanelDraft, setRulesPanelDraft] = useState(null);   // candidates seeded from distill
  const [rulesPanelSrcSym, setRulesPanelSrcSym] = useState(null);
  const [rulesPanelSrcId, setRulesPanelSrcId] = useState(null);

  // ---- chat categories ----
  const [categories, setCategories] = useState([]);
  const [categoryProfiles, setCategoryProfiles] = useState([]);
  const [catManagerOpen, setCatManagerOpen] = useState(false);

  // ---- tweaks ----
  const [tweaks, setTweaks] = useState(() => ({ ...DEFAULTS }));
  const [tweaksOpen, setTweaksOpen] = useState(false);

  // ==========================================================
  // Apply tweaks to DOM
  // ==========================================================
  useEffect(() => {
    const html = document.documentElement;
    html.setAttribute("data-skin", tweaks.skin);
    html.setAttribute("data-theme", tweaks.theme);
    document.body.setAttribute("data-density", tweaks.density);
    html.style.setProperty("--accent", ACCENT_COLORS[tweaks.accent] || ACCENT_COLORS.ochre);
    html.style.setProperty("--type-scale", tweaks.typeScale + "px");
    html.setAttribute("data-serif-heads", tweaks.serifHeads ? "true" : "false");
  }, [tweaks]);

  // Keyboard shortcut: T opens/closes tweaks
  useEffect(() => {
    const handler = (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key === "t" || e.key === "T") setTweaksOpen((o) => !o);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // ==========================================================
  // Boot + status poll
  // ==========================================================
  const bootCheck = useCallback(async () => {
    try {
      const d = await apiJSON("/api/status");
      setConnected(!!d.connected);
      setUser(d.user || null);
      setHasAI(!!d.has_ai);
      setHasEmb(!!d.has_embeddings);
      setBooted(true);
    } catch (e) {
      setBooted(true);
      setConnected(false);
      showBanner("error", "連線失敗：" + (e.message || "unknown"));
    }
  }, [showBanner]);

  useEffect(() => { bootCheck(); }, [bootCheck]);

  useEffect(() => {
    if (!connected) return;
    const t = setInterval(() => {
      apiJSON("/api/status").then((d) => {
        setConnected(!!d.connected);
        setUser(d.user || null);
        setHasAI(!!d.has_ai);
      }).catch(() => {});
    }, 60000);
    return () => clearInterval(t);
  }, [connected]);

  // ==========================================================
  // Load data once logged in
  // ==========================================================
  const loadDialogs = useCallback(async () => {
    if (!connected) return;
    setChatsLoading(true);
    try {
      const d = await apiJSON("/api/dialogs?limit=200");
      setChats(d.dialogs || []);
      setLastSyncTs(Date.now());
    } catch (e) {
      showBanner("error", "載入對話失敗：" + (e.message || ""));
    } finally {
      setChatsLoading(false);
    }
  }, [connected, showBanner]);

  const loadWatchlist = useCallback(async () => {
    try {
      const d = await apiJSON("/api/watchlist");
      setWatchlist(d.keywords || []);
    } catch (e) { /* silent */ }
  }, []);

  const loadNotes = useCallback(async () => {
    try {
      const d = await apiJSON("/api/memory/notes?days=60");
      setNotes(d.notes || []);
    } catch (e) { /* silent */ }
  }, []);

  const loadTimeline = useCallback(async () => {
    try {
      const d = await apiJSON("/api/memory/timeline?days=60");
      setTimeline(d.timeline || []);
    } catch (e) { /* silent */ }
  }, []);

  const loadCategories = useCallback(async () => {
    try {
      const d = await apiJSON("/api/chat_categories");
      setCategories(d.categories || []);
      if (Array.isArray(d.profiles) && d.profiles.length) {
        setCategoryProfiles(d.profiles);
      }
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => {
    if (!connected) return;
    loadDialogs();
    loadWatchlist();
    loadNotes();
    loadTimeline();
    loadCategories();
    loadProfiles();
  }, [connected, loadDialogs, loadWatchlist, loadNotes, loadTimeline, loadCategories, loadProfiles]);

  // ---- category CRUD ----
  const assignCategory = useCallback(async (chatId, categoryId) => {
    try {
      await apiJSON("/api/chat_category_map", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, category_id: categoryId }),
      });
      loadDialogs();
      loadCategories();
    } catch (e) {
      showBanner("error", "指派分類失敗：" + (e.message || ""));
      throw e;
    }
  }, [loadDialogs, loadCategories, showBanner]);

  const createCategory = useCallback(async (name, color, promptProfile) => {
    try {
      const body = { name, color };
      if (promptProfile) body.prompt_profile = promptProfile;
      await apiJSON("/api/chat_categories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      loadCategories();
    } catch (e) {
      showBanner("error", e.message || "新增失敗");
    }
  }, [loadCategories, showBanner]);

  const updateCategory = useCallback(async (id, patch) => {
    try {
      await apiJSON(`/api/chat_categories/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      loadCategories();
      loadDialogs();
    } catch (e) {
      showBanner("error", e.message || "更新失敗");
    }
  }, [loadCategories, loadDialogs, showBanner]);

  const deleteCategory = useCallback(async (id) => {
    try {
      await apiJSON(`/api/chat_categories/${id}`, { method: "DELETE" });
      loadCategories();
      loadDialogs();
    } catch (e) {
      showBanner("error", e.message || "刪除失敗");
    }
  }, [loadCategories, loadDialogs, showBanner]);

  // ==========================================================
  // Chat selection → fetch topics (if forum) + messages
  // ==========================================================
  const fetchTopicsFor = useCallback(async (chat) => {
    if (!chat || !chat.is_forum) { setTopics([]); return; }
    try {
      const d = await apiJSON("/api/topics?chat=" + encodeURIComponent(chat.id));
      setTopics(d.topics || []);
    } catch (e) {
      setTopics([]);
      showBanner("error", "topic 擷取失敗：" + (e.message || ""));
    }
  }, [showBanner]);

  const fetchMessagesFor = useCallback(async (chat, hrs, topicIds) => {
    if (!chat) return;
    setMsgLoading(true);
    try {
      let url = `/api/messages?chat=${encodeURIComponent(chat.id)}&hours=${hrs}`;
      if (topicIds && topicIds.size) {
        url += `&topics=${Array.from(topicIds).join(",")}`;
      }
      const d = await apiJSON(url);
      setMessages(d.messages || []);
    } catch (e) {
      if (e.status === 409) return;  // superseded by newer request — keep current view
      setMessages([]);
      showBanner("error", "載入訊息失敗：" + (e.message || ""));
    } finally {
      setMsgLoading(false);
    }
  }, [showBanner]);

  const selectChat = useCallback(async (chat) => {
    setSelectedChat(chat);
    setSelectedTopics(new Set());
    setSummary(""); setEvents([]); setSaved(false); setProfileJson(null);
    setStreamed(""); streamedRef.current = "";
    setBriefMode("single");
    setView("brief");
    await fetchTopicsFor(chat);
  }, [fetchTopicsFor]);

  // Re-fetch messages when selection / hours / selected topics change
  useEffect(() => {
    if (!selectedChat) return;
    fetchMessagesFor(selectedChat, hours, selectedTopics);
  }, [selectedChat, hours, selectedTopics, fetchMessagesFor]);

  // ==========================================================
  // SSE Summarize
  // ==========================================================
  const runSummarize = useCallback(async () => {
    if (!selectedChat) { showBanner("error", "請先選聊天室"); return; }
    if (!hasAI) { showBanner("error", "尚未啟用 AI（請設定 CLAUDE_API_KEY）"); return; }
    if (msgLoading) { showBanner("error", "訊息仍在載入中，請稍等"); return; }
    if (!messages.length) { showBanner("error", "沒有可總結的訊息"); return; }

    setStreaming(true);
    setStreamed(""); streamedRef.current = "";
    setSummary(""); setEvents([]); setSaved(false);
    setProgress(3); setStageTxt("準備中…");

    let controller = new AbortController();
    try {
      const r = await apiFetch("/api/summarize/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages,
          chat_name: selectedChat.name,
          chat_id: String(selectedChat.id),
          hours,
          save_to_memory: true,
          model,
        }),
        signal: controller.signal,
      });
      if (!r.ok) throw new Error("HTTP " + r.status);

      const reader = r.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop();

        for (const frame of frames) {
          if (!frame.startsWith("data:")) continue;
          const raw = frame.slice(5).trim();
          if (!raw) continue;
          let ev = null;
          try { ev = JSON.parse(raw); } catch { continue; }

          if (ev.type === "progress") {
            if (typeof ev.progress === "number") setProgress(ev.progress);
            if (ev.msg) setStageTxt(ev.msg);
          } else if (ev.type === "token") {
            streamedRef.current += ev.token || "";
            setStreamed(streamedRef.current);
            // smooth progress creep during streaming (after 65% kickoff)
            const creep = Math.min(86, 65 + Math.log10(streamedRef.current.length + 10) * 10);
            setProgress((p) => (p < creep ? creep : p));
          } else if (ev.type === "done") {
            setSummary(ev.summary || streamedRef.current || "");
            setEvents(ev.events || []);
            setSaved(!!ev.saved);
            setProfileJson(ev.profile_json || null);
            setProgress(100);
            setStageTxt("完成");
          } else if (ev.type === "error") {
            throw new Error(ev.error || "總結失敗");
          }
        }
      }
    } catch (e) {
      showBanner("error", e.message || "總結失敗");
    } finally {
      setStreaming(false);
      // refresh timeline to show new entry
      loadTimeline();
    }
  }, [selectedChat, messages, hours, model, hasAI, msgLoading, showBanner, loadTimeline]);

  // ==========================================================
  // Daily Brief aggregate (multi-chat, sequential SSE)
  // ==========================================================
  const runDailyBrief = useCallback(async () => {
    if (!hasAI) { showBanner("error", "尚未啟用 AI（請設定 CLAUDE_API_KEY）"); return; }
    if (!batchSet.size) { showBanner("error", "請先勾選對話"); return; }

    const chatIds = Array.from(batchSet);
    const init = chatIds.map((id) => {
      const c = chats.find((x) => String(x.id) === id);
      return {
        chatId: id, chatName: c?.name || id,
        status: "pending",
        summary: "", events: [], streamed: "",
        progress: 0, stageTxt: "",
        messageCount: 0, error: null,
        profileJson: null,
      };
    });
    briefItemsRef.current = init;
    setBriefItems(init);
    setBriefRunning(true);

    const patch = (idx, p) => {
      briefItemsRef.current = briefItemsRef.current.map((it, i) => i === idx ? { ...it, ...p } : it);
      setBriefItems(briefItemsRef.current);
    };

    for (let i = 0; i < chatIds.length; i++) {
      const chatId = chatIds[i];
      const chatName = briefItemsRef.current[i].chatName;
      patch(i, { status: "fetching", stageTxt: "擷取訊息…", progress: 2 });

      let msgs = [];
      try {
        const d = await apiJSON(`/api/messages?chat=${encodeURIComponent(chatId)}&hours=${batchHours}&ctx=brief`);
        msgs = d.messages || [];
        patch(i, { messageCount: msgs.length });
      } catch (e) {
        patch(i, { status: "error", error: "擷取失敗：" + (e.message || "") });
        continue;
      }

      if (!msgs.length) {
        patch(i, { status: "done", summary: "（這段時間沒有訊息）", events: [], progress: 100 });
        continue;
      }

      patch(i, { status: "running", stageTxt: "準備中…", progress: 5 });

      let streamedLocal = "";
      try {
        const r = await apiFetch("/api/summarize/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: msgs,
            chat_name: chatName,
            chat_id: chatId,
            hours: batchHours,
            save_to_memory: true,
            model,
          }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);

        const reader = r.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const frames = buf.split("\n\n");
          buf = frames.pop();

          for (const frame of frames) {
            if (!frame.startsWith("data:")) continue;
            const raw = frame.slice(5).trim();
            if (!raw) continue;
            let ev = null;
            try { ev = JSON.parse(raw); } catch { continue; }

            if (ev.type === "progress") {
              const cur = briefItemsRef.current[i];
              patch(i, {
                progress: typeof ev.progress === "number" ? ev.progress : cur.progress,
                stageTxt: ev.msg || cur.stageTxt,
              });
            } else if (ev.type === "token") {
              streamedLocal += ev.token || "";
              const creep = Math.min(86, 65 + Math.log10(streamedLocal.length + 10) * 10);
              const cur = briefItemsRef.current[i];
              patch(i, {
                streamed: streamedLocal,
                progress: creep > cur.progress ? creep : cur.progress,
              });
            } else if (ev.type === "done") {
              patch(i, {
                status: "done",
                summary: ev.summary || streamedLocal,
                events: ev.events || [],
                progress: 100,
                stageTxt: "完成",
                profileJson: ev.profile_json || null,
              });
            } else if (ev.type === "error") {
              throw new Error(ev.error || "總結失敗");
            }
          }
        }
      } catch (e) {
        patch(i, { status: "error", error: e.message || "失敗", progress: 0 });
      }
    }

    setBriefRunning(false);
    loadTimeline();
    const okCount = briefItemsRef.current.filter((x) => x.status === "done").length;
    const badCount = briefItemsRef.current.filter((x) => x.status === "error").length;
    showBanner(
      badCount > 0 ? "info" : "success",
      `Daily Brief 完成：${okCount} 成功${badCount > 0 ? ` · ${badCount} 失敗` : ""}`,
      4000
    );
  }, [batchSet, batchHours, chats, model, hasAI, showBanner, loadTimeline]);

  // ==========================================================
  // Batch export CSV
  // ==========================================================
  const toggleBatch = useCallback((id) => {
    if (id === "__clear") { setBatchSet(new Set()); return; }
    setBatchSet((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const batchExport = useCallback(async () => {
    if (!batchSet.size) return;
    setBatchExportBusy(true);
    try {
      const url = `/api/messages/export_csv?chats=${Array.from(batchSet).join(",")}&hours=${batchHours}`;
      const r = await apiFetch(url);
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
        throw new Error(msg);
      }
      const blob = await r.blob();
      // filename from Content-Disposition (RFC 5987 filename*=UTF-8'')
      const cd = r.headers.get("Content-Disposition") || "";
      let fname = "export.csv";
      const m5987 = cd.match(/filename\*=UTF-8''([^;]+)/i);
      const mPlain = cd.match(/filename="?([^";]+)"?/i);
      if (m5987) { try { fname = decodeURIComponent(m5987[1]); } catch { fname = m5987[1]; } }
      else if (mPlain) { fname = mPlain[1]; }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
      const count = r.headers.get("X-Export-Count") || "?";
      showBanner("success", `已匯出 ${count} 則訊息（${batchSet.size} 個聊天室）`, 4000);
    } catch (e) {
      showBanner("error", "匯出失敗：" + (e.message || ""));
    } finally {
      setBatchExportBusy(false);
    }
  }, [batchSet, batchHours, showBanner]);

  // ==========================================================
  // Memory actions
  // ==========================================================
  const openDay = useCallback(async (date, slot = "", opts = {}) => {
    const cleanSlot = (slot || "").trim();
    setActiveDate(date);
    setActiveSlot(cleanSlot);
    setDayData(null);
    setDigest("");
    setHighlightQuery(opts.highlight || "");
    setHighlightTarget(opts.target || null);
    setView("memory-day");
    try {
      const slotParam = cleanSlot ? "?slot=" + encodeURIComponent(cleanSlot) : "";
      const d = await apiJSON("/api/memory/day/" + encodeURIComponent(date) + slotParam);
      setDayData(d);
    } catch (e) {
      showBanner("error", "載入該日記錄失敗：" + (e.message || ""));
    }
  }, [showBanner]);

  useEffect(() => {
    const retryQueuedForActiveSlot = retryAutoSummaryQueued
      && retryAutoSummaryQueued.date === activeDate
      && retryAutoSummaryQueued.slot === activeSlot;
    if (
      view !== "memory-day"
      || !activeDate
      || !activeSlot
      || (dayData?.summary_run?.summary_status !== "running" && !retryQueuedForActiveSlot)
    ) {
      return;
    }
    const id = setInterval(async () => {
      try {
        const d = await apiJSON(
          "/api/memory/day/" + encodeURIComponent(activeDate)
          + "?slot=" + encodeURIComponent(activeSlot)
        );
        setDayData(d);
        loadTimeline();
      } catch {
        // Keep the current view stable; the normal status poll will surface
        // broader connectivity problems elsewhere in the app.
      }
    }, retryQueuedForActiveSlot ? 5000 : 15000);
    return () => clearInterval(id);
  }, [
    view,
    activeDate,
    activeSlot,
    dayData?.summary_run?.summary_status,
    retryAutoSummaryQueued,
    loadTimeline,
  ]);

  useEffect(() => {
    if (!retryAutoSummaryQueued || !dayData?.summary_run) return;
    if (retryAutoSummaryQueued.date !== activeDate || retryAutoSummaryQueued.slot !== activeSlot) return;
    const run = dayData.summary_run;
    const fallbackCount = run.fallback_chats || 0;
    if (fallbackCount === 0 && run.summary_status !== "running") {
      setRetryAutoSummaryQueued(null);
    }
  }, [activeDate, activeSlot, dayData?.summary_run, retryAutoSummaryQueued]);

  const retryAutoSummaryActive = retryAutoSummaryBusy || !!(
    retryAutoSummaryQueued
    && retryAutoSummaryQueued.date === activeDate
    && retryAutoSummaryQueued.slot === activeSlot
  ) || dayData?.summary_run?.retrying || dayData?.summary_run?.summary_status === "running";

  const generateDigest = useCallback(async () => {
    if (!activeDate) return;
    setDigestBusy(true);
    try {
      const d = await apiJSON("/api/memory/digest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: activeDate }),
      });
      setDigest(d.digest || "");
    } catch (e) {
      showBanner("error", "生成摘要失敗：" + (e.message || ""));
    } finally {
      setDigestBusy(false);
    }
  }, [activeDate, showBanner]);

  const retryAutoSummary = useCallback(async (opts = {}) => {
    const retryQueuedForActiveSlot = retryAutoSummaryQueued
      && retryAutoSummaryQueued.date === activeDate
      && retryAutoSummaryQueued.slot === activeSlot;
    if (!activeDate || !activeSlot || retryAutoSummaryBusy || retryQueuedForActiveSlot) return;
    setRetryAutoSummaryBusy(true);
    try {
      await apiJSON("/api/memory/auto_summary/retry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          date: activeDate,
          slot: activeSlot,
          retry_fallbacks: !!opts.retryFallbacks,
        }),
      });
      setRetryAutoSummaryQueued({
        date: activeDate,
        slot: activeSlot,
        retryFallbacks: !!opts.retryFallbacks,
        startedAt: Date.now(),
      });
      const d = await apiJSON(
        "/api/memory/day/" + encodeURIComponent(activeDate)
        + "?slot=" + encodeURIComponent(activeSlot)
      );
      setDayData(d);
      loadTimeline();
      showBanner("info", opts.retryFallbacks
        ? `已開始重跑 ${activeDate} ${activeSlot} 的 fallback summaries`
        : `已開始重試 ${activeDate} ${activeSlot} 未完成的 chats`);
    } catch (e) {
      showBanner("error", "重試失敗:" + (e.message || ""));
    } finally {
      setRetryAutoSummaryBusy(false);
    }
  }, [
    activeDate,
    activeSlot,
    retryAutoSummaryBusy,
    retryAutoSummaryQueued,
    loadTimeline,
    showBanner,
  ]);

  const askMemory = useCallback(async (question) => {
    setQaLoading(true);
    // optimistic slot
    setQaHistory((prev) => [...prev, { q: question, a: "…" }]);
    try {
      const d = await apiJSON("/api/memory/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      setQaHistory((prev) => {
        const next = prev.slice();
        next[next.length - 1] = { q: question, a: d.answer || "(no answer)" };
        return next;
      });
    } catch (e) {
      setQaHistory((prev) => {
        const next = prev.slice();
        next[next.length - 1] = { q: question, a: "失敗：" + (e.message || "") };
        return next;
      });
    } finally {
      setQaLoading(false);
    }
  }, []);

  const runSearch = useCallback(async () => {
    const q = searchQuery.trim();
    if (!q) { setSearchResult(null); return; }
    setSearchBusy(true);
    const isTicker = /^\$[A-Za-z0-9_]{1,15}$/.test(q);
    const isEvmCA = /^0x[a-fA-F0-9]{40}$/.test(q);
    const isSolCA = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(q);
    const useCoin = isTicker || isEvmCA || isSolCA;
    try {
      const url = useCoin
        ? "/api/coin/search?q=" + encodeURIComponent(q)
        : "/api/memory/search?q=" + encodeURIComponent(q);
      const d = await apiJSON(url);
      setSearchResult({ ...d, _kind: useCoin ? "coin" : "memory" });
    } catch (e) {
      setSearchResult({ summaries: [], events: [], notes: [], _kind: "memory" });
      showBanner("error", "搜尋失敗：" + (e.message || ""));
    } finally {
      setSearchBusy(false);
    }
  }, [searchQuery, showBanner]);

  const clearSearch = useCallback(() => {
    setSearchQuery("");
    setSearchResult(null);
    setHighlightQuery("");
    setHighlightTarget(null);
  }, []);

  const extractHolderCA = useCallback((value) => {
    const raw = String(value || "").trim().replace(/`/g, "");
    const evm = raw.match(/0x[a-fA-F0-9]{40}/);
    if (evm) return evm[0];
    const sol = raw.match(/[1-9A-HJ-NP-Za-km-z]{32,44}/);
    if (sol) return sol[0];
    return raw;
  }, []);

  const runHolderLookup = useCallback(async () => {
    const ca = extractHolderCA(holderQuery);
    if (!ca) {
      setHolderError("請貼上 EVM / Solana CA");
      setHolderResult(null);
      return;
    }
    setHolderBusy(true);
    setHolderError("");
    try {
      const d = await apiJSON(
        "/api/coin/holders?ca=" + encodeURIComponent(ca)
        + "&days=" + encodeURIComponent(holderDays)
      );
      setHolderQuery(ca);
      setHolderResult(d);
    } catch (e) {
      setHolderResult(null);
      setHolderError(e.message || "查詢失敗");
      showBanner("error", "持倉查詢失敗：" + (e.message || ""));
    } finally {
      setHolderBusy(false);
    }
  }, [extractHolderCA, holderQuery, holderDays, showBanner]);

  const exportMemory = useCallback(async () => {
    try {
      const r = await apiFetch("/api/memory/export");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename="?([^";]+)"?/i);
      const fname = m ? m[1] : `tg_memory_${new Date().toISOString().slice(0,10)}.json`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
      showBanner("success", "已匯出記憶庫", 3000);
    } catch (e) {
      showBanner("error", "匯出失敗：" + (e.message || ""));
    }
  }, [showBanner]);

  const importMemory = useCallback(async (file) => {
    if (!file) return;
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const d = await apiJSON("/api/memory/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const i = d.imported || {};
      showBanner(
        "success",
        `匯入：摘要 ${i.summaries||0} · 事件 ${i.events||0} · 筆記 ${i.notes||0}`,
        4000
      );
      loadTimeline();
      loadNotes();
    } catch (e) {
      showBanner("error", "匯入失敗：" + (e.message || ""));
    }
  }, [loadTimeline, loadNotes, showBanner]);

  // ==========================================================
  // Compare (GitHub Trending)
  // ==========================================================
  const loadGithubTrending = useCallback(async ({ force = false } = {}) => {
    setGithubTrendingBusy(true);
    setGithubTrendingError("");
    try {
      const qs = new URLSearchParams({
        since: githubSince,
        limit: "12",
      });
      if (githubLanguage) qs.set("language", githubLanguage);
      if (force) qs.set("_", String(Date.now()));
      const d = await apiJSON("/api/github/trending?" + qs.toString());
      setGithubTrending(d.repos || []);
    } catch (e) {
      setGithubTrendingError("GitHub Trending 暫時讀不到，稍後再試。");
      showBanner("error", "GitHub Trending 憭望?:" + (e.message || ""));
    } finally {
      setGithubTrendingBusy(false);
    }
  }, [githubSince, githubLanguage, showBanner]);

  // ==========================================================
  // Watchtower (entity radar)
  // ==========================================================
  const loadEntities = useCallback(async (windowDays) => {
    setEntitiesLoading(true);
    try {
      const w = windowDays ?? entityWindow;
      const d = await apiJSON("/api/watchtower/entities?days=" + w);
      setEntities(d.entities || []);
    } catch (e) {
      showBanner("error", "讀取 Watchtower 失敗:" + (e.message || ""));
      setEntities([]);
    } finally {
      setEntitiesLoading(false);
    }
  }, [entityWindow, showBanner]);

  // ==========================================================
  // Coin profiles
  // ==========================================================
  const loadProfiles = useCallback(async () => {
    try {
      const d = await apiJSON("/api/coin_profiles");
      setProfiles(d.profiles || []);
    } catch (e) {
      showBanner("error", "讀取 profiles 失敗:" + (e.message || ""));
    }
  }, [showBanner]);

  const createProfile = useCallback(async (symbolOrPayload) => {
    const payload = typeof symbolOrPayload === "string"
      ? { symbol: symbolOrPayload }
      : symbolOrPayload;
    try {
      const d = await apiJSON("/api/coin_profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (d.id) setSelectedProfileId(d.id);
      await loadProfiles();
      if (d.merged) showBanner("info", `已併入現有 $${d.profile?.symbol || payload.symbol} profile`);
      return d.profile;
    } catch (e) {
      showBanner("error", "新增失敗:" + (e.message || ""));
      return null;
    }
  }, [loadProfiles, showBanner]);

  const updateProfile = useCallback(async (id, payload) => {
    try {
      const d = await apiJSON("/api/coin_profiles/" + id, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      // Refresh list so meta (last_updated, status, narrative snippet) updates.
      await loadProfiles();
      return d.profile;
    } catch (e) {
      showBanner("error", "儲存失敗:" + (e.message || ""));
    }
  }, [loadProfiles, showBanner]);

  const deleteProfile = useCallback(async (id) => {
    try {
      await apiJSON("/api/coin_profiles/" + id, { method: "DELETE" });
      if (selectedProfileId === id) setSelectedProfileId(null);
      await loadProfiles();
    } catch (e) {
      showBanner("error", "刪除失敗:" + (e.message || ""));
    }
  }, [loadProfiles, selectedProfileId, showBanner]);

  const patchProfileDraftTask = useCallback((profileId, patch) => {
    if (!profileId) return;
    setProfileDraftTasks((prev) => {
      const current = prev[profileId] || {
        busy: false, stage: "", error: "", stream: "", lastResult: null,
      };
      const next = {
        ...prev,
        [profileId]: { ...current, ...patch },
      };
      profileDraftTasksRef.current = next;
      return next;
    });
  }, []);

  const dismissProfileDraftTask = useCallback((profileId) => {
    if (!profileId) return;
    setProfileDraftTasks((prev) => {
      const next = { ...prev };
      delete next[profileId];
      profileDraftTasksRef.current = next;
      return next;
    });
  }, []);

  const runProfileDraft = useCallback(async (profileId) => {
    if (!profileId) return;
    const existing = profileDraftTasksRef.current[profileId];
    if (existing?.busy) return;

    patchProfileDraftTask(profileId, {
      busy: true,
      stage: "Collecting profile context...",
      error: "",
      stream: "",
      lastResult: null,
    });

    let acc = "";
    let doneEvent = null;
    try {
      const r = await apiFetch(`/api/coin_profiles/${profileId}/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "sonnet", days: 30 }),
      });
      if (!r.ok || !r.body) {
        let msg = `HTTP ${r.status}`;
        try {
          const j = await r.json();
          msg = j.error || msg;
        } catch {}
        throw new Error(msg);
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
            patchProfileDraftTask(profileId, { stream: acc });
          } else if (ev.type === "progress") {
            if (ev.msg) patchProfileDraftTask(profileId, { stage: ev.msg });
          } else if (ev.type === "done") {
            doneEvent = ev;
          } else if (ev.type === "error") {
            throw new Error(ev.error || "draft failed");
          }
        }
      }

      if (!doneEvent) {
        throw new Error("draft stream ended before completion");
      }
      const written = doneEvent.sections_written || [];
      await loadProfiles();
      patchProfileDraftTask(profileId, {
        busy: false,
        stage: `Done. Wrote ${written.length} fields.`,
        error: "",
        stream: acc,
        lastResult: { sectionsWritten: written, ts: Date.now() },
      });
    } catch (e) {
      patchProfileDraftTask(profileId, {
        busy: false,
        stage: "",
        error: e.message || String(e),
      });
    }
  }, [loadProfiles, patchProfileDraftTask]);

  // Bootstrap a brand-new profile from a single pasted review (must contain
  // CA or $TICKER). All in-flight UI state (busy / stage / error / lastResult)
  // lives in `reviewBuild` at the App level so the user can switch views
  // mid-build and come back to see the live status or final result. The SSE
  // loop runs against the App's render closure — it does NOT die when the
  // ProfilesCol unmounts, so the build always finishes regardless of where
  // the user is. Returns a promise; ProfilesCol only awaits it to clear its
  // own textarea on success and surface synchronous errors.
  const createProfileFromNotes = useCallback(async (notes) => {
    setReviewBuild({
      busy: true, stage: "📦 解析筆記...", error: "", lastResult: null,
    });
    try {
      const r = await apiFetch("/api/coin_profiles/from_notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes, model: "sonnet" }),
      });
      if (!r.ok || !r.body) {
        let msg = `HTTP ${r.status}`;
        try {
          const j = await r.json();
          msg = j.error || msg;
        } catch {
          if (r.status === 404) {
            msg = "/api/coin_profiles/from_notes 不存在 — 請重啟 server.py(後端 Python 不會自動 reload)";
          }
        }
        throw new Error(msg);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let result = null;
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
          if (ev.type === "progress") {
            if (ev.msg) setReviewBuild((b) => ({ ...b, stage: ev.msg }));
          } else if (ev.type === "done") {
            result = ev;
          } else if (ev.type === "error") {
            throw new Error(ev.error || "from_notes failed");
          }
        }
      }
      if (!result || !result.profile) throw new Error("server 沒回 profile");
      await loadProfiles();
      setSelectedProfileId(result.profile.id);
      setReviewBuild({
        busy: false, stage: "", error: "",
        lastResult: {
          profileId: result.profile.id,
          symbol: result.profile.symbol,
          merged: !!result.merged,
          ts: Date.now(),
        },
      });
      return result;
    } catch (e) {
      setReviewBuild({
        busy: false, stage: "", error: e.message || String(e), lastResult: null,
      });
      throw e;
    }
  }, [loadProfiles]);

  // Manual dismiss for the lastResult / error banner — wiped on next build.
  const dismissReviewBuild = useCallback(() => {
    setReviewBuild((b) => ({ ...b, error: "", lastResult: null }));
  }, []);

  // ==========================================================
  // Notes actions
  // ==========================================================
  const addNote = useCallback(async (content, tags) => {
    try {
      await apiJSON("/api/memory/notes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, tags }),
      });
      loadNotes();
      loadTimeline();
    } catch (e) {
      showBanner("error", e.message || "儲存失敗");
    }
  }, [loadNotes, loadTimeline, showBanner]);

  const removeNote = useCallback(async (id) => {
    try {
      await apiJSON("/api/memory/notes?id=" + id, { method: "DELETE" });
      loadNotes();
      if (activeDate) openDay(activeDate, activeSlot);
    } catch (e) {
      showBanner("error", e.message || "刪除失敗");
    }
  }, [loadNotes, activeDate, activeSlot, openDay, showBanner]);

  const removeEvent = useCallback(async (id) => {
    try {
      await apiJSON("/api/memory/events?id=" + id, { method: "DELETE" });
      loadTimeline();
      if (activeDate) openDay(activeDate, activeSlot);
    } catch (e) {
      showBanner("error", e.message || "刪除失敗");
    }
  }, [loadTimeline, activeDate, activeSlot, openDay, showBanner]);

  const removeSummary = useCallback(async (id) => {
    try {
      await apiJSON("/api/memory/summaries?id=" + id, { method: "DELETE" });
      loadTimeline();
      if (activeDate) openDay(activeDate, activeSlot);
    } catch (e) {
      showBanner("error", e.message || "刪除失敗");
    }
  }, [loadTimeline, activeDate, activeSlot, openDay, showBanner]);

  // Bulk delete: nuke a whole day's daily_summaries (and their cascading
  // auto-extracted events / sentiment / embeddings). Manual events and
  // notes are kept — those are user-curated content the user wrote.
  const removeDay = useCallback(async (date, slot = "") => {
    if (!date) return;
    const cleanSlot = (slot || "").trim();
    if (!window.confirm(
      `刪除 ${date} 的所有 daily summary?\n\n` +
      "會一併清除自動抽取的 events / sentiment / embedding。\n" +
      "手動加的 events、notes 不會被刪。\n\n此動作無法復原。"
    )) return;
    try {
      const slotParam = cleanSlot ? "&slot=" + encodeURIComponent(cleanSlot) : "";
      const r = await apiJSON("/api/memory/summaries?date=" + encodeURIComponent(date) + slotParam, { method: "DELETE" });
      showBanner("info", `已刪除 ${date}:${r.count || 0} 筆 summary`);
      if (activeDate === date && (activeSlot || "") === cleanSlot) {
        setActiveDate(null);
        setActiveSlot("");
        setDayData(null);
        setView("memory");
      }
      loadTimeline();
    } catch (e) {
      showBanner("error", "刪除失敗:" + (e.message || ""));
    }
  }, [activeDate, activeSlot, loadTimeline, showBanner]);

  // ==========================================================
  // Derived
  // ==========================================================
  const alertCount = useMemo(
    () => messages.filter((m) => m.alerts && m.alerts.length).length,
    [messages]
  );

  const modelLabel = model === "opus" ? "Opus" : "Sonnet";

  const selectedEntity = useMemo(() => {
    if (!selectedEntityKey) return null;
    return entities.find((e) => `${e.kind}:${e.value}` === selectedEntityKey) || null;
  }, [entities, selectedEntityKey]);

  const selectedProfile = useMemo(
    () => profiles.find((p) => p.id === selectedProfileId) || null,
    [profiles, selectedProfileId]
  );

  useEffect(() => {
    if (view !== "compare") return;
    loadGithubTrending();
  }, [view, githubSince, githubLanguage, loadGithubTrending]);

  // Refresh entities when entering Watchtower or changing window — server
  // computes the harvest fresh each time (cheap, scans recent summaries only).
  useEffect(() => {
    if (view !== "watchtower") return;
    loadEntities(entityWindow);
  }, [view, entityWindow, loadEntities]);

  useEffect(() => {
    if (view !== "watchtower") return;
    if (!entities.length) { setSelectedEntityKey(null); return; }
    if (!selectedEntityKey || !entities.some((e) => `${e.kind}:${e.value}` === selectedEntityKey)) {
      const first = entities[0];
      setSelectedEntityKey(`${first.kind}:${first.value}`);
    }
  }, [view, entities, selectedEntityKey]);

  // Load profiles on entering the view (or once on connect — see boot effect).
  useEffect(() => {
    if (view !== "profiles") return;
    if (profiles.length === 0) loadProfiles();
  }, [view, profiles.length, loadProfiles]);

  useEffect(() => {
    if (view !== "profiles") return;
    if (!profiles.length) { setSelectedProfileId(null); return; }
    if (!selectedProfileId || !profiles.some((p) => p.id === selectedProfileId)) {
      setSelectedProfileId(profiles[0].id);
    }
  }, [view, profiles, selectedProfileId]);

  // Promote an entity (from Watchtower) into a coin profile — pre-populates
  // symbol + chain + ca where known, then jumps to the new profile in the
  // Coin Profiles view.
  const promoteEntityToProfile = useCallback(async (entity) => {
    const payload = { symbol: entity.value };
    if (entity.kind === "ca") {
      payload.ca = entity.value;
      // Heuristic: 0x prefix → EVM (default to base), base58 → solana.
      payload.symbol = entity.value.slice(0, 6).toUpperCase();
      payload.chain = entity.value.startsWith("0x") ? "base" : "solana";
    }
    const created = await createProfile(payload);
    if (created) {
      setView("profiles");
      setSelectedProfileId(created.id);
      // Refresh entities so the ⭐ has-profile flag updates.
      loadEntities(entityWindow);
    }
  }, [createProfile, loadEntities, entityWindow]);

  const openProfileById = useCallback((id) => {
    setView("profiles");
    setSelectedProfileId(id);
    if (profiles.length === 0) loadProfiles();
  }, [profiles.length, loadProfiles]);

  // ==========================================================
  // Middle column render
  // ==========================================================
  let midCol;
  if (view === "memory" || view === "memory-day") {
    midCol = (
      <MemoryCol
        timeline={timeline}
        activeDate={activeDate}
        activeSlot={activeSlot}
        onOpenDay={openDay}
        onDeleteDay={removeDay}
        qaHistory={qaHistory}
        onAsk={askMemory}
        qaLoading={qaLoading}
        onSearch={runSearch}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        searchResult={searchResult}
        searchBusy={searchBusy}
        onClearSearch={clearSearch}
        onExport={exportMemory}
        onImport={importMemory}
        hasAI={hasAI}
      />
    );
  } else if (view === "watchtower") {
    midCol = (
      <WatchtowerCol
        entities={entities}
        loading={entitiesLoading}
        windowDays={entityWindow}
        setWindowDays={setEntityWindow}
        kindFilter={entityKindFilter}
        setKindFilter={setEntityKindFilter}
        search={entitySearch}
        setSearch={setEntitySearch}
        activeKey={selectedEntityKey}
        onSelect={(e) => setSelectedEntityKey(`${e.kind}:${e.value}`)}
      />
    );
  } else if (view === "compare") {
    midCol = <CompareLeaderboardCol />;
  } else if (view === "holders") {
    midCol = (
      <SmartHoldersCol
        query={holderQuery}
        setQuery={setHolderQuery}
        days={holderDays}
        setDays={setHolderDays}
        result={holderResult}
        busy={holderBusy}
        onRun={runHolderLookup}
      />
    );
  } else if (view === "profiles") {
    midCol = (
      <ProfilesCol
        profiles={profiles}
        activeId={selectedProfileId}
        onSelect={(p) => setSelectedProfileId(p.id)}
        onCreate={(symbol) => createProfile(symbol)}
        onCreateFromNotes={createProfileFromNotes}
        reviewBuild={reviewBuild}
        onDismissReviewBuild={dismissReviewBuild}
        statusFilter={profilesStatus}
        setStatusFilter={setProfilesStatus}
        search={profilesSearch}
        setSearch={setProfilesSearch}
      />
    );
  } else {
    const inBriefAggregate = view === "brief" && briefMode === "aggregate";
    midCol = (
      <Inbox
        chats={chats}
        loading={chatsLoading}
        lastSyncTs={lastSyncTs}
        onRefresh={loadDialogs}
        selectedChat={selectedChat?.id}
        onSelect={selectChat}
        batchMode={batchMode}
        setBatchMode={(v) => { setBatchMode(v); if (!v) setBatchSet(new Set()); }}
        batchSet={batchSet}
        onBatch={toggleBatch}
        onBatchExport={inBriefAggregate ? null : batchExport}
        batchHours={batchHours}
        setBatchHours={setBatchHours}
        batchExportBusy={batchExportBusy}
        onRunBrief={inBriefAggregate ? runDailyBrief : null}
        briefRunning={briefRunning}
        categories={categories}
        profiles={categoryProfiles}
        onAssignCategory={assignCategory}
        onManageCategories={() => setCatManagerOpen(true)}
      />
    );
  }

  // ==========================================================
  // Stage render
  // ==========================================================
  let stage;
  if (!booted) {
    stage = <Empty mark="•—" title="Waking up" desc="正在連接 Telegram…" />;
  } else if (!connected) {
    stage = <LoginView onLogin={bootCheck} onError={(m) => showBanner("error", m || "登入錯誤")} />;
  } else if (view === "memory-day") {
    stage = (
      <div className="stage-body">
        <DayView
          date={activeDate}
          slot={activeSlot}
          data={dayData}
          digest={digest}
          digestBusy={digestBusy}
          onGenerateDigest={generateDigest}
          onRetryAutoSummary={retryAutoSummary}
          retryAutoSummaryBusy={retryAutoSummaryActive}
          onDeleteSummary={removeSummary}
          onDeleteEvent={removeEvent}
          onDeleteNote={removeNote}
          hasAI={hasAI}
          highlightQuery={highlightQuery}
          highlightTarget={highlightTarget}
        />
      </div>
    );
  } else if (view === "memory") {
    stage = (
      <div className="stage-body">
        <StageHead title="Memory" meta="Archive of daily dispatches" />
        <Empty mark="⏳" title="Open a day" desc="從中間欄點任一日期，或用上方搜尋框查詢關鍵字。" />
      </div>
    );
  } else if (view === "watchtower") {
    stage = (
      <div className="stage-body">
        <StageHead
          title="Watchtower"
          meta={
            entitiesLoading
              ? "scanning…"
              : `${entities.length} entities · ${entityWindow}d window`
          }
        />
        <WatchtowerStage
          entity={selectedEntity}
          windowDays={entityWindow}
          onCreateProfile={promoteEntityToProfile}
          onOpenProfile={openProfileById}
          onOpenDay={openDay}
          onBriefGenerated={() => loadEntities(entityWindow)}
        />
      </div>
    );
  } else if (view === "compare") {
    stage = (
      <CompareTrendingStage
        repos={githubTrending}
        loading={githubTrendingBusy}
        error={githubTrendingError}
        since={githubSince}
        setSince={setGithubSince}
        language={githubLanguage}
        setLanguage={setGithubLanguage}
        onRefresh={() => loadGithubTrending({ force: true })}
      />
    );
  } else if (view === "holders") {
    stage = (
      <SmartHoldersStage
        query={holderQuery}
        result={holderResult}
        busy={holderBusy}
        error={holderError}
        onOpenDay={openDay}
      />
    );
  } else if (view === "profiles") {
    stage = (
      <div className="stage-body">
        <StageHead title="Coin Profiles" meta={`${profiles.length} dossiers on file`} />
        <ProfileStage
          profile={selectedProfile}
          onUpdate={updateProfile}
          onDelete={deleteProfile}
          onAfterDraft={loadProfiles}
          draftTask={selectedProfile ? profileDraftTasks[selectedProfile.id] : null}
          onRunAIDraft={runProfileDraft}
          onDismissDraft={dismissProfileDraftTask}
          onDistillRulesDone={(candidates, sym, pid) => {
            setRulesPanelDraft(candidates);
            setRulesPanelSrcSym(sym || null);
            setRulesPanelSrcId(pid || null);
            setRulesPanelOpen(true);
          }}
        />
      </div>
    );
  } else if (view === "brief" && briefMode === "aggregate") {
    const hasItems = briefItems.length > 0;
    stage = (
      <div className="stage-body">
        <StageHead
          title="Daily Brief"
          meta={
            hasItems
              ? `${briefItems.length} chats · ${batchHours}h window`
              : batchSet.size === 0
                ? "勾選對話 → 一鍵聚合摘要"
                : `已勾選 ${batchSet.size} 個對話`
          }
          extra={
            <TimeControl
              hours={batchHours}
              setHours={(v) => { if (!briefRunning) setBatchHours(v); }}
            />
          }
          right={
            <div style={{ display: "flex", gap: 4 }}>
              {[["sonnet","Sonnet"],["opus","Opus"]].map(([k,l]) => (
                <button
                  key={k}
                  className={cls("btn btn-sm", model === k && "btn-accent")}
                  onClick={() => setModel(k)}
                  disabled={briefRunning}
                >{l}</button>
              ))}
            </div>
          }
        />
        {hasItems ? (
          <BriefAggregate
            items={briefItems}
            running={briefRunning}
            hours={batchHours}
            modelLabel={model === "opus" ? "Opus" : "Sonnet"}
            onClear={() => { setBriefItems([]); briefItemsRef.current = []; }}
          />
        ) : (
          <Empty
            mark="◉"
            title="聚合今日 Brief"
            desc={
              batchSet.size === 0
                ? "從左側 Inbox 勾選多個對話（每列右邊的方塊），選好時間窗後按「✧ Run Brief」。每個對話會依序跑 AI 摘要並存入 Memory。"
                : `已勾選 ${batchSet.size} 個對話 · 按左下「✧ Run Brief」開始。`
            }
          />
        )}
      </div>
    );
  } else {
    // inbox or brief single-chat
    if (!selectedChat) {
      const isInbox = view === "inbox";
      stage = (
        <div className="stage-body">
          <StageHead
            title={isInbox ? "Inbox" : "Daily Brief"}
            meta={isInbox ? "Browse your channels" : "Pick a channel to begin"}
          />
          <Empty
            mark={isInbox ? "◧" : "◉"}
            title="選一個聊天室"
            desc={
              isInbox
                ? "從左側列表點任一頻道或群組，這裡會顯示最近訊息與可選的 AI 摘要。"
                : "切到 Daily Brief 後勾選多個對話，可一鍵聚合摘要；或從 Inbox 點單一頻道只看/總結那一個。"
            }
          />
        </div>
      );
    } else {
      const fetchInfo = `${messages.length} msgs · ${hours}h window${selectedChat.is_forum && selectedTopics.size ? ` · ${selectedTopics.size} topics` : ""}`;
      stage = (
        <div className="stage-body">
          <StageHead
            title={selectedChat.name}
            meta={fetchInfo}
            extra={<TimeControl hours={hours} setHours={setHours} />}
            right={
              <BriefHeadRight
                model={model}
                setModel={setModel}
                onRun={runSummarize}
                running={streaming}
                disabled={!hasAI || msgLoading || !messages.length}
              />
            }
          />

          {selectedChat.is_forum && topics.length > 0 && (
            <div className="topics-row">
              <button
                className={cls("topic-pill", selectedTopics.size === 0 && "active")}
                onClick={() => setSelectedTopics(new Set())}
              >All topics</button>
              {topics.map((t) => {
                const active = selectedTopics.has(t.id);
                return (
                  <button
                    key={t.id}
                    className={cls("topic-pill", active && "active")}
                    onClick={() => {
                      const next = new Set(selectedTopics);
                      if (active) next.delete(t.id); else next.add(t.id);
                      setSelectedTopics(next);
                    }}
                    title={t.title}
                  >
                    {t.title}
                    {t.unread > 0 && (
                      <span style={{
                        marginLeft: 6, fontFamily: "var(--mono)", fontSize: 9,
                        background: "var(--accent)", color: "var(--paper)",
                        padding: "1px 5px", borderRadius: 8, fontWeight: 600,
                      }}>{t.unread > 99 ? "99+" : t.unread}</span>
                    )}
                  </button>
                );
              })}
            </div>
          )}

          {(summary || streaming) && (
            <Dispatch
              chatName={selectedChat.name}
              hours={hours}
              summary={summary}
              events={events}
              saved={saved}
              streaming={streaming}
              streamed={streamed}
              progress={progress}
              stageTxt={stageTxt}
              modelLabel={modelLabel}
              profileJson={profileJson}
              messageCount={messages.length}
            />
          )}

          {msgLoading ? (
            <div style={{ padding: "40px 0", textAlign: "center", color: "var(--ink-3)" }}>
              <span className="spinner" /> 載入訊息…
            </div>
          ) : (
            <MessagesList messages={messages} watchlist={watchlist} topicsById={topicsById} />
          )}
        </div>
      );
    }
  }

  // ==========================================================
  // Render
  // ==========================================================
  const shellStyle = connected ? null : { gridTemplateColumns: "64px 1fr" };

  return (
    <div className="shell" style={shellStyle}>
      <Rail
        view={view}
        setView={(v) => {
          setView(v);
          if (v === "memory") { loadTimeline(); setActiveDate(null); setActiveSlot(""); setDayData(null); setHighlightQuery(""); setHighlightTarget(null); }
          if (v === "brief") {
            setBriefMode("aggregate");
            setBatchMode(true);
          } else {
            setBriefMode("single");
            setBatchMode(false);
            setBatchSet(new Set());
          }
        }}
        alertCount={alertCount}
        onAvatar={() => setTweaksOpen((o) => !o)}
        onOpenRules={() => {
          setRulesPanelDraft(null);
          setRulesPanelSrcSym(null);
          setRulesPanelSrcId(null);
          setRulesPanelOpen(true);
        }}
        user={user}
      />

      {connected && midCol}

      <main className="stage">
        {stage}
      </main>

      <Banner banner={banner} />
      <TweaksPanel
        tweaks={tweaks}
        setTweaks={setTweaks}
        open={tweaksOpen}
        onClose={() => setTweaksOpen(false)}
      />
      <CategoryManager
        open={catManagerOpen}
        onClose={() => setCatManagerOpen(false)}
        categories={categories}
        chats={chats}
        profiles={categoryProfiles}
        onCreate={createCategory}
        onUpdate={updateCategory}
        onDelete={deleteCategory}
      />
      <RulesPanel
        open={rulesPanelOpen}
        onClose={() => {
          setRulesPanelOpen(false);
          setRulesPanelDraft(null);
          setRulesPanelSrcSym(null);
          setRulesPanelSrcId(null);
        }}
        initialDraft={rulesPanelDraft}
        sourceProfileSymbol={rulesPanelSrcSym}
        sourceProfileId={rulesPanelSrcId}
      />
    </div>
  );
}

// ---------- mount ----------
Object.assign(window, { App });
ReactDOM.createRoot(document.getElementById("root")).render(<App />);
