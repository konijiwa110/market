# ChangeLog

## 1.15.0 — 按请求头判定真实上下文窗口,有效 trigger 自动夹到窗口之下

### 背景
代理只看请求体,本不知当前模型的真实窗口是 200k 还是 1M。很多用户实际是 200k 却不自知(模型不支持
1M,或第三方端点没透传 1M)。一旦把 `trigger` 配到 200k 以上(比如为 1M 调到 320k),主动压缩永远
不触发,模型先撞 200k 墙、上游返回 400 —— 现有 `_emergency_compress` 能接住不崩,但这是退化路径:
每次溢出都先白跑一发上游 400 + 付同步压缩延迟,且不自愈。

关键信号:Claude Code 用 `model[1m]` 时会给请求带 `anthropic-beta: context-1m-2025-08-07` 头。代理拦在
链路上能直接读到 → 确定性、每请求、零配置地判出真实窗口,在撞墙前就主动压。

### 变更(`proxy/server.py`)
- **请求头窗口判定**:新增 `_request_window(req_headers)` —— `anthropic-beta` 含 `context-1m` 子串判
  1M,否则 200k(子串匹配,对日期后缀变化稳健;头名大小写无关)。
- **有效 trigger 夹紧**:新增 `_effective_trigger(req_headers) = min(配置 trigger, 真实窗口 × 0.9)`。
  主动触发判定(唯一 gating 点)由比 `TRIGGER_TOKENS` 改为比有效 trigger,并在日志标出判出的窗口与
  是否夹紧。只在配置 trigger 超过窗口 90% 时才夹,正常配置(160k < 180k)不受影响。
- **第三方谎报 1M 的钉死覆盖**:新增配置 `context_window` / 环境 `ROLLING_CONTEXT_CONTEXT_WINDOW`
  (默认 0 = 走头判定),显式覆盖头判定。供「上游发了 1M 头实际只有 200k」的端点钉死真实窗口。
- **启动日志**:打印判定模式(头自动判 / config 钉死的窗口值)与夹紧比例。
- **不动**:`_emergency_compress` 与 400 prompt-too-long 同步兜底原样保留,作为头判定/配置都没拦住的
  真实撞墙的最后一层。

### 实测(2026-06-28)
`proxy/test_emergency.py` 新增 `RequestWindow`(含/不含 context-1m、混在多 beta 中、无头、头名大小写、
config 覆盖优先)与 `EffectiveTrigger`(正常不夹、配超夹到 200k×90%、1M 窗口不夹、override 钉死)共
9 例。全套 `test_emergency` + `test_compressor` + `test_stats` 合计 **41 例全绿**。

## 1.14.0 — 第二轮审查(compressor / stats / hooks)四处加固:统计隔离、文件压实、空响应防护、脚本对齐

### 背景
1.13 审查只覆盖了 `server.py`。这轮把审查扩到此前未细看的 `compressor.py`、`stats.py` 与四个
hook 脚本,修掉一处确定的隔离泄漏和三处健壮性/一致性遗漏。

### 变更
- **统计落盘尊重隔离目录(`proxy/server.py`)**:`stats = StatsCollector()` 原用 `stats.py` 里硬编码的
  `~/.claude/rolling-context-stats.jsonl`,而 pid/version/store 三个状态文件都走 `_CLAUDE_DIR`
  (读 `ROLLING_CONTEXT_STATE_DIR`)。后果是隔离实例(冒烟测试、`ROLLING_CONTEXT_DEV` 备用实例)会把
  统计写进真实 `~/.claude`、污染生产数据。改为从 `_CLAUDE_DIR` 拼路径传入,与其余状态文件一致。
- **统计 JSONL 压实防无界增长(`proxy/stats.py`)**:内存环形缓冲有 `MAX_RECORDS` 上限,但落盘文件
  只追加从不轮转,长存进程会让文件无限涨;且 `_load` 用 `readlines()` 整文件读进内存只为取尾部。
  新增 `_compact_locked()`:**启动时**若文件行数超上限即压实一次,**运行期**每追加满一个上限量
  再压实一次(`tmp` + `os.replace` 原子替换);文件写移入锁内,压实与并发写无竞态。文件稳态
  ≤ ~2×上限行。
- **摘要器空响应防护(`proxy/compressor.py`)**:`_summarize_chunk` 末尾直接 `data["content"][0]["text"]`,
  若上游回 200 但 `content` 为空(罕见 stop_reason / 拒答)会裸抛 `IndexError`,而自愈层只兜
  `RuntimeError` → 整次压缩失败。改为校验后抛**受控 `RuntimeError`**,`finally` 仍正常记一条统计。
- **平台脚本本地地址判断对齐(`hooks/start-proxy.sh`)**:判断「BASE_URL 是否指向本代理」时,`.ps1` 用
  端口级正则 `127\.0\.0\.1.*$Port`,`.sh` 只判 `"127.0.0.1" in existing`。本机另跑别的端口的本地上游时
  两端链式/fail-open 行为会分叉。`.sh` 改用端口级标记 `127.0.0.1:PORT`,与 `.ps1` 口径一致。

### 实测(2026-06-27)
新增 `proxy/test_stats.py`(运行期压实封顶、启动压实超大历史、未达上限重开不丢)、
`proxy/test_compressor.py`(空 / 缺 `content` 抛 `RuntimeError`、正常内容仍返回),
`test_emergency.py` 增 `StatsPathIsolation`(统计路径落在隔离目录)。
全套 `test_stats` + `test_compressor` + `test_emergency` + `test_lifecycle` 合计 **33 例全绿**。

## 1.13.0 — 整体功能审查后的四处加固:发布顺序竞态、日志降噪、内存封顶、emergency 回收

### 背景
1.11/1.12 把落盘与父条目回收做完后,对代理核心 `server.py` 做了一遍整体审查,修掉四处遗漏。

### 变更(`proxy/server.py`)
- **发布顺序竞态**:`_do_background_compression` 原先先置 `pending` 再置 `pending_hashes`。
  `promote_pending` 以「`pending` 非空」为转正信号,若恰在两次赋值之间转正,会读到 `pending` 已设、
  `pending_hashes` 仍 `None` → `original_hashes` 被置空,这条压缩白丢还留个永不命中的死条目。改为
  **最后才发布 `pending`**(hashes/debug 先备好),`pending` 一旦可见即保证 hashes 已就绪。
- **日志降噪**:`find_match` 原对每条不匹配条目都打 `warning` 并 dump。store 全局共享、多会话并存时
  「单条不匹配」是常态,逐条 warning 按条数刷爆两个 handler、还拖慢热路径。改为**仅整体未命中时记
  一条 `debug`**,诊断 dump 提取到 `_log_no_match`(只在 DEBUG 开启时跑)。
- **内存表封顶**:`STORE_MAX_ENTRIES` 原只在落盘/加载两端裁剪,进程内存里的 `_compressions` 只增不减,
  emergency 兜底与跨会话残留会让全表线性扫描越来越慢。新增 `_prune_locked()`,`add()` 时裁剪最老的
  空闲条目;**正在后台压缩(thread alive)的一律保留**,绝不删掉马上要转正的成果。
- **emergency 条目回收**:`_emergency_compress` 改为返回 `(compressed, entry)`;同步兜底后若这发又触发
  后台压缩(`match` 必为 None),用 emergency 登记的条目当父条目回收,避免每次兜底多留一条死条目。

### 实测(2026-06-27)
`proxy/test_emergency.py` 新增 `BackgroundCompressionPublish`/`MemoryCap`/`EmergencyReturn` 单元用例,
以及 `EmergencyRetryEndToEnd` 端到端集成测试(假上游先 400 too-long、重试 200,断言客户端只见 200、
上游恰好两跳、同步压缩条目落库)。全套 24 例 + `test_lifecycle` 2 例,合计 26 例全绿。

## 1.12.0 — 并发会话回收父死条目:store 的 40 槽真正等于 40 条并发血脉

### 背景(全局共享 store 在并发下会被「死条目」悄悄占满）
压缩 store 是全局共享(不按会话切分),靠内容哈希自动认领——这本是 `--resume`/换客户端冷启动命中的
基础,不能动。但代价在并发下暴露:每个会话每压一轮就 `add()` 一条新条目,而只有最新那条会被 `best`
选中,上一条从此再不可能命中,却**从不删除**。结果几个会话并行跑久了,40 槽被一堆死条目占满,
真实可承载的并发血脉远不到 40——表面是「会话数不够」,实质是「死条目从不回收」。

### 变更:转正即回收父条目 + 上限可配(`proxy/server.py`)
- 触发后台压缩时,把本轮所赖的 `match`(上一条已转正条目)记为新条目的 `parent`。新条目**转正**
  (pending→prefix)那一刻,父条目已注定再不可能是 `best`,当即 `remove(parent)` 回收。每个会话
  因此收敛到**恒只 1 条活条目**,40 槽 = 40 条并发血脉。
- 把 promote 的内联循环收进 `CompressionStore.promote_pending()`:转正 + 回收父 + 落盘 + 返回条数,
  一处闭环;handler 侧只剩一行调用。首压无父(parent 默认 None)安全跳过;无 pending 即空操作。
- 上限改为可配:`STORE_MAX_ENTRIES = _cfg("store_max_entries", "ROLLING_CONTEXT_STORE_MAX", 40)`,
  重并发可调高。

### 实测(2026-06-27)
`proxy/test_emergency.py` 加 `PromoteReaping` 3 例:子转正回收父且落盘只剩 1 条、首压无父安全转正、
无 pending 空操作。全套 18 例全绿;`test_lifecycle` 2 例无回归。

## 1.11.0 — 压缩成果落盘:消除重启/恢复时的冷启动满历史发送

### 背景(异步压缩只能为「下一发」备料,救不了「这一发」)
稳态体验好,是因为每轮 CC 都把全量 messages 发上来、代理一匹配就换成压缩版。痛点精确地在一处:
压缩成果(prefix 摘要 + `original_hashes` 链)只活在**内存 store**。一旦代理重启(版本闸门 /
`refresh-proxy` / 重启 / 崩溃自拉)、新开 `--resume` 一个长会话、或换客户端,store 就是空的——第一发
带着全量历史进来无可匹配 → 裸转全文。异步压缩要等这发转发**之后**才算,所以它只能让第二发变快,
第一发已满额发出。这就是「刚启动那发满历史发送」的根因:不是压缩不好,是 store 凉了。

### 变更:把可用压缩条目落盘,启动时加载(`proxy/server.py`)
- `CompressionStore` 新增 `_load()`/`persist()`:可用条目(有 `prefix` + 哈希链)原子写盘到
  `~/.claude/rolling-context-store.json`(临时文件 + `os.replace`,防崩溃半写);只存
  `original_hashes`/`prefix`/`used`/`pre_tokens`,`pending`/`thread`/`_debug_messages` 是运行期
  状态不落盘。`__init__` 启动即加载,**重启后 store 是热的**,长会话首发直接命中、不再满历史发送。
- 落盘时机:后台压缩**转正**(pending→prefix)与 emergency 同步兜底**登记**这两个产生可用条目的点。
- **失败开放**:文件缺失/损坏/写失败一律当空开始或跳过,绝不挡启动。**内容哈希自校验**:陈旧条目
  滑不中就是不被使用,绝不注入错的 → 持久化只可能多命中、不可能出错。
- 防膨胀:`STORE_MAX_ENTRIES=40`,只保留最近 N 条(越晚的压缩覆盖历史越多)。
- 覆盖范围:代理重启、开机、`refresh-proxy`、`--resume`、同机换客户端(5588 共享单例 + 同一份落盘,
  resume 时 CC 重放的旧消息哈希不变,照样命中)。残留边角——代理从没见过、但本身已巨大的会话首发
  仍发一次全文,但超限会被 1.10.0 的 emergency-compress 同步救回,不报错。

### 实测(2026-06-27)
`proxy/test_emergency.py` 加 `StorePersistence` 5 例:缺文件/损坏文件均当空加载(不抛)、写盘→新实例
重载后哈希链仍命中且运行期字段被重置、残缺条目不落盘、超额裁剪保留最新丢弃最旧。全套 15 例全绿;
`test_lifecycle` 2 例无回归。

## 1.10.0 — 代理永不超发:未命中缓存的超限请求,当场同步压一次再重试,CC 端永不见 400

### 背景(后台异步压缩的固有竞态 + autoCompact 跟着遭殃）
旧路径压缩只在后台异步跑:总 hash 链未命中、且这一发请求已经超出上游上限时,代理会把**整条未压缩的
原始请求**(实测可达 2.1M tokens)裸转给上游 → 上游 400 `prompt is too long: N tokens > 1000000
maximum`,这发 400 直接回到 CC,界面表现为「上下文 100% → 触发原生压缩 → 压缩又失败」。更糟的是 CC
自己的 autoCompact/`/compact` 摘要请求**也走这个代理**,同样因未命中而超限被拒——于是连「把真实
transcript 焊小」这条唯一的持久收敛路径也被堵死;插件一旦停用/换客户端,上下文又从头跑一遍累积量,
可能直接超过模型上限。

### 变更:超限 400 → 同步压缩 → 重试一发(`proxy/server.py`)
- 转发后拿到上游 `400` 且本发**未注入过**压缩时,先读出错误体判型:`_looks_too_long` 命中「prompt
  too long / maximum…token」类才触发兜底,鉴权/格式类 400 原样回 CC。
- `_parse_reported_tokens` 从错误串里取上游自报的**真实 token 数**(如 2100398)作 keep 比例分母 →
  一次压到上限内(解析不到再用 `字符数/3` 粗估兜底)。
- `_emergency_compress` 当场同步压一次,产出**完整新消息数组**换进请求体、重打一发上游,只把这发
  成功结果流式回 CC;并把这次压缩**登记进 store**(prefix + key 链),后续请求直接命中、不再付同步延迟。
  **最多重试一发**(压不出可压旧消息、或重试仍失败,则原样回那发 400,绝不成环)。
- 副作用收益:CC 自己的 autoCompact/`/compact` 那发超限摘要请求经此兜底得以**成功**,真实 transcript
  被持久焊小——这正是「停插件/换客户端后上下文也维持在压缩态」所依赖的机制。
- 开关 `ROLLING_CONTEXT_EMERGENCY_COMPRESS`(默认开,设 `0/false/off/no` 关闭)。
- 抽出 `_compression_key_hashes` / `_remark_cache_breakpoints` 两个辅助,后台压缩与同步兜底共用,
  保证两条路径产出的匹配 key 与缓存断点完全一致。

### 实测(2026-06-27)
新增 `proxy/test_emergency.py`(stdlib `unittest`、隔离 HOME/状态目录、不碰 5588):覆盖
`_looks_too_long`(超限/最大 token 措辞命中,鉴权 400 与乱码字节不误触)、`_parse_reported_tokens`
(含千分位)、`_compression_key_hashes`(只 key 被摘段、与后台路径一致、跳过头部既有 summary 对)。
10 例全绿;既有 `test_lifecycle` 2 例无回归。

## 1.9.1 — 堵掉 1.9.0 的升级假成功:启动后校验「起来的确实是本版本」+ 占位者腾位 + 冒烟测试

### 背景(1.9.0 自查发现的真洞)
1.9.0 的启动轮询是 `if (Get-Health) { 成功 }`——只要端口上有**任一**健康代理应答就当自己起好了。坑在
**绑定即锁**这条新路径上:旧/异版本代理仍占着 5588 时,本会话起的新 `server.py` 撞 `EADDRINUSE`
干净退出,轮询却探到那个**老代理**在答 /health,于是日志谎报 `Proxy is up (v1.9.0)`、实际跑的还是老版本。
对真实用户最致命的一幕:老代理 /health **不报 pid**(pre-1.9.0)、而 pidfile 又恰好不准(git-bash 下
`$!` 记成包装层 PID 的老坑)时,没有任何手段腾出端口——升级**永远静默失败**且每次都谎报成功。1.9.0 为避开
旧版「杀端口」竞态而删掉了 kill-by-port,反把这条一次性迁移路径的兜底也删没了。

### 变更
- `hooks/start-proxy.{ps1,sh}`:启动后轮询改为**校验版本**——只有 `/health` 报的 version **等于本会话
  版本**才算成功。若探到健康代理但版本不符 = **占位者**(老代理赖着端口),仅凭这一「确证版本不符」的证据
  作**最后手段**杀端口腾位(`Stop-PortHolder`/`_free_port`:先按 /health pid,再兜底杀端口监听者),
  **再起一发**;最多两发,腾不动则 fail-open。**只在确证版本不符时才杀端口**,稳态复用根本走不到这一步,
  不会误杀健康实例;两个新版本并发也收敛(高版本赢,闸门挡回踢)。
- `proxy/server.py`:`_CLAUDE_DIR` 支持 `ROLLING_CONTEXT_STATE_DIR` 覆盖,让冒烟测试把 pidfile/version
  写到 tmp 目录、不污染用户真实 ~/.claude(否则跑测试就把活着的 5588 网关记号冲掉)。
- `proxy/test_lifecycle.py`(新增,stdlib `unittest`、隔离端口+状态目录、不碰 5588):断言 ① /health 的
  version 来自 plugin.json、pid 是真实监听者;② 自写 pidfile == 该 pid;③ 绑定即锁——第二实例 exit 0、
  首实例仍独占端口。`python -m unittest test_lifecycle` 一条命令复跑。这是这套生命周期**首个自动化回归**,
  专挡上面这类「轮询/PID/双绑」回归。

### 实测(2026-06-27)
起 stub 老代理(/health 只回 `{"status":"ok"}`、无 version/pid)占住测试端口,隔离 USERPROFILE 跑真 hook:
日志依次 `attempt 1` → `Port held by v (PID ) != v1.9.1 - freeing, retrying` → `attempt 2` →
`Proxy is up`,/health 随后报真实版本——占位者被正确驱逐、不再谎报。冒烟测试 2 例全绿。

## 1.9.0 — 网关生命周期改「健康自识别 + 失败开放」:从 cache 跑、绑定即锁、代理挂了也不挡 CC

### 背景(取代 1.8.2 的「从 marketplace clone 跑」模型)
1.8.2 把网关代码源绑到 marketplace clone(`marketplaces/<MP>/plugins/rolling-context/proxy`),
更新靠 `git pull` + `refresh-proxy`。坑:① 依赖那份 clone 一直在、且能 `git pull`(CI/无网/clone 被清
就跑不起来);② hook 仍靠 pidfile 猜在跑哪个 PID,git-bash 下 `$!` 拿到的是包装层 PID,pidfile 跟真实
监听者对不上,杀错/复用错;③ 代理一旦没起来,`ANTHROPIC_BASE_URL` 已指向 5588,CC 直接连不上、整个会话
废掉(没有「压不了就裸传」的退路)。

### 变更:代理自报身份,hook 只看 `/health`
- `proxy/server.py`:
  - **绑定即锁**:`ThreadedHTTPServer.allow_reuse_address = False`,`server_bind()` 撞 `EADDRINUSE`
    即**干净 `exit(0)`**(清掉自己写过的 stale pidfile)。并发起的第二个实例不会双绑抢端口——谁先绑上谁是
    单例,后来者自动让位。根治「双实例抢 5588」。
  - **绑定成功后自写 pidfile**:写真正监听端口的进程 PID + 版本到 `~/.claude/rolling-context-proxy.pid`,
    绕开 hook 在 git-bash 下拿到包装层 PID 的老坑(PID 不再靠 hook 猜)。
  - `/health` 新增 `version`(读 `../.claude-plugin/plugin.json`)与 `pid` 字段——代理**自报身份**,
    版本/PID 单一权威来源就是活着的代理本身,不再依赖 verfile/pidfile 这些旁路记号。
- `hooks/start-proxy.{ps1,sh}`:
  - **默认从 cache 跑**(回到标准 `/plugin update` 升级链路);`ROLLING_CONTEXT_DEV=/path/to/repo`
    可覆盖到开发源(本仓库 clone)做本地联调,日志标 `[DEV]`。
  - **版本闸门改读 `/health`**(不再读 verfile):在跑版本 ≥ 本会话版本则复用;旧版/识别不出版本(老代理
    /health 无 version)则视作需要升级 → 重启。仍是只升不降,沿用 1.8.1 的反互踢语义。
  - **失败开放(fail-open)**:起完代理后再探一次 `/health`——健康才把 `ANTHROPIC_BASE_URL` 指 5588;
    代理没起来则**回退到上游**(settings.json 里的真实 baseURL),CC 照常工作(只是这一程没压缩),
    绝不因代理挂掉把会话也带死。
- `hooks/refresh-proxy.{ps1,sh}`:删掉 clone 解析 / `git pull` / pidfile 自愈,**瘦成纯本地重启**助手
  (按端口杀监听者 + 重跑 start-proxy,尊重 `ROLLING_CONTEXT_DEV`)。
- `.ps1` 统一 **UTF-8 BOM**:让 GBK 区的 PS 5.1 正确解码脚本里的中文串(比「全 ASCII」更省心,中文注释/
  提示都能留)。`.sh` 仍无 BOM UTF-8。

### 升级方式
`/plugin marketplace update konijiwa-plugin` → `/plugin update` 把 1.9.0 进 cache;下一个 CC 会话的 hook
探到 `/health` 版本变化即自动重启代理到新版。5588 仍是全体会话共享的单例;代理挂了走失败开放、不挡 CC。

## 1.8.1 — 修掉 giant 会话压缩失败(摘要器 200K 超限),压缩调用进看板,标记压缩生效点

### 背景(1.8.0 上线后从生产日志发现)
线上日志里有 31 次 `prompt is too long` 被上游拒。拆开看是两类、且互为因果:
- **摘要器侧 200000 超限(主因,约 26 次)**:`[BG] Compression failed: Summarization API returned 400:
  ... 202835 tokens > 200000 maximum`。摘要器是 200K 上下文的 Haiku,而单块正文预算
  `SUMMARIZER_INPUT_CHAR_BUDGET` 旧值 450K 字符——注释按 3~3.7 字符/token 估算,但实测代码/CJK
  内容约 **2.2 字符/token**,450K 字符 ≈ 205K token 已超限。**一块超限就抛 `RuntimeError`,让整次
  压缩失败** → 主上下文永不收缩。
- **主请求侧 1000000 超限(后果,约 4 次)**:压缩从不成功,历史无限增长,最终主请求撞 1M 硬墙。
- 另有数次摘要器调用 `read operation timed out`(120s 偏紧,16K 输出可能跑 100~200s)。

### 变更
- `proxy/compressor.py`:
  - `SUMMARIZER_INPUT_CHAR_BUDGET` 450K → **250K 字符**(≈110~135K token),给既有滚动摘要 + 模板
    留足余量。
  - 新增 `_summarize_messages()`:某块即便仍被摘要器以 "prompt is too long" 拒绝,也会把消息**二分递归
    续摘**(上半段摘要作为下半段的 existing_summary),单条已截到 ~4KB 必然收敛——任何单块都不再拖垮整次
    压缩。`compress()` 统一走「分块 + 逐块滚动 + 自愈」。
  - 摘要器超时 120s → **240s**(可配 `ROLLING_CONTEXT_SUMMARIZER_TIMEOUT`);更小的块也让单次更快。
  - 新增 `stats_sink` 回调:每次摘要器调用(成功/失败)落一条统计。
- `proxy/server.py`:压缩器的摘要调用本不经过代理请求路径、看板看不到;现注入 `_record_compression_call`
  回灌,**压缩请求(含 200000 超限的摘要器 400)也进 `/stats`**。压缩条目新增 `used`/`pre_tokens`,在
  **压缩生效的第一个请求**上打标记(带压缩前→后的 token 规模)。
- `proxy/stats.py`:`recent` 透传 `kind`/`first_compressed`/`pre_tokens`/`conv_chars`;`totals` 新增
  `compression_calls`/`compression_errors`。
- `proxy/dashboard.html`:
  - 错误明细体抽出上游 JSON 的 `.error.message` 置顶展示,并按 `prompt is too long: A > B` 给人话提示
    (区分 200000=摘要器超限 / 1000000=主请求未压缩);状态格悬停即见。
  - 最近请求表给压缩调用打「压缩」徽标、给压缩生效的首个请求打「✂ 生效 2.0M→82k」徽标。

### 启动 hook:版本闸门(只升级不降级)+ 发版单一来源
- `hooks/start-proxy.{ps1,sh}`:SessionStart 探测到 5588 已有代理在跑时,旧逻辑是「版本不等就重启」——
  导致**多版本会话互踢**:新会话起 1.8.1,旧 1.8.0 会话的 hook 又把它拽回 1.8.0,反复 kill+restart,
  每次掐断在传请求并清空内存里的压缩状态(resume 第一发因此没现成压缩、原样转发巨上下文)。
  改为**版本闸门**:同版本复用;在跑的版本 **≥** 本会话版本则**复用、绝不降级**;仅当本会话版本**严格更高**
  才重启升级。语义版本比较(ps1 用 `[version]`、sh 用 `sort -V`,`1.8.10 > 1.8.9` 判断正确)。
- `.claude-plugin/marketplace.json`:删掉 rolling-context 的 `version` 字段,版本号**单一来源 = 插件自身
  `plugin.json`**(对齐官方 marketplace:234 插件里 220 个都不写 version,由 `source` 指向的 plugin.json 推导)。
  根治版本漂移——历史上 `plugin.json` 改了、`marketplace.json` 没改,`/plugin update` 比对目录版本认为「已最新」、
  CC 一直跑 cache 旧版(1.7.13/1.7.16/1.8.1 都踩过)。

## 1.8.0 — 新增网关统计看板(`/stats`):token、各类耗时、吞吐、缓存的图表化

### 背景
代理此前只在内存里数 `compression_count` / `total_tokens_saved`,SSE 里也只解析出输入 token 用于触发
压缩,既不持久化、也看不到全貌。用户想要一个页面统计「用了多少 token、各种时间(输入/输出/响应)、以及
其他值得看的指标」,并以图表呈现。

### 变更
- 新增 `proxy/stats.py`:`StatsCollector` —— 内存环形缓冲(上限 5 万条)+ 落盘
  `~/.claude/rolling-context-stats.jsonl`,代理重启后自动从文件尾部回载历史。内含 `aggregate()`:
  按时间分桶、求 p50/p90/p99 分位、按模型/会话汇总。零依赖,纯标准库。
- `proxy/server.py`:
  - `_handle_messages` 埋点。每个真实 `/v1/messages` 生成调用记一条:输入/缓存读/缓存创建/输出 token、
    请求与响应字节、是否注入了压缩前缀、状态码,以及四段耗时——
    **代理开销**(进入→转发)、**首字 prefill**(转发→首字节,≈输入处理时间)、
    **生成**(首字→末字,≈输出时间)、**总响应时间**。`count_tokens` 探测调用不计,避免污染。
  - 统一了 SSE usage 解析,新增**输出 token**提取(原先只取输入);原压缩触发逻辑不变。
  - 新增三个 GET 路由:`/stats`(看板 HTML)、`/stats/data?hours=`(聚合 JSON)。
- 新增 `proxy/dashboard.html`:单文件暗色看板,Tailwind(CDN)+ Chart.js(已加 SRI 校验)。
  10 张 KPI 卡 + 8 张图表(token 分项堆叠、请求数、响应时间拆解、延迟分位、输出吞吐、缓存命中率、
  按模型占比、按会话 Top)+ 最近 100 条请求表。支持 1h/6h/24h/7天/30天/全部 时间窗与 15s 自动刷新。

### 输出速度:单请求 / 并发分别统计 + 缓冲突发识别
- 每条记录算出**单请求 tok/s**(output_tokens ÷ 生成耗时),在最近请求表里逐条列出。
- 并发检测:用在途请求登记表,任一时刻在途 >1 即把这段时间内的请求都标 `concurrent`。
  并发期吞吐会互相挤占而失真,因此 KPI/折线把**单请求吞吐**与**并发吞吐**分开,并发请求在表里以 `⇄` 标注。
- **缓冲突发(bursty)识别**:单条 Anthropic 流的真实速率约 50~100 tok/s,持续高于此物理上不可能,
  几乎一定是上游(sub2api)把整条 SSE 缓冲后一次性吐出 —— 首字=末字、生成耗时塌缩、tok/s 虚高。
  为此记录每条响应的**数据块数 `stream_chunks`**(真流式应有几十~上百块);当 tok/s > 250
  或整条响应仅 1~2 块送达时判为 bursty,从吞吐统计里**隔离**(否则真实速率被虚高值拉爆),
  在表里以琥珀色 `⚡` 标注、KPI 给出「疑似缓冲 N 次」。这样「单请求吞吐」反映的才是真实生成速率。
- 最近请求表支持**按模型 / 会话筛选**(下拉框,在最新 100 条内即时过滤)。

### 错误归因:Cloudflare 边缘 vs 上游(sub2api)origin
- 对 >=400 的响应记录来源指纹(`server` / `cf-ray` / `cf-mitigated` / `via` / `x-request-id` /
  `content-type` / `retry-after`)与**响应体片段**(封顶 500 字),并判定 `err_source`:
  - **cloudflare**:CF 自身拦截(text/html 错误页或带 `cf-mitigated`)。
  - **upstream**:上游 origin 生成(JSON 错误体 + 源站 `Via`/`X-Request-Id`)——
    关键点是 sub2api 也挂在 CF 后面,`Server: cloudflare`/`CF-RAY` 几乎人人都有,不能据此判 CF。
- 看板:KPI「请求数」副标题给出 `CF x · 上游 y` 拆分;最近请求表错误行可**点击展开**,显示来源、
  指纹与原始响应体(便于直接看上游到底回了什么)。同时这些细节也 `log.warning` 进调试日志。

### 日志按大小滚动,封顶磁盘占用
- `rolling-context-debug.log` 改用滚动写:单文件 10MB、保留 5 个历史(≈60MB 封顶),
  防止长期运行把磁盘写爆。可用环境变量 `ROLLING_CONTEXT_LOG_MB` / `ROLLING_CONTEXT_LOG_BACKUPS` 覆写。
- stdout(被 start-proxy 重定向到 `rolling-context-proxy.log`)降到 INFO 级,体量约为原来的 1/10;
  完整 DEBUG 仍写入会滚动的 debug.log。首次升级启动时,既有的超大 debug.log 会被滚成 `.1` 并随后被淘汰。

### 用法
代理已把 `ANTHROPIC_BASE_URL` 指向本机,浏览器打开 `http://127.0.0.1:5588/stats` 即可。
统计随对话自然累积,JSONL 落盘后跨重启保留。

### 影响
- 默认开启,零配置。统计为「旁路」记录,不改变代理转发/压缩行为,失败被吞不影响请求。
- 新增本地文件 `~/.claude/rolling-context-stats.jsonl`(追加写,一行一条);看板的 Tailwind/Chart.js
  走公网 CDN,浏览器需可联网(本机离线时图表不渲染,数据接口不受影响)。

## 1.7.19 — 真正修掉 Windows hook 报 `<盘符>:\dev\null`(1.7.17 修漏了)

### 背景
1.7.17 自以为修好了 SessionStart hook 报 `C:\dev\null`,只删了 powershell 分支后的
`2>/dev/null`，**保留了结尾 bash 分支的那个**，并断言「Windows 因 `||` 短路仅解析不执行、
不触发 Out-File」。该断言是错的。最小复现:
- `powershell -Command "exit 0" || bash -c "true" 2>/dev/null` —— powershell **成功**时短路,**不报错**;
- `powershell -Command "exit 1" || bash -c "true" 2>/dev/null` —— powershell **失败**时走到 bash 分支,
  pwsh 真正处理 `2>/dev/null` → `Out-File '<盘符>:\dev\null'` → 目录不存在报错(在 D 盘即 `D:\dev\null`)。

即只要 powershell 分支某次返回非零(如 compact 触发的 SessionStart 执行环境里 `powershell` 不在
PATH、或 .ps1 非零退出),fallback 的这个重定向就炸。

### 变更
`hooks/hooks.json`:删除结尾 bash 分支的 `2>/dev/null`，命令变为
`powershell ...start-proxy.ps1 || bash ...start-proxy.sh`。对 pwsh 全程安全(无任何 `/dev/null`)。
代价:Mac/Linux 下 `start-proxy.sh` 自身 stderr 不再被该重定向吞掉——但 `.sh` 本就把日志写进
`~/.claude/rolling-context-hook.log`、内部子命令各自带 `2>/dev/null`,实际噪声可忽略;且 `powershell
not found` 那行噪声本就不受该重定向控制(它只作用于 bash 命令),故无新增回归。

## 1.7.18 — 摘要逐字保留用户提供的密钥/密码

### 背景
压缩把旧消息交给 Haiku 摘要器概括，而摘要的本性是「提炼语义、丢弃字面」。用户早先在对话里输入的
API key、密码、token、连接串等凭据，被摘要成「用户提供了一个 API key」之类的描述后，原始字符就丢了。
压缩后主模型不再持有这些值，于是又回头让用户重新输入——这正是用户反馈的痛点。

### 变更
仅改 `compressor.py` 的 `SUMMARIZE_PROMPT`（摘要器的唯一指令来源），零代码逻辑改动：
- 新增一条 RULE：要求**逐字**保留用户给出的 secrets/credentials（API key、token、密码、连接串、
  env 值、账号/项目 ID），按精确字符照抄，**禁止**掩码、截断、改写或替换成 `<redacted>`/`***`。
- 新增一个 FORMAT 段 `## Secrets & Credentials (verbatim)`：把这些凭据集中、原样、带用途标注地列出；
  用户没给凭据时该段整体省略。

分块摘要路径（giant 会话）天然继承：每块都用同一 prompt，凭据在首次出现的那块被逐字搬进滚动摘要，
后续块以 `existing_summary` 续摘时会原样带下去。

### 影响
- 压缩后主模型仍持有原始凭据，不再二次索要。
- 安全权衡：凭据会出现在摘要文本里（随对话留在本地 transcript、并在摘要时经摘要上游）。但**原始对话
  本就逐字经过同一摘要上游、也本就落在同一 transcript**，故未新增暴露路径；不信任第三方摘要上游的用户
  应自行评估。
- 纯 prompt 改动，重启代理即生效，无需迁移、无数据格式变化。

## 1.7.17 — 修复 Windows SessionStart hook 报 `C:\dev\null`

### 背景
Windows 上每次会话启动报 non-blocking 错误:`Out-File: Could not find a part of the path 'C:\dev\null'`。
根因是 SessionStart hook 命令按 bash 写法在 powershell 分支尾部带了 `2>/dev/null`;Windows 下 CC 用
pwsh 执行该命令,`2>/dev/null` 被解析为「把 stderr 写到文件 `/dev/null`」,而 pwsh 把 `/dev/null`
当成当前盘根下的 `C:\dev\null`,目录不存在 → Out-File 报错。代理实际照常起,只是噪声报错。

### 变更
- `hooks/hooks.json`:删除 powershell 分支后的 `2>/dev/null`。`.ps1` 本身已全程静音
  (`$ErrorActionPreference = "SilentlyContinue"` + 只写日志文件),该重定向在 Windows 上纯属害处。
  bash 分支的 `2>/dev/null` 保留——它只在 Mac/Linux(sh)真正执行,Windows 因 `||` 短路仅被解析不执行,
  不会触发 Out-File。

## 1.7.16 — 默认 trigger 100K → 160K

### 变更
默认压缩触发阈值 `trigger` 从 `100000` tokens 提升到 `160000`。新装/无 config 无 env 的环境受影响;
已有 `rolling-context.json` 或 settings.json env 显式指定 `trigger` 的环境不变。

### 动机
100K 触发偏激进,会话刚到中等体量就压缩,牺牲了一段本可保留的近期上下文;且压缩本身有成本(一次
Haiku 摘要调用 + 后台延迟)。160K 在「离 Claude Code 自身上下文上限仍有充裕余量」与「少压一次、
多留上下文」之间更平衡。同步改动:`server.py` 默认值、两个 hook(`.ps1`/`.sh`)写入 settings.json
的 env 默认、`README.md`、`rolling-context.example.json`。

## 1.7.15 — 分块摘要(根治 giant 会话 400 prompt too long)+ .ps1 端口去重兜底

### 背景
1.7.14 消灭了「无干净边界放行」,中等会话能稳定压缩;但 giant 会话(`to_compress` 文本超摘要器
窗口,实测 6.6MB / 1879 messages 的会话)会把整段一次性塞进摘要 prompt → 上游返
`400 prompt too long > 200K` → 压缩硬失败 → 体量继续堆、最终打网关 502。根因是摘要输入**未限总量**。

另:排障时发现一处长期误判——Windows 上 CC 实际走 `start-proxy.ps1`(`.sh` 仅 powershell 不可用
时兜底)。`.ps1` 用 `Start-Process -PassThru`,`.Id` 即真正的 python PID、且已带
`-RedirectStandardOutput/-RedirectStandardError`,故「PIDFILE 记错」「SessionStart 挂死」在真实
路径本就不存在;之前的现象是**手动跑过 `.sh`(nohup,`$!` 取到包装层 PID)污染了 PidFile** 所致。

### 变更
- **分块摘要**(`compressor.compress`):
  - 新增预算 `SUMMARIZER_INPUT_CHAR_BUDGET`(默认 450K 字符 ≈ 112K token,环境变量
    `ROLLING_CONTEXT_SUMMARIZER_CHAR_BUDGET` 可覆盖)。
  - `to_compress` 文本 **≤ 预算**:整段一次摘要,**行为与 1.7.14 完全一致**(零回归)。
  - **> 预算**:`_chunk_by_chars` 按时间序贪心切块(**单条消息不拆**,保证内容完整),逐块调用
    `_summarize_chunk`,把**上一块的摘要作为下一块的 `existing_summary` 续摘** → 得到一份整合的
    滚动摘要,绝不会一次塞爆窗口。`recent_messages` 始终原样保留在摘要之后。
  - 摘要单次调用抽成 `_summarize_chunk(conversation_text, existing_summary, auth_headers)`。
- **`.ps1` 端口去重兜底**:起新代理前(PidFile 清理后、`Start-Process` 前)用
  `Get-NetTCPConnection -LocalPort $Port` 找到仍监听本端口的进程并 `Stop-Process`,根治
  「PidFile 被污染 → 旧代理没杀掉 + 起新的 → 双实例抢端口」。同版本健康代理在前面 `exit 0`,
  走不到这里,不会误杀。

### 影响
- giant 会话不再因 400 硬失败,可分块压下;严格优于此前「直接失败 + 502」。
- 代价:giant 会话触发**多次串行摘要调用**(如 6.6MB ≈ 15 块),压缩耗时拉长;任一块返非 200
  会整体抛错重来。常见(≤预算)会话路径不受影响。
- 版本升级时不再因 PidFile 污染产生双实例。需重启代理生效。
- 已用结构化测试覆盖:分块切分无丢失、超预算多块串联(首块 existing 空、后续串上一块摘要)、
  预算内恰好单次调用(单次路径零回归)。

## 1.7.14 — tool 对边界切点(根治 agentic 突发期压不动)

### 背景
1.7.13 在「近端无干净 user 边界」时向**后**回退到上一次人类输入处,缓解了边界冻结;但仍有
遗留局限:在一段**没有任何人类插话的 agentic 长突发**里,start_idx 之后压根不存在干净 user
边界(每个 user 都带 `tool_result`),向前扫到尾、向后又退回 start_idx → 仍然直通,体量在
工具连跑期一路堆到 90K–260K+,只能等人敲字才压一次。

根因是切点的「干净 user 边界」是个**充分但过强**的约束。真正的硬约束只有两条:注入后角色
必须交替、`tool_result` 必须紧跟其 `tool_use`(不留孤儿,否则 `_validate_tool_pairs` 会把摘要
连同孤儿消息一起丢弃 → 上下文丢失 / 上游 400)。干净 user 边界只是满足它的一种方式。

### 变更
- 新增 **tool 对边界**切法,把切点选择抽成 `compressor._select_cut`,返回 `(start_idx,
  keep_from_idx, prefix_len)`:
  - **clean 模式(prefix_len=2)**:命中干净 user 边界,前缀仍是 `[summary, ack]`。压得最深,
    **优先**——已有正常路径行为完全不变(回归测试验证切点与 1.7.13 逐一致)。
  - **toolpair 模式(prefix_len=1)**:突发期扫不到干净 user 时,改在「assistant 开启的新一轮」
    前切(`role==assistant` 且前一条是 `user`)。此时 `summary(user)→assistant(tool_use)→
    user(tool_result)` 角色合法、工具对都在保留段内、无孤儿;前缀**去掉 ack**(否则 `summary,
    ack, assistant` 连续两个 assistant → 上游 400)。
  - 两者都不可用时,回退到 1.7.13 的向后干净边界,再不行才直通。
- `compress()` 返回值由 `list` 改为 `(compressed, prefix_len)`;直通返回 `(messages, 0)`。
- `server._do_background_compression`:按 `prefix_len` 动态切前缀(去掉硬编码 `[:2]` / `-2`),
  以 `prefix_len==0` 判直通。注入侧 `_validate_tool_pairs` / `_mark_cache_breakpoint`(支持
  `tool_use` 块)无需改动。

### 影响
- agentic 突发期**无需等人类插话即可压缩**,长突发体量不再堆到 90K–260K+。
- 间接缓解用户实测延迟:压住此前直通的大上下文 → 请求体变小、上传与大上下文首字延迟同时下降。
- 纯压缩切点逻辑改动,不改转发行为。已用结构化测试覆盖突发/纯对话(零回归)/混合/边缘/
  已有摘要五类场景,验证 toolpair 切点无孤儿、摘要存活、clean 模式切点与 1.7.13 一致。需重启代理生效。

## 1.7.13 — 修复摘要边界冻结(agentic 长跑下压缩空转)

### 背景(实测日志定位)
读真实调试日志发现:压缩本身在工作(实测注入 1077 次、224K→77K 字符),但摘要边界
长期**冻结**在某一点(日志里反复 `replaced 0-88`,而消息数从 160 涨到 181),注入后的
体量随之从 77K 一路爬到 96K——「滚动」不前移,长会话仍会逼近上限。

根因在 `compress()` 的切点边界选择:`_find_keep_index` 按 keep_ratio 算出切点后,只**向后**
找「干净 user 边界」(`role==user` 且不含 `tool_result`)。但 Claude Code 里凡回应工具调用的
user 消息都带 `tool_result`,**只有人类亲手敲字才产生干净 user**。在 agentic 工具连跑期间,
最近窗口全是 assistant/tool_use/tool_result,无干净边界,切点一路推到 `len(messages)` →
触发 `keep_from_idx >= len` → 直通,摘要永远生成不出来。日志佐证:工具连跑期边界冻在 `0-88`,
人一敲字立刻跳到 `0-177`,停说话又退回——完全由「近端有无人类输入」决定。

附带:直通路径下 `_do_background_compression` 把「前 2 条原始消息」误存成压缩条目
(日志里反复出现的 `28,779 chars / key=2 hashes / summarized 2 messages` 冻结条目),
既无用又污染匹配。

### 变更
- `compressor.compress`:切点向后越界(近端无干净边界)时,改为**向前回退**到「最近一个
  干净 user 边界」。摘要至少推进到上一次人类输入处,而非冻结;切点更靠前=多留逐字,但仍
  落在合法边界(不破坏 system/角色交替/工具配对,不触发上游 400)。
- `server._do_background_compression`:检测到直通(`compressed is messages`)即不存条目并
  `store.remove(entry)`,消除 `key=2` 垃圾条目。

### 影响
- agentic 长跑中摘要边界可持续前移,长会话注入后体量不再单调爬升。
- 纯压缩调度逻辑修正,不改转发行为。需重启代理生效。

### 关于 keep_ratio 单位不一致(降级,不再单列待办)
`keep_ratio = target / real_token_count`,分母含 system+tools,却只切消息字符。实测
system+tools 合计 < 34K token(此前「约 150K」的猜测经上游回报 token 证伪),失真很小,
暂不处理。

## 1.7.12 — 压缩计量认图片/thinking + 请求体拆解诊断

### 背景
排查「对话始终高于触发线、每轮空转压缩」时,发现 `_count_chars` 只统计
`text / tool_use.input / tool_result 文本`,完全不计图片与 thinking 块,
导致压缩切点/触发判断对这部分内容失明。

进一步用真实会话核算后纠正了一个判断:**图片虽然 base64 字符极多,但单图 token
有上限(约 1600),真实 token 占比远小于字符占比**——实测 80 张图也仅折算约
8000 token。真正的「隐藏大头」是 `system + tools`(大量 MCP + skill 的工具定义,
量级可达上百 K token),而它们不在 `messages` 里,`_count_chars` 结构上看不到。

### 变更
- `compressor._count_chars`:新增 `thinking`、`image`(顶层及 tool_result 内)的
  统计。图片按 `_image_chars` 估算:`min(1600, base64长度//1000)` token × 4 换算成
  字符当量(无图片尺寸,只能据 base64 长度粗估,封顶对齐 Anthropic 单图量级上限)。
- `server._handle_messages`:新增**只读**的请求体拆解诊断日志 `[MSG] breakdown:`,
  打印 `body / system / tools(数量) / 消息字符 / 图片数 / 估算 token`,
  用于定位真实 token 分布(图片 vs system+tools),为后续修 keep_ratio 提供实测依据。

### 影响
- 计量更接近真实 token,日志 `chars=` 数值会因计入图片而变大(更诚实)。
- breakdown 诊断不改变任何转发/压缩行为。
- 需重启代理生效。

### 待办(待 breakdown 实测后再做)
- `keep_ratio = target_tokens / real_token_count` 的分母含 system+tools,却只对
  messages 切割,单位不一致。待实测 system+tools 量级后,改为按「消息 token 预算」
  (`target - system - tools`)计算,或在 system+tools 已超 target 时跳过无效压缩。

## 1.7.11 — 会话级日志(多会话排障)

### 背景
多个 Claude Code 会话共用同一个代理进程(127.0.0.1:5588)与同一份日志文件
`~/.claude/rolling-context-debug.log`。原日志只记录请求头的**名字**、不记
`X-Claude-Code-Session-Id` 的**值**,导致多会话并发时无法判断某条请求归属哪个会话、
卡在转发前还是等上游响应——排查「多会话只有一个在跑」这类问题缺少抓手。

### 变更
- 新增线程本地会话标签 `_sess_ctx` + 日志 `_SessionFilter`:把会话标签与短线程 id
  注入每一条日志记录,formatter 统一为
  `%(asctime)s [%(levelname)s] [%(sess)s|t%(tid)05d] %(message)s`。
  因 Filter 挂在 handler 上,所有日志(含 `[BG]`、`[MATCH]` 及压缩器子 logger)
  都自动带标签,无需逐条改 log 调用。
- 会话标签取自 `X-Claude-Code-Session-Id` 的前 8 位(UUID 前缀);缺失时记为
  `no-sess-`,默认线程记为 `--------`。
- 每个 `do_GET/POST/PUT/DELETE/PATCH/OPTIONS` 入口调用 `_tag_session()` 设置标签。
- 后台压缩线程不继承请求线程的线程本地,显式把发起会话的标签传入
  `_do_background_compression(..., sess=...)` 并在线程起始重设,使 `[BG]` 与压缩器
  日志正确归属。

### 影响
- 纯诊断增强,不改变转发/压缩行为。日志每行新增 `[会话前缀|t线程id]` 字段。
- 需重启代理进程后生效(旧进程仍跑旧代码)。

### 已知待办(本次未改)
- 压缩 `keep_ratio` 用「完整 API token(含 system+tools+缓存)」对「仅消息字符」
  作切割,对含大体量 system/工具定义的会话基本压不动,且每轮空跑、堆积
  `key=0` 死条目。后续单独修。
