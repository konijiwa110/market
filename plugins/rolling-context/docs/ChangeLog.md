# ChangeLog

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
