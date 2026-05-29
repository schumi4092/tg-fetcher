/* Telegraph — shared utilities, Banner, Rail, LoginView, Inbox (live API) */
const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ---------- utilities ----------
const API = "";
const esc = (s) => (s == null ? "" : String(s));
const cls = (...a) => a.filter(Boolean).join(" ");

function apiToken() {
  const m = document.cookie.match(/(?:^|;\s*)tg_fetcher_api_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function withApiToken(opts = {}) {
  const headers = new Headers(opts.headers || {});
  const token = apiToken();
  if (token) headers.set("X-TG-Fetcher-Token", token);
  return { ...opts, headers };
}

function apiFetch(url, opts = {}) {
  return fetch(API + url, withApiToken(opts));
}

async function refreshApiTokenCookie() {
  try {
    await fetch(API + "/", { cache: "no-store" });
  } catch {}
}

async function apiJSON(url, opts) {
  let r = await apiFetch(url, opts);
  if (r.status === 403) {
    await refreshApiTokenCookie();
    r = await apiFetch(url, opts);
  }
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) {
    const msg = (data && data.error) || `HTTP ${r.status}`;
    const err = new Error(msg);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data || {};
}

function highlightKw(text, keywords) {
  if (!keywords || !keywords.length) return text;
  let parts = [text];
  keywords.forEach((kw) => {
    if (!kw) return;
    const safe = kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`(${safe})`, "gi");
    for (let i = 0; i < parts.length; i++) {
      if (typeof parts[i] !== "string") continue;
      const split = parts[i].split(re);
      if (split.length > 1) {
        parts.splice(
          i, 1,
          ...split.map((p, idx) => idx % 2
            ? <span key={`${kw}-${i}-${idx}`} className="kw-hit">{p}</span>
            : p)
        );
        i += split.length - 1;
      }
    }
  });
  return parts;
}

function formatDate(dateStr) {
  const d = new Date(dateStr);
  const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  return { day: d.getDate(), month: months[d.getMonth()], year: d.getFullYear() };
}

function formatTime(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return "";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

function sinceAgo(tsOrStr) {
  try {
    const d = typeof tsOrStr === "number" ? new Date(tsOrStr) : new Date(tsOrStr);
    const diff = Date.now() - d.getTime();
    if (isNaN(diff)) return "";
    if (diff < 60 * 1000) return "剛剛";
    if (diff < 3600 * 1000) return Math.floor(diff / 60000) + " 分前";
    if (diff < 86400 * 1000) return Math.floor(diff / 3600000) + " 小時前";
    return Math.floor(diff / 86400000) + " 天前";
  } catch { return ""; }
}

const MEDIA_LABEL = {
  photo: "📷 圖片", video: "🎬 影片", document: "📎 檔案",
  voice: "🎤 語音", audio: "🎵 音訊", sticker: "🏷 貼圖",
  location: "📍 位置", poll: "📊 投票", gif: "🎞 GIF",
  video_note: "⭕ 圓形影片", contact: "👤 聯絡人",
  webpage: "🔗 連結", other: "📦 其他",
};

function mediaLabel(m) { return m ? (MEDIA_LABEL[m] || m) : ""; }

// ---------- Banner ----------
function Banner({ banner }) {
  if (!banner) return null;
  const klass = "banner-toast " + (banner.type || "info");
  return (
    <div className={klass}>
      {banner.type === "loading" && <span className="spinner" />}
      <span>{banner.text}</span>
    </div>
  );
}

// ---------- Rail ----------
function Rail({ view, setView, alertCount, onAvatar, onOpenRules, user }) {
  const items = [
    { id: "inbox", icon: "◧", label: "Inbox" },
    { id: "brief", icon: "◉", label: "Daily Brief" },
    { id: "memory", icon: "⏳", label: "Memory" },
    { id: "watchtower", icon: "◎", label: "Watchtower", badge: alertCount > 0 },
    { id: "compare", icon: "AI", label: "Compare", size: 12 },
    { id: "holders", icon: "CA", label: "CA Holders", size: 12 },
    { id: "profiles", icon: "◇", label: "Coin Profiles" },
  ];
  const initial = user?.name ? String(user.name).charAt(0).toUpperCase() : "·";
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
          <span style={{ fontFamily: "var(--mono)", fontSize: it.size || (it.id === "watchtower" ? 16 : 18), fontWeight: 600 }}>
            {it.icon}
          </span>
          {it.badge && <span className="dot" />}
        </button>
      ))}
      <div className="rail-spacer" />
      {onOpenRules && (
        <button
          className="rail-btn"
          aria-label="Trading rules"
          title="交易法則(浮層,任何頁面都可呼叫)"
          onClick={onOpenRules}
        >
          <span style={{ fontFamily: "var(--mono)", fontSize: 18, fontWeight: 600 }}>⚖</span>
        </button>
      )}
      <button className="rail-btn" aria-label="Settings" title="Settings">
        <span style={{ fontFamily: "var(--mono)", fontSize: 18 }}>⚙</span>
      </button>
      <div className="rail-avatar" title={user ? `${user.name} ${user.username ? "@"+user.username : ""}` : "Account"} onClick={onAvatar}>{initial}</div>
    </nav>
  );
}

// ---------- Login (real API) ----------
function LoginView({ onLogin, onError }) {
  const [phase, setPhase] = useState("phone");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [pw, setPw] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setErr("");
    setLoading(true);
    try {
      if (phase === "phone") {
        if (!phone.trim()) { setErr("請輸入手機號碼"); setLoading(false); return; }
        await apiJSON("/api/login/send_code", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone: phone.trim() }),
        });
        setPhase("code");
      } else if (phase === "code") {
        if (!code.trim()) { setErr("請輸入驗證碼"); setLoading(false); return; }
        const d = await apiJSON("/api/login/verify_code", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: code.trim() }),
        });
        if (d.status === "need_password") {
          setPhase("pw");
        } else if (d.status === "logged_in") {
          await onLogin();
        }
      } else if (phase === "pw") {
        await apiJSON("/api/login/verify_password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: pw }),
        });
        await onLogin();
      }
    } catch (e) {
      setErr(e.message || "錯誤");
      onError && onError(e.message);
    }
    setLoading(false);
  };

  const onKey = (e) => { if (e.key === "Enter") submit(); };

  return (
    <div className="stage-body" style={{ maxWidth: "unset" }}>
      <div className="login-card">
        <div className="login-eyebrow">Telegraph — Sign in to the wire</div>
        <h1 className="login-title">Fetch <em>signal</em> from your channels.</h1>
        <p className="login-sub">Telegraph connects to your Telegram account, fetches channel messages, summarises them into daily dispatches, and builds a searchable memory. Session stays on this device.</p>

        <div className="login-step">
          <span className="num">{phase === "phone" ? 1 : phase === "code" ? 2 : 3}</span>
          <span>{phase === "phone" ? "Phone number" : phase === "code" ? "Verification code" : "Two-step password"}</span>
        </div>

        {phase === "phone" && (
          <div>
            <label className="label">Phone (with country code)</label>
            <input className="input mono" value={phone} onChange={(e)=>setPhone(e.target.value)} onKeyDown={onKey} placeholder="+886 9xx xxx xxx" autoFocus />
          </div>
        )}
        {phase === "code" && (
          <div>
            <label className="label">Enter the code sent to {phone}</label>
            <input className="input mono" value={code} onChange={(e)=>setCode(e.target.value)} onKeyDown={onKey} placeholder="12345" autoFocus />
          </div>
        )}
        {phase === "pw" && (
          <div>
            <label className="label">Cloud password (two-step)</label>
            <input className="input mono" type="password" value={pw} onChange={(e)=>setPw(e.target.value)} onKeyDown={onKey} placeholder="••••••••" autoFocus />
          </div>
        )}

        {err && <div style={{ marginTop: 12, color: "var(--alert, #b04226)", fontSize: 12 }}>{err}</div>}

        <div style={{ marginTop: 20, display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={submit} disabled={loading} style={{ flex: 1 }}>
            {loading ? <><span className="spinner" />sending…</> : phase === "phone" ? "Send code →" : phase === "code" ? "Verify →" : "Sign in →"}
          </button>
          {phase !== "phone" && (
            <button className="btn" onClick={() => { setPhase("phone"); setErr(""); }}>Back</button>
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

// ---------- Inbox (chat list, backed by /api/dialogs) ----------
// Short visual marker for each prompt profile — shows up as a small icon on
// the chat row so the user can tell at a glance which prompt will run.
const PROFILE_ICONS = {
  group_chat: "💬",
  broadcast: "📢",
  wallet_log: "💰",
};

// Profile types the user should think twice about when they're on a
// chat.type that doesn't typically match. Returns a warning string or null.
function profileTypeHint(chatType, profile) {
  if (!profile) return null;
  if (profile === "wallet_log" && chatType === "private") return "錢包紀錄通常用在 bot 頻道,這是私訊";
  if (profile === "broadcast" && chatType === "private") return "關注與推文通常用在聚合頻道,這是私訊";
  return null;
}

function Inbox({
  chats, loading, lastSyncTs, onRefresh,
  selectedChat, onSelect,
  batchMode, setBatchMode, batchSet, onBatch, onBatchExport,
  batchHours, setBatchHours, batchExportBusy,
  onRunBrief, briefRunning,
  categories, profiles, onAssignCategory, onManageCategories,
}) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("all"); // "all" | "unread" | "uncat" | `cat:<id>`
  const [addingToCat, setAddingToCat] = useState(null); // cat id whose add-picker is open
  const profileLabel = (v) => {
    const p = (profiles || []).find(x => x.value === v);
    return p ? p.label : v;
  };

  const visible = useMemo(() => {
    let list = chats || [];
    if (filter === "unread") list = list.filter(c => (c.unread || 0) > 0);
    else if (filter === "uncat") list = list.filter(c => !c.category_id);
    else if (filter.startsWith("cat:")) {
      const cid = Number(filter.slice(4));
      list = list.filter(c => c.category_id === cid);
    }
    if (q) {
      const lc = q.toLowerCase();
      list = list.filter(c =>
        (c.name || "").toLowerCase().includes(lc) ||
        (c.username || "").toLowerCase().includes(lc) ||
        String(c.id).includes(lc)
      );
    }
    return list;
  }, [chats, q, filter]);

  const unreadCnt = useMemo(() => chats.filter(c => (c.unread||0) > 0).length, [chats]);

  return (
    <div className="col">
      <div className="col-head">
        <div className="col-eyebrow">Section 01 · Inbox</div>
        <div className="col-title"><em>Channels</em> <span style={{ fontStyle: "italic" }}>&amp;</span> groups</div>
        <div className="col-sub">
          {chats.length} subscribed · {unreadCnt} with unread
          {lastSyncTs ? <> · synced <span className="mono">{sinceAgo(lastSyncTs)}</span></> : null}
          {" "}
          <button className="btn btn-sm btn-ghost" onClick={onRefresh} disabled={loading} style={{ padding: "2px 8px", marginLeft: 6 }}>
            {loading ? <span className="spinner" /> : "↻"}
          </button>
        </div>
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
        <button className={cls("filter-chip", filter==="all" && "active")} onClick={()=>setFilter("all")}>
          All
        </button>
        {(categories || []).map(cat => (
          <button
            key={cat.id}
            className={cls("filter-chip", filter===`cat:${cat.id}` && "active")}
            onClick={()=>setFilter(`cat:${cat.id}`)}
            style={filter===`cat:${cat.id}` ? { background: cat.color, borderColor: cat.color, color: "#fff" } : { borderColor: cat.color, color: cat.color }}
            title={`${cat.chat_count||0} chats`}
          >
            {cat.name}
          </button>
        ))}
        <button className="filter-chip" onClick={onManageCategories} title="管理分類">＋ Manage</button>
      </div>

      <div className="col-body">
        <div className="chat-list">
          {loading && !chats.length ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
              <span className="spinner" /> 載入中...
            </div>
          ) : visible.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--ink-3)", fontSize: 12 }}>
              {chats.length === 0 ? "還沒有對話 — 點 ↻ 同步" : "沒有符合條件的聊天室"}
            </div>
          ) : visible.map(c => {
            const idStr = String(c.id);
            const isActive = String(selectedChat) === idStr;
            const isBatch = batchSet.has(idStr);
            const initial = (c.name || "#").charAt(0).toUpperCase();
            const typeClass = c.type === "channel" ? "channel"
                            : c.type === "private" ? "private"
                            : "group";
            return (
              <div
                key={idStr}
                className={cls("chat-item", isActive && "active", isBatch && "batch-checked")}
                onClick={() => batchMode ? onBatch(idStr) : onSelect(c)}
              >
                {batchMode && <span className="batch-checkbox">{isBatch ? "✓" : ""}</span>}
                <div className={cls("chat-avatar", typeClass)}>{initial}</div>
                <div className="chat-body">
                  <div className="chat-row-top">
                    <div className="chat-name">
                      {c.name}
                      {c.is_forum && <span className="chat-forum-badge">Forum</span>}
                    </div>
                    {c.username && <span className="chat-time mono" style={{ fontSize: 10 }}>@{c.username}</span>}
                  </div>
                  <div className="chat-preview">
                    <span className="mono" style={{ fontSize: 10, color: "var(--ink-3)" }}>
                      {c.type}{c.is_forum ? " · forum" : ""} · ID {idStr}
                    </span>
                  </div>
                </div>
                {c.prompt_profile && !batchMode && (() => {
                  const hint = profileTypeHint(c.type, c.prompt_profile);
                  const icon = PROFILE_ICONS[c.prompt_profile] || "";
                  const tip = `${profileLabel(c.prompt_profile)}${hint ? ` — ⚠️ ${hint}` : ""}`;
                  return (
                    <span className={cls("chat-profile-badge", hint && "warn")} title={tip}>
                      {icon}
                    </span>
                  );
                })()}
                {(c.unread || 0) > 0 && !batchMode && (
                  <span className="chat-unread">{c.unread > 99 ? "99+" : c.unread}</span>
                )}
              </div>
            );
          })}
        </div>
        {batchMode && (
          <div className="batch-bar">
            <span className="batch-count">{batchSet.size} selected</span>
            {!onRunBrief && (
              <select value={batchHours} onChange={(e)=>setBatchHours(Number(e.target.value))}>
                <option value="4">4h</option>
                <option value="8">8h</option>
                <option value="24">24h</option>
                <option value="48">48h</option>
                <option value="72">72h</option>
                <option value="168">7d</option>
              </select>
            )}
            {onRunBrief && (
              <button
                className="btn btn-sm btn-accent"
                disabled={batchSet.size === 0 || briefRunning}
                onClick={onRunBrief}
              >
                {briefRunning ? <><span className="spinner" />running…</> : "✧ Run Brief"}
              </button>
            )}
            {onBatchExport && (
              <button
                className="btn btn-sm btn-primary"
                disabled={batchSet.size === 0 || batchExportBusy}
                onClick={onBatchExport}
              >
                {batchExportBusy ? "…匯出中" : "Export CSV"}
              </button>
            )}
            <button className="btn btn-sm btn-ghost" onClick={() => onBatch("__clear")}>Clear</button>
            <div className="summary">
              {batchSet.size === 0
                ? (onRunBrief ? "勾選多個對話 → 一鍵聚合摘要" : "點聊天室加入匯出清單")
                : `Queued: ${Array.from(batchSet).slice(0,3).map(id => (chats.find(c=>String(c.id)===id)?.name) || id).join(" · ")}${batchSet.size>3?` +${batchSet.size-3}`:""}`}
            </div>
          </div>
        )}
      </div>

      {!batchMode && filter.startsWith("cat:") && (() => {
        const activeId = Number(filter.slice(4));
        const activeCat = (categories || []).find(c => c.id === activeId);
        if (!activeCat) return null;
        return (
          <button
            className="cat-add-fab"
            onClick={() => setAddingToCat(activeId)}
            title={`新增聊天室到「${activeCat.name}」`}
            aria-label={`新增聊天室到 ${activeCat.name}`}
          >＋</button>
        );
      })()}

      {addingToCat != null && (() => {
        const cat = (categories || []).find(c => c.id === addingToCat);
        if (!cat) return null;
        return (
          <CategoryAddPicker
            category={cat}
            chats={chats || []}
            onToggle={(chatId, inCat) => onAssignCategory(chatId, inCat ? null : addingToCat)}
            onClose={() => setAddingToCat(null)}
          />
        );
      })()}
    </div>
  );
}

// ---------- Category Add-Members picker modal ----------
function CategoryAddPicker({ category, chats, onToggle, onClose }) {
  const [q, setQ] = useState("");
  const [localChats, setLocalChats] = useState(chats || []);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setLocalChats(chats || []);
  }, [chats]);

  const filtered = useMemo(() => {
    const list = localChats || [];
    if (!q) return list;
    const lc = q.toLowerCase();
    return list.filter(c =>
      (c.name || "").toLowerCase().includes(lc) ||
      (c.username || "").toLowerCase().includes(lc) ||
      String(c.id).includes(lc)
    );
  }, [localChats, q]);

  const toggle = async (chatId, inCat) => {
    if (busyId != null) return;
    const prev = localChats.find(c => c.id === chatId);
    if (!prev) return;
    setBusyId(chatId);
    setError("");
    setLocalChats(list => list.map(c => c.id === chatId
      ? (inCat
          ? { ...c, category_id: null, category_name: null, category_color: null }
          : {
              ...c,
              category_id: category.id,
              category_name: category.name,
              category_color: category.color,
              prompt_profile: category.prompt_profile,
            })
      : c
    ));
    try {
      await onToggle(chatId, inCat);
    } catch (e) {
      setLocalChats(list => list.map(c => c.id === chatId ? prev : c));
      const msg = e?.status === 403
        ? "權限 token 已刷新，請再點一次；如果仍失敗，重新整理頁面。"
        : (e?.message || "指派分類失敗");
      setError(msg);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="cat-modal-backdrop" onClick={onClose}>
      <div className="cat-modal cat-add-modal" onClick={(e)=>e.stopPropagation()}>
        <div className="cat-modal-head">
          <strong>
            管理{" "}
            <span style={{ color: category.color }}>「{category.name}」</span>
            {" "}成員
          </strong>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>完成</button>
        </div>
        <div className="cat-add-search">
          <input
            className="input"
            placeholder="搜尋聊天室…"
            value={q}
            onChange={(e)=>setQ(e.target.value)}
            autoFocus
          />
          <div className="cat-add-hint">一個聊天室只能屬於一個分類；移入會取代原分類。</div>
          {error && <div className="cat-add-error">{error}</div>}
        </div>
        <div className="cat-add-list">
          {filtered.length === 0 ? (
            <div className="cat-add-empty">沒有符合的聊天室</div>
          ) : filtered.map(c => {
            const inCat = c.category_id === category.id;
            const typeClass = c.type === "channel" ? "channel"
                            : c.type === "private" ? "private"
                            : "group";
            const initial = (c.name || "#").charAt(0).toUpperCase();
            const otherCat = c.category_id && c.category_id !== category.id;
            const statusText = inCat
              ? `已在「${category.name}」`
              : otherCat
                ? `目前在「${c.category_name}」；移入會取代原分類`
                : "未分類";
            const actionText = inCat ? "移除" : otherCat ? "移入" : "加入";
            const actionClass = inCat ? "remove" : otherCat ? "move" : "add";
            return (
              <div
                key={c.id}
                className={cls("cat-add-item", inCat && "in-cat", busyId === c.id && "busy")}
                onClick={() => toggle(c.id, inCat)}
              >
                <div className={cls("chat-avatar", typeClass)} style={{ width: 28, height: 28, fontSize: 11 }}>{initial}</div>
                <div className="cat-add-body">
                  <div className="cat-add-name">{c.name}</div>
                  <div className="cat-add-sub" style={{ color: otherCat ? c.category_color : undefined }}>{statusText}</div>
                </div>
                <span className={cls("cat-add-action", actionClass)}>
                  {busyId === c.id ? <span className="spinner" /> : actionText}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---------- Category Manager modal ----------
const CATEGORY_COLORS = [
  "#9a5b2a", "#2a3d52", "#5a6b3a", "#8a3a2a",
  "#3a4a5a", "#3a5a4a", "#6a2a3a", "#7a5a2a",
];

function CategoryManager({ open, onClose, categories, chats, profiles, onCreate, onUpdate, onDelete }) {
  const profileOptions = (profiles && profiles.length)
    ? profiles
    : [{ value: "group_chat", label: "群聊總結" }];
  const defaultProfile = profileOptions[0].value;
  const profileLabel = (v) => {
    const p = profileOptions.find(x => x.value === v);
    return p ? p.label : v;
  };

  const [newName, setNewName] = useState("");
  const [newColor, setNewColor] = useState(CATEGORY_COLORS[0]);
  const [newProfile, setNewProfile] = useState(defaultProfile);
  const [editingId, setEditingId] = useState(null);
  const [editName, setEditName] = useState("");
  const [editColor, setEditColor] = useState("");
  const [editProfile, setEditProfile] = useState(defaultProfile);

  if (!open) return null;

  const submitNew = () => {
    const name = newName.trim();
    if (!name) return;
    onCreate(name, newColor, newProfile);
    setNewName("");
    setNewColor(CATEGORY_COLORS[0]);
    setNewProfile(defaultProfile);
  };

  const startEdit = (cat) => {
    setEditingId(cat.id);
    setEditName(cat.name);
    setEditColor(cat.color);
    setEditProfile(cat.prompt_profile || defaultProfile);
  };

  const saveEdit = () => {
    const name = editName.trim();
    if (!name) return;
    onUpdate(editingId, { name, color: editColor, prompt_profile: editProfile });
    setEditingId(null);
  };

  return (
    <div className="cat-modal-backdrop" onClick={onClose}>
      <div className="cat-modal" onClick={(e)=>e.stopPropagation()}>
        <div className="cat-modal-head">
          <strong>Manage Categories</strong>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>×</button>
        </div>

        <div className="cat-modal-section">
          <div className="label">New category</div>
          <div style={{ display: "flex", gap: 6 }}>
            <input className="input" placeholder="name" value={newName}
                   onChange={(e)=>setNewName(e.target.value)}
                   onKeyDown={(e)=>e.key === "Enter" && submitNew()} />
            <button className="btn btn-sm btn-primary" onClick={submitNew}>Add</button>
          </div>
          <div className="cat-color-row">
            {CATEGORY_COLORS.map(col => (
              <button key={col} title={col} onClick={()=>setNewColor(col)}
                      className={cls("cat-color-swatch", newColor === col && "active")}
                      style={{ background: col }} />
            ))}
          </div>
          <div className="cat-profile-row">
            <span className="cat-profile-label">Prompt:</span>
            <select
              className="input cat-profile-select"
              value={newProfile}
              onChange={(e)=>setNewProfile(e.target.value)}
            >
              {profileOptions.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="cat-modal-section">
          <div className="label">Existing ({categories.length})</div>
          {categories.length === 0 ? (
            <div style={{ fontSize: 11, color: "var(--ink-3)", padding: "6px 0" }}>還沒建立分類</div>
          ) : categories.map(cat => {
            const catChats = (chats || []).filter(c => c.category_id === cat.id);
            const catChatNames = catChats.map(c => c.name || `#${c.id}`);
            // Inline up to 2 names; collapse the rest into "+N" with full
            // list available via title tooltip. Avoids the row blowing up
            // when a category has many chats.
            const inlineLabel = catChatNames.length === 0
              ? "(尚未綁定聊天室)"
              : catChatNames.length <= 2
                ? catChatNames.join(", ")
                : `${catChatNames.slice(0, 2).join(", ")} +${catChatNames.length - 2}`;
            return (
            <div key={cat.id} className="cat-row">
              {editingId === cat.id ? (
                <>
                  <input className="input" value={editName}
                         onChange={(e)=>setEditName(e.target.value)}
                         style={{ flex: 1 }} />
                  <div style={{ display: "flex", gap: 2 }}>
                    {CATEGORY_COLORS.map(col => (
                      <button key={col} onClick={()=>setEditColor(col)}
                              className={cls("cat-color-swatch-sm", editColor === col && "active")}
                              style={{ background: col }} />
                    ))}
                  </div>
                  <select
                    className="input cat-profile-select-sm"
                    value={editProfile}
                    onChange={(e)=>setEditProfile(e.target.value)}
                  >
                    {profileOptions.map(p => (
                      <option key={p.value} value={p.value}>{p.label}</option>
                    ))}
                  </select>
                  <button className="btn btn-sm btn-primary" onClick={saveEdit}>✓</button>
                  <button className="btn btn-sm btn-ghost" onClick={()=>setEditingId(null)}>×</button>
                </>
              ) : (
                <>
                  <span className="cat-swatch" style={{ background: cat.color }} />
                  <span style={{ flex: 1, fontSize: 13 }}>{cat.name}</span>
                  <span className="cat-profile-tag" title="Prompt profile">
                    {profileLabel(cat.prompt_profile || defaultProfile)}
                  </span>
                  <span
                    className="mono"
                    style={{
                      fontSize: 10,
                      color: catChats.length === 0 ? "var(--ink-3)" : "var(--ink-2,var(--ink-3))",
                      maxWidth: 220,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={catChatNames.length ? catChatNames.join("\n") : "尚未綁定聊天室"}
                  >
                    {inlineLabel}
                  </span>
                  <button className="btn btn-sm btn-ghost" onClick={()=>startEdit(cat)}>Edit</button>
                  <button className="btn btn-sm btn-ghost"
                          onClick={()=>{ if (confirm(`刪除「${cat.name}」?此分類下的聊天會變成未分類。`)) onDelete(cat.id); }}>
                    ×
                  </button>
                </>
              )}
            </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, {
  API, apiJSON, esc, cls, highlightKw, formatDate, formatTime, sinceAgo,
  mediaLabel, Banner, Rail, LoginView, Inbox, CategoryManager,
});
