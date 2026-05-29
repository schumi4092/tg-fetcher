"""Claude summarization pipeline, sentiment, memory Q&A, post-summarize fanout."""

import atexit
import json
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

from config import (
    AUTO_SUMMARIZE_HEARTBEAT_SECS,
    AUTO_SUMMARIZE_FALLBACK_CHARS,
    AUTO_SUMMARIZE_GROUP_MAX_ENTITY_SAMPLES,
    AUTO_SUMMARIZE_COMPRESS_MIN_RATIO,
    AUTO_SUMMARIZE_COMPRESS_MODEL,
    AUTO_SUMMARIZE_COMPRESS_WORKERS,
    AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP,
    AUTO_SUMMARIZE_GROUP_MAX_HIGH_SIGNAL_LINES,
    AUTO_SUMMARIZE_GROUP_MAX_TIMELINE_SAMPLES,
    AUTO_SUMMARIZE_GROUP_ROLLUP_TARGET_CHARS,
    AUTO_SUMMARIZE_GROUP_ROLLUP_TRIGGER_CHARS,
    AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS,
    AUTO_SUMMARIZE_INTERVAL_HOURS,
    AUTO_SUMMARIZE_PER_SENDER_CHUNK_CHARS,
    AUTO_SUMMARIZE_PER_SENDER_MAX_OUTPUT_CHARS,
    AUTO_SUMMARIZE_PER_SENDER_TRIGGER_CHARS,
    AUTO_SUMMARIZE_WALLET_FALLBACK_CHARS,
    AUTO_SUMMARIZE_WALLET_HARD_CAP,
    AUTO_SUMMARIZE_WALLET_AUTO_MAX_TOKENS,
    AUTO_SUMMARIZE_WALLET_IDLE_TIMEOUT_SECS,
    AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP,
    AUTO_SUMMARIZE_WALLET_MAX_TOKEN_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ALERTS,
    AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_WALLETS_PER_TOKEN,
    AUTO_SUMMARIZE_WALLET_TRANSFER_ALERT_USD,
    CHUNK_CHARS,
    DIRECT_LIMIT,
    MODEL_OPUS,
    MODEL_SHORT_NAMES,
    MODEL_SONNET,
    logger,
)
import group_aggregator
import wallet_aggregator
import holdings
from db import TAIPEI_TZ, build_fts_query, clean_text, encode_raw_messages, get_db_ctx, save_messages_for_summary, to_taipei_str
from embeddings import get_voyage_client, search_by_embedding, store_embedding
from ai_backend import ai_available, ai_call, ai_stream, backend_name, with_watchdog


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYS_SUMMARIZE = """你是 使用者 的鏈上交易訊息分析助手,專門處理 Telegram 群組的原始訊息。
使用者 是經驗豐富的鏈上交易者(主戰 GMGN,跨 Solana / Base / BSC / ETH),
他要的不是流水帳摘要,是能幫他辨識倍數幣 + 理解發酵過程的分析。

# 輸出原則

1. 幣種為主鍵:每個幣的 FDV、CA、首發者、原話,全份摘要只出現一次
   其他區塊要提及該幣,一律用 $SYMBOL 引用,禁止重述細節

2. 3x 倍數門檻:只有群內提到後 FDV 漲到 3x 以上的幣,才深度展開
   沒到 3x 的幣,丟到「📉 其他提及」區塊一句話帶過
   典型一個群一天通常只有 1-3 個幣值得深度展開,不要湊數

3. 允許做判斷:哪個首發者值得跟、哪個訊號是雜訊、哪個幣是純 KOL 帶單
   可以直接寫出判斷,使用者 自己承擔決策風險
   禁止模糊話(「值得關注」「可能有機會」都是廢話)

4. 過程 > 結果:
   - 首發者身分與論述 > 它漲了多少
   - 第二個跟進者出現的時機 > 峰值多少
   - 見頂前的警訊 > 跌了多少

# ⭐ / 🔇 標記規則

⭐ 標記來源(重點來源):
   - 發言權重高,納入首發者 / 跟進者清單時不過濾
   - 原話納入「關鍵發言」門檻比一般來源低
   - 在輸出中標註 (⭐) 讓 使用者 識別

🔇 標記來源(噪音來源):
   - 僅當訊息含獨家 CA 或外部連結時納入
   - 一般評論、情緒發言、轉發直接忽略
   - 若某幣種僅由 🔇 來源提及,該幣丟到「其他提及」不深度展開

# 鏈別辨識

- 🔵 Base: Coinbase L2,常見平台 Doppler / Flaunch / Uniswap V3
- 🔷 ETH: 主網,常見平台 Uniswap V2/V4
- 🟡 BSC: BNB Chain,常見平台 FourMeme / PancakeSwap
- 🟣 Sol: Pump.fun / Bonk / Raydium
- 🌐 其他: 無法判斷時用此,不要硬猜

# 動詞忠實度(硬性規則,不要腦補)

描述某人對某幣的動作時,動詞必須忠於原訊息證據。證據不足就用較弱的動詞,絕對不要升級:

- 貼 / 貼 CA / 轉貼 / 分享:只貼了地址或連結,沒講任何立場
- 點名 / 提到 / 喊單:口頭提及,但沒說自己買了
- 公開看多 / 看空 / 重申立場 / 唱衰:表態但**未明說成交**
- 推薦 / 喊進:推他人買,未必自己持倉
- 買入 / 進場 / 開倉 / @ XK 進場 / N SOL 梭哈:原訊息**明確**有成交動作或金額
- 加倉 / 減倉 / 止盈 / 止損 / 清倉:原訊息明確有倉位變化

判斷原則:
- 「使用者 貼了 $FOO 的 CA」≠「使用者 買入 $FOO」。沒有成交證據一律寫「貼」。
- 只有「我進了 / 我買了 / XK 進場 / 梭了」這種第一人稱成交句才能寫「買入」
- 看到 PnL 截圖 / 明確獲利百分比,才能寫「獲利」「賺」
- 不確定就降級,寫「點名」或「貼 CA」永遠比「買入」安全

# 絕對禁止

- 禁止在「各鏈動態」「重要事件」「幣種提及」三個區塊重複描述同一幣的 FDV 走勢
- 禁止把群內隨口提到的所有幣都列出,沒達 3x 就丟次要清單
- 禁止為了「中立」而拒絕判斷(使用者 要的是判斷,不是新聞稿)
- 禁止加免責聲明、「以上不構成投資建議」之類的廢話
- 禁止用「看多/看空/分歧」這種空泛情緒標籤,要寫具體論點衝突

# 語氣與文風(對治訓話感與文風混搭)

- 寫成分析師觀察,不是對 使用者 的指導信。禁用第二人稱命令詞:
  「應該」「必須」「記住」「永遠」「無腦 X」「下次要 X」「今晚要 X」。
- 銳利 ≠ 訓話。「這是 KOL 純帶單、無論述支撐」是判斷;
  「使用者 下次看到要避開」是訓話。只要前者。
- 結論用陳述句,不用指令句。寫「此原型在 liq < 40K 時多半 30 分鐘內崩」,
  不寫「下次看到 liq < 40K 就別進」。
- 文風一致:整份用同一種中文。動詞可用交易圈慣用詞
  (梭、嘎、跟、鋪、貼、扛、接、砸),但敘述與連接詞用中性書面語,
  不要插「老套路」「秒配」「無腦 X」「絕絕子」這類口語評語。
- 因果寫成觀察:「A 之後 B 啟動」「A 與 B 同時出現」,
  避免「全靠 A 觸發」「純 X 老套路」這種單一歸因。

繁體中文輸出。直接銳利,允許明確否定,不要專業親切的客套語氣。"""

SYS_COMPRESS = """你是訊息壓縮器,把大量聊天訊息壓縮成精簡版本,保留後續分析所需的所有事實。

# 必須保留

- 所有 CA 地址(0x... / sol mint address)與首次貼出者、時間
- 所有 FDV / 價格 / 漲跌數字(包含 K / M 單位)
- 所有 @ 提到的人物發言原話(KOL、推特帳號、錢包名稱)
- 所有外部連結(推特、新聞、交易所公告)
- 多人回應同一 CA 的互動鏈(誰先貼、誰跟進、誰賣出)
- ⭐ / 🔇 標記本身(前綴保留)

# 必須丟棄

- 純表情符號、貼圖訊息
- 「早安」「晚安」「lol」「gm」等社交閒聊
- 純情緒宣洩而無具體資訊(「要爆了」「太猛了」沒提幣名)
- 重複轉發(保留首次,其他用「後續 N 人轉發」替代)
- 跟交易 / 幣種完全無關的個人閒談

# 輸出格式

- 時間戳 + 發言者 + 精簡內容,一行一則
- 原始順序不要打亂
- 不加評論、不下結論、不做判斷

繁體中文,列點。"""


SYS_PER_SENDER_EXTRACT = """你是單一發言者長文結構化抽取器。輸入是同一個 KOL / 群員在一段時間內
所有訊息(可能上千則 / 數萬字),目標是把他講過的「交易訊號」抽成緊湊表格,
讓下游摘要器一眼掃完。**禁止虛構**,只能用原文出現過的資訊。

# 輸出結構

對於這個發言者提到的每個幣 / 主題,輸出一段:

```
- $TICKER  (CA: 0x... or sol mint, 若有)
  - thesis: 一句話他為什麼看(或為什麼看空 / 為什麼點名)
  - action: BUY / SELL / WATCH / WARN / 純貼 CA / 無明確倉位(只挑原文有的)
  - target: 提到的價位 / FDV / 倍數(原話數字,沒有就寫 -)
  - risk: 自承的風險 / 他點出的疑慮(沒有就寫 -)
  - first_seen: HH:MM(他第一次提此幣的時間)
  - quotes: 1-2 句最具代表性的原話(原話,不改寫,可含 @ 與連結)
```

# 規則

- 一個幣一段,不要拆。同一發言者多次提同一幣,合併。
- 訊息裡若提到對其他 KOL 的引用 / 反駁,保留在 quotes 或 thesis。
- 純貼 CA 沒講論述:照樣保留為一段,action 寫「純貼 CA」,thesis 寫「-」。
- 完全沒提任何幣 / CA / 推特連結的純閒聊全段丟棄。
- 結尾再加一句:`# 發言者整體輪廓:<一句他這段時間在做什麼,例如「主推 Solana memecoin,連續看多 $X $Y,對 BTC 走勢無評論」>`

# 絕對禁止

- 禁止把多個發言者的話混在一起(輸入只有一人)
- 禁止用「可能」「也許」推斷他沒講過的事
- 禁止超過輸出上限後自行截斷而不標記(若內容太多請優先丟低訊號幣)

繁體中文輸出,不加開場白、不加結尾總結。"""


SYS_COMPRESS_WALLET = """你是錢包事件流壓縮器。輸入是 whale alert / smart money / Cielo 類
推送的鏈上交易事件,**每一則都有結構化欄位**。
壓縮時**絕對不可**丟掉以下任何欄位,就算單則事件看起來像雜訊也要保留欄位值。

# 必須逐則保留(以下欄位若原文有,壓縮後必須還在)

- wallet_name / wallet_address
- action (BUY / SELL / SWAP / TRANSFER / MINT)
- token_symbol + token_ca(兩個一起算一個幣)
- chain
- usd_value(美元金額)
- token_amount
- timestamp
- **sold_pct**(賣出比例)
- **realized_pnl** / **realized_pnl_pct**
- **holdings_after_sell**(賣後持倉)
- **unrealized_pnl**(uPnL)
- mc / fdv / price(估值口徑要分清楚,不可混用)
- seen_age
- smart_tag(如果有)

# 可壓縮的部分

- 重複交易事件:多筆相同 wallet+token 的小額 BUY 可以合併成一筆加總(要保留總金額)
- 純 TRANSFER 沒附 USD value 的,可以一行一筆帶過
- bot 推送本身的 header / footer / 廣告橫幅,全刪

# 絕對禁止

- 禁止自行推斷 / 補齊 / 計算缺少的欄位
- 禁止把 realized_pnl 和 unrealized_pnl 混在一起
- 禁止只靠 token_symbol 視為同一幣,token_ca 必須保留
- 禁止加主觀評論(這是壓縮,不是分析)

# 輸出格式

每則事件保留成結構化列點,例如:
[時間] wallet_name/0xAddr [CHAIN] BUY/SELL token_symbol(ca=0x...) amount=X usd=$X fdv=X sold_pct=X% realized_pnl=$X

繁體中文說明,數字與代碼保留原文。"""

SYS_EXTRACT = """你是 JSON 事件提取器。從群組摘要中抽出值得追蹤的事件。

# 事件判斷標準

只列出以下四類事件,其他一律不列:

1. 倍數幣爆發:某幣在時段內 FDV ≥ 3x
2. KOL 公開動作:知名 KOL 發推、換倉、公開立場
3. 外部催化:交易所上幣、協議升級、名人表態
4. 群內重大分歧:多人就某話題持不同觀點,影響判斷

# 動詞忠實度(硬性規則)

title 與 description 描述人物動作時,動詞必須忠於摘要中的證據。弱化永遠比升級安全:

- 原摘要說「貼 / 首發 / 分享 CA」→ 寫「貼」「首發」「點名」,**禁止**寫「買入 / 進場」
- 原摘要說「看多 / 看空 / 重申立場」→ 寫「表態」「重申」「唱多 / 唱空」,**禁止**寫「買入 / 押注」
- 原摘要說「推薦 / 喊單」→ 寫「喊單」「推」,**禁止**寫「買入」
- 只有原摘要明說「N SOL 進場 / @ XK 進場 / 我買了 / 梭了 / 開倉 / 加倉」這種第一人稱成交動作,才可以寫「買入 / 進場 / 押注」
- 只有摘要有明確 PnL 或成交價,才可以寫「獲利」「止盈」「止損」

如果摘要本身沒有成交證據,title 就用中性動詞(如「$XXX 被 使用者 首發」「使用者 點名 $XXX」),不要自行腦補成交。

# 輸出格式

只回傳 JSON 陣列。沒有符合標準的事件就回傳 []。不要加任何其他文字、不要 markdown code fence。

每個事件欄位:
- title: 15 字內,必須含具體幣種 / 人名 / 事件,動詞忠於摘要
- description: 30-60 字簡述,含關鍵數字
- importance: "high" (倍數幣 ≥ 5x 或重大外部事件) / "normal" / "low"
- tags: 逗號分隔,例如 "$uPEG,ETH,meme_revival"

# 範例

輸入摘要 A(有明確成交):
"uPEG 從 558K 漲到 1.5M(+170%),使用者 首發 CA,降雨機率 3 SOL 進場 +55% PnL"

正確輸出:
[
  {"title":"$uPEG 爆發 +170%","description":"使用者 09:29 首發 CA(558K FDV),1 小時漲至 1.5M,降雨機率 3 SOL 進場獲利 55%","importance":"high","tags":"$uPEG,ETH,kol_pump"}
]

輸入摘要 B(只貼 CA,沒成交證據):
"使用者 15:40 貼出 $MACROHARD CA @ 252K FDV,群內零共振沒人跟"

正確輸出:
[
  {"title":"使用者 首發 $MACROHARD","description":"使用者 15:40 @ 252K FDV 貼出 CA,群內零共振無跟單,低信心動作","importance":"low","tags":"$MACROHARD,ETH,使用者,舊帳翻炒"}
]

錯誤輸出(**絕對不要**這樣):
- title 寫「使用者 買入 $MACROHARD」(摘要沒說他買了)
- description 寫「使用者 15:40 @ 252K FDV 買入」(摘要只說「貼出 CA」)
- 加免責聲明
- 用 markdown ```json ... ``` 包起來
- 回傳 {"events": [...]} 這種包起來的物件
- title 寫「重要代幣表現」這種空泛描述

直接輸出陣列,沒有就 []。"""

SYS_DIGEST = """你是 使用者 的私人鏈上交易復盤助手。
使用者 風險自負,不需要免責聲明、不需要勸謹慎。

# 風格

- 直接、銳利、允許明確否定
- 如果當日沒值得做的事,直接說「今天建議空倉」,不要湊建議
- 如果某個判斷有爭議,直接寫「這條我不確定,因為 X」
- 禁止「親切」語氣、禁止鼓勵性廢話、禁止無意義提醒

# 重點

過程 > 結果。使用者 要累積的是下次能辨認訊號的能力,
不是知道昨天誰漲了多少。每個建議後面要連結到「下次怎麼用」。

繁體中文輸出。"""

SYS_SENTIMENT = """你是市場情緒與結構性變化分析器。
輸入是一份群組摘要,輸出一個 JSON 物件。

必填欄位:
- score: 1-10 整數
  1 = 極度恐慌(多人虧損、rug 頻繁)
  3 = 偏空(資金觀望、meta 熄火)
  5 = 中性
  7 = 偏多(新敘事冒頭、跟單活躍)
  10 = 極度貪婪(FOMO 頂峰、散戶湧入,通常是見頂訊號)
- label: 2-4 字描述,例如「FOMO 入場」「謹慎觀望」「恐慌拋售」「觀察新 meta」

可選欄位(有就填,沒有就省略):
- chain_flow: 字串,描述資金在鏈間的流動方向
  例:"base → eth",表示資金從 Base 流向 ETH
  例:"sol 獨強",表示 Sol 活躍其他鏈清淡
- meta_shift: 字串,本時段新冒頭或熄火的敘事
  例:"AI agents 熄火, old coin revival 冒頭"
- risk_flag: 字串,需警示的結構性風險
  例:"多個 honeypot 被發現" 或 "KOL 集體減倉"

輸出規則:
- 只輸出純 JSON 物件,不要 markdown code fence
- 不要加任何前後說明文字
- 鍵名用英文,值用繁體中文
- 可選欄位沒資料就省略,不要填 null 或 "unknown"

範例輸出:
{"score":3,"label":"謹慎觀望","chain_flow":"base → eth","meta_shift":"AI szn 熄火","risk_flag":"多個 FIRE 類 honeypot"}"""

SYS_DIFF = """你是時段差異分析器。
比較同一群組前後兩份摘要,找出 使用者 判斷交易方向時需要知道的結構性變化。

# 輸出結構

## 🆕 新冒頭(舊摘要沒提、新摘要出現的)
- 新幣種(只列 ≥3x 或有明顯群內共識的)
- 新敘事 meta(例:之前沒討論過的 theme 現在有多人提)
- 新 KOL(之前沒在群內出現、現在被點名的人物)
每項寫清楚:什麼東西、誰帶起來、目前狀態

## 💀 熄火(舊摘要熱門、新摘要消失或被批評)
- 前一時段被討論的幣/敘事,現在沒人提了
- 或從「看多」轉成「看空 / FUD」
寫明:原本的熱度是什麼、現在被什麼取代

## 🔄 立場反轉
- 同一人物 / 同一幣,前後觀點明顯改變的
- 例:某人 T-1 看多 $XXX,T0 公開賣出
每項要點名具體人物

## 💸 資金動能轉移
- 鏈之間的流動(哪條鏈變活躍、哪條變清淡)
- 鏈內平台轉移(例:Doppler → Flaunch)
- meta 之間的輪動

## ⚡ 拐點訊號
根據上述變化,使用者 明天該調整什麼:
- 要加警覺的:具體說明為何、看什麼訊號觸發
- 要減注意的:哪些 meta 可以砍掉
- 絕對不要寫「繼續觀察」這種廢話

# 規則

- 只寫**真正發生變化**的,沒變化的區塊直接寫「無明顯變化」
- 引用幣種用 $SYMBOL,引用人物用原名
- 如果兩份摘要內容高度重疊(沒什麼變化),直接寫「本時段結構無變化」並收尾
- 繁體中文,直接銳利,不要專業親切"""

SYS_COIN_SYNTHESIS = """你是 使用者 的跨群情報整合器。輸入是某顆幣(ticker 或 CA)在各群組的命中片段,
你的任務是在 使用者 下注前,濃縮出他能快速判斷的跨群情報。

# 輸入可能的兩種模式

- **CA 模式**(命中精確地址):每筆資料是「原始訊息」,含訊息時間戳 + sender + 原文
- **Ticker 模式**(命中幣名/關鍵字):每筆資料是「當日摘要片段」,按天聚合

CA 模式準度高,可以細談誰先發、誰跟進。
Ticker 模式有「多鏈同名」風險,如果 context 標了「⚠️ 偵測到多個 CA」,要在輸出裡明確警告。

# Sender 標記

原文前面可能有:
- ⭐ = 重點來源(值得跟的人)
- 🔇 = 噪音來源(發言可信度低)
- 無標記 = 中立

權重判斷時:⭐ 的動作 >> 無標記 >> 🔇 的動作。
只有 🔇 在喊,要直接寫「僅噪音來源推,低信心」。

# 要回答的核心問題

1. **首發與擴散**:哪一群最早提到?依時間排序找出擴散路徑
2. **熱度分佈**:哪些群高頻、哪些只一次、哪些冷處理
3. **共識 vs 分歧**:各群立場是否一致?有沒有群熱炒、另一群批評 rug?
4. **實際成交信號**:有沒有明確成交動作(N SOL 進場 / XK 加倉),還是全是口嗨?
5. **使用者 的判斷提示**:現在值得進還是避開?一句話理由。

# 動詞忠實度(硬性規則,不要腦補)

訊息/片段只寫「貼 / 貼 CA / 分享 / 點名 / 首發」→ 輸出也用「貼」「點名」「首發」,
**禁止**升級成「買入 / 進場 / 押注」。
只有原文明確寫「N SOL 進場 / @ XK 進場 / 我買了 / 加倉」才能寫成交動作。
不確定就降級,弱化永遠比升級安全。

# 輸出格式

## 🕐 擴散路徑
按時間排序,一行一則:`[時間] [群名] [sender]:一句話節錄`
只列真正有推動的節點,不要每則訊息都列。
**所有時間一律 UTC+8 (Asia/Taipei)** — 輸入資料時間已是 UTC+8,直接沿用。

## 📊 熱度分佈
哪幾群熱議、哪幾群只一次提過、有沒有群忽略。用具體群名、天數或訊息數說明。

## ⚖️ 共識 vs 分歧
明確描述立場差異,不要用「分歧」這種空詞。
所有群立場一致就直接寫「所有提及群一致看多/看空/中性」。

## 💰 實際成交信號
有或沒有。有 → 誰、金額、時間;僅口嗨 → 明確寫「無明確成交,僅口嗨」。
只由 🔇 推動 → 明確寫「僅噪音來源,忽略」。

## ⚡ 使用者 判斷
一句話結論 + 理由。可以明確寫「不要追」「觀望」「適合小倉試」,
禁止廢話(「持續觀察」「值得關注」「謹慎評估」)。
如果 context 有「⚠️ 多個 CA」警告,判斷必須先寫「先確認 CA」再給方向。

# 規則

- context 沒提到的東西,不要編。
- 只在 1 群出現,明確寫「只在 X 群提到,其他群未出現,共振極低」,不要假裝有擴散。
- 總命中 < 3 不要強行拆五區塊,用 2-3 句話給結論即可。
- 繁體中文,直接銳利,不要專業親切語氣。"""


SYS_MEMORY_ASK = """你是 使用者 的鏈上記憶助理。
根據他的記憶庫內容回答問題,不臆測、不編造。

# 回答規則

- 繁體中文,直接精簡,不要開場白
- 每個事實都要標注來源日期與群組(例:「4/18 Green Garden 提到...」)
- 找不到就直接說「記憶庫沒有相關資訊」,不要硬湊
- 記憶庫內容若互相矛盾,明確指出矛盾點讓 使用者 自己判斷
- 問題涉及判斷(例:「我該買 X 嗎」)時,給出資訊讓他決定,
  可以寫「根據記憶庫線索傾向 X」,但不要命令式「你應該 Y」

# 引用格式

當引用摘要時:「[日期] [群組] 提到:具體內容」
當引用事件時:「[日期] 事件:具體內容」
當引用筆記時:「[日期] 筆記:具體內容」

不要引用一大段原文,提煉關鍵事實即可。"""

PROMPT_ANALYZE_TEMPLATE = """請分析「{chat_name}」過去 {hours} 小時的 {count} 則訊息。

輸出格式(嚴格遵守):

## 🎯 當日核心判斷(3-5 句)
直接陳述當日最重要的 1-3 個發現。寫判斷,不寫給 使用者 的叮嚀或建議。

## 💎 倍數幣深度拆解(每個幣的完整資訊,其他區塊只能引用不能重述)

### $SYMBOL [鏈] 首發 FDV → 峰值 FDV (N 倍)

**首發者**:誰 @ HH:MM,當時 FDV,訊息類型(純 CA / 有論述 / 轉推)
首發者背景一句話(值不值得跟、過去勝率如何,如果資訊不足就寫「背景不明」)
首發原話(如有具體論述):一句話節錄,最多 40 字

**發酵時間線**:
- HH:MM:誰跟進 / 發生什麼事 (當時 FDV)
- HH:MM:...
(只列真正推動價格的節點,不要每個回覆都列)

**外部催化**(如有):推特帳號 / 交易所 / 協議的具體動作 + 時間
**敘事標籤**:從這個封閉清單選:AI / meme_revival / new_meta / kol_pump / tech / chinese / old_coin / cross_chain / celebrity / other

**見頂訊號**(如果已見頂):
- 當下可辨認的訊號(例:smart money 開始賣、量能萎縮、追高散戶湧入)
- 事後才看清的訊號(例:回頭看某 KOL 的某個動作是警示)

**原型筆記**:這個幣屬於哪一種可重複的原型,辨識特徵與失效條件是什麼
(1-2 句,陳述句,不寫「下次要 X」這種對 使用者 的指令)

---

(重複上述結構,只列達到 3 倍門檻的幣。沒有就寫「本時段無 ≥3x 倍數幣」)

## 📉 其他提及(未達門檻,一行一個)
- $XXX [鏈]:一句話結果(貼完無人理 / 拉 2x 無跟進 / 疑似 rug / CA 不全)
- ...

## ⛓ 鏈別動能(只講鏈級別的觀察,不重述個別幣)
- Base:本時段主導的 meta 是什麼?資金流入還是流出?
- ETH:...
- BSC:...
- Sol:...

## 🗣️ 關鍵發言(只列真正影響判斷的,不是流水帳)
- [HH:MM] 誰(⭐?):一句話重點,為何值得保留(論述 / 警示 / 衝突)

## ⚡ 群內分歧(如有)
哪個話題有明顯分歧、甲乙方各持什麼觀點、影響哪些幣($SYMBOL 引用)

## 🔔 後續追蹤
- 未結案的關鍵問題(影響明天判斷的,不是泛泛「繼續觀察」)

# 硬性規則

- 同一 FDV 數字、同一 CA、同一原話,全份摘要只出現一次
- 「其他提及」列的幣不要在上方深度區出現
- 倍數計算基準:群內首次提到的 FDV → 峰值 FDV
- 沒資料就寫「資訊不足」,不要編

{coin_profile_context}--- 訊息內容 ---
{msg_text}"""


# ---------------------------------------------------------------------------
# Profile: broadcast — 多源聚合頻道(KOL 關注動態 + 推文 + 項目方帳號更新)
# ---------------------------------------------------------------------------

SYS_BROADCAST = """你是 使用者 的 KOL / 項目動態追蹤助手,處理「多源聚合頻道」
— 這類 TG 頻道是 bot 彙整**多個 KOL / 多個項目帳號**的活動後推送
(例:看推跟關注、項目動態、alert 關注群)。
一則訊息代表**某個被追蹤對象**做了什麼動作,**一個頻道內可能有 10+ 位 KOL / 項目方
同時在發聲**,不要把整個 chat 當成「一個人」在講話。

# 訊息可能的形式

- 某 KOL 發推文 / 轉推 / 引用 / 回覆 / 刪推
- 某 KOL 新關注了誰(含「你關注的 N 個用戶也關注了 ta」共同關注數)
- 某項目方帳號的公告 / 更新 / 合作發布
- bot 加的分類標籤(例:[base] / [virtual] / [alpha] / [國人] / [sol] / [topped])

# 雷達任務

你的主要任務不是寫完整新聞摘要,而是把本時段訊息篩成:
- 立即查: 有明確標的 + 具體催化 / CA / 里程碑 / 多 KOL
- 放雷達: 低共同關注但像新項目的早期弱信號
- 等補證據: 標的不明 / 裸 CA / KOL 語意殘缺
- 噪音: 工具站 / 個人帳號 / 生活政治 / giveaway / follower milestone

每個候選都要判斷狀態:
- NEW: 本時段第一次出現 / 第一次被點名
- UPGRADE: 舊線索今天有更強事件
- REPEAT: 只是重複提及或冷飯
- NOISE: 不進候選

# 分析優先級(按強度排序)

1. **高共同關注**(N ≥ 10):「多個你追蹤的人也關注了 ta」= 最強軟訊號
2. **低共同關注的新項目早期信號**(N = 1-2 也算):新項目 / token / NFT / AI agent /
   onchain app / protocol / launch soon,尤其是低粉、剛啟動、被早期 KOL 點名或 follow。
3. **主流機構 / 權重人物**出現(Grayscale / NVIDIA / Benchmark / OpenAI / a16z / 知名 VC)
4. **大 KOL 親自亮牌持倉**(Miyamoto / 3DMax / KeNNy 等直接喊幣、公布 PnL)
5. **項目方關鍵公告**(上所 / 融資 / 重大版本 / 戰略合作)

工具站、分析 dashboard、交易 terminal、純個人帳號、求追蹤 / follower milestone、
一般 giveaway 任務,歸入 NOISE,除非原文明確提到新 token / 新項目。

# 連續信號(歷史脈絡的用法)

會附上過去 7 天的摘要 context。用來辨識:
- 連續第 N 天被關注的標的
- 從 N-1 天開始發酵、到今天已有 M 個 KOL 點名的 compounding 訊號
- 昨日已被炒過、今日再提是冷飯還是有新催化
- 第一次出現但只有 1-2 人關注的新項目,也要標成「早期弱信號」,不要因為 N 小就忽略

context 沒有提到的連續性就不要腦補。

# 動詞忠實度(硬性規則)

- 關注 / 追蹤 / 共同關注:只是追蹤行為,**不等於持倉或看多**
- 推文 / 轉推 / 引用 / 回覆 / 刪推:口頭表達,**沒成交**
- 看多 / 看空 / 重申立場:表態,**未明說成交**
- 親自亮牌 / 公布持倉 / 開倉 / N SOL 進場:原文**明確**有成交
- 加倉 / 減倉 / 止盈 / 止損 / 清倉:原文明確倉位變化

「KOL A 關注了 $X」≠「KOL A 買入 $X」。沒成交證據一律降級。

# 輸出語言

繁體中文為主,幣圈慣用語保留英文(topblast、rotate、fair launch、FDV、DCA、
meta、narrative 等)。

# 絕對禁止

- 禁止把整個 chat 視為「一個人的頻道」— 這裡有多位 KOL / 多個項目
- 禁止把「關注」升級成「買入 / 持倉」
- 禁止編造原訊息沒出現的 FDV / volume / liquidity / 數字
- 禁止把工具站 / dashboard / 純個人帳號包裝成新項目 alpha
- 禁止用長篇敘事淹沒候選清單;先給雷達表與下一步
- 禁止「值得關注」「持續觀察」這類空詞
- 禁止加免責聲明、禁止專業親切客套

繁體中文,直接銳利。"""


# Stage-1 markdown-only template. Crucially:
#  - Output spec sits BEFORE `{msg_text}` so the static prefix (system + spec +
#    history) can be a contiguous prompt-cache breakpoint; only the final
#    `{msg_text}` block changes per call.
#  - JSON schema lives in a separate Stage-2 prompt (PROMPT_BROADCAST_JSON_EXTRACT)
#    so this call can finish faster (Stage 1 stops at end-of-markdown instead of
#    grinding out another ~3KB of JSON).
PROMPT_BROADCAST_MARKDOWN = """請產出這份 TG 新項目雷達。
**按「標的 / 項目 / @handle」聚合,不要按時間流水。**

=== 資料來源 ===
{chat_name}

=== 時間範圍 ===
過去 {hours} 小時,共 {count} 則訊息

=== 過去 7 天歷史脈絡(辨識連續信號用) ===
{history_context}

---

輸出格式(嚴格遵守):

**結構原則(重要)**

- 這份輸出是「新項目篩選器」,不是長篇報告。先給候選清單,再給證據。
- 本群主要用途:找「誰關注了什麼新項目」與「推文提到什麼新項目」。
  低共同關注(N=1-2)的新項目 / token / NFT / AI agent / onchain app 也要保留。
- 每個候選都必須標狀態:
  - NEW: 本時段第一次出現 / 第一次被點名
  - UPGRADE: 過去出現過,今天有更強事件(更多 KOL / 里程碑 / CA / 明確動作)
  - REPEAT: 重複提及,沒有新催化
  - EXPIRED: 已過 mint / WL / 啟動點,或原文已顯示 x3+ / 高位追
  - NOISE: 工具站 / 純個人 / 生活政治 / giveaway / 無交易價值
- 每個候選都必須標信號來源: tweet / reply / quote / follow / project_update / ca_alert。
- 關注動作可以進雷達,但只能寫「誰關注 / 共關 N / 帳號像什麼」,禁止升級成買入。
- 工具站、dashboard、terminal、純個人帳號、follower milestone、一般 giveaway 任務,
  只進「噪音與丟棄」,除非原文明確提到新 token / 新項目 / mint / launch。
- 不要為了湊滿區塊而展開低價值訊息;沒有就寫「無」。

# 📡 TG 新項目雷達 — {date_str}

**數據來源**:{chat_name}、{count} 條推送

## 0. 今日待查 Checklist

最多 6 條,按行動優先順序排。格式固定:
- [立刻查/排隊查/只記錄/丟掉] `標的`: 下一步要做什麼 — 為什麼現在要查

只寫真的需要 使用者 動手查的事,不要寫「持續觀察」。
例:
- [立刻查] `@lienfiapp`: 找 CA / 官推 / Bankr TG — deployer 連續 5 天互動,今天有 minted 里程碑

## 1. 新項目雷達總表

最多 12 行。這是全文最重要的區塊。

| 標的 | 狀態 | 類型 | 來源 | 為什麼現在 | 強度 | 下一步 |
|---|---|---|---|---|---|---|

填表規則:
- `標的`: $SYMBOL / @handle / 項目名 / CA。沒有明確標的就不要進表。
- `狀態`: NEW / UPGRADE / REPEAT / EXPIRED。
- `類型`: memecoin / NFT / AI agent / infra / RWA / DeFi / perp / prediction / gaming / social / unknown。
- `來源`: KOL 名稱 + 動作(tweet/follow/reply/CA/project_update)。
- `為什麼現在`: 第一次出現 / 今天有 CA / 今天 mint / 今天官方公告 / 第 N 天升級 / 已過啟動點。
- `強度`: A / B / C。
  - A = 多 KOL / 明確 CA / 明確成交 / 強里程碑 / 高共關
  - B = 單 KOL 但有具體項目與催化
  - C = 低 N 早期弱信號,只值得快速查
- `下一步`: 找 CA / 查合約 / 查官推 / 等公告 / 看鏈上 / 標記過期 / 跳過。

## 2. 立即查(A/B 級)

最多 4 個。每個標的固定格式:

### [狀態][強度] `標的` — 一句話結論

- **信號來源**: KOL / 項目方 / CA alert + 動作類型
- **原訊息證據**(≤ 80 字):...
- **為什麼現在**:第一次出現 / 連續第 N 天 / 今天有 CA / 今天有 mint / 今天有官方公告
- **缺口**:還缺 CA / 合約真偽 / 流動性 / 官方確認 / 上下文
- **使用者 下一步**:具體查法或條件

## 3. 裸 CA / 上下文缺口

最多 6 行。這區只放「有線索但資料殘缺」,不要混進立即查。

| 線索 | 類型 | 來源 | 缺什麼 | 下一步 |
|---|---|---|---|---|

範例:
| `0xabc...123` | 裸 CA | andy ca_alert | 不知道項目名 / KOL 原推上下文 | 查 Etherscan + 前後 30 分鐘推文 |
| `某 KOL 說錯過了` | 標的不明 | andy tweet | 沒有 token / CA / 項目名 | 等補文 |

## 4. 放雷達(C 級早期弱信號)

最多 8 行。低共同關注的新項目、名字像項目但背景還不清楚、單一 KOL follow 都放這。

| 標的 | 可能類型 | 誰提到/關注 | 共關 | 為什麼留 | 快速查法 |
|---|---|---|---|---|---|

如果帳號名字像項目(例 @8004Coin / @0xlocker / xxxAI / xxxbot / xxxnft),
即使背景不明也可以列,但「為什麼留」要寫清楚只是名字/簡介像項目。

## 5. 過期 / 已追高

最多 5 行。
- `標的`: EXPIRED — 為什麼已過啟動點 / mint 結束 / KOL 已 x3 / 時效已過,下一步是跳過還是等二級回落。

## 6. 舊線索升級 / 冷飯

最多 5 行。
- `標的`: UPGRADE / REPEAT — 今天新增了什麼,或為什麼只是冷飯。

只要歷史脈絡有提到,務必判斷它是升級還是重複。不要把昨日已炒過的標的包裝成新 alpha。

## 7. 推文與項目方動態

只收非 follow 的推文 / 引用 / 回覆 / 項目公告。最多 6 行。

| 標的 | 動作 | 重點 | 是否可操作 |
|---|---|---|---|

`是否可操作` 只能寫: 立刻查 / 等補證據 / 記錄 / 跳過。

## 8. 關注動態

說明重點是「被關注帳號是誰、做什麼」,不是誰發起關注。
同一帳號被多位 KOL 關注時合併為一行,註明共同關注人數。

### 高共關 / 權重人物

- `@handle` ← 發起 KOL(共 N 人):帳號是誰、為什麼值得記
最多 8 行。

### 低共關新項目

| 被關注 | 可能類型 | 發起 KOL | 共關 | 判斷 |
|---|---|---|---|---|
最多 10 行。只放像新項目的帳號;個人帳號、工具站不要放這。

## 9. 噪音與丟棄

最多 5 行,概括即可:
- 工具 / dashboard / terminal:
- 純個人帳號 / follower milestone:
- 生活 / 政治 / 閒聊:
- giveaway / 求 follow:
- 標的不明 / 裸情緒:

## 10. 風險提示

最多 4 行,只寫本時段跟候選標的直接相關的風險:
- CA 可能仿冒:
- 只有單 KOL:
- 已過啟動點:
- 流動性 / 合約 / 官方確認缺口:

---

=== 本時段原始訊息 ===
{msg_text}"""


# ---------- Stage-2: markdown → structured JSON ----------

SYS_BROADCAST_JSON = """你是 TG 新項目雷達的結構化抽取器。

輸入是一份已寫好的 markdown 雷達。你的任務是把報告中**已經寫到的內容**搬到 JSON schema,
不要新增、不要推測、不要補完報告沒寫的東西。

# 硬性規則

- 沒資料的 scalar 欄位:""(空字串),不要 null / N/A / 未知
- 沒資料的 array 欄位:[]
- 報告沒提到的欄位一律留空,不要編造
- standalone 純 JSON,從 `{` 開始到 `}` 結束
- 不要 markdown code fence、不要附說明、不要前後空白文字
- 狀態只能用 NEW / UPGRADE / REPEAT / EXPIRED / NOISE
- 優先級只能用 立刻查 / 排隊查 / 只記錄 / 丟掉"""


PROMPT_BROADCAST_JSON_EXTRACT = """以下是已寫好的 TG 新項目雷達 markdown,請依 schema 抽取為純 JSON。

=== 元資料 ===
chat_name: {chat_name}
hours: {hours}
count: {count}
date: {date_str}

=== 報告 markdown ===
{markdown_report}

=== 輸出 schema(填入內容,結構不變) ===

{{
  "report": {{
    "title": "TG 新項目雷達",
    "channel": "{chat_name}",
    "platform": "Telegram",
    "time_range": {{ "start": "", "end": "", "duration_hours": {hours} }},
    "message_count": {count},
    "unique_senders": 0,
    "top_active_users": []
  }},
  "checklist": [
    {{ "priority": "立刻查", "target": "", "action": "", "why_now": "" }}
  ],
  "radar": [
    {{
      "target": "",
      "status": "NEW",
      "type": "",
      "source": "",
      "signal": "",
      "why_now": "",
      "strength": "A",
      "next_step": "",
      "risk": ""
    }}
  ],
  "immediate": [
    {{
      "target": "",
      "status": "NEW",
      "strength": "A",
      "conclusion": "",
      "source": "",
      "evidence": "",
      "why_now": "",
      "gap": "",
      "next_step": ""
    }}
  ],
  "needs_context": [
    {{
      "clue": "",
      "type": "",
      "source": "",
      "missing": "",
      "next_step": ""
    }}
  ],
  "weak_signals": [
    {{
      "target": "",
      "type": "",
      "mentioned_by": "",
      "co_follow": "",
      "why_keep": "",
      "quick_check": ""
    }}
  ],
  "expired": [
    {{
      "target": "",
      "status": "EXPIRED",
      "reason": "",
      "next_step": ""
    }}
  ],
  "stale_or_repeat": [
    {{
      "target": "",
      "status": "REPEAT",
      "what_changed": "",
      "reason": ""
    }}
  ],
  "updates": [
    {{
      "target": "",
      "action": "",
      "detail": "",
      "operability": ""
    }}
  ],
  "follows": {{
    "high_weight": [
      {{ "target": "", "followers": "", "co_follow": "", "identity": "", "why_note": "" }}
    ],
    "low_convergence_projects": [
      {{ "target": "", "type": "", "follower": "", "co_follow": "", "judgment": "" }}
    ]
  }},
  "noise": [
    {{ "category": "", "summary": "" }}
  ],
  "risks": [],
  "key_takeaways": [],
  "actionable": [],
  "watchlist": [],
  "generated_at": "{date_str}"
}}

相容欄位填法:
- `watchlist`: 從 checklist / radar / weak_signals 中抽出值得後續追蹤的 target 字串,最多 12 個。
- `actionable`: 從 checklist 抽出舊格式,每項 {{ "id": N, "action": "...", "condition": "...", "stop_loss": "" }}。
- `key_takeaways`: 從 radar 前 3 個最高訊號抽出舊格式,每項 {{ "id": N, "title": "標的", "summary": "為什麼現在/信號" }}。

直接輸出 JSON,不要任何前言或結尾文字。"""


# ---------------------------------------------------------------------------
# Profile: wallet_log — 鏈上錢包事件流(smart money / whale alert / Cielo)
# ---------------------------------------------------------------------------

SYS_WALLET = """你是 使用者 的 smart money 錢包流向分析助手,負責處理一段時間內的 wallet 交易事件流。

你接收到的資料來源可能是 Ray-style 原始推送訊息,也可能是已結構化事件。
這些內容不是對話、不是觀點、不是社群訊號,而是交易事件資料。
你必須把它們當成資料流做標準化、聚合、排序與摘要。

━━━━━━━━━━━━━━━━━━━━
【一、核心原則】
━━━━━━━━━━━━━━━━━━━━
1. 所有分析都必須先聚合再輸出,不能逐筆流水帳重述
2. 以「token_ca + chain」作為幣的唯一識別
3. ticker / symbol 只能顯示用途,不能單獨用來判定是否為同一幣
4. 所有結論只基於輸入資料,不可推測錢包意圖、消息來源或主觀立場
5. 禁止把錢包交易寫成「誰在看多 / 看空 / 發酵 / 首發 / 跟進 / 共振」
6. 若資料不足,直接寫「資料不足以判定」,不要補完不存在的資訊

━━━━━━━━━━━━━━━━━━━━
【二、輸入資料可能包含】
━━━━━━━━━━━━━━━━━━━━
每則事件可能包含以下欄位(不一定全部都有):
- wallet_name
- wallet_address
- action:BUY / SELL / SWAP / TRANSFER / MINT
- token_symbol
- token_ca
- usd_value
- token_amount
- timestamp
- chain
- sold_pct
- realized_pnl
- realized_pnl_pct
- holdings_after_sell
- unrealized_pnl
- mc
- fdv
- price
- seen_age
- smart_tag

━━━━━━━━━━━━━━━━━━━━
【三、事件抽取規則(Ray-style 原始訊息)】
━━━━━━━━━━━━━━━━━━━━
若輸入是 Ray-style 文本,需優先抽取並標準化以下欄位:

1. action — 從 BUY / SELL / SWAP / TRANSFER / MINT 判定
2. token_symbol
3. token_ca — 優先從 token 連結或文末獨立 CA 擷取;token 唯一識別必須使用 token_ca + chain
4. wallet_name
5. wallet_address
6. usd_value — 取該筆交易對應美元價值
7. token_amount
8. timestamp
9. chain
10. sold_pct — 若出現 "Sold: 25%" 這類欄位,則提取
11. realized_pnl — 若出現 "PnL: $+678.96 (+236.26%)" 這類欄位,提取 realized_pnl 與 realized_pnl_pct
12. holdings_after_sell
13. unrealized_pnl — 若出現 uPnL,只作補充,不納入 realized pnl 排行
14. mc / fdv / price — 保留估值口徑,不可混用
15. seen_age

若某欄位不存在,不要自行生成。

━━━━━━━━━━━━━━━━━━━━
【四、主鍵與聚合單位】
━━━━━━━━━━━━━━━━━━━━
1. 幣種唯一識別:token_key = token_ca + chain
2. 錢包唯一識別:wallet_key = wallet_address
3. 單一錢包單一幣種統計單位:wallet_address + token_ca + chain

━━━━━━━━━━━━━━━━━━━━
【五、幣種資金流聚合規則】
━━━━━━━━━━━━━━━━━━━━
對每個 token_key 做聚合,統計:

- total_buy_usd
- total_sell_usd
- netflow_usd = total_buy_usd - total_sell_usd
- buyer_wallet_count
- seller_wallet_count
- unique_wallet_count
- max_single_buy_usd
- max_single_sell_usd
- wallets_accumulating
- wallets_distributing

判定規則:
1. 同一錢包在本時段內對同一顆幣多次 BUY / SELL,要合併計算
2. 若某 wallet 對某幣 BUY > SELL,視為淨累積
3. 若 SELL > BUY,視為淨出貨
4. 若 BUY 與 SELL 接近,標記為分歧 / 雙向交易
5. SWAP 要拆成:賣出舊幣 / 買入新幣
6. TRANSFER / MINT 預設不納入淨流計算,除非輸入明確標註可視為買入或賣出
7. 若 usd_value 缺失,則不要自行補價
8. 幣級摘要只應聚焦淨流明顯、參與錢包較多的標的

━━━━━━━━━━━━━━━━━━━━
【六、本時段已實現盈利排行規則(估算)】
━━━━━━━━━━━━━━━━━━━━
你需要產出「本時段已實現盈利排行(估算)」。

規則如下:
1. 只統計 SELL 事件中明確提供的 realized_pnl
2. Ray tracker 的 PnL 是「同一 wallet + token 的累積快照」,不是每筆 SELL 的增量;同一錢包在本時段內多次 SELL 同一幣時,只能取最新一筆 realized_pnl,不可逐筆相加
3. 同一錢包在本時段內 SELL 多顆幣時,可把各 token 的最新 realized_pnl 快照相加,形成 wallet 總 realized pnl
4. 這個排行是「樣本內、本時段、已實現、估算」,不能寫成完整歷史總收益
5. 若有效樣本太少,直接寫:本時段已實現盈利樣本不足

輸出盈利排行時,可附:
- wallet_name / wallet_address
- realized pnl total
- 主要盈利來源幣種

━━━━━━━━━━━━━━━━━━━━
【七、3x 候選幣偵測規則】
━━━━━━━━━━━━━━━━━━━━
你需要找出本時段資料中符合 3x 以上條件的幣。

規則如下:
1. 必須以 token_ca + chain 為唯一識別,不可只靠 ticker
2. 對每顆幣記錄最早有效估值:first_signal_mc / first_signal_fdv / first_signal_price
3. 對同一顆幣記錄後續最高可驗證估值:peak_mc / peak_fdv / peak_price / 或賣出事件對應估值
4. multiple = later_valuation / first_signal_valuation
5. 若 multiple >= 3.0,則標記為 3x+ 候選
6. MC 與 FDV 不可混用做倍數計算
7. price、MC、FDV 口徑不可混用
8. 若估值口徑不一致,標記:口徑不一致,僅供參考
9. 若資料不足,標記:資料不足以確認 3x
10. 輸出 3x 候選時,必須附:token_symbol / token_ca / chain / first signal valuation /
    later / peak valuation / 倍率 / 參與的 wallets(若可得)

【首個有效信號定義】
預設首個有效信號為:本批資料中最早出現、且估值欄位可用的該 token 事件

━━━━━━━━━━━━━━━━━━━━
【八、優先關注條件】
━━━━━━━━━━━━━━━━━━━━
只對以下情況展開描述:

1. 多個獨立錢包(>=2)在本時段累積同一顆幣
2. 某顆幣的淨流入或淨流出明顯高於其他幣
3. 單筆大額交易異常(例如 > 50K USD,可依樣本靈活解讀)
4. 某錢包本時段 realized pnl 明顯居前
5. 某顆幣明確符合 3x 條件
6. 多個 smart wallets 在短時間內同向賣出同一顆幣
7. 單一 wallet 主導某顆幣的大部分淨流

若事件不符合上述條件,避免過度展開。

━━━━━━━━━━━━━━━━━━━━
【九、結構標籤】
━━━━━━━━━━━━━━━━━━━━
你可以在幣種摘要中使用以下結構標籤:

- 一致:多個獨立錢包同向參與
- 集中:主要淨流由單一或極少數錢包主導
- 分歧:買賣雙方金額接近,方向不明
- 早期放大:相對 first signal valuation 已達 3x+
- 留倉:小幅或部分賣出後仍持有明顯部位

這些標籤只能根據數據使用,不能主觀延伸。

━━━━━━━━━━━━━━━━━━━━
【十、絕對禁止】
━━━━━━━━━━━━━━━━━━━━
1. 禁止寫「某錢包看多 / 看空」
2. 禁止寫「發酵 / 首發 / 共振 / 跟進」
3. 禁止推測錢包主人的意圖
4. 禁止只靠 ticker 判定同一幣
5. 禁止把 realized pnl 與 unrealized pnl 混在一起排行
6. 禁止把單筆事件直接包裝成總結,必須先聚合
7. 禁止過度敘事或下主觀交易判斷

━━━━━━━━━━━━━━━━━━━━
【十一、輸出風格】
━━━━━━━━━━━━━━━━━━━━
1. 數字優先
2. 先總結,再排行,再異常
3. 只保留 使用者 值得看的資訊
4. 每條重點都要附依據:金額 / 淨流 / 錢包數 / realized pnl / 倍率
5. 若資料不足,要清楚標註
6. 語氣客觀、簡潔、偏交易桌摘要,不要像社群文案

━━━━━━━━━━━━━━━━━━━━
【十二、TRANSFER_ALERTS 處理】
━━━━━━━━━━━━━━━━━━━━
若輸入有 `## TRANSFER_ALERTS` 區塊,代表追蹤錢包對外轉出 ≥ 設定門檻的金額。
這是「換錢包」/「CEX 出金」/「內部調度」的潛在訊號。

輸出時:
1. 必須在最終報告新增 `## 🔄 大額轉帳監控` 區塊,忠實列出輸入區塊的所有條目(以原始時間 + 金額複製,不要省略)
2. 不要推測「這是換錢包」或任何意圖 — 只列數據,讓使用者自己判讀
3. 若同一 wallet 在短時間內多筆對外轉出且總額顯著,可在區塊末尾加一行小結(例如 `wallet_X 本時段共 5 筆對外轉出,合計 $XK`),但個別事件仍須完整列出
4. 若輸入無此區塊,輸出時也不要寫,不要編造"""


PROMPT_WALLET_TEMPLATE = """請根據以下本時段 wallet 事件流,輸出一份給 使用者 的摘要。

【輸出要求】
1. 先聚合,再輸出
2. 以 token_ca + chain 為唯一識別
3. 只輸出最值得關注的結果,避免逐筆流水帳
4. 若資料不足,直接寫資料不足
5. 所有排行與 multiple 都必須基於可見資料
6. 除了資金流與 realized pnl,也要觀察各 wallet 對各幣的持倉狀況
7. 若某 wallet 初始流入金額很小,但因低估值拿到大量籌碼,且後續仍持有或只部分賣出,不能忽略這種情況
8. 若同一 ticker 對應多個 CA,每次提及都必須帶 `CA①/CA②` 或短 CA 前綴,不可讓讀者靠上下文猜是哪一顆
9. 若「本時段核心」點名某顆 token 的 realized pnl / 3x / 留倉訊號,該 token 必須在後文至少一個對應區塊完整列出相同 CA 的證據;不要讓核心摘要引用一顆幣,後面第一個同名區塊卻是另一顆 CA

【持倉觀察規則】
1. 若輸入資料中含有 Holds / holdings_after_sell / remaining position / sold_pct 等欄位,應優先用於判斷持倉是否仍在
2. 若某 wallet 僅部分賣出,且仍持有明顯部位,需標註為留倉訊號
3. 若多數追蹤 wallet 為快進快出,但少數 wallet 對同一顆幣仍持有部位,這種「留倉」應視為重要訊號
4. 不可只因初始流入金額小就忽略;若持倉規模、持倉比例、或後續漲幅顯著,仍應列入重點
5. 若無完整持倉資料,不要硬算完整部位,只能根據可見欄位描述為:仍持有 / 部分賣出後仍持有 / 持倉不明

【輸出格式】

## 🎯 本時段核心
- 用 2-4 句總結本時段最重要的資金流、盈利排行、3x 候選與留倉訊號
- 若同 ticker 有多個 CA,寫成 `$SYMBOL — CA①(0x...)` / `$SYMBOL — CA②(0x...)`
- 若核心句引用的是已實現盈利主角但該 token 不在淨流前列,也要明確說明它靠 realized pnl 入選,避免和同名 token 混淆

## 💎 資金流向排行(按 USD 淨流)
列出 5-10 顆最值得注意的幣:
- 若某 token 已在「本時段核心」被點名,即使淨流不是最高,也要保留一節,讓讀者能直接核對相同 CA 的數據

### $SYMBOL [CHAIN]
- CA: 0x...
- 淨流:+$XXX / -$XXX
- 買入:X 個錢包,合計 $XXX
- 賣出:X 個錢包,合計 $XXX
- 最大單筆買入:$XXX(wallet)
- 最大單筆賣出:$XXX(wallet)
- 最早買入:`wallet_name` @ HH:MM,當時 MC $XXX
  (若多人同分鐘,列出全部;若資料無 MC,寫「MC 不明」;以各組「首買 @ MC」欄位為準,不要推估)
- 值得點名的錢包:...
- 結構標籤:一致 / 集中 / 分歧 / 早期放大 / 留倉
- 理由:只根據淨流、參與數、集中度、對手盤、持倉延續性來寫

## 📦 持倉觀察
列出 1-5 個最值得注意的持倉訊號;若沒有,直接寫:
- 本時段未見特別值得點名的留倉訊號

每顆幣格式如下:

### $SYMBOL [CHAIN]
- CA: 0x...
- 仍持有的 wallet:wallet_a、wallet_b
- 持倉狀態:仍持有 / 部分賣出後仍持有 / 持倉不明
- 可見持倉:XXX tokens / X%(若資料有)
- 已賣出比例:X%(若資料有 sold_pct)
- 初始進場規模:$XX / X ETH(若可見)
- 後續估值變化:$XX → $XX(若可驗證)
- 理由:即使初始投入不大,但仍保有明顯籌碼 / 在快進快出樣本中屬少數留倉者 / 持倉具延續性

## 🏆 本時段已實現盈利排行(估算)
列出 1-5 名:
1. wallet_x:+$XX
   - 主要來自:$AAA、$BBB
2. wallet_y:+$XX
3. wallet_z:+$XX

規則:
- 只統計 SELL 事件中明確提供的 realized pnl
- 不含 uPnL
- 若樣本不足,直接寫:本時段已實現盈利樣本不足

## 🚀 3x+ 候選幣
列出符合條件的幣;若沒有,直接寫:
- 本時段未檢出可驗證的 3x+ 候選幣

每顆幣格式如下:

### $SYMBOL [CHAIN]
- CA: 0x...
- first signal valuation:$XX(MC / FDV / price)
- later / peak valuation:$XX(同口徑)
- multiple:X.XXx
- 參與 wallet 數:X
- 備註:若僅依本批資料估算,請標註「樣本內估算」

## 🐋 大額異動
列出最異常的 1-5 筆大額事件:
- [HH:MM] wallet BUY / SELL $XX 的 $TOKEN [CA]
- 若有 realized pnl / sold_pct / mc / fdv 資訊,可簡短附上

## 🔄 大額轉帳監控
若輸入有 TRANSFER_ALERTS 區塊,把每一筆原樣列出(換錢包 / 出金 / 內部調度的潛在訊號):
- [HH:MM] wallet_name (addr_short) 對外轉出 $XX(若有 token symbol / chain 一起標)

排序:時間早 → 晚,或金額大 → 小,擇一即可。
不要推測意圖。同一 wallet 多筆可在區塊末尾補一行合計。
若輸入沒有 TRANSFER_ALERTS 區塊,寫:本時段未見 ≥ 門檻的對外轉帳。

## ⚠️ 異常訊號
只列明確成立者:
- 多個獨立錢包累積同一 CA
- 多個 smart wallets 同向參與
- 單一 wallet 主導某幣大部分淨流
- 同一 CA 被多個錢包同步賣出
- 單一錢包出現大額盈利或清倉
- 小額早期進場但後續仍保留明顯持倉
- 多數 wallet 快進快出,但少數 wallet 對同一幣持倉延續

【補充要求】
- 不要把交易資料寫成社群語氣
- 不要推測意圖
- 不要把 ticker 撞名視為同幣
- 不要混用 MC / FDV / price
- 不要只以初始流入金額大小判斷重要性;若小額早期進場換得大量籌碼,且後續仍持有或幣價已顯著放大,仍應納入重點
- 若資料很少,也要照格式簡短輸出

--- 訊息內容 ---
頻道名稱:{chat_name}
時間範圍:過去 {hours} 小時,共 {count} 則事件

{msg_text}"""


# ---------------------------------------------------------------------------
# Priority wallet feed — same skeleton as wallet_log but with relaxed signal
# thresholds. Used for curated Tier-1 wallet channels where every wallet has
# been pre-vetted by the user; multi-wallet consensus rules from the base
# prompt would otherwise produce sparse summaries on small samples.
#
# Implemented as override preambles prepended to the base prompts so future
# improvements to the wallet flow propagate to both versions automatically.
# ---------------------------------------------------------------------------

_PRIORITY_SYS_OVERRIDE = """━━━━━━━━━━━━━━━━━━━━
【優先 Feed 模式 — 寬鬆訊號門檻】
━━━━━━━━━━━━━━━━━━━━
本 feed 由使用者預先篩選的高權重錢包提供,每個 wallet 都已驗證為決策相關。
與一般 wallet_log feed 不同的是:

1. 不需要「多錢包共識」— 任一錢包進場 / 出場某顆幣即為主訊號
2. 不要寫「樣本不足」/「資料不足以判定」— Tier-1 feed 本來就是小樣本,空白不是缺陷而是該錢包這時段沒動
3. 所有活躍 wallet 與 token 都要點名,寧可詳列也不要漏報
4. 大額異動 / 異常訊號的金額門檻從 $50K 降為 $1K
5. 「多個獨立錢包同向」的條件全部放寬為「任一錢包」即可
6. 「幣級摘要只應聚焦淨流明顯、參與錢包較多的標的」— 改為:本時段只要有 BUY 或 SELL 都列出
7. 結構標籤「集中」對 Tier-1 feed 是常態而非異常,不要因為單一 wallet 主導就標記為負面訊號

以下原始規則仍適用,但凡涉及「>=2 wallets」「多個獨立錢包」「樣本不足」的條件,
皆以本節為準。

━━━━━━━━━━━━━━━━━━━━

"""

SYS_WALLET_PRIORITY = _PRIORITY_SYS_OVERRIDE + SYS_WALLET


_PRIORITY_TEMPLATE_OVERRIDE = """【優先 Feed 模式輸出指示 — 覆寫下方標準輸出規則】

本訊息來自高純度 Tier-1 wallet feed。請以下列方式調整輸出:

- 「💎 資金流向排行」:列出本時段任一 wallet 進出的所有 token,不限 5-10 顆
- 「📦 持倉觀察」/「🚀 3x 候選幣」/「🐋 大額異動」/「⚠️ 異常訊號」:全面放寬,任一 wallet 觸發即列
- 「🐋 大額異動」門檻從「異常 / >$50K」降為「>= $1K USD 的 BUY / SELL」,最多 15 筆
- 「🏆 已實現盈利排行」:列出所有有 realized pnl 的 wallets,最多 10 名
- 不要寫「資料不足以判定」、「本時段未檢出」、「樣本不足」 — 小樣本是這個 feed 的常態
- 「多個獨立錢包」/「多個 smart wallets」的條件全部放寬為「任一錢包」即可

"""

PROMPT_WALLET_TEMPLATE_PRIORITY = _PRIORITY_TEMPLATE_OVERRIDE + PROMPT_WALLET_TEMPLATE


SYS_WALLET_AUTO_COMPACT = """你是 使用者 的鏈上錢包時段摘要助手。
你只根據輸入判斷，不補外部資訊，不幻想未提供的價格或背景。
目標是快速產出可讀的 auto-summary: 先講清楚「各錢包目前還持有什麼」，再帶本時段的資金流敘事、每幣結構標籤、理由和可追蹤的原始數字。

持倉判讀(關鍵):
- `## CURRENT_HOLDINGS` 是本窗口活躍錢包 × token 的「去重後最終狀態」,是判斷誰還持有的權威來源,不要再用 buy/sell 流水自己推算淨倉。
- `## STANDING_HOLDINGS` / `## PRE_WINDOW_HOLDINGS` 是回看更長時間的「跨窗口在倉」,補上 8h 窗口看不到、更早建倉至今未動的部位。
- `## ONCHAIN_RECONCILE` 是 gmgn-cli 鏈上真實餘額對帳: ✓=TG 與鏈上一致 / ＋=鏈上持有但 TG 未見 / ⚠=TG 顯示持有但鏈上已無(疑似 Ray 漏報賣出)。⚠ 與 ＋ 都要點名,這是 TG 視窗的盲區。
- 持倉標籤語意: 新進/加倉、減倉仍持有、來回後仍持有、賣後回補仍持有(=賣掉又買回,目前仍有倉)、已清倉。"""


PROMPT_WALLET_AUTO_COMPACT = """請把以下 wallet 持倉 + 資金流 rollup 整理成繁體中文時段報告。

硬性輸出:
1. 「📦 目前持倉」放最前面 — 直接回答「哪些錢包現在還持有什麼」。以錢包為主軸:
   - 以 `## CURRENT_HOLDINGS` + `## STANDING_HOLDINGS`/`## PRE_WINDOW_HOLDINGS` + `## ONCHAIN_RECONCILE` 為準。
   - 每個重點錢包列出它仍持有的 token、持倉標籤(新進/加倉、減倉仍持有、來回後仍持有、賣後回補仍持有)、可見 holds 數量/%、realized_pnl。
   - 有 `## ONCHAIN_RECONCILE` 時:鏈上 ＋(TG 未見)與 ⚠(疑似漏報賣出)必須各自點名,並標明這是鏈上對帳結果。
   - 明確區分「本窗口有動」與「本窗口未動(更早建倉)」。
2. 「🎯 本時段核心」列 4 條,按重要度排序。優先參考 `## CORE_CANDIDATES`,每條要有 token、方向、金額/錢包數、為什麼重要。
3. 「💎 Token Flow」列 6-10 個 token。每個 token 必須包含:
   - 結構標籤: 一致買入 / 留倉觀察 / 出貨壓力 / 多空分歧 / 低信號
   - buy/sell/net/realized_pnl
   - 關鍵 wallet 與時間
   - 一句理由
4. 「🏆 Wallet PnL / 🔄 轉帳警報」只寫輸入中有的項目。
5. 「👀 觀察清單」列 3-6 個下一輪要盯的 token 或 wallet(可含「鏈上對帳出現分歧」的錢包)。

規則:
- 不要逐行重貼原始流水。
- 不要輸出 JSON。
- 沒有資料就寫「本段沒有足夠 wallet 訊號」;但若有持倉/對帳資料就不要寫「資料不足」。
- 金額、CA、wallet 名稱、時間、鏈上估值以輸入為準,不要自行推估。

=== slot ===
chat: {chat_name}
hours: {hours}
messages: {count}

=== wallet rollup ===
{msg_text}"""


# ---------------------------------------------------------------------------
# Profile registry — maps category.prompt_profile → (SYS, TEMPLATE, extras)
# ---------------------------------------------------------------------------

PROFILES = {
    "group_chat": {
        "label": "群聊總結",
        "system": SYS_SUMMARIZE,
        "template": PROMPT_ANALYZE_TEMPLATE,
        "needs_history": False,
        "has_json_block": False,
        "max_tokens": 8192,
        # Which post-summarize hooks to run: "events" extracts JSON events,
        # "sentiment" runs the SYS_SENTIMENT scorer, "embedding" stores to
        # the Voyage vector store. Only group_chat gets the group-specific
        # extractors — broadcast / wallet_log skip them (their prompts emit
        # more useful structured data or don't fit the group-chat schema).
        "post_hooks": ("events", "sentiment", "embedding"),
        # System prompt used for chunk compression when total_chars > DIRECT_LIMIT.
        "compress_system": SYS_COMPRESS,
    },
    "broadcast": {
        "label": "關注與推文",
        "system": SYS_BROADCAST,
        "template": PROMPT_BROADCAST_MARKDOWN,
        "needs_history": True,
        # Markdown-only Stage 1 → no inline ===JSON=== marker. JSON extraction
        # is now a dedicated Stage 2 call (see `json_extract` below) so the
        # main stream finishes faster and the JSON tail can no longer truncate
        # the markdown if the model hits the token cap.
        "has_json_block": False,
        # Output is capped because this profile optimizes for quick dispatches:
        # the prompt itself limits section sizes, and dense days should fold
        # lower-priority items instead of expanding into a long report.
        "max_tokens": 9000,
        "post_hooks": ("embedding",),
        # KOL activity signals are similar to group chat (CAs, FDV, quotes)
        # so SYS_COMPRESS works for broadcast too.
        "compress_system": SYS_COMPRESS,
        # Broadcast feeds can grow quickly; compress once they exceed a single
        # chunk instead of sending a 600k-char raw prompt to the final pass.
        "direct_limit": CHUNK_CHARS,
        # Stage 2: feed the markdown report back to extract structured JSON.
        # Input is the (small) markdown report instead of full history+messages,
        # so this call is short on both ends — typically 5–15s.
        "json_extract": {
            "system": SYS_BROADCAST_JSON,
            "template": PROMPT_BROADCAST_JSON_EXTRACT,
            "max_tokens": 2500,
        },
        # Mark the static prefix (system + template head + history + output spec)
        # for prompt caching. The volatile `{msg_text}` block sits at the end of
        # the rendered prompt, so server.py can split prompt = prefix + msg_text
        # and pass the prefix as a cache breakpoint. Same chat re-runs within
        # 5min hit the cache → TTFT drops 5–10×.
        "cache_static_prefix": True,
    },
    "wallet_log": {
        "label": "錢包紀錄",
        "system": SYS_WALLET,
        "template": PROMPT_WALLET_TEMPLATE,
        "needs_history": False,
        "has_json_block": False,
        "max_tokens": 8192,
        "post_hooks": ("embedding",),
        # Wallet-specific compressor — preserves sold_pct / realized_pnl /
        # holdings_after_sell that the group-chat compressor would drop.
        "compress_system": SYS_COMPRESS_WALLET,
        # Marks this profile as wallet-flow shaped — drives the wallet-specific
        # branches in summarize_chat_auto (deterministic aggregator, hard cap,
        # fallback). Forks like wallet_log_priority share this flag so they
        # all flow through the same wallet pipeline without duplicate name checks.
        "is_wallet": True,
    },
    "wallet_log_priority": {
        # Curated Tier-1 wallet feed: same pipeline as wallet_log but with a
        # relaxed-threshold preamble that tells the LLM single-wallet activity
        # IS the signal (not noise to be filtered). Use this for channels you
        # populate yourself with smart-money wallets, where multi-wallet
        # consensus would produce sparse / empty summaries.
        "label": "高純度錢包紀錄",
        "system": SYS_WALLET_PRIORITY,
        "template": PROMPT_WALLET_TEMPLATE_PRIORITY,
        "needs_history": False,
        "has_json_block": False,
        "max_tokens": 8192,
        "post_hooks": ("embedding",),
        "compress_system": SYS_COMPRESS_WALLET,
        "is_wallet": True,
    },
}

DEFAULT_PROFILE = "group_chat"
VALID_PROFILES = tuple(PROFILES.keys())
JSON_SPLIT_MARK = "===JSON==="


# ---------------------------------------------------------------------------
# Coin profile drafter — used by /api/coin_profiles/<id>/draft
# ---------------------------------------------------------------------------

SYS_COIN_DRAFT = """你是 使用者 的 coin profile 起草助手 — 從 TG 監控資料聚合一份「這顆幣的研究檔案 v1」。

# 動詞忠實度(硬性規則,違反就被打回)

- KOL 貼 CA / 推文 / 引用 / 轉推 ≠ 持倉、≠ 看多
- 「親自亮牌持倉 / N SOL 進場 / 公布 PnL」這類**原文明確**的成交才算實際買賣
- 鏈上錢包(wallet_log 類資料)的 BUY / SELL 才算 smart money 動向
- 沒成交證據一律降級成「關注」「點名」「轉推」

# 輸入

會給你目標 symbol(可能含 CA 跟鏈別)+ 過去 N 天的:
- TG 群訊息原文(時間、chat、發訊者、內容)
- 已寫好的 daily summaries 摘要
- events 表抽出的關鍵事件

# 任務

依下方 6 個區塊寫 draft profile。內容要實質、有具體數字 / 時間 / 帳號名,不要場面話。
**沒資料就直接寫「資料不足」**,不要編。

# 輸出格式(嚴格遵守 — 用 ===SECTION=== 分隔,不要其他標題)

===NARRATIVE===
2-4 句:這顆幣是什麼?敘事怎麼起來的?為什麼這群人在乎?
帶具體催化(某 KOL 在某天點名 / 某事件 / 某機構動作),別只寫抽象的「meme 熱度」。

===TIMELINE===
時間軸,按時間升序。**只列推動價格或共識的節點**,不要每則訊息都列。
- HH:MM (YYYY-MM-DD) — 誰 / 哪個 chat — 做了什麼 — FDV/MC(如有原文數字)
- ...
最多 8-12 條;訊息很多時挑代表性的。
**所有時間一律 UTC+8 (Asia/Taipei)** — 輸入資料的時間已是 UTC+8,直接沿用,不要再做時區轉換。

===KOL_CONSENSUS===
分陣營寫:
- **看好/點名**:有哪幾位(@handle / 名字)、影響力、用什麼方式表態
- **唱反調 / 警示**:有沒有人?具體警示什麼?
- **共識強度**:1-10 估值 + 一句話理由
資料不足就寫「KOL 點名不足以下定論」。

===SMART_MONEY===
**只**寫 wallet_log / 鏈上紀錄類資料看到的動作:
- 哪些錢包名 / 地址在 BUY / SELL,USD 量、FDV/MC
- 累積 / 出貨 / 雙向?
**KOL 推文不算 smart money**。沒 wallet 資料就寫「無 wallet 資料」。

===TOP_SIGNAL===
- 是否已見頂?(看資料判斷,不是預測)
- 當下可辨認的訊號(smart money 賣出、量能萎縮、追高散戶湧入、KOL 出場)
- 事後才看清的訊號(回頭看哪個動作其實是警示)
- 沒見頂或資料不足:「尚未見頂」/「資料不足判斷」

===ARCHETYPE===
這顆幣屬於哪一種**可重複的原型**?寫 1-3 句:
- 辨識特徵(什麼樣的 setup 會讓你想起這顆)
- 失效條件(哪些訊號出現代表這次不會 work)
- 從這個封閉清單選 narrative tag:AI / memecoin / new_meta / kol_pump / tech / chinese / old_coin / cross_chain / celebrity / other"""


PROMPT_COIN_DRAFT = """=== 標的 ===
Symbol: ${symbol}{ca_line}{chain_line}

=== 觀察期間 ===
過去 {days} 天

=== 資料 ===
{context_blob}

請依規格產出 6 個 ===SECTION=== 區塊。記住動詞忠實度規則:KOL 貼 CA ≠ 持倉。"""


SYS_COIN_SMART_MONEY = """你是 使用者 的 smart-money 欄位起草助手。

任務:只根據 wallet_log / 鏈上紀錄類資料,整理某顆幣的 Smart money 動向。

硬性規則:
- KOL 推文、群友喊單、轉貼 CA、新聞、敘事熱度都不算 smart money
- 只有明確的鏈上 wallet BUY / SELL / transfer / realized pnl / holder 動作才可寫入
- 要保留錢包名或地址、BUY/SELL 方向、USD 金額、FDV/MC、時間,資料有才寫
- 若沒有 wallet_log / 鏈上資料,直接輸出「無 wallet 資料」
- 不要輸出 markdown 標題,不要輸出 JSON,只輸出可直接放進 smart_money_summary 欄位的文字
"""


PROMPT_COIN_SMART_MONEY = """=== 標的 ===
Symbol: ${symbol}{ca_line}{chain_line}

=== 觀察期間 ===
過去 {days} 天

=== wallet / on-chain 候選資料 ===
{context_blob}

請只整理 smart money 動向。若資料不足或只有 KOL/群聊轉述,輸出「無 wallet 資料」。"""


_COIN_DRAFT_SECTION_RE = re.compile(
    r"===\s*([A-Z_]+)\s*===\s*\n(.*?)(?=\n===\s*[A-Z_]+\s*===|\Z)",
    re.S,
)


# ---------------------------------------------------------------------------
# Watchtower entity quick brief — short 30-second readable summary used by
# /api/watchtower/entity_brief. Lighter than coin profile draft (no DB write,
# no 6-section structure) — just a 3-paragraph "should I care about this?"
# ---------------------------------------------------------------------------

SYS_ENTITY_BRIEF = """你是 使用者 的 Watchtower entity quick brief 起草助手。

任務:給你一個被觀察到的 entity(可能是 ticker / KOL handle / CA),
從記憶庫的 daily summaries 摘要 + 群組原始訊息 + **第一手 X (Twitter) 內容** 中,
寫一份 30 秒能讀完的 quick brief,讓 使用者 判斷這顆/這位是不是值得 promote 為
coin profile / 深入追蹤,還是純 watch / skip。

# 資料來源權重(重要)

- **X 第一手 tweets**:最高權重。原作者親自說的話,而不是 TG 群裡的轉述。
  引用 KOL 觀點時,**優先引用 tweet 原文 + 互動數**(❤N 🔁M),別寫成「群裡有人說」
- **TG daily summaries**:已經 AI 蒸餾過的群觀點,二手但密度高
- **TG 原始訊息**:第一手但只看到群內反應,看不到原 tweet
- 三者衝突時,以 X 第一手為準(因為那是真的原話)

# 動詞忠實度(硬性規則)

- KOL 推文 / 引用 / 轉推 / 跟蹤 / 共同關注 ≠ 持倉、≠ 看多
- 「親自亮牌持倉 / N SOL 進場 / 公布 PnL」原文明確才算實際成交
- 鏈上錢包(wallet_log 類資料)的 BUY / SELL 才算 smart money 動向
- 沒成交證據一律降級成「點名 / 表態 / 關注」
- X 上的「sub」「ape'd」「bag」可能暗示持倉,但要看上下文,別硬解讀成成交

# 輸出格式(嚴格遵守)

寫 3 段,**段落間用空行分隔**,**不要加 markdown 標題、不要 ===SECTION===**:

第一段:這個 entity 是什麼 + 為什麼會被記憶庫看到。
要有具體催化點(某 KOL 在某天點名 / 某事件 / 某 wallet 動作 / 某新訊號),
不要寫成「這顆幣很有趣」這種空話。

第二段:目前的 KOL 共識。
列 1-3 位代表性 KOL(用名字或 @handle),他們的角色 / 影響力 / 看好或唱反調,
最後一句話估計共識強度(強 / 中 / 弱 / 分歧)。
資料不足就誠實寫「點名 KOL 樣本太少,不下定論」。

第三段:使用者 視角的初判,**從這三選一**:
- **值得 promote 為 profile**:理由 + 哪些訊號支持(連續天數 / 多 KOL 共振 / 鏈上資金流)
- **持續 watch 不急**:為什麼還沒成熟,要等什麼訊號才升級
- **可以 skip**:為什麼像噪音(資料太少 / 純 bot 推送 / 一次性點名)

# 硬性規則

- **長度彈性**:目標 350-500 字。資料豐富(KOL 樣本 ≥5 / 多平台共振 / 鏈上有具體動作)時可放寬到 600 字,但不要塞水詞、套話、客套。資料極少時也不要硬湊,200 字也行
- 段落間用空行分隔,別用 markdown 標題或 ===SECTION===
- 沒資料的細節寫「資料不足」,不要編人名 / 數字 / 時間
- 繁體中文為主,幣圈慣用語保留英文(FDV、CTO、meta、narrative、alpha 等)
- 不要寫「值得關注 / 持續觀察」這類空詞,要具體
- 引用 X tweet 時可加上互動數(❤、👁、follower 數)當權重提示,但別塞太多數字"""


PROMPT_ENTITY_BRIEF = """=== Entity ===
{entity_label}  (kind: {kind})

=== 觀察期間 ===
過去 {days} 天

=== X (Twitter) 第一手 tweets — 最高權重 ===
{twitter_blob}

=== 已蒸餾的 daily summaries 摘要 ===
{summary_blob}

=== 群組原始訊息(已過濾 tracker bot)===
{message_blob}

請依規格寫 3 段,共 ≤ 250 字,段落間空行分隔。
**引用 KOL 觀點時優先引用 X 原文(若有 tweets 區塊),不要只寫 TG 群轉述**。"""


# ---------------------------------------------------------------------------
# Coin profile field extractor — used by /api/coin_profiles/<id>/fill.
# User pastes ANY raw notes (tweets, group chat, their own observations,
# trade screenshots) and Sonnet maps relevant content into typed profile
# fields. Output is strict JSON for reliable parsing.
# ---------------------------------------------------------------------------

SYS_PROFILE_FILL = """你是 使用者 的 coin profile 資料擷取器。

任務:給你兩份輸入 — (1) 使用者隨手貼的素材、(2) 自動從 X (Twitter) 搜回來的相關
tweets — 把兩邊**真的有提到**的資訊**合併**抽出來,填到 coin profile 的對應欄位。

# 資料來源權重

- **使用者貼的內容**:最高權重(因為是 使用者 親自挑的素材,反映他關心的點)
  - 「我的買賣 / verdict / lesson」這類主觀欄位**只能**從使用者素材抽,Twitter 不會有
- **Twitter 搜回來的 tweets**:用來補強客觀區塊(narrative / kol_consensus / smart_money)
  - 引用 tweet 時可帶 @handle + ❤N 互動數,讓共識強度有量化依據
  - 沒提到的事不要因為是「Twitter 的內容」就降級;它是真實第一手資料

# 動詞忠實度(同舊規則)

- KOL 推文 / 引用 / 轉推 / 跟蹤 ≠ 持倉、≠ 看多
- 「親自亮牌持倉 / N SOL 進場 / 公布 PnL」原文明確才算實際成交
- 鏈上 wallet BUY/SELL 才算 smart money 動向
- 沒明確證據就降級成「點名 / 表態 / 關注」

# 可填欄位(只能用這些 key)

身份類:
  symbol     — 幣的 ticker (e.g. "PEPE", "$" 不需要)
  chain      — base / solana / ethereum / bsc / monad / story / 其他
  ca         — contract address
  status     — tracking / held / exited / dropped(只能這四個值)
  first_seen_date — YYYY-MM-DD

研究類:
  narrative          — 敘事(這顆幣是什麼、故事怎麼起來的)
  timeline_json      — 時間軸(自由文字,不要硬寫成 JSON)
  kol_consensus      — KOL 共識(誰看好/警示/中立)
  smart_money_summary — 鏈上 wallet 動向
  top_signal         — 見頂訊號(可辨識的 / 事後看清的)
  archetype          — 原型筆記(這屬於哪一種可重複的 setup)

我的買賣類(只有原文明確提到才填):
  my_entry_fdv, my_entry_size, my_exit_fdv, my_exit_size
  my_pnl, my_wallet

判斷類:
  my_verdict — 回頭看的判斷(對嗎?該重倉嗎?該更早出嗎?)
  my_lesson  — 教訓(下次同型態出現要怎麼做不一樣?)

其他:
  tags  — 自由文字 tag(逗號分隔),例:memecoin, AI, base

# 系統管理欄位(不要寫)

- `my_raw_notes` 是 server 自動保存的「使用者原文」欄位,**你絕對不要在 JSON 輸出
  這個 key**,server 會自己 append。你的工作只是抽結構化資料,不要重複保存原文。

# 輸出格式(嚴格遵守)

只輸出 JSON object,不要任何前言、不要 markdown code fence、不要解釋。
**只把使用者素材裡明確有的欄位放進 JSON,完全沒提到的欄位不要出現**(不要寫 ""、不要寫 null)。
不確定 / 推測的欄位不要寫,寧可空。

# 跟現有 profile 共存的規則

如果使用者素材跟現有 profile 衝突:
- 使用者明確說「修正 / 改成 / 更新」→ 用使用者的值覆蓋
- 沒明確說 → 沿用舊值(也就是不要在 JSON 裡輸出該欄位,讓 server 端保持原狀)
- 使用者明確補充細節(例如原本只有 narrative,使用者新增了 my_entry)→ 寫新欄位

# 範例

使用者貼:「剛剛在 PEPE 1.2M 進場 0.3 SOL,看到 cobie 推說 generational launch」
→ {"my_entry_fdv": "1.2M", "my_entry_size": "0.3 SOL", "kol_consensus": "cobie 推稱 generational launch(轉述,未公布持倉)"}

使用者貼:「這顆是 base 上的 ai meme,12 hours ago dev 拉砸過一次」
→ {"chain": "base", "narrative": "Base 上的 AI meme,dev 12 小時前拉砸過一次", "tags": "ai, memecoin"}"""


PROMPT_PROFILE_FILL = """=== 現有 profile 狀態 ===
Symbol: ${current_symbol}
Chain: {current_chain}
CA: {current_ca}
Status: {current_status}
已填區塊: {filled_sections}

=== 使用者貼的素材(最高權重)===
{user_notes}

=== X (Twitter) 搜回來的 tweets(用來補強客觀區塊)===
{twitter_blob}

請依規格輸出純 JSON,合併兩份輸入,只放真的有提到的欄位。
**主觀欄位(my_*)只能從使用者素材抽**,Twitter 不會有。"""


# ---------------------------------------------------------------------------
# Coin profile bootstrap from a single review/post-mortem paste
# (used by /api/coin_profiles/from_notes — creates a brand-new profile rather
# than filling an existing one). Same field schema as PROFILE_FILL, but
# `symbol` is REQUIRED in the output because we INSERT immediately afterwards.
# ---------------------------------------------------------------------------

SYS_PROFILE_FROM_NOTES = """你是 使用者 的 coin profile 建檔助手。

任務:使用者 剛貼了一篇**復盤 / 觀察 / 研究筆記**(內含 CA 或 $TICKER),你要
從中 + 自動搜回的 X tweets,**新建**一份 coin profile。

# 資料來源權重(同 fill 規則)

- **使用者貼的內容**:最高權重(他親自寫的復盤,反映他真正的觀點)
  - 「我的買賣 / verdict / lesson」這類主觀欄位**只能**從使用者素材抽
- **Twitter 搜回來的 tweets**:用來補強客觀區塊(narrative / kol_consensus / smart_money)
  - 引用時可帶 @handle + ❤N 互動數
  - 不要因為是 Twitter 內容就降級;它是真實第一手資料

# 動詞忠實度(同舊規則,不可放寬)

- KOL 推文 / 引用 / 轉推 / 跟蹤 ≠ 持倉、≠ 看多
- 「親自亮牌持倉 / N SOL 進場 / 公布 PnL」原文明確才算實際成交
- 鏈上 wallet BUY/SELL 才算 smart money 動向
- 沒明確證據就降級成「點名 / 表態 / 關注」

# 必填欄位(這份是新檔,沒有舊資料)

- `symbol` — **必填**,從 $TICKER / 推文 user 名 / 復盤標題判斷,大寫無 $
- `chain`  — 從 CA 形狀判斷:0x40hex → evm 系(看上下文判斷 base/ethereum/bsc),
             base58 32-44 → solana。從筆記/推文取最具體的鏈名
- `ca`     — 如使用者筆記裡有 CA 就抄進來

# 可選欄位(只有真的提到才填)

研究類:
  narrative           — 敘事(這顆幣是什麼、故事怎麼起來的)
  timeline_json       — 時間軸。**雖然欄位名有 `_json`,但內容必須寫純文字、
                        不要硬寫成 JSON / list / dict / Python repr**。
                        每行一筆,格式建議:`YYYY-MM-DD 或 HH:MM — 事件描述`
                        所有時間一律 UTC+8 (Asia/Taipei)
  kol_consensus       — KOL 共識(誰看好/警示/中立)
  smart_money_summary — 鏈上 wallet 動向
  top_signal          — 見頂訊號(可辨識的 / 事後看清的)
  archetype           — 原型筆記(這屬於哪一種可重複的 setup)

我的買賣類(原文明確才填):
  my_entry_fdv, my_entry_size, my_exit_fdv, my_exit_size, my_pnl, my_wallet

判斷類:
  my_verdict, my_lesson

其他:
  status — tracking / held / exited / dropped(沒線索就 tracking)
  first_seen_date — YYYY-MM-DD
  tags — 自由文字 tag,逗號分隔

# 數字 / 日期忠實度

- 年份、日期、價格、FDV、size:**逐字照抄**使用者原文,不要重算、不要猜
- 例:用戶寫 2023 → 寫 2023(不可改成 2013、2024、近期等)
- 沒寫具體日期就不要硬填日期欄位

# 系統管理欄位(不要寫)

- `my_raw_notes` 是 server 自動保存的「使用者原文」欄位,**你絕對不要在 JSON 輸出
  這個 key**,server 會自己存原文。

# 輸出格式(嚴格遵守)

只輸出 JSON object,不要任何前言、不要 markdown code fence、不要解釋。
**`symbol` 必須有值**(這是新建檔的最低要求)。其他可選欄位只有真的提到才放,
沒提到的不要寫(不要 ""、不要 null)。

# 範例

使用者貼:「BONK 復盤 — DwK4...vAsRr 這顆我在 0.000005 進 0.5 SOL,後來砸到 0.000002 全賣,虧 60%。教訓:Pump 後沒接力就要早跑」
推文補強:「@cobie: BONK is over」
→ {
  "symbol": "BONK", "chain": "solana", "ca": "DwK4...vAsRr", "status": "exited",
  "narrative": "Solana memecoin,Pump 後缺乏接力資金",
  "kol_consensus": "@cobie 看空『BONK is over』",
  "my_entry_fdv": "0.000005", "my_entry_size": "0.5 SOL",
  "my_exit_fdv": "0.000002", "my_pnl": "-60%",
  "my_lesson": "Pump 後沒接力買盤就要早跑"
}"""


PROMPT_PROFILE_FROM_NOTES = """=== 自動偵測到的線索 ===
CA: {ca_hint}
$TICKER 候選: {symbol_hint}
鏈推測(從 CA 形狀): {chain_hint}

=== 使用者貼的復盤 / 筆記(最高權重)===
{user_notes}

=== X (Twitter) 搜回來的 tweets(用來補強客觀區塊)===
{twitter_blob}

請依規格輸出純 JSON。**`symbol` 必填**。"""


# ---------------------------------------------------------------------------
# Distill trading rules from a single coin profile
# (used by /api/coin_profiles/<id>/distill_rules — the user clicks 「✦ 提煉成法則」
# on a profile detail page; AI reads the profile's lesson + raw notes + narrative
# + archetype and proposes 1-3 cross-coin reusable rules. Output is a candidate
# list; user picks/edits before any DB write.)
# ---------------------------------------------------------------------------

SYS_DISTILL_RULES = """你是 使用者 的交易法則提煉器。

任務:從一顆 coin profile 的復盤(my_lesson、my_verdict、my_raw_notes、
narrative、archetype),提煉出**跨幣可複用**的交易法則。

# 核心要求

- 法則必須**抽象**到「下次任何幣遇到同型態」都適用,不能寫成「BONK 該如何」
- 一條法則只講一件事(進場 / 出場 / 風控 / 倉位 / 一般紀律之一)
- 沒結論的素材(只記事實沒判斷)→ 直接回 `[]`,不要硬湊

# 輸出欄位(每條法則)

- `rule_text` — 一句話法則本體,**≤ 30 字**,祈使句或陳述句皆可
  好例:「Pump 後 30 分沒接力買盤就出」「KOL 親自亮牌 ≥10% 倉位才跟」
  壞例:「BONK 那次沒接力很慘」(綁特定幣)、「謹慎觀察」(空泛)
- `reason` — **≤ 60 字**解釋這條為何成立,可帶來源(「來自 $X 復盤:…」)
- `scope` — 五選一:`entry` / `exit` / `risk` / `sizing` / `general`

# 數量

最多 3 條,寧少勿濫。一份復盤通常只能提煉 1-2 條真正可用的紀律。

# 輸出格式(嚴格遵守)

只輸出 JSON 陣列,不要任何前言、不要 markdown code fence、不要解釋:

[
  {"rule_text": "...", "reason": "...", "scope": "..."},
  ...
]

如果素材不足(只有事實沒結論 / lesson 為空),回 `[]`。

# 範例

輸入 profile:
  symbol: BONK
  archetype: Solana memecoin Pump 後缺接力資金
  my_verdict: 進場太重,沒設止損
  my_lesson: Pump 後沒接力買盤就要早跑
  my_raw_notes: [2024-01-15] 0.000005 進 0.5 SOL,砸到 0.000002 全賣

輸出:
[
  {"rule_text": "Pump 後 30 分沒接力買盤就出", "reason": "來自 $BONK 復盤:Pump 後沒接力買盤砸 60%。", "scope": "exit"},
  {"rule_text": "memecoin 單筆倉位先 ≤0.3 SOL 試水", "reason": "來自 $BONK 復盤:0.5 SOL 一把進、沒設止損,虧 60%。", "scope": "sizing"}
]"""


PROMPT_DISTILL_RULES = """=== 待提煉的 coin profile ===
symbol: ${symbol}
chain: {chain}
archetype: {archetype}
narrative: {narrative}
my_verdict: {my_verdict}
my_lesson: {my_lesson}
my_raw_notes: {my_raw_notes}

請依規格輸出 JSON 陣列。素材不足回 `[]`。"""


def build_trading_rules_block(max_rules=30, max_chars=2200):
    """Render an active-trading-rules block for digest / summarize prompts.

    Returns "" when no active rules exist — caller can drop this directly
    into the prompt without a guard.
    """
    sql = (
        "SELECT id, rule_text, reason, scope FROM trading_rules "
        "WHERE status = 'active' "
        "ORDER BY pinned DESC, hit_count DESC, id ASC LIMIT ?"
    )
    with get_db_ctx() as conn:
        rows = [dict(r) for r in conn.execute(sql, (max_rules,)).fetchall()]
    if not rows:
        return ""

    out = [
        "--- ⚖ 你既訂的交易法則(編號 # 開頭)---",
        "用途:檢查本時段訊號是否符合或違反任一條既訂法則。",
        "命中時請在對應幣段落結尾加一行 `✓ 法則 #N` 或 "
        "`⚠️ 違反 #N — <一句話原因>`。沒命中就不要硬寫,寧缺勿濫。",
        "",
    ]
    for d in rows:
        rid = d.get("id")
        scope = (d.get("scope") or "general").strip()
        rule = (d.get("rule_text") or "").strip()
        reason = (d.get("reason") or "").strip()
        line = f"#{rid} [{scope}] {rule}"
        if reason:
            line += f" — {reason}"
        out.append(line)

    block = "\n".join(out) + "\n\n"
    if len(block) > max_chars:
        block = block[:max_chars] + "…(已截斷)\n\n"
    return block


_RULE_HIT_RE = re.compile(r"(?:法則|rule)\s*#(\d+)", re.IGNORECASE)


def update_rule_hits(text):
    """Scan a digest/summarize output for `法則 #N` / `rule #N` references and
    bump hit_count + last_hit_at on each cited rule. Idempotent per call —
    one bump per unique rule id per call. Silent on errors (telemetry only)."""
    if not text:
        return []
    try:
        ids = sorted({int(m) for m in _RULE_HIT_RE.findall(text)})
    except ValueError:
        return []
    if not ids:
        return []
    try:
        with get_db_ctx() as conn:
            placeholders = ",".join("?" * len(ids))
            existing = {
                row[0] for row in conn.execute(
                    f"SELECT id FROM trading_rules WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            applied = [rid for rid in ids if rid in existing]
            if applied:
                conn.executemany(
                    "UPDATE trading_rules SET hit_count = hit_count + 1, "
                    "last_hit_at = datetime('now', 'localtime') WHERE id = ?",
                    [(rid,) for rid in applied],
                )
                conn.commit()
        return applied
    except Exception as e:
        logger.warning("update_rule_hits failed: %s", e)
        return []


def parse_coin_draft(text):
    """Parse the AI's `===SECTION===` blocks back into a {section: body} dict.

    Returns dict with keys NARRATIVE / TIMELINE / KOL_CONSENSUS / SMART_MONEY /
    TOP_SIGNAL / ARCHETYPE — values stripped, missing sections absent. Caller
    decides which fields to update on the profile (preserve existing if section
    is empty / missing).
    """
    out = {}
    if not text:
        return out
    for m in _COIN_DRAFT_SECTION_RE.finditer(text):
        name = m.group(1).strip()
        body = m.group(2).strip()
        if body and body != "資料不足":
            out[name] = body
    return out


def get_profile(name):
    """Return the profile config dict; falls back to group_chat for unknown values."""
    return PROFILES.get(name) or PROFILES[DEFAULT_PROFILE]


# ---------------------------------------------------------------------------
# Coin profile cross-reference for summarize prompts
# ---------------------------------------------------------------------------

# Same shapes as server.py's _RE_*; duplicated here to avoid an import cycle.
_SUM_RE_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,9})\b")
_SUM_RE_CA_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SUM_RE_CA_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def build_coin_profile_context(messages, max_profiles=10, max_chars=2500):
    """Render a "you already have these coin profiles" block to inject into
    the summarize prompt.

    Scans messages for $TICKER / CA mentions, looks them up in coin_profiles,
    and writes a compact dossier so the AI can cross-reference current group
    chatter against the user's past verdicts / lessons / archetypes — that's
    how a coin's mention becomes "this looks like the BONK pump-without-relay
    archetype you wrote up" instead of just another ticker in the recap.

    Always appends the active trading-rules block (so even when no coin
    profiles match the current messages, the AI still sees the rules and
    can flag ✓/⚠️ on coins it discusses). Caller drops this directly into
    {coin_profile_context} without needing a guard.
    """
    rules_block = build_trading_rules_block()

    if not messages:
        return rules_block

    text_blob = "\n".join(
        (m.get("text") or "") for m in messages if isinstance(m, dict)
    )
    if not text_blob.strip():
        return rules_block

    symbols = {m.group(1).upper() for m in _SUM_RE_TICKER.finditer(text_blob)}
    cas = set(_SUM_RE_CA_EVM.findall(text_blob))
    for s in _SUM_RE_CA_SOL.findall(text_blob):
        # Same heuristic as server.py — pure-digit / pure-case strings of
        # this length are usually noise, not actual base58 CAs.
        if s.isdigit() or s.isupper() or s.islower():
            continue
        cas.add(s)

    if not (symbols or cas):
        return rules_block

    clauses, params = [], []
    if symbols:
        clauses.append("UPPER(symbol) IN (" + ",".join("?" * len(symbols)) + ")")
        params.extend(symbols)
    if cas:
        clauses.append("ca IN (" + ",".join("?" * len(cas)) + ")")
        params.extend(cas)
    sql = (
        "SELECT symbol, chain, status, narrative, archetype, "
        "my_verdict, my_lesson, top_signal "
        "FROM coin_profiles WHERE " + " OR ".join(clauses) +
        " ORDER BY pinned DESC, last_updated DESC LIMIT ?"
    )
    params.append(max_profiles)

    with get_db_ctx() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return rules_block

    out = [
        "--- 🗂 你既有的 coin profile 對照(此時段訊息中提到的幣)---",
        "用途:檢查訊息中的幣是否符合你過去歸納的原型 / 教訓 / verdict。"
        "命中時請在「💎 倍數幣深度拆解」相關段落加一行「⚠️ 對照你的 $X 復盤:<一句話差異>」。",
        "",
    ]
    for d in rows:
        sym = d.get("symbol") or "?"
        head_bits = [f"${sym}"]
        if d.get("chain"):
            head_bits.append(d["chain"])
        head_bits.append(d.get("status") or "tracking")
        out.append(f"• {' · '.join(head_bits)}")
        for label, key in (
            ("敘事",     "narrative"),
            ("原型",     "archetype"),
            ("verdict",  "my_verdict"),
            ("教訓",     "my_lesson"),
            ("見頂訊號", "top_signal"),
        ):
            val = (d.get(key) or "").strip()
            if not val:
                continue
            if len(val) > 200:
                val = val[:200] + "…"
            out.append(f"    {label}: {val}")

    block = "\n".join(out) + "\n\n"
    if len(block) > max_chars:
        block = block[:max_chars] + "…(已截斷)\n\n"
    # Append the active trading rules block so the same {coin_profile_context}
    # placeholder carries both signals — per-coin lessons + cross-coin rules.
    return block + rules_block


def build_history_context(chat_id, days=7, max_chars_per_row=500, max_rows=30):
    """Build the {history_context} block for profiles that need it.

    Pulls recent daily_summaries for this chat, truncated, newest first.
    Current-day rows are excluded so re-running a brief does not feed the
    previous version of the same report back into the prompt.
    Returns a human-readable string (not a DB object).
    """
    if not chat_id:
        return "(無歷史資料)"
    with get_db_ctx() as conn:
        rows = conn.execute(
            """
            SELECT date, chat_name, substr(summary, 1, ?) AS snippet
            FROM daily_summaries
            WHERE chat_id = ?
              AND date >= date('now', 'localtime', ?)
              AND date < date('now', 'localtime')
            ORDER BY date DESC
            LIMIT ?
            """,
            (max_chars_per_row, str(chat_id), f"-{days} days", max_rows),
        ).fetchall()
    if not rows:
        return "(無歷史資料,今日為首日)"
    return "\n\n".join(
        f"[{r['date']}] [{r['chat_name']}]\n{r['snippet']}" for r in rows
    )


def split_profile_output(raw_text):
    """Split `===JSON===` marker out of a profile-formatted summary.

    Returns (markdown_part, json_text_or_None). If the marker is missing,
    returns (raw_text, None). Does NOT json.loads — caller decides whether
    to validate.
    """
    if not raw_text:
        return raw_text, None
    idx = raw_text.find(JSON_SPLIT_MARK)
    if idx < 0:
        return raw_text, None
    markdown = raw_text[:idx].rstrip()
    json_text = raw_text[idx + len(JSON_SPLIT_MARK):].strip()
    return markdown, (json_text or None)


_cost_lock = threading.Lock()
total_cost_session = 0.0


def get_claude_client():
    """Back-compat shim — returns a truthy value when AI is available."""
    return ai_available()


def _log_cost(label, usage, model):
    """Log token usage & estimated $cost; no-op when backend doesn't return usage (CLI)."""
    if usage is None:
        model_short = MODEL_SHORT_NAMES.get(model, "Unknown")
        logger.info("📊 %s (%s, CLI): 使用訂閱額度", label, model_short)
        return
    global total_cost_session
    rates = {
        MODEL_OPUS:   (15, 75),
        MODEL_SONNET: (3, 15),
    }
    r_in, r_out = rates.get(model, (3, 15))
    cost_in = usage.input_tokens * r_in / 1_000_000
    cost_out = usage.output_tokens * r_out / 1_000_000
    cost = cost_in + cost_out
    with _cost_lock:
        total_cost_session += cost
        session_total = total_cost_session
    model_short = MODEL_SHORT_NAMES.get(model, "Unknown")
    logger.info(
        "📊 %s (%s): in=%d out=%d 💰$%.4f (累計$%.4f)",
        label, model_short, usage.input_tokens, usage.output_tokens, cost, session_total,
    )


# ---------------------------------------------------------------------------
# Trust map (cached)
# ---------------------------------------------------------------------------

_trust_map_cache = {"ts": 0.0, "map": {}}
_trust_map_lock = threading.Lock()
TRUST_MAP_TTL = 30.0


def load_trust_map():
    now = _time.monotonic()
    with _trust_map_lock:
        if now - _trust_map_cache["ts"] < TRUST_MAP_TTL:
            return _trust_map_cache["map"]
    with get_db_ctx() as conn:
        rows = conn.execute("SELECT sender_id, trust_level FROM trusted_senders").fetchall()
    m = {row["sender_id"]: row["trust_level"] for row in rows}
    with _trust_map_lock:
        _trust_map_cache["ts"] = now
        _trust_map_cache["map"] = m
    return m


def invalidate_trust_map():
    with _trust_map_lock:
        _trust_map_cache["ts"] = 0.0
        _trust_map_cache["map"] = {}


# ---------------------------------------------------------------------------
# Message preparation
# ---------------------------------------------------------------------------

def _prepare_lines(messages, trust_map=None):
    lines = []
    for m in messages:
        dt = to_taipei_str(m["date"], fmt="%H:%M")
        media = f" [{m['media']}]" if m.get("media") else ""
        text = m.get("text", "")
        sender = m["from"]
        prefix = ""
        if trust_map and m.get("sender_id"):
            level = trust_map.get(m["sender_id"])
            if level == "trusted":
                prefix = "⭐ "
            elif level == "noise":
                prefix = "🔇 "
        if text or media:
            lines.append(f"[{dt}] {prefix}{sender}: {text}{media}")
    return lines


def _split_chunks(lines, max_chars=CHUNK_CHARS):
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _compress_chunk(chunk_text, chunk_idx, total_chunks, chat_name, compress_system):
    prompt = f"壓縮以下「{chat_name}」的訊息（第 {chunk_idx}/{total_chunks} 批），保留所有重要資訊：\n\n{chunk_text}"
    # 4096 matches the 2x larger CHUNK_CHARS; wallet_log compressor preserves
    # every event's structured fields so output tracks input size closely.
    primary_model = AUTO_SUMMARIZE_COMPRESS_MODEL
    text, usage = ai_call(prompt, compress_system, primary_model, max_tokens=4096)
    _log_cost(f"壓縮 {chunk_idx}/{total_chunks}", usage, primary_model)

    # Short-output safety net: if the primary model returned suspiciously little
    # (refusal, early-stop, length-limit cutoff), retry once on Sonnet so we do
    # not silently drop alpha. Only kicks in when compression model isn't already
    # Sonnet, and only if input was non-trivial (skip tiny chunks where ratio is
    # meaningless).
    if (
        primary_model != MODEL_SONNET
        and AUTO_SUMMARIZE_COMPRESS_MIN_RATIO > 0
        and len(chunk_text) >= 4000
        and len((text or "").strip()) < int(len(chunk_text) * AUTO_SUMMARIZE_COMPRESS_MIN_RATIO)
    ):
        logger.warning(
            "壓縮 %d/%d 輸出過短 (%d/%d 字, ratio=%.3f < %.3f) — fallback Sonnet 重試",
            chunk_idx, total_chunks, len((text or "").strip()), len(chunk_text),
            len((text or "").strip()) / max(1, len(chunk_text)),
            AUTO_SUMMARIZE_COMPRESS_MIN_RATIO,
        )
        text, usage = ai_call(prompt, compress_system, MODEL_SONNET, max_tokens=4096)
        _log_cost(f"壓縮 {chunk_idx}/{total_chunks} (Sonnet fallback)", usage, MODEL_SONNET)
    return text


def _compress_chunks_parallel(chunks, chat_name, max_workers=None, compress_system=None):
    """Generator. Yields {'done','total','results'} dicts — 'results' is None
    while work is in progress, and set on the final yield. Default worker count
    comes from AUTO_SUMMARIZE_COMPRESS_WORKERS (3) — subscription Max-5x easily
    runs 3 concurrent Haiku/Sonnet CLI calls; dial down if rate-limited."""
    compress_system = compress_system or SYS_COMPRESS
    total = len(chunks)
    if max_workers is None:
        max_workers = AUTO_SUMMARIZE_COMPRESS_WORKERS
    # Cap at total chunks (no point spinning idle workers).
    max_workers = max(1, min(max_workers, total))
    results = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_compress_chunk, chunk, i + 1, total, chat_name, compress_system): i
            for i, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = f"【第 {idx + 1} 批摘要】\n{future.result()}"
            except Exception as e:
                logger.warning("第 %d 批壓縮失敗: %s", idx + 1, e)
                results[idx] = f"【第 {idx + 1} 批】（壓縮失敗）"
            done += 1
            yield {"done": done, "total": total, "results": None}
    yield {"done": total, "total": total, "results": results}


def _per_sender_extract_one(sender_label, sender_text, chat_name):
    """Run Haiku structured extraction on one whale's concatenated messages.
    Returns the structured text (possibly empty on failure)."""
    chunk_chars = max(4000, AUTO_SUMMARIZE_PER_SENDER_CHUNK_CHARS)
    chunks = []
    for start in range(0, len(sender_text), chunk_chars):
        chunks.append(sender_text[start:start + chunk_chars])
    extracts = [None] * len(chunks)
    total = len(chunks)

    def _do_one(i, body):
        prompt = (
            f"以下是「{chat_name}」群組中 [{sender_label}] 一個人的訊息"
            f"(第 {i + 1}/{total} 批),依規則抽成結構化清單:\n\n{body}"
        )
        try:
            text, usage = ai_call(
                prompt, SYS_PER_SENDER_EXTRACT,
                AUTO_SUMMARIZE_COMPRESS_MODEL, max_tokens=4096,
            )
            _log_cost(
                f"per-sender 抽取 {sender_label} {i + 1}/{total}",
                usage, AUTO_SUMMARIZE_COMPRESS_MODEL,
            )
            return (text or "").strip()
        except Exception as e:
            logger.warning(
                "per-sender 抽取失敗 sender=%s chunk=%d/%d: %s",
                sender_label, i + 1, total, e,
            )
            return ""

    if total == 1:
        extracts[0] = _do_one(0, chunks[0])
    else:
        max_workers = max(1, min(AUTO_SUMMARIZE_COMPRESS_WORKERS, total))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_do_one, i, body): i for i, body in enumerate(chunks)}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    extracts[idx] = future.result()
                except Exception as e:
                    logger.warning("per-sender future 失敗 idx=%d: %s", idx, e)
                    extracts[idx] = ""

    joined = "\n\n".join(part for part in extracts if part).strip()
    cap = max(2000, AUTO_SUMMARIZE_PER_SENDER_MAX_OUTPUT_CHARS)
    if len(joined) > cap:
        joined = joined[:cap].rstrip() + f"\n\n…(per-sender 抽取截斷至 {cap:,} 字)"
    return joined


def per_sender_pre_summarize(messages, chat_name):
    """For group_chat slots dominated by 1-2 big posters, replace each whale's
    raw messages with a single synthetic message holding a Haiku-extracted
    structured table. Other senders pass through unchanged.

    Returns (new_messages, stats). When no sender crosses the trigger, returns
    the original messages list (same object) and stats={'transformed': 0}.
    """
    trigger = max(0, AUTO_SUMMARIZE_PER_SENDER_TRIGGER_CHARS)
    if trigger == 0 or not messages:
        return messages, {"transformed": 0}

    by_sender_chars = {}
    by_sender_indices = {}
    for idx, m in enumerate(messages):
        sender = m.get("from") or m.get("sender_name") or "unknown"
        by_sender_chars[sender] = by_sender_chars.get(sender, 0) + len(m.get("text") or "")
        by_sender_indices.setdefault(sender, []).append(idx)

    whales = [s for s, n in by_sender_chars.items() if n >= trigger]
    if not whales:
        return messages, {"transformed": 0}

    whales.sort(key=lambda s: by_sender_chars[s], reverse=True)
    logger.info(
        "auto-summarize: %s per-sender pre-extract triggered for %d whale(s): %s",
        chat_name, len(whales),
        ", ".join(f"{s}({by_sender_chars[s]:,}c)" for s in whales),
    )

    replacements = {}
    saved_total = 0
    for sender in whales:
        indices = by_sender_indices[sender]
        anchor = messages[indices[0]]
        sender_lines = []
        for i in indices:
            m = messages[i]
            ts = to_taipei_str(m.get("date"), fmt="%H:%M") or "??:??"
            text = (m.get("text") or "").strip()
            media = f" [{m.get('media')}]" if m.get("media") else ""
            if text or media:
                sender_lines.append(f"[{ts}] {text}{media}")
        sender_text = "\n".join(sender_lines)
        before_len = len(sender_text)
        extract = _per_sender_extract_one(sender, sender_text, chat_name)
        if not extract:
            logger.warning(
                "auto-summarize: %s per-sender 抽取 [%s] 空結果,保留原文",
                chat_name, sender,
            )
            continue
        synthetic = {
            "id": anchor.get("id"),
            "date": anchor.get("date"),
            "from": sender,
            "username": anchor.get("username"),
            "sender_id": anchor.get("sender_id"),
            "topic_id": anchor.get("topic_id"),
            "text": (
                f"[PER_SENDER_STRUCTURED_EXTRACT msgs={len(indices)} "
                f"raw_chars={before_len}]\n{extract}"
            ),
            "media": "",
        }
        replacements[sender] = (set(indices), synthetic)
        saved_total += max(0, before_len - len(extract))
        logger.info(
            "auto-summarize: %s per-sender [%s] %d msgs %d → %d chars",
            chat_name, sender, len(indices), before_len, len(extract),
        )

    if not replacements:
        return messages, {"transformed": 0}

    consumed = set()
    new_messages = []
    for idx, m in enumerate(messages):
        sender = m.get("from") or m.get("sender_name") or "unknown"
        if sender in replacements:
            indices_set, synthetic = replacements[sender]
            if idx in indices_set:
                if sender not in consumed:
                    new_messages.append(synthetic)
                    consumed.add(sender)
                continue
        new_messages.append(m)

    return new_messages, {
        "transformed": len(replacements),
        "saved_chars": saved_total,
    }


def prepare_lines(messages):
    """Trust-annotated lines (used by the streaming endpoint which handles progress itself)."""
    return _prepare_lines(messages, load_trust_map())


def split_chunks(lines):
    return _split_chunks(lines, CHUNK_CHARS)


def compress_chunks_parallel(chunks, chat_name, max_workers=None, compress_system=None):
    """Public wrapper. Generator — yields {'done','total','results'} dicts, with
    'results' set only on the terminal yield. `compress_system` overrides the
    default SYS_COMPRESS — callers pass the profile-specific compressor
    (e.g. SYS_COMPRESS_WALLET). Pass max_workers=None to use the env default
    (AUTO_SUMMARIZE_COMPRESS_WORKERS, default 3)."""
    yield from _compress_chunks_parallel(chunks, chat_name, max_workers, compress_system)


# ---------------------------------------------------------------------------
# Sentiment / diff / extraction
# ---------------------------------------------------------------------------

def ai_extract_sentiment(summary, chat_name):
    if not ai_available():
        return None
    try:
        text, usage = ai_call(
            f"群組「{chat_name}」的摘要：\n{summary[:3000]}",
            SYS_SENTIMENT, MODEL_SONNET, max_tokens=200,
        )
        _log_cost("情緒分析", usage, MODEL_SONNET)
        text = (text or "").strip()
        if not text:
            return None
        # 有時模型會在 JSON 前後加字，抓第一個 { ... }
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        result = json.loads(text)
        score = max(1, min(10, float(result.get("score", 5))))
        label = result.get("label", "中性")
        out = {"score": score, "label": label}
        for key in ("chain_flow", "meta_shift", "risk_flag"):
            val = result.get(key)
            if isinstance(val, str) and val.strip() and val.strip().lower() not in ("unknown", "null", "none"):
                out[key] = val.strip()
        return out
    except Exception as e:
        logger.warning("情緒分析失敗: %s", e)
        return None


def save_sentiment(summary_id, chat_id, chat_name, sentiment):
    if not sentiment:
        return
    today = date.today().isoformat()
    with get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO sentiment_scores
            (date, chat_id, chat_name, score, label, chain_flow, meta_shift, risk_flag, summary_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, chat_id) DO UPDATE SET
                chat_name = excluded.chat_name,
                score = excluded.score,
                label = excluded.label,
                chain_flow = COALESCE(excluded.chain_flow, sentiment_scores.chain_flow),
                meta_shift = COALESCE(excluded.meta_shift, sentiment_scores.meta_shift),
                risk_flag = COALESCE(excluded.risk_flag, sentiment_scores.risk_flag),
                summary_id = COALESCE(excluded.summary_id, sentiment_scores.summary_id)
        """, (today, str(chat_id), chat_name,
              sentiment["score"], sentiment["label"],
              sentiment.get("chain_flow"),
              sentiment.get("meta_shift"),
              sentiment.get("risk_flag"),
              summary_id))
        conn.commit()


def ai_diff_summaries(old_summary, new_summary, chat_name):
    if not ai_available():
        return None, "AI backend 未就緒"
    try:
        prompt = f"""比較「{chat_name}」的兩份摘要，分析變化：

格式：
🆕 新出現的話題（之前沒提到，現在出現的）
❌ 消失的話題（之前有提到，現在沒了的）
🔄 觀點轉變（同一話題但方向或情緒變了）
📊 情緒變化（整體市場情緒的轉變）
⚡ 值得注意（根據變化推測的趨勢或機會）

=== 較早的摘要 ===
{old_summary[:4000]}

=== 較新的摘要 ===
{new_summary[:4000]}"""
        text, usage = ai_call(prompt, SYS_DIFF, MODEL_OPUS, max_tokens=1500)
        _log_cost("摘要差異", usage, MODEL_OPUS)
        return text, None
    except Exception as e:
        return None, str(e)


def ai_extract_events(summary, chat_name):
    if not ai_available():
        return []
    try:
        prompt = f"""群組「{chat_name}」的總結：
{summary}

JSON 格式：[{{"title":"15字內","description":"簡述","importance":"high/normal/low","tags":"標籤"}}]"""
        text, usage = ai_call(prompt, SYS_EXTRACT, MODEL_SONNET, max_tokens=300)
        _log_cost("事件提取", usage, MODEL_SONNET)
        text = (text or "").strip()
        if not text:
            return []
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        return json.loads(text)
    except Exception as e:
        logger.warning("事件提取失敗: %s", e)
        return []


def ai_daily_digest(date_str):
    if not ai_available():
        return None, "AI backend 未就緒"

    with get_db_ctx() as conn:
        summaries = conn.execute(
            """
            SELECT chat_name, summary, message_count, summary_slot
            FROM daily_summaries
            WHERE date = ?
            ORDER BY summary_slot, chat_name
            """,
            (date_str,)
        ).fetchall()
        events = conn.execute(
            "SELECT title, description, importance, tags FROM events WHERE date = ?",
            (date_str,)
        ).fetchall()
        notes = conn.execute(
            "SELECT content, tags FROM notes WHERE date = ?",
            (date_str,)
        ).fetchall()

    if not summaries and not events and not notes:
        return None, "該日期沒有任何記錄"

    context = f"日期：{date_str}\n\n"
    rules_block = build_trading_rules_block()
    if rules_block:
        context += rules_block
    if summaries:
        context += "=== 各群組摘要 ===\n"
        for s in summaries:
            slot = f" {s['summary_slot']}" if s["summary_slot"] else ""
            context += f"\n【{s['chat_name']}{slot}】({s['message_count']} 則訊息)\n{s['summary']}\n"
    if events:
        context += "\n=== 標記的重要事件 ===\n"
        for e in events:
            context += f"- [{e['importance'].upper()}] {e['title']}: {e['description']} (標籤: {e['tags']})\n"
    if notes:
        context += "\n=== 筆記與觀察 ===\n"
        for n in notes:
            context += f"- {n['content']}" + (f" (標籤: {n['tags']})" if n['tags'] else "") + "\n"

    try:
        prompt = f"""生成每日情報簡報：

格式：
1. 今日概覽（2-3 句）
2. 關鍵事件排序
3. 市場情緒
4. 明日關注
5. 個人提醒

{context}"""
        text, usage = ai_call(prompt, SYS_DIGEST, MODEL_OPUS, max_tokens=1000)
        _log_cost("每日報告", usage, MODEL_OPUS)
        if text:
            update_rule_hits(text)
        return text, None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Save/update helpers (DB write side of the summarize pipeline)
# ---------------------------------------------------------------------------

def save_daily_summary(chat_id, chat_name, hours, messages, summary, summary_date=None,
                       summary_json=None, source="manual", summary_slot=None,
                       period_start=None, period_end=None):
    """Persist a daily summary row.

    `summary_json` is the raw JSON tail (string) emitted by profiles that
    output a structured block after `===JSON===`. Stored as-is for
    debuggability; validation/parsing is the consumer's job.

    `summary_slot` is the UTC+8 slot label (for example "10:00" or "22:00")
    used by auto runs. Manual summaries use the empty slot so a manual rerun
    still replaces the prior manual row for that day/chat.

    `source` is 'manual' (UI button) or 'auto' (background loop). On
    conflict, source is only overwritten when the new write is also
    'manual' — auto cycles never clobber a manual summary, but a manual
    rerun replaces an earlier auto draft.
    """
    summary_date = summary_date or date.today().isoformat()
    chat_id = str(chat_id)
    summary_slot = (summary_slot or "").strip()
    period_start = period_start or ""
    period_end = period_end or ""
    raw_messages = encode_raw_messages(messages)

    with get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary,
             raw_messages, summary_json, summary_slot, period_start, period_end,
             source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, chat_id, summary_slot) DO UPDATE SET
                chat_name = excluded.chat_name,
                hours = excluded.hours,
                message_count = excluded.message_count,
                summary = excluded.summary,
                raw_messages = excluded.raw_messages,
                summary_json = excluded.summary_json,
                period_start = excluded.period_start,
                period_end = excluded.period_end,
                source = CASE
                    WHEN excluded.source = 'manual' THEN excluded.source
                    ELSE daily_summaries.source
                END
        """, (summary_date, chat_id, chat_name, hours, len(messages), summary,
              raw_messages, summary_json, summary_slot, period_start, period_end,
              source))
        row = conn.execute(
            """
            SELECT id FROM daily_summaries
            WHERE date = ? AND chat_id = ? AND summary_slot = ?
            """,
            (summary_date, chat_id, summary_slot),
        ).fetchone()
        if row is not None:
            try:
                save_messages_for_summary(conn, messages, chat_id, chat_name, summary_id=row["id"])
            except Exception:
                logger.exception("save_messages_for_summary 失敗 (summary=%s)", row["id"])
        conn.commit()

    if row is None:
        raise RuntimeError("Failed to save daily summary.")
    return row["id"], summary_date


def _auto_message_cutoff_iso(hours):
    """Return a UTC ISO cutoff matching the format stored from Telethon."""
    try:
        hours_value = max(0.1, float(hours))
    except (TypeError, ValueError):
        hours_value = 24.0
    return (datetime.now(timezone.utc) - timedelta(hours=hours_value)).isoformat()


def _wallet_auto_fallback_summary(msg_text, reason):
    """Return a deterministic wallet_log body when the LLM path is too slow."""
    try:
        fallback_cap = max(1000, int(AUTO_SUMMARIZE_WALLET_FALLBACK_CHARS))
    except (TypeError, ValueError):
        fallback_cap = 24000
    fallback_cap = min(fallback_cap, max(1000, AUTO_SUMMARIZE_WALLET_HARD_CAP))
    fallback_body = (msg_text or "")[:fallback_cap]
    if len(msg_text or "") > fallback_cap:
        fallback_body += (
            f"\n\n[Truncated deterministic wallet rollup: "
            f"{len(msg_text):,} -> {fallback_cap:,} chars]"
        )
    return (
        f"[AI auto-summary skipped: {reason}; saved deterministic wallet rollup "
        "so this batch will not retry forever.]\n\n"
        f"{fallback_body}"
    )


def _auto_fallback_summary(msg_text, reason, profile_name="group_chat"):
    """Return a deterministic body when auto LLM generation is unavailable."""
    if profile_name in ("wallet_log", "wallet_log_priority"):
        return _wallet_auto_fallback_summary(msg_text, reason)
    try:
        fallback_cap = max(1000, int(AUTO_SUMMARIZE_FALLBACK_CHARS))
    except (TypeError, ValueError):
        fallback_cap = 12000
    fallback_body = (msg_text or "")[:fallback_cap]
    if len(msg_text or "") > fallback_cap:
        fallback_body += (
            f"\n\n[Truncated deterministic source: "
            f"{len(msg_text):,} -> {fallback_cap:,} chars]"
        )
    return (
        f"[AI auto-summary fallback: {reason}; saved deterministic source excerpt "
        "so this slot has coverage.]\n\n"
        f"{fallback_body}"
    )


def _run_auto_summary_post_hooks(summary_id, summary_text, profile_name, profile,
                                 chat_id, chat_name, summary_date, hours, count,
                                 model):
    """Best-effort parity with the manual summary path."""
    if not summary_id or not summary_text:
        return

    post_hooks = set(profile.get("post_hooks", ("events", "sentiment", "embedding")))

    if "events" in post_hooks:
        try:
            events = ai_extract_events(summary_text, chat_name)
            replace_summary_events(summary_id, summary_date, chat_name, events)
        except Exception:
            logger.exception("auto-summarize: event extraction failed (summary=%s)",
                             summary_id)

    downstream = tuple(h for h in ("sentiment", "embedding") if h in post_hooks)
    if downstream:
        try:
            post_summarize(summary_text, summary_id, chat_id, chat_name, hooks=downstream)
        except Exception:
            logger.exception("auto-summarize: post hooks failed (summary=%s)",
                             summary_id)

    json_cfg = profile.get("json_extract")
    if json_cfg:
        try:
            submit_summary_json_extract(
                summary_id, profile_name, json_cfg, summary_text,
                chat_name, hours, count, summary_date, model,
            )
        except Exception:
            logger.exception("auto-summarize: JSON extract submit failed (summary=%s)",
                             summary_id)


def summarize_chat_auto(chat_id, chat_name, hours=24, model=None,
                        since_iso=None, until_iso=None, summary_date=None,
                        summary_slot=None, topic_ids=None,
                        force_fallback_reason=None, collect_metrics=False):
    """Synchronous summarize for the auto loop — no streaming, no SSE.

    Pulls the chat's last `hours` of messages from the messages table
    (already populated by auto-fetch), resolves the prompt profile from
    the chat's category, runs ai_call once, saves with source='auto'.

    Returns (summary_id, status) where status ∈
      {'ok', 'skipped_existing', 'skipped_no_messages', 'skipped_ai_unavailable'}.
    Raises only on unexpected failures.

    wallet_log is included — it uses a deterministic pre-aggregator instead
    of LLM compression, but the final summary call still runs through the
    wallet_log profile's prompt.

    Slot-aligned catch-up: when `since_iso` / `until_iso` are given, the
    message window is `[since_iso, until_iso)` instead of the rolling
    `hours`-derived cutoff. `summary_date` selects which daily_summaries
    row to write. `summary_slot` (or the HH:MM derived from `until_iso`) keeps
    the 10:00 and 22:00 runs as separate rows on the same date/chat.
    """
    chat_id = str(chat_id)
    metrics = {
        "chat_id": chat_id,
        "chat_name": chat_name,
        "profile": "",
        "prompt_len": 0,
        "msg_text_len": 0,
        "message_count": 0,
        "fallback_used": False,
        "stream_error": "",
        "prep_mode": "",
    }

    def _ret(summary_id, status):
        if collect_metrics:
            return summary_id, status, dict(metrics)
        return summary_id, status

    if not ai_available():
        return _ret(None, "skipped_ai_unavailable")

    topic_filter = []
    for topic_id in topic_ids or []:
        try:
            topic_filter.append(int(topic_id))
        except (TypeError, ValueError):
            pass
    topic_filter = sorted(set(topic_filter))
    target_date = summary_date or date.today().isoformat()
    interval_secs = AUTO_SUMMARIZE_INTERVAL_HOURS * 3600
    if since_iso is None:
        since_iso = _auto_message_cutoff_iso(hours)
    period_start = since_iso or ""
    period_end = until_iso or ""
    if summary_slot is None:
        if until_iso is not None:
            try:
                slot_dt = datetime.fromisoformat(until_iso).astimezone(TAIPEI_TZ)
                summary_slot = slot_dt.strftime("%H:%M")
            except Exception:
                summary_slot = ""
        else:
            summary_slot = ""
    summary_slot = (summary_slot or "").strip()

    # Per-slot dedupe:
    # - manual summary in the same slot never gets clobbered
    # - rerunning a slot appends only newly archived messages to that slot row
    # - different slots on the same day become separate daily_summaries rows
    existing_row = None
    recent_existing = False
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT id, source, summary, message_count, hours, created_at, "
            "       summary_slot, period_start, period_end "
            "FROM daily_summaries "
            "WHERE date = ? AND chat_id = ? AND summary_slot = ?",
            (target_date, chat_id, summary_slot),
        ).fetchone()
        if row:
            if (row["source"] or "manual") == "manual":
                return _ret(row["id"], "skipped_existing")
            try:
                last_dt = datetime.fromisoformat(row["created_at"])
                if (datetime.now() - last_dt).total_seconds() < interval_secs - 300:
                    recent_existing = True
            except Exception:
                pass
            existing_row = dict(row)

        # Resolve prompt profile from category mapping (same logic as the
        # streaming endpoint — keeps auto / manual behavior consistent).
        profile_name = DEFAULT_PROFILE
        row = conn.execute("""
            SELECT cc.prompt_profile FROM chat_category_map m
            JOIN chat_categories cc ON cc.id = m.category_id
            WHERE m.chat_id = ?
        """, (chat_id,)).fetchone()
        if row and row["prompt_profile"] in PROFILES:
            profile_name = row["prompt_profile"]
        metrics["profile"] = profile_name

        sql = """
            SELECT m.id AS message_row_id, m.msg_id, m.date, m.sender_name,
                   m.sender_username, m.sender_id, m.topic_id, m.text, m.media
            FROM messages m
            WHERE m.chat_id = ?
              AND m.date >= ?
        """
        params = [chat_id, since_iso]
        if topic_filter:
            placeholders = ",".join("?" for _ in topic_filter)
            sql += f" AND m.topic_id IN ({placeholders})"
            params.extend(topic_filter)
        if until_iso is not None:
            sql += " AND m.date < ?"
            params.append(until_iso)
        if summary_slot:
            # Slot runs may use a lookback buffer. Only skip messages already
            # attached to an auto summary; manual/on-demand summaries should
            # not prevent the scheduled slot from covering the same message.
            sql += """
                AND NOT EXISTS (
                    SELECT 1
                    FROM message_summary_links l
                    JOIN daily_summaries ds ON ds.id = l.summary_id
                    WHERE l.message_id = m.id
                      AND ds.source = 'auto'
                )
            """
        elif existing_row:
            # Auto-fetch writes new rows with summary_id NULL. Once an auto
            # summary saves, save_messages_for_summary links covered rows to
            # that summary_id. Re-summarize only rows not yet covered by this
            # auto row so append cycles do not duplicate the same window.
            sql += """
                AND NOT EXISTS (
                    SELECT 1 FROM message_summary_links l
                    WHERE l.message_id = m.id AND l.summary_id = ?
                )
            """
            params.append(existing_row["id"])
        sql += " ORDER BY m.date"
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return _ret(None, "skipped_no_messages")
    if recent_existing:
        logger.info(
            "auto-summarize: %s has %d unsummarized message(s) despite recent auto row; appending catch-up",
            chat_name, len(rows),
        )

    messages = [{
        "id": r["msg_id"],
        "date": r["date"],
        "from": r["sender_name"],
        "username": r["sender_username"],
        "sender_id": r["sender_id"],
        "topic_id": r["topic_id"],
        "text": r["text"] or "",
        "media": r["media"] or "",
    } for r in rows]
    metrics["message_count"] = len(messages)

    profile = get_profile(profile_name)
    is_wallet_profile = bool(profile.get("is_wallet"))

    # ---- prep stage: profile-specific message preparation ----
    # Both branches end with `prep_lines` (a list of text lines) so the
    # downstream "size check → compress if needed → hard cap" logic is
    # shared between wallet profiles and others.
    if is_wallet_profile:
        agg_text = wallet_aggregator.aggregate_token_flows(
            messages,
            hours=hours,
            max_tokens=AUTO_SUMMARIZE_WALLET_MAX_TOKEN_ITEMS,
            max_wallets_per_token=AUTO_SUMMARIZE_WALLET_MAX_WALLETS_PER_TOKEN,
            max_unparsed_items=AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS,
            transfer_alert_usd=AUTO_SUMMARIZE_WALLET_TRANSFER_ALERT_USD,
            max_transfer_alerts=AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ALERTS,
        )
        if not agg_text or not agg_text.strip():
            return _ret(None, "skipped_no_messages")
        # Cross-window + (Tier-1) on-chain holdings — the 8h rollup can't show
        # positions opened earlier. Prepend so it leads the prompt and survives
        # the tail hard-cap. Best-effort: never let a holdings lookup break the
        # summary run.
        try:
            holdings_block = holdings.build_holdings_context(messages, profile_name)
        except Exception as exc:
            logger.warning("auto-summarize: %s holdings context failed: %r", chat_name, exc)
            holdings_block = ""
        if holdings_block:
            agg_text = holdings_block + "\n" + agg_text
            metrics["holdings_context_chars"] = len(holdings_block)
        prep_lines = agg_text.splitlines()
        # Wallet auto uses a compact token-centric deterministic rollup,
        # then still runs the final AI analysis. Avoid an LLM compression pass
        # here; dense Ray days can hang there before reaching the final pass.
        direct_limit = None
    else:
        prep_lines = prepare_lines(messages)
        if not prep_lines:
            return _ret(None, "skipped_no_messages")
        direct_limit = profile.get("direct_limit", DIRECT_LIMIT)

    total_chars = sum(len(l) for l in prep_lines)
    if (
        profile_name == "group_chat"
        and total_chars > AUTO_SUMMARIZE_GROUP_ROLLUP_TRIGGER_CHARS
        and not force_fallback_reason
    ):
        # R1: Per-sender pre-summarize. One alpha-caller dumping 100k+ chars
        # makes the rollup hit its hard cap on flat text, which Sonnet can't
        # chew on (0 tokens for 600s, retry, repeat). Compress each whale's
        # messages into a structured {ticker, thesis, action, target, risk}
        # table via Haiku BEFORE the rollup; the rollup then operates on a
        # much smaller signal-dense input.
        messages_for_rollup, per_sender_stats = per_sender_pre_summarize(
            messages, chat_name,
        )
        if per_sender_stats.get("transformed"):
            metrics["per_sender_extracts"] = per_sender_stats["transformed"]
            metrics["per_sender_saved_chars"] = per_sender_stats.get("saved_chars", 0)
        msg_text = group_aggregator.build_group_chat_rollup(
            messages_for_rollup,
            trust_map=load_trust_map(),
            target_chars=AUTO_SUMMARIZE_GROUP_ROLLUP_TARGET_CHARS,
            max_entity_samples=AUTO_SUMMARIZE_GROUP_MAX_ENTITY_SAMPLES,
            max_timeline_samples=AUTO_SUMMARIZE_GROUP_MAX_TIMELINE_SAMPLES,
            max_high_signal_lines=AUTO_SUMMARIZE_GROUP_MAX_HIGH_SIGNAL_LINES,
        )
        metrics["prep_mode"] = "group_rollup"
        logger.info(
            "auto-summarize: %s [%s] group rollup %d → %d chars",
            chat_name, profile_name, total_chars, len(msg_text),
        )
    elif force_fallback_reason and not is_wallet_profile:
        msg_text = "\n".join(prep_lines)
        metrics["prep_mode"] = "fallback_raw"
    elif direct_limit is None or total_chars <= direct_limit:
        msg_text = "\n".join(prep_lines)
        metrics["prep_mode"] = "direct"
    else:
        # Fan out compression for non-wallet profiles.
        logger.info(
            "auto-summarize: %s [%s] prompt %d chars > %d limit — running %s compression",
            chat_name, profile_name, total_chars, direct_limit,
            "wallet" if is_wallet_profile else "standard",
        )
        chunks = split_chunks(prep_lines)
        compressed = None
        for prog in compress_chunks_parallel(
            chunks, chat_name,
            compress_system=profile.get("compress_system"),
        ):
            if prog["results"] is not None:
                compressed = prog["results"]
        msg_text = "\n\n".join(compressed or [])
        metrics["prep_mode"] = "llm_compress"
        logger.info(
            "auto-summarize: %s [%s] compressed %d → %d chars",
            chat_name, profile_name, total_chars, len(msg_text),
        )

    # Hard cap for wallet profiles only — final safety net even after
    # compression, because pathologically dense days can compress poorly and
    # the CLI subprocess can hang at 0 tokens forever on >120k prompts.
    # Truncated tail is the lowest-value bucket (small approves /
    # micro-transfers).
    if is_wallet_profile and len(msg_text) > AUTO_SUMMARIZE_WALLET_HARD_CAP:
        original_len = len(msg_text)
        msg_text = msg_text[:AUTO_SUMMARIZE_WALLET_HARD_CAP] + (
            f"\n\n…(原始 {original_len:,} 字符,已截斷至 "
            f"{AUTO_SUMMARIZE_WALLET_HARD_CAP:,} 字以避免 CLI buffer 卡住)"
        )
        logger.warning(
            "auto-summarize: %s [%s] truncated %d → %d chars (hard cap)",
            chat_name, profile_name, original_len, len(msg_text),
        )

    # Same safety net for group_chat. Rollup targets 70k but doesn't guarantee
    # it, and direct/compress paths can leave msg_text well past 100k on
    # high-density days — exactly the region where CLI hangs at 0 tokens.
    if (
        profile_name == "group_chat"
        and len(msg_text) > AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP
    ):
        original_len = len(msg_text)
        msg_text = msg_text[:AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP] + (
            f"\n\n…(原始 {original_len:,} 字符,已截斷至 "
            f"{AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP:,} 字以避免 CLI buffer 卡住)"
        )
        logger.warning(
            "auto-summarize: %s [%s] truncated %d → %d chars (group_chat hard cap)",
            chat_name, profile_name, original_len, len(msg_text),
        )

    fmt_kwargs = {
        "chat_name": chat_name,
        "hours": hours,
        "count": len(messages),
        "msg_text": msg_text,
        "coin_profile_context": build_coin_profile_context(messages),
    }
    if profile["needs_history"]:
        fmt_kwargs["history_context"] = build_history_context(chat_id, days=7)
        fmt_kwargs["date_str"] = target_date

    prompt_template = PROMPT_WALLET_AUTO_COMPACT if is_wallet_profile else profile["template"]
    prompt = prompt_template.format(**fmt_kwargs)
    metrics["prompt_len"] = len(prompt)
    metrics["msg_text_len"] = len(msg_text)
    system_prompt = SYS_WALLET_AUTO_COMPACT if is_wallet_profile else profile["system"]
    max_output_tokens = (
        AUTO_SUMMARIZE_WALLET_AUTO_MAX_TOKENS
        if is_wallet_profile
        else profile.get("max_tokens", 8192)
    )
    idle_timeout = (
        AUTO_SUMMARIZE_WALLET_IDLE_TIMEOUT_SECS
        if is_wallet_profile
        else AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS
    )
    target_model = model or MODEL_SONNET
    summary_text = ""

    # ---- generation stage: stream with idle watchdog ----
    # Manual path uses ai_stream + with_watchdog because broadcast on CLI
    # legitimately runs 3–5min generating its 9k-token report. The auto
    # path's earlier 300s idle timeout false-positived on heavy chats whose
    # first token took 4–5min on CLI (stdout buffering). Now configurable;
    # 600s default gives heavy summaries room without burning quota forever.
    if force_fallback_reason:
        metrics["fallback_used"] = True
        metrics["stream_error"] = force_fallback_reason
        summary_text = _auto_fallback_summary(
            msg_text, force_fallback_reason, profile_name,
        )
        logger.warning(
            "auto-summarize: %s [%s] forced deterministic fallback; "
            "prompt_len=%d msg_text_len=%d fallback=%d chars reason=%s",
            chat_name, profile_name, len(prompt), len(msg_text),
            len(summary_text), force_fallback_reason,
        )
        text_acc = []
        stream_error = None
    elif is_wallet_profile and len(prompt) > AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP:
        metrics["fallback_used"] = True
        reason = (
            f"wallet prompt {len(prompt):,} chars exceeds "
            f"{AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP:,} char auto LLM cap"
        )
        metrics["stream_error"] = reason
        summary_text = _wallet_auto_fallback_summary(msg_text, reason)
        logger.warning(
            "auto-summarize: %s [%s] skipped LLM; prompt_len=%d "
            "msg_text_len=%d cap=%d fallback=%d chars",
            chat_name, profile_name, len(prompt), len(msg_text),
            AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP, len(summary_text),
        )
        text_acc = []
        stream_error = None
    else:
        # Retry once on zero-token outcomes (watchdog trip or empty stream).
        # Historically these are transient CLI subprocess hangs that clear up
        # on a fresh invocation — much cheaper than waiting the full idle
        # timeout twice and then falling back deterministically. Partial
        # output is kept (no retry) because the model already did real work.
        max_attempts = 2
        text_acc = []
        stream_error = None
        for attempt in range(1, max_attempts + 1):
            text_acc = []
            stream_error = None
            try:
                source = ai_stream(
                    prompt, system_prompt, target_model,
                    max_tokens=max_output_tokens,
                )
                for evt in with_watchdog(
                    source,
                    idle_timeout=idle_timeout,
                    heartbeat_every=AUTO_SUMMARIZE_HEARTBEAT_SECS,
                ):
                    if evt["type"] == "token":
                        token = evt.get("token", "")
                        if token:
                            text_acc.append(token)
                    elif evt["type"] == "heartbeat":
                        # No UI watching — log so you can see "still alive" while
                        # CLI's stdout sits buffered. Mentions whether tokens have
                        # started flowing yet (useful for diagnosing first-token lag).
                        idle = evt.get("idle_secs", 0)
                        elapsed = evt.get("elapsed_secs", 0)
                        state = f"got {len(text_acc)} tokens" if text_acc else "no tokens yet"
                        attempt_tag = f" attempt={attempt}/{max_attempts}" if max_attempts > 1 else ""
                        logger.info(
                            "auto-summarize: ⏳ %s [%s] still running — elapsed=%ds, idle=%ds, %s%s",
                            chat_name, profile_name, elapsed, idle, state, attempt_tag,
                        )
                    elif evt["type"] == "error":
                        stream_error = evt.get("error", "unknown")
                        break
                    # ignore done
            except Exception as e:
                stream_error = str(e)

            # Keep partial output as-is; only retry on truly zero progress.
            if text_acc:
                break
            if attempt < max_attempts:
                metrics["retries"] = attempt
                logger.warning(
                    "auto-summarize: %s [%s] attempt %d/%d got 0 tokens "
                    "(stream_error=%s) — retrying once",
                    chat_name, profile_name, attempt, max_attempts,
                    stream_error or "(empty output)",
                )

        summary_text = "".join(text_acc).strip()
    if stream_error or not summary_text:
        if stream_error:
            metrics["stream_error"] = str(stream_error)
        # Surface the prompt-side context so the upstream CLI diagnostic line
        # (logged from _cli_stream) can be cross-referenced with which chat /
        # profile / size triggered the hang.
        logger.warning(
            "auto-summarize 中斷診斷: chat=%s [id=%s] profile=%s model=%s "
            "prompt_len=%d msg_text_len=%d messages=%d tokens_received=%d "
            "stream_error=%s",
            chat_name, chat_id, profile_name, target_model,
            len(prompt), len(msg_text), len(messages), len(text_acc),
            stream_error or "(empty output)",
        )
        if not summary_text:
            metrics["fallback_used"] = True
            summary_text = _auto_fallback_summary(
                msg_text,
                f"stream failed before usable output ({stream_error or 'empty output'})",
                profile_name,
            )
            logger.warning(
                "auto-summarize: %s [%s] saved deterministic fallback (%d chars)",
                chat_name, profile_name, len(summary_text),
            )
        else:
            metrics["fallback_used"] = True
            summary_text += (
                f"\n\n[AI stream ended with error after partial output: "
                f"{stream_error or 'no output'}]"
            )
            logger.warning(
                "auto-summarize: %s [%s] saved partial output after stream error",
                chat_name, profile_name,
            )

    # Heading for an append uses the chunk's window endpoint when given so
    # catch-up entries on a past row are labeled by their slot's wall-clock
    # time, not by when the catch-up happens to run.
    if until_iso is not None:
        try:
            heading_dt = datetime.fromisoformat(until_iso).astimezone(TAIPEI_TZ)
        except Exception:
            heading_dt = datetime.now(TAIPEI_TZ)
    else:
        heading_dt = datetime.now(TAIPEI_TZ)
    heading_str = heading_dt.strftime('%Y-%m-%d %H:%M')

    if existing_row:
        # Append this chunk to the existing auto row instead of replacing.
        # message_count / hours accumulate so the row reflects total coverage;
        # raw_messages stays as-is (per-message data lives in the messages table).
        merged_summary = (
            f"{existing_row['summary']}\n\n"
            f"--- 補充更新 ({heading_str}) ---\n\n"
            f"{summary_text}"
        )
        merged_count = (existing_row["message_count"] or 0) + len(messages)
        merged_hours = float(existing_row["hours"] or 0) + float(hours)
        merged_period_start = existing_row.get("period_start") or period_start
        with get_db_ctx() as conn:
            conn.execute(
                "UPDATE daily_summaries "
                "SET summary = ?, message_count = ?, hours = ?, "
                "    period_start = ?, period_end = ?, "
                "    chat_name = COALESCE(?, chat_name), "
                "    created_at = datetime('now', 'localtime') "
                "WHERE id = ?",
                (merged_summary, merged_count, merged_hours,
                 merged_period_start, period_end, chat_name, existing_row["id"]),
            )
            try:
                save_messages_for_summary(conn, messages, chat_id, chat_name,
                                          summary_id=existing_row["id"])
            except Exception:
                logger.exception("save_messages_for_summary 失敗 (summary=%s)",
                                 existing_row["id"])
            conn.commit()
        update_rule_hits(summary_text)
        _run_auto_summary_post_hooks(
            existing_row["id"], merged_summary, profile_name, profile,
            chat_id, chat_name, target_date, hours, len(messages), target_model,
        )
        return _ret(existing_row["id"], "ok")

    summary_id, _ = save_daily_summary(
        chat_id, chat_name, hours, messages, summary_text,
        summary_date=target_date, source="auto", summary_slot=summary_slot,
        period_start=period_start, period_end=period_end,
    )
    update_rule_hits(summary_text)
    _run_auto_summary_post_hooks(
        summary_id, summary_text, profile_name, profile,
        chat_id, chat_name, target_date, hours, len(messages), target_model,
    )
    return _ret(summary_id, "ok")


def update_summary_json(summary_id, summary_json):
    """Attach or replace the structured JSON sidecar for an existing summary."""
    if not summary_id:
        return
    with get_db_ctx() as conn:
        conn.execute(
            "UPDATE daily_summaries SET summary_json = ? WHERE id = ?",
            (summary_json, summary_id),
        )
        conn.commit()


def _bg_extract_summary_json(summary_id, profile_name, json_cfg, summary,
                             chat_name, hours, count, date_str, model):
    try:
        json_prompt = json_cfg["template"].format(
            markdown_report=summary,
            chat_name=chat_name,
            hours=hours,
            count=count,
            date_str=date_str,
        )
        json_text, json_usage = ai_call(
            json_prompt, json_cfg["system"], model,
            max_tokens=json_cfg.get("max_tokens", 3000),
        )
        _log_cost(f"JSON 背景抽取({profile_name})", json_usage, model)
        json_text = (json_text or "").strip()
        if not json_text:
            return
        try:
            json.loads(json_text)
        except Exception as e:
            logger.warning(
                "profile=%s background JSON malformed (summary=%s, %d chars): %s",
                profile_name, summary_id, len(json_text), e,
            )
        update_summary_json(summary_id, json_text)
        logger.info("profile=%s background JSON saved (summary=%s)", profile_name, summary_id)
    except Exception:
        logger.exception("profile=%s background JSON extract failed (summary=%s)",
                         profile_name, summary_id)


def submit_summary_json_extract(summary_id, profile_name, json_cfg, summary,
                                chat_name, hours, count, date_str, model):
    """Queue non-critical structured JSON extraction after markdown is saved."""
    if not summary_id or not json_cfg:
        return None
    return _bg_executor.submit(
        _bg_extract_summary_json,
        summary_id, profile_name, json_cfg, summary,
        chat_name, hours, count, date_str, model,
    )


def replace_summary_events(summary_id, summary_date, chat_name, events):
    if not summary_id or not events:
        return []

    saved_events = []
    with get_db_ctx() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM events WHERE source_summary_id = ?", (summary_id,))
            for event in events:
                title = clean_text(event.get("title"))
                if not title:
                    continue
                saved = {
                    "title": title,
                    "description": clean_text(event.get("description")),
                    "importance": clean_text(event.get("importance")) or "normal",
                    "tags": clean_text(event.get("tags")),
                }
                conn.execute("""
                    INSERT INTO events
                    (date, title, description, importance, tags, source_chat, source_summary_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (summary_date, saved["title"], saved["description"],
                      saved["importance"], saved["tags"], chat_name, summary_id))
                saved_events.append(saved)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return saved_events


# ---------------------------------------------------------------------------
# Post-summarize: sentiment (sync for return) + embedding (fire-and-forget)
# ---------------------------------------------------------------------------

_bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="post-summarize")
atexit.register(_bg_executor.shutdown, wait=True)


def _bg_store_embedding(source_type, source_id, text):
    try:
        store_embedding(source_type, source_id, text)
    except Exception:
        logger.exception("Embedding 儲存失敗 (source_type=%s, source_id=%s)",
                         source_type, source_id)


def post_summarize(summary, summary_id, chat_id, chat_name, hooks=("sentiment", "embedding")):
    """Run post-summarize hooks. `hooks` controls which sub-steps fire:
    - "sentiment" → synchronous score extraction (returned) + async persist
    - "embedding" → fire-and-forget Voyage embedding store
    Returns the sentiment dict (or None if not requested / unavailable).
    """
    if "embedding" in hooks and get_voyage_client():
        _bg_executor.submit(_bg_store_embedding, "summary", summary_id, summary)

    sentiment = None
    if "sentiment" in hooks:
        sentiment = ai_extract_sentiment(summary, chat_name)
        if sentiment:
            _bg_executor.submit(save_sentiment, summary_id, chat_id, chat_name, sentiment)
    return sentiment


# ---------------------------------------------------------------------------
# Memory Q&A (RAG)
# ---------------------------------------------------------------------------

_TRUST_MARK = {"trusted": "⭐", "noise": "🔇"}


def _format_coin_sample_line(sample, mode):
    """Render a single sample entry for the synthesis context."""
    raw_date = sample.get("date") or ""
    if mode == "ca":
        # Message-level: [timestamp UTC+8] trust sender: text
        ts = to_taipei_str(raw_date)
        sender = sample.get("sender_name") or "未知"
        mark = _TRUST_MARK.get(sample.get("trust"), "")
        text = (sample.get("text") or "").replace("\n", " ").strip()
        return f"  [{ts}] {mark}{sender}: {text}"
    # Ticker mode: [date] snippet — date is already YYYY-MM-DD (no tz)
    snippet = (sample.get("snippet") or "").replace("\n", " ").strip()
    return f"  [{raw_date}] {snippet}"


def ai_synthesize_coin(query, search_result, model=None):
    """Cross-chat synthesis for a coin/CA. search_result comes from db.search_coin().
    `model`: optional Claude model override; defaults to Sonnet."""
    if not ai_available():
        return None, "AI backend 未就緒"

    per_chat = search_result.get("per_chat") or []
    events = search_result.get("events") or []
    if not per_chat and not events:
        return None, "記憶庫裡找不到這顆幣的討論"

    mode = search_result.get("mode") or "ticker"
    total_hits = search_result.get("total_hits") or 0
    total_msgs = search_result.get("total_msgs")
    ca_candidates = search_result.get("ca_candidates") or []

    lines = [f"查詢:{query}", f"搜尋模式:{mode}"]
    if mode == "ca":
        lines.append(f"總訊息命中:{total_hits} 則 / {len(per_chat)} 個群")
    else:
        msg_part = f",跨群總訊息 {total_msgs} 則" if total_msgs is not None else ""
        lines.append(f"總天數命中:{total_hits} 天 / {len(per_chat)} 個群{msg_part}")

    if mode == "ticker" and len(ca_candidates) > 1:
        def _ca_label(c):
            if isinstance(c, dict):
                return f"{c.get('ca', '?')}({c.get('chat_count', 0)}群/{c.get('msg_count', 0)}則)"
            return str(c)
        lines.append(
            f"\n⚠️ 偵測到 {len(ca_candidates)} 個不同 CA 被命中訊息引用,"
            f"可能是多鏈同名/多版本幣:{', '.join(_ca_label(c) for c in ca_candidates[:6])}"
        )

    if per_chat:
        if mode == "ca":
            lines.append("\n【各群訊息命中(按首次提及時間排序,時間為 UTC+8)】")
        else:
            lines.append("\n【各群命中(按天數熱度排序)】")
        for b in per_chat:
            header_extra = ""
            if mode == "ca":
                header_extra = f" — {b.get('msg_count', 0)} 則訊息"
                first_disp = to_taipei_str(b.get("first_date") or "") or b.get("first_date") or ""
                last_disp = to_taipei_str(b.get("last_date") or "") or b.get("last_date") or ""
            else:
                days = b.get("hit_days") or b.get("hit_count") or 0
                msgs = b.get("msg_count") or 0
                header_extra = f" — {days} 天 / {msgs} 則訊息"
                if b.get("cas"):
                    header_extra += f",該群命中 CA:{', '.join(b['cas'][:3])}"
                first_disp = b.get("first_date") or ""
                last_disp = b.get("last_date") or ""
            lines.append(
                f"\n▸ {b['chat_name']}{header_extra},"
                f"{first_disp} → {last_disp}"
            )
            for s in b.get("samples") or []:
                lines.append(_format_coin_sample_line(s, mode))

    if events:
        lines.append("\n【匹配的重要事件】")
        for e in events:
            lines.append(
                f"- {e['date']} [{e.get('importance') or 'normal'}] "
                f"{e.get('title') or ''}: {e.get('description') or ''}"
            )

    context = "\n".join(lines)
    if model == "opus":
        target_model = MODEL_OPUS
    elif model == "sonnet":
        target_model = MODEL_SONNET
    else:
        target_model = model or MODEL_SONNET

    try:
        text, usage = ai_call(
            f"整合以下關於「{query}」的跨群情報,依格式輸出:\n\n{context}",
            SYS_COIN_SYNTHESIS, target_model, max_tokens=2000,
        )
        _log_cost("跨群幣種總結", usage, target_model)
        return (text or "").strip(), None
    except Exception as e:
        return None, str(e)


def _format_structured_summary_lines(rows):
    """Extract high-signal bullets from daily_summaries.summary_json rows.

    Looks for the radar schema produced by the broadcast profile, with a
    fallback to the legacy key_takeaways / actionable / watchlist fields.
    """
    lines = []
    for r in rows:
        raw = r["summary_json"] if "summary_json" in r.keys() else None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        slot = f" {r['summary_slot']}" if "summary_slot" in r.keys() and r["summary_slot"] else ""
        date_tag = f"{r['date']} {r['chat_name']}{slot}"
        checklist = data.get("checklist") or []
        for item in checklist[:6]:
            if not isinstance(item, dict):
                continue
            priority = (item.get("priority") or "").strip()
            target = (item.get("target") or "").strip()
            action = (item.get("action") or "").strip()
            why = (item.get("why_now") or "").strip()
            if target or action:
                head = f"{priority} " if priority else ""
                lines.append(
                    f"[{date_tag}] 待查:{head}{target}{(' — ' + action) if action else ''}"
                    f"{(' | 因:' + why) if why else ''}"
                )
        radar = data.get("radar") or []
        for item in radar[:8]:
            if not isinstance(item, dict):
                continue
            target = (item.get("target") or "").strip()
            status = (item.get("status") or "").strip()
            strength = (item.get("strength") or "").strip()
            why = (item.get("why_now") or item.get("signal") or "").strip()
            next_step = (item.get("next_step") or "").strip()
            if target:
                lines.append(
                    f"[{date_tag}] 雷達:{target} [{status}/{strength}]"
                    f"{(' — ' + why) if why else ''}"
                    f"{(' | 下一步:' + next_step) if next_step else ''}"
                )
        needs_context = data.get("needs_context") or []
        for item in needs_context[:4]:
            if not isinstance(item, dict):
                continue
            clue = (item.get("clue") or "").strip()
            missing = (item.get("missing") or "").strip()
            if clue:
                lines.append(f"[{date_tag}] 缺口:{clue}{(' — 缺:' + missing) if missing else ''}")
        expired = data.get("expired") or []
        for item in expired[:4]:
            if not isinstance(item, dict):
                continue
            target = (item.get("target") or "").strip()
            reason = (item.get("reason") or "").strip()
            if target:
                lines.append(f"[{date_tag}] 過期:{target}{(' — ' + reason) if reason else ''}")
        takeaways = data.get("key_takeaways") or []
        for t in takeaways[:5]:
            title = (t.get("title") or "").strip()
            summary = (t.get("summary") or "").strip()
            if title or summary:
                lines.append(f"[{date_tag}] 重點:{title}{(' — ' + summary) if summary else ''}")
        actionable = data.get("actionable") or []
        for a in actionable[:4]:
            act = (a.get("action") or "").strip()
            cond = (a.get("condition") or "").strip()
            if act:
                lines.append(f"[{date_tag}] 行動:{act}{(' | 條件:' + cond) if cond else ''}")
        watchlist = data.get("watchlist") or []
        if watchlist:
            lines.append(f"[{date_tag}] watchlist:{', '.join(str(w) for w in watchlist[:10])}")
    return lines


def ai_memory_ask(question):
    if not ai_available():
        return None, "AI backend 未就緒"

    embedding_results = search_by_embedding(question, limit=8) or []

    summaries = []
    events = []
    notes = []
    with get_db_ctx() as conn:
        fts_query = build_fts_query(question, joiner=" OR ", min_len=2)
        if fts_query:
            try:
                summaries = conn.execute("""
                    SELECT ds.date, ds.chat_name, ds.summary, ds.summary_json,
                           ds.summary_slot
                    FROM summaries_fts fts
                    JOIN daily_summaries ds ON ds.id = fts.rowid
                    WHERE summaries_fts MATCH ?
                    ORDER BY rank LIMIT 10
                """, (fts_query,)).fetchall()
            except Exception:
                pass
            try:
                events = conn.execute("""
                    SELECT e.date, e.title, e.description, e.importance, e.tags
                    FROM events_fts fts
                    JOIN events e ON e.id = fts.rowid
                    WHERE events_fts MATCH ?
                    ORDER BY rank LIMIT 20
                """, (fts_query,)).fetchall()
            except Exception:
                pass
            try:
                notes = conn.execute("""
                    SELECT n.date, n.content, n.tags
                    FROM notes_fts fts
                    JOIN notes n ON n.id = fts.rowid
                    WHERE notes_fts MATCH ?
                    ORDER BY rank LIMIT 15
                """, (fts_query,)).fetchall()
            except Exception:
                pass

        # Fall back to recent rows when FTS is under-recall (keeps context populated
        # even for vague questions or queries where keywords don't match).
        if len(summaries) < 5:
            extra = conn.execute(
                """
                SELECT date, chat_name, summary, summary_json, summary_slot
                FROM daily_summaries
                ORDER BY date DESC, summary_slot DESC
                LIMIT ?
                """,
                (10 - len(summaries),)
            ).fetchall()
            seen = {(s['date'], s['chat_name'], s['summary_slot']) for s in summaries}
            summaries = list(summaries) + [
                r for r in extra
                if (r['date'], r['chat_name'], r['summary_slot']) not in seen
            ]
        if len(events) < 10:
            extra = conn.execute(
                "SELECT date, title, description, importance, tags FROM events ORDER BY date DESC LIMIT ?",
                (20 - len(events),)
            ).fetchall()
            seen = {(e['date'], e['title']) for e in events}
            events = list(events) + [r for r in extra if (r['date'], r['title']) not in seen]
        if len(notes) < 5:
            extra = conn.execute(
                "SELECT date, content, tags FROM notes ORDER BY date DESC LIMIT ?",
                (10 - len(notes),)
            ).fetchall()
            seen = {(n['date'], n['content']) for n in notes}
            notes = list(notes) + [r for r in extra if (r['date'], r['content']) not in seen]

    structured_lines = _format_structured_summary_lines(summaries)

    if not embedding_results and not summaries and not events and not notes:
        return None, "記憶庫是空的，請先擷取並 AI 總結一些群組訊息"

    context_parts = []
    if embedding_results:
        context_parts.append("【向量搜尋結果（按語意相關度排序）】")
        for r in embedding_results:
            context_parts.append(
                f"[{r['source_type']}] (相似度 {r['similarity']:.2f}): {r['chunk_text']}"
            )
        context_parts.append("")
    if structured_lines:
        context_parts.append("【broadcast 結構化重點（摘取自 summary_json）】")
        context_parts.extend(structured_lines)
        context_parts.append("")
    if summaries:
        context_parts.append("【群組摘要（關鍵字命中 + 近期）】")
        for s in summaries:
            slot = f" {s['summary_slot']}" if s["summary_slot"] else ""
            context_parts.append(f"{s['date']} {s['chat_name']}{slot}: {s['summary'][:400]}")
        context_parts.append("")
    if events:
        context_parts.append("【重要事件】")
        for e in events:
            context_parts.append(
                f"{e['date']} [{e['importance']}] {e['title']}: {e['description']} (標籤:{e['tags']})"
            )
        context_parts.append("")
    if notes:
        context_parts.append("【筆記與觀察】")
        for n in notes:
            context_parts.append(f"{n['date']}: {n['content']}")

    context = "\n".join(context_parts).strip()
    if not context:
        return None, "記憶庫是空的，請先擷取並 AI 總結一些群組訊息"

    try:
        text, usage = ai_call(
            f"記憶庫內容：\n{context}\n\n問題：{question}",
            SYS_MEMORY_ASK,
            MODEL_SONNET, max_tokens=800,
        )
        _log_cost("記憶問答", usage, MODEL_SONNET)
        return text, None
    except Exception as e:
        return None, str(e)
