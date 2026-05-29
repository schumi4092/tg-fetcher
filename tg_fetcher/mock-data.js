// Mock data for the Telegraph prototype — simulates a real Crypto-intel Telegram reader.
window.MOCK = (() => {

  const chats = [
    { id: '1001', name: '加密貓的每日情報', username: 'cryptocat_daily', type: 'channel', unread: 42, is_forum: false, tag: 'tier-1', last: '4 分鐘前', preview: 'BTC 突破 $104k，鏈上淨流入達七日高點…' },
    { id: '1002', name: 'Wintermute Flow', username: 'wmflow', type: 'channel', unread: 8, is_forum: false, tag: 'market', last: '12 分鐘前', preview: 'Perp OI shifting — SOL longs closing into thin order books.' },
    { id: '1003', name: 'DegenCasino VIP', type: 'supergroup', unread: 211, is_forum: true, tag: 'alpha', last: '1 分鐘前', preview: '「那個新幣圖怎麼看」— @solman' },
    { id: '1004', name: '鏈上分析師研究室', username: 'onchain_lab', type: 'channel', unread: 0, is_forum: false, tag: 'research', last: '2 小時前', preview: '深度報告：Aave V4 流動性模型拆解' },
    { id: '1005', name: 'MEV Whisperers', type: 'supergroup', unread: 3, is_forum: false, tag: 'tech', last: '18 分鐘前', preview: 'builder landscape map v3 dropped' },
    { id: '1006', name: 'Alpha Leaks EN', username: 'alphaleaks', type: 'channel', unread: 17, is_forum: false, tag: 'alpha', last: '32 分鐘前', preview: 'Pre-TGE: Infinex airdrop criteria leaked' },
    { id: '1007', name: '台灣幣圈聊天室', type: 'supergroup', unread: 89, is_forum: true, tag: 'community', last: '3 分鐘前', preview: '有人知道幣安這次掛鉤問題嗎' },
    { id: '1008', name: 'Ethena Announcements', username: 'ethena_labs', type: 'channel', unread: 1, is_forum: false, tag: 'project', last: '昨天', preview: 'sUSDe 質押上線 Arbitrum' },
    { id: '1009', name: 'DeFi Treasury Desk', type: 'supergroup', unread: 0, is_forum: false, tag: 'research', last: '3 小時前', preview: 'Treasury ops thread — week 47' },
    { id: '1010', name: 'Whale Watchers', username: 'whalewatcher', type: 'channel', unread: 5, is_forum: false, tag: 'market', last: '7 分鐘前', preview: '0x7f39 moved 2,410 ETH to Binance' },
    { id: '1011', name: '毛哥的盤感筆記', type: 'private', unread: 2, is_forum: false, tag: 'friend', last: '1 小時前', preview: '你看 ETH/BTC 這週的結構了嗎' },
    { id: '1012', name: 'Solana Builders', username: 'sol_builders', type: 'channel', unread: 0, is_forum: false, tag: 'tech', last: '5 小時前', preview: 'Firedancer 主網 rollout 時程' },
  ];

  // A realistic 8-hour fetch from "加密貓的每日情報"
  const messages = [
    { id: 901, from: '加密貓', username: 'cryptocat', text: 'BTC 剛剛突破 $104,000，24h 漲幅 3.8%。鏈上方面，Glassnode 數據顯示短期持有者的未實現獲利率回到 0.28，歷史上這個位置通常對應中期頂部前的最後一段，但不是立即訊號。', time: '14:32', ts: Date.now() - 1000*60*8, alerts: ['BTC'] },
    { id: 902, from: '加密貓', username: 'cryptocat', text: '以太坊方面，ETF 淨流入連續 7 天為正，BlackRock ETHA 單日 +$127M。Gas 維持在 14 gwei，L2 吞吐量相對溫和。', time: '14:41', ts: Date.now() - 1000*60*15, alerts: ['ETF'] },
    { id: 903, from: '加密貓', username: 'cryptocat', text: '值得注意的是 SOL/ETH 比率今天回測 0.058 遭拒，如果守不住 0.055 可能看到資金從 SOL 回流到 ETH。', time: '15:02', ts: Date.now() - 1000*60*28 },
    { id: 904, from: '研究員 Apollo', username: 'apollo_res', text: '補充一下：穩定幣市值今天新高 $187B，USDT 佔比 69.2%。USDC 增發速度明顯放緩，可能與 Circle 近期 IPO 前的審計調整有關。', time: '15:17', ts: Date.now() - 1000*60*45, alerts: ['USDC', 'USDT'] },
    { id: 905, from: '加密貓', username: 'cryptocat', text: '幣安永續資金費率快照：BTC +0.011% / ETH +0.008% / SOL +0.021%（偏多但不極端）。爆倉量過去 24h 約 $342M，多空比 1.7:1。', time: '15:40', ts: Date.now() - 1000*60*62, alerts: ['爆倉'] },
    { id: 906, from: '加密貓', username: 'cryptocat', text: 'Macro：美 10Y 殖利率跌至 4.18%，DXY 107.3。聯準會 12 月 18 日會議前 Polymarket 降息 25bp 機率 67%。', time: '16:05', ts: Date.now() - 1000*60*80 },
    { id: 907, from: '研究員 Apollo', username: 'apollo_res', text: '關注 $JITO 質押量今天回到 14.2M SOL 新高，相關 LST 賽道的 TVL 佔 Solana DeFi 已 38%。', time: '16:22', ts: Date.now() - 1000*60*98 },
    { id: 908, from: '加密貓', username: 'cryptocat', text: '事件提醒：Kaito Yaps Season 2 今天正式開跑，KOL 賽道短期流量會集中。已將 @KaitoAI 加入觀察。', time: '16:55', ts: Date.now() - 1000*60*115, media: 'photo' },
    { id: 909, from: '加密貓', username: 'cryptocat', text: '晚間戰術觀察：BTC 如果能站上 $104,500 並且 funding 維持 <0.02%，偏向 continuation。破 $102,000 則進場觀望。ETH 參考 $3,420 / $3,280 兩個轉折位。', time: '17:18', ts: Date.now() - 1000*60*130, alerts: ['BTC', 'ETH'] },
    { id: 910, from: '加密貓', username: 'cryptocat', text: '今日重要事件彙整：\n① Circle 更新儲備結構 → USDC\n② Ethena sUSDe 登陸 Arbitrum\n③ Polymarket 降息機率變化\n④ JITO 質押新高', time: '17:42', ts: Date.now() - 1000*60*148 },
    { id: 911, from: '研究員 Apollo', username: 'apollo_res', text: '快訊：Coinbase 剛上線 $MORPHO，盤前 +18%。', time: '18:01', ts: Date.now() - 1000*60*160, alerts: ['Coinbase'] },
  ];

  const aiSummary = {
    summary: `過去 8 小時市場延續偏多結構，但未出現急拉，BTC 重新站上 $104k、ETH 維持 $3.4k 上方，資金費率仍在中性偏多區間，顯示多頭倉位還沒過度擁擠。

鏈上與資金面出現三個值得追蹤的變化：（1）ETF 連續 7 天淨流入，BlackRock 單日吸納 $127M；（2）穩定幣市值創高，但 USDC 增速放緩，與 Circle IPO 前的審計動作有關；（3）Solana 的 LST 賽道 TVL 佔比攀升至 38%，JITO 質押量回到歷史高點。

宏觀側主要變數為 12/18 聯準會會議，Polymarket 賦予 25bp 降息 67% 的機率，支撐風險資產情緒。

戰術上，加密貓給出 BTC $104,500 為 continuation 指標、$102,000 為退場觀望線，並將 ETH $3,420 / $3,280 設為兩個結構轉折。近期 Kaito Season 2 推動的 KOL 流量值得列入觀察名單。`,
    events: [
      { importance: 'high', title: 'BTC 突破 $104k，鏈上短期獲利率接近歷史中期高點', tags: 'BTC · on-chain · STH', desc: 'Glassnode 數據顯示 STH 未實現獲利率 0.28，歷史上對應中期頂部前最後一段。' },
      { importance: 'high', title: 'Ethena sUSDe 正式登陸 Arbitrum', tags: 'ETHENA · Arbitrum · 穩定幣', desc: '原生質押拓展到 L2，可能引發新一輪 TVL 輪動。' },
      { importance: 'normal', title: 'USDC 增發放緩，可能關聯 Circle IPO 前審計', tags: 'USDC · Circle', desc: '雖然穩定幣總市值新高，但 USDC 市佔被 USDT 持續擠壓。' },
      { importance: 'normal', title: 'JITO 質押量回到 14.2M SOL 歷史高點', tags: 'JITO · SOL · LST', desc: 'Solana LST 賽道 TVL 佔 DeFi 38%。' },
      { importance: 'low', title: 'Coinbase 上線 $MORPHO', tags: 'MORPHO · Coinbase', desc: '盤前漲幅 +18%。' },
    ]
  };

  // Memory timeline — past 14 days of daily briefs
  const today = new Date();
  const timeline = Array.from({ length: 14 }, (_, i) => {
    const d = new Date(today.getTime() - i * 86400000);
    const y = d.getFullYear(), m = String(d.getMonth()+1).padStart(2,'0'), day = String(d.getDate()).padStart(2,'0');
    const dateStr = `${y}-${m}-${day}`;
    const summaries = Math.max(1, Math.floor(Math.random()*5) + (i===0 ? 3 : 1));
    const events = Math.max(0, Math.floor(Math.random()*8));
    const notes = Math.floor(Math.random()*3);
    const high = Math.floor(Math.random()*3);
    const normal = events - high;
    return { date: dateStr, summaries, events, notes, high, normal, headlines: [
      ['BTC 站上 $104k', 'ETH ETF 連續 7 日淨流入', 'USDC 增速放緩'],
      ['Bitcoin ETF 淨流入 $342M', 'Solana LST TVL 新高', 'Aave V4 白皮書發布'],
      ['Fed FOMC 會議結果', 'BTC 回測 $98k 支撐', '穩定幣市值新高'],
      ['中國 CPI 數據超預期', 'ETH / BTC 比率破 0.032', 'Polymarket 降息機率上調'],
      ['比特幣礦工賣壓減弱', 'Base 週活躍地址新高', 'Hyperliquid 週費第一'],
      ['SEC 撤銷對 Consensys 訴訟', 'Solana 迷因板塊輪動結束', 'Uniswap V4 主網啟動'],
      ['Trump 加密關稅提議', 'Tether 季度利潤 $2.4B', 'Coinbase 新上幣 MORPHO'],
    ][i % 7] };
  });

  const notes = [
    { id: 1, date: '2 小時前', tags: ['策略', 'BTC'], text: 'BTC 在 $104,500 上方築底，如果 funding 能維持中性偏低，下一個目標看 $108k 前高。風控位 $100,800。' },
    { id: 2, date: '昨天', tags: ['研究', 'Ethena'], text: 'sUSDe 到 Arbitrum 會不會分走 stETH 在 L2 的流量？想追蹤 48 小時後的 TVL 流向。' },
    { id: 3, date: '3 天前', tags: ['宏觀'], text: '12/18 FOMC 之前，波動率可能會壓縮，選擇權 IV 已經掉到近一季低點。' },
    { id: 4, date: '一週前', tags: ['觀察', 'SOL'], text: 'SOL/ETH 比率 0.058 是個關鍵位置，過去兩次被拒都出現輪動。' },
  ];

  const watchlist = [
    { keyword: 'BTC', category: '主流' },
    { keyword: 'ETH', category: '主流' },
    { keyword: '爆倉', category: '市場結構' },
    { keyword: 'ETF', category: '資金' },
    { keyword: 'Circle', category: '穩定幣' },
    { keyword: 'Coinbase', category: '交易所' },
    { keyword: 'SEC', category: '監管' },
  ];

  const qa = [
    { q: '上週 ETH 有什麼大事？', a: '上週 ETH 主要有三件事：\n\n1) **ETF 流入轉正** — BlackRock ETHA 從週二開始連續 5 天淨流入，週累計 $412M，是 10 月以來最強的一週。\n2) **Pectra 升級路線圖更新** — 基金會確認下一次升級時間軸（目標 2026 Q2）。\n3) **ETH/BTC 比率觸底回升** — 從 0.0296 反彈到 0.0314，是 8 月以來首次站回 EMA 50。\n\n整體來說是結構性的一週，沒有單一爆炸性事件，但多個中長期指標同步轉正。' },
  ];

  return { chats, messages, aiSummary, timeline, notes, watchlist, qa };
})();
