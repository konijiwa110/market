# ChangeLog

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
