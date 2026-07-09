# ChangeLog

## 1.21.4 — 延迟 reap + 注入后体积复检:根治「深度倒退 → 1.8M 裸透 400 风暴」

### 事故(2026-07-09 19:41,session 230062c0,fable 1M 窗)
子压缩条目 promote 时父条目(覆盖 0-8071)被立即 reap;285ms 后子条目失配(used=False,
一次都没命中过),find_match 退到更浅的旧条目(0-6019),保留段 2266 条原始消息含全量
图片 ≈1.8M tokens 原样透传 → 上游 400 `prompt is too long: 1806356 > 1000000`。随后
starvation 重压 + proactive 全量重压(2.5M est,317s 后 502)循环,看板满屏 haiku 压缩,
19:52 才自愈。

### 根因链(全部实证:store 条目 [34]/[35] 哈希链交叉滑窗 + CC transcript hash 定位)
1. **子链覆盖了仍会漂移的近端消息**:子链 149 条对应 raw[8072:8221](压缩时的「次新」段),
   其中 raw[8113](19:09:41 一条带 2 附件的 Bash tool_result)在 80 秒内被 CC 改变表示——
   一条变两条。1.20.3 thinking 的同族(history drift),但这次是**消息数量**漂移,哈希
   免疫救不了。链前 41 条完全对齐、[41] 不存在、[42:] 整体右移一位,交叉验证坐实。
2. **promote 即 reap = 拆安全网**:父若还在,这发照样命中 8071,啥事没有。
3. **注入 = known-good 尺寸的假设被打破**:injected=True 跳过 proactive 预检、也被挡在
   emergency 兜底门外(`not injected or prewarm`),1.8M 就这样发了出去,400 裸透给 CC。

### 变更(server.py)
- **延迟 reap**:promote_pending 不再立即回收父;新增 `store.reap_parent(entry)`,在子条目
  首次真实命中注入(used 首次置 True)时才兑现。父子共存期 find_match 仍选 end 最深者,
  正确性不变,只多占一槽(孤儿由 _prune_locked 回收)。深度倒退→裸奔路径从机制上消灭。
- **注入后体积复检**:决策表 "pre" 分支新增 window 参数——injected 且 est 仍超模型窗口
  → `("sync", "injected-over-window")` 同步重压,不放行;est 改为基于「当前 payload 实际
  形态」编码估算(此前用注入前的 raw_body,量不出倒退后的真实体积)。
- **emergency 兜底放宽**:400 'too long' 一律放行同步重压(去掉 `not injected or prewarm`
  闸门),_looks_too_long + 只重试一发的上限不变。CC 永不见 400 的承诺补上最后一块。
- **诊断修正**:`_log_no_match` 从 0 偏移比对改为滑窗最佳部分匹配(旧输出对链在中部的增量
  子条目永远是「diff at [0]」废话,本次排障实翻车);新增 `_warn_depth_regression`——同
  会话注入深度倒退即 WARNING(事故第一现场信号,此前靠人肉对比两行 Injecting 才看出来)。

### 验证
新增 13 测试:决策表矩阵 +4(injected-over-window / within-window / not-injected 不受
window 影响)、ProactiveGate 改签名 +2、延迟 reap 生命周期 +2、深度倒退告警 +4、滑窗
诊断 +2、回放 fixture NearTailDriftDepthRegression 3 例(父兜底漂移 / 无漂移子胜出并
reap / 反事实:改回立即 reap 立刻红)。全套 150 全绿。

### 观察项(暂不动)
- key 链是否避开「距尾 N 条」消息:会伤压缩比;延迟 reap 落地后此漂移只导致一次温和重压,
  先观察发生频率。
- 摘要失败无退避(17 连发遗留)与摘要同步超时兜底:仍在待办。

## 1.21.3 — 剥离推广为「门票型 beta」家族:不等下一炸

### 背景
1.21.2 只剥了 context-management(已炸的那个)。但同一结构缺陷(伪装模板照抄主请求头 +
摘要 body 自建 → 头体不一致)下,CC 二进制常量池里还有同族的票:`compact-2026-01-12`、
`structured-outputs-2025-12-15`、`effort-2025-11-24`——全是「功能靠 body 字段驱动、头只是门票」,
摘要 body 永不带对应字段,带票零收益、有被上游/中转「见票校验或注入」的同款风险(1M 与
context-management 已分别炸过一次,黑名单不该一次只追一个)。

### 变更(compressor.py)
`_strip_context_management_beta` 推广为 `_strip_body_coupled_betas`:按前缀黑名单
`_BODY_COUPLED_BETA_PREFIXES`(context-management / compact- / structured-outputs / effort-)
一律剥。判据写死在注释:新 beta 若功能需 body 字段配合、且摘要 body 不带 → 加名单。
观察名单(明确不剥):interleaved-thinking——实测挂一个月无害,且是 CC 头形态高频常客(拟真
价值);若炸,1.21.2 的 [sent anthropic-beta] 诊断可秒定位。

### 验证
测试同步改名并补 2 例(全家族剥净、interleaved 保留),全套 137 全绿。

## 1.21.2 — 摘要请求剥 context-management beta:根治 clear_thinking 400 间歇性压死压缩

### 事故(2026-07-08 21:43 起,session 230062c0)
后台压缩的摘要器调用被上游 400 拒:`clear_thinking_20251015 strategy requires thinking to be
enabled or adaptive`。21:43–21:52 连挂 17 次,22:45 成功、23:53 又挂、00:04 又成功——间歇性,
期间该会话 183k 一直压不下来。

### 根因链
- CC 主请求(fable,thinking:adaptive)带 beta 头 `context-management-2025-06-27` + body
  `context_management:{edits:[{type:clear_thinking_20251015,keep:all}]}`。cli.js 实证:CC 只在
  请求开 thinking 时才加该字段,主请求永远合法。
- 摘要请求经「伪装模板」照抄了主请求的 anthropic-beta 头(含 context-management token),但摘要
  body 既无 thinking 也无 context_management。
- 光带头不该触发该校验——最可能是中转加工层见头注入 context_management 字段,注入了
  clear_thinking 却没配 thinking → 官方 400。间歇形态与中转多节点灰度吻合(同形请求时好时坏)。

### 修复(compressor.py)
1. `_strip_context_management_beta`:摘要请求一律从 anthropic-beta 剥掉 context-management
   token(与 1M 剥离同款;摘要体永不带 thinking/context_management,该头零收益纯风险)。
2. 失败诊断补头值:非 200 时 err_snippet 追加 `[sent anthropic-beta: ...]`(_emit_stat 截断
   600→800)——beta 头诱发的 400 家族(1M / context-management)以后看一眼诊断即可定位,
   不用再反推 CC 二进制。

### 验证
新增 5 测试(剥离 3 例 + 出站请求端到端 1 例 + 400 诊断带头值 1 例),全套 135 全绿。
遗留观察:压缩失败无退避(9 分钟 17 连发),暂不动(功能冻结),复发再议。

## 1.21.1 — 看板观测三件套:失败行输入可见 / 行级复制诊断 / 归档走下载(纯观测,零决策变更)

### 背景
真实案例(07-06 16:28,摘要器调用 503):失败响应没有 usage,看板 token 列全 0、行里又没有
请求体大小列,被误读成「没记录输入」甚至「超过模型上限」。实际 req_bytes/conv_chars 一直有落库
(该次输入 116KB / 76,698 字符 ≈ 35k token,同请求体 3 分钟后重试成功),只是展示层没给列。

### 变更(dashboard.html + stats.py recent 补 req_bytes 字段 + server.py 归档端点)
1. **错误展开区显示本次输入**:`本次输入=NB(≈N tok)· 被摘正文=N 字符`,失败行不再无从判断
   实际发了多大。
2. **行级复制诊断按钮**(新列 📋):一键复制该行全部字段的纯文本(时间/模型/kind/会话/状态/
   token 四项/req_bytes/耗时/标志位/错误元信息+响应体/归档文件名),可直接粘给 AI 排查。
   localhost 用 clipboard API,非 secure context 退回 execCommand。
3. **归档走下载**:服务端 `Content-Disposition: attachment`(.json.gz → .json 文件名)+ 前端
   `download` 属性,不再浏览器内打开——数 MB JSON 直接渲染会卡死标签页,下载成文件也方便丢给 AI。

### 验证
130 测试全绿;5599 临时实例浏览器实测:17 列渲染、复制按钮点击变 ✓(剪贴板写入成功)、
存档链接带 download 属性、curl 确认响应头 `content-disposition: attachment; filename="....json"`。

## 1.21.0 — 压缩决策收敛为一张决策表 + 真实事故形状的回放测试(行为等价,零功能变更)

### 背景
版本评审(1.7 → 1.20.3)发现:返工最重的两族 bug(1.17.4/1.19.1 去重族、1.20.1/1.20.2 护栏族)
全部源自同一结构问题——**压缩决策散在转发前(proactive)与响应后(bg trigger)两处**,每加一个
条件(end_turn 闸门、硬顶、注入状态)都可能与另一处打架,且组合路况无法穷举测试,只能等线上撞出来。

### 变更
1. **决策表收敛**(`server.py` 新增 `decide_compression(stage, ...)`):纯函数、唯一事实来源,
   pre 段(粗估口径)产 `sync|forward`,post 段(上游真实 token 口径)产 `bg|defer|skip`,
   reason 为稳定短码。`_should_proactive_compress` 降级为组装层薄封装;响应后 if/elif 改为查表分发。
   **所有日志原文保持不变**(`mid tool-loop starvation…` 等哨兵行照旧可 grep)。
   去重(`claim_compression` 原子判定)与切点(`_select_cut`)不在此层,原位不动。
2. **回放测试**(新增 `test_replay.py`):把三次真实事故的**形状**做成合成 fixture(不含真实对话)——
   ① resume 剥 thinking(fa861c68 480k 裸奔)、② 同轮重发尾部追加指令块、③ role:system 尾巴
   (1.20.1 风暴形状);外加**机会性回放**:本机存在真实归档时逐份校验哈希不变量(JSON 往返、
   剥 thinking 均不得改变哈希),归档不存在自动跳过,真实对话永不入库。
3. **决策矩阵穷举**(`test_emergency.DecideCompression` 13 例):pre/post 全路况 + 边界
   (恰在 trigger / 恰在硬顶)+ 未知 stage 拒绝。今后新增决策条件必须先过这张矩阵。

### 验证
全套 114 测试全绿(95 → 114)。重构为行为等价:既有 proactive 测试经薄封装穿透新决策表通过,
post 段五路况与旧 if/elif 逐条对齐(end_turn 建条目 / 循环中推迟 / 超硬顶强压 / 未超不动 / 无用量不动)。

## 1.20.3 — 哈希对 resume 免疫:剥离 thinking 块,恢复会话后深条目不再全灭

### 背景
实测(max,fa861c68):会话进行中深条目正常命中,billed 稳定 113k;次日 resume 后同一会话
第一发直接 480k 裸奔——深条目(221 条哈希链)中 **61 条 assistant 消息哈希全部失配**。
字节级 diff 定位根因:**CC 在 resume 时会剥掉历史 assistant 消息里的 thinking/redacted_thinking
块**(同一 tool_use id 的消息,会话中 2,489B 带 `signature`,resume 后 454B 无 thinking),
而 `_hash_message` 把 thinking 块算进了哈希 → 任何带 thinking 的会话一 resume 深条目必死,
之后是全套连锁:浅条目兜底 → 480k 出门 → 硬顶逃生阀重压一次 → 过渡期两发全尺寸计费
(含一次 433.6k 缓存重建,叠加 CC 插话强制文字重发的前缀分叉,单次损失 40 万+ cache-write)。

### 修复
- **`_normalize_content` 丢弃 `thinking`/`redacted_thinking` 块后再哈希**(server.py)。
  thinking 是易失内容,消息身份由 text/tool_use 承载;剥离后 resume 前后哈希一致,深条目
  照常命中,480k 场景从源头消失。reminder 剥离(`_VOLATILE_TAGS_RE`)经排查工作正常,未改动。

### 迁移成本
哈希算法变更 → 已落盘旧条目升级后一次性失配(内容自校验,失配即不用,不会注错),各活跃
会话首次超 trigger 时重压一次即恢复,无需手工清理 store。

### 验证
- 新增 `test_emergency.HashResumeImmunity`(4 用例:thinking 剥离等价、redacted_thinking 等价、
  reminder 剥离回归、正文/工具输入变更仍必失配),95 个测试全绿。
- 运行观察:resume 一个带 thinking 的长会话,首发应照常注入深条目(billed 维持压缩后水平),
  不再出现"恢复会话后第一发全尺寸 + 硬顶强压"的过渡。

## 1.20.2 — 饥饿逃生阀:超长工具循环中硬顶强制压缩 + 摘要尾块合并

### 背景
1.20.0 的 end_turn 闸门(后台建条目只在回合结束时进行)在超长 agentic 工具循环下暴露反面代价——
**压缩饥饿**:实测一个会话连续数小时不出 `end_turn`,条目停留在第 509 条消息不再重建,token 从 180k
无界涨到 **323k**(15 连发全部 `deferring compression`),最终被 CC 自身 compact 抢先兜底(比代理摘要
粗暴、丢细节多),代理刚建好的新条目因前缀被 CC 改写永不命中,白跑一趟。

另一处小毛病:摘要分块按 250,000 字符预算贪心切,258,223 字符刚好压线超 3% → 拆成 250k + 8k 两块、
串行两次 haiku 调用(面板上显示为同一会话"连续压缩 2 次",第二次仅 5.6k token),多等 34 秒。

### 修复
1. **饥饿逃生阀**(`server.py` 新增 `_hard_ceiling`):`min(trigger×1.2, 窗口×95%)` 为硬顶——
   1M 窗口 = 216k,200k 窗口夹回 190k(先于撞墙)。循环中 token 超硬顶时不再等 end_turn,强制建
   条目;切点走 `_select_cut` 既有 toolpair 模式,与 proactive/emergency 同一套逻辑,工具对完整。
   未超硬顶维持原闸门行为,正常短循环(涨十几 k 自然 end_turn)不受影响。
2. **摘要尾块合并**(`compressor.py` `_chunk_by_chars`):尾块 < 预算 15% 时并入前一块,预算本是
   防超摘要器窗口的软性粗界,超 15% 一次过比多跑一次串行摘要调用划算。

### 验证
- 新增 `test_emergency.HardCeiling`(3 用例:1M 取 trigger 缓冲、200k 夹回 95%、硬顶恒大于 trigger)
  与 `test_compressor.ChunkTailMerge`(3 用例:小尾并入、正常尾不动、单块不动),91 个测试全绿。
- 运行观察:超长循环中日志应出现 `mid tool-loop starvation, compressing anyway (toolpair cut)`
  且 token 不再越过硬顶持续上涨;压缩临界尺寸(250k~287k 字符)不再拆出微型第二块。

## 1.20.1 — 修复「压缩风暴」:注入自检误判 system 尾巴 + 误删健康条目 + 图片虚高估算

### 背景
1.20.0 为治问题②(压缩后 count/工具调用失败)加了注入产物自检 `_injection_is_safe`,但尾部条件写死成
`role == "user"`。实测(请求归档 + transcript 逐发对照)发现:CC 会把任务提醒、IDE 诊断等附件作为**独立的
`role:"system"` 消息**挂在 messages 末尾——这类请求完全合法,却被自检误判为畸形。三个缺陷咬合成自我喂养
的循环:①自检误判 → ② malformed 分支 `store.remove` 把上一秒还在正常服务的**健康条目误删** → 本发透传
全量 → ③ `_estimate_body_tokens = body//4` **不扣图片 base64**(一张截图 ~600KB ≈ 虚增 15 万 token),
未到 trigger 的请求被误判超限 → proactive 当场压缩建新条目 → 下一条 system 提醒到来再走一遍。表现为
「一直在压缩」「没到 180k 也压」。

### 变更(仅 `proxy/server.py` + 单测)
- **`_injection_is_safe` 尾部条件放宽**:`!= "user"` 改为 `== "assistant"` 才拒——只有末尾 assistant 会被
  上游当 prefill 续写(count 的真正来源);user/system 尾巴均合法,带附件的请求恢复正常注入。
- **malformed 分支不再 `store.remove`**:条目经内容哈希自校验,对其他请求可能完全健康,只跳过本发注入;
  日志补打 head/tail 角色与消息数,再遇真畸形可直接定位。
- **新增 `_image_excess_bytes`**:proactive 触发估算前扣除图片 base64 超出单图 token 上限的字节当量
  (与 `compressor._image_chars` 同口径:单图 `min(1600, max(1, b64//1000))` token),带图会话不再虚高
  近 3 倍;keep_ratio 同步用修正后的估算,不再过切。
- 单测:`test_injection_safety` 补「system 尾巴安全」;`test_emergency` 补 `_image_excess_bytes` 三例
  (顶层图 / tool_result 嵌套图 / 无图归零)。

### 取舍
1.20.0 的 A(自检)方向保留、只放宽一档;B(end_turn gating)不动。真畸形(assistant 尾巴/前缀被切)仍会
拒注入并透传,由 proactive/emergency 兜底——修掉③后,透传不再被图片字节误触发,风暴闭环断在三处。

## 1.19.0 — 升级替换「排空再杀」:不再切断旧会话的在途流式请求

### 背景
版本闸门在检测到「在跑的旧代理版本更低」时,直接 `Stop-Process -Force` 杀掉旧代理再起新版。但旧代理可能正为
另一个**还开着的旧 CC 会话**做流式(SSE)转发;硬杀会切断这条在途连接,对端收到 RST,那个旧会话就看到一发
502。触发条件是「旧会话正传输时,去开一个更新版本的新会话」——升级瞬间的偶发掉发,部分 502 由此而来。

### 变更
- **`proxy/server.py`**:`/health` 响应新增 `inflight` 字段,报当前在途转发请求数(读现成的 `_inflight`
  列表,`_inflight_lock` 保护)。纯增量字段,不影响既有消费者。
- **`hooks/start-proxy.ps1` / `hooks/start-proxy.sh`**:升级分支杀旧代理前先**排空**——轮询旧代理
  `/health.inflight`,为 0 立即杀并接管;大于 0 则每秒轮询等它归零,**封顶 10 秒**后仍硬杀。等待期间新代理
  尚未绑定端口,新会话走既有 fail-open(直连上游、这几发不压),符合「宁可不压、绝不卡死 CC」的一贯取向。
- **`.sh`** 新增 `proxy_inflight()` 辅助(/health 不通或字段缺失一律当 0,不阻塞替换)。

### 取舍
把「硬切在途流(502)」换成了「升级瞬间新会话短暂 fail-open + 超过 10 秒的超长流仍会被切」。封顶时限受
SessionStart hook 总超时 30s 约束(须给随后的启动+轮询留足约 10s),故取 10s;不改 hook timeout。
未动 `allow_reuse_address = False` 的「绑定即锁」单例语义——不引入 `SO_REUSEADDR` 双监听交接,避免重开
已被根治的双实例竞态。要连超长流也不切,需另行调大 hook timeout 与排空封顶,本版不做。

### 无关联行为变更
仅生命周期替换时机,压缩 / 匹配 / 转发 / 记账逻辑完全不变。

## 1.18.1 — 版本号推进,让客户端识别到 1.18 的 marketplace 元数据修复

### 背景
1.18.0 首发后补了 `.claude-plugin/marketplace.json` 的 `version` 字段(插件市场列表页的版本来源),但当时版本号
仍停在 1.18.0;客户端版本闸门「只升不降」,已装 1.18.0 的客户端不会把「同号但元数据变了」识别为更新。

### 变更
- `plugin.json` 与 `.claude-plugin/marketplace.json` 的 `version` → 1.18.1,仅推进 patch 号,使上述元数据修复 +
  1.18.0 的客户端伪装功能一并被 `/plugin marketplace update` → `/plugin update` 识别并拉取。**无代码 / 行为变更。**

## 1.18.0 — 客户端伪装增强:代理自发请求套用「最近大请求」的真实客户端头

### 背景
代理自发的请求(后台压缩、emergency 兜底)此前用**触发那一刻请求的透传头**发给上游 haiku。多数场景没问题,但
触发请求本身可能是个小请求(头部并不完整),而上游中转的 `claude_code_only` 校验看的是 UA / `x-app` /
`x-stainless-*` / `anthropic-*` 这组「像不像真 Claude Code 客户端」的头。用小请求的头去发,偶发被判非客户端流量。

### 变更(`proxy/server.py`)
- **伪装模板缓存**:每个超 `target` 阈值的**真实大请求**经过时(`_maybe_capture_disguise`),把它的完整客户端头
  (UA/`x-app`/`x-stainless-*`/`anthropic-version`/`anthropic-beta` 等)快照进进程级模板 `_disguise_template`
  (`_disguise_lock` 跨线程保护)。鉴权(`authorization`/`x-api-key`)与逐请求易变的连接/编码头
  (`host`/`content-length`/`transfer-encoding`/`accept-encoding`)**不进模板**。token-count 探测请求不刷新模板。
- **`_apply_disguise(auth_headers)`**:代理自发请求发出前,用模板替换头、**但鉴权仍用当次请求的**;
  后台压缩(`_do_background_compression`)与 emergency 兜底(`_emergency_compress`)各在调上游前套用一次。
  `model` 仍由压缩器强制 haiku、`anthropic-beta` 的 `1m` 仍由 `_strip_unsupported_1m_beta` 剥离,不受影响。
- **开关**:`disguise_client` / `ROLLING_CONTEXT_DISGUISE`,默认 `1`(开)。设 `0/false/off` 退回
  「用当次触发请求透传头」的旧行为。启动日志新增 `Disguise client: on/off` 一行。
- **安全回退**:开关关、或尚无大请求填过模板时,`_apply_disguise` 原样返回当次透传头,不影响压缩。

### 不做 TLS 指纹
入站是本地明文 HTTP(无 TLS 握手可采),出站用 Python stdlib `ssl`(JA3/JA4 固定且不可逐字段定制),
模板化 TLS 伪装需换 `curl_cffi`/`tls-client` 重依赖且无 Claude Code(Node)预设——本版只做 HTTP 头伪装。

### 测试
新增 `DisguiseClient` 9 例:`_apply_disguise` 三态(开关关 / 无模板 / 有模板替换头但保鉴权)+
`_maybe_capture_disguise` 刷新条件(大请求刷新 / 小请求不刷新 / count 探测不刷新 / 开关关不刷新 / 排除鉴权连接头)
+ 端到端「大请求捕获→自发请求套 UA」;全套 88 测试通过。

## 1.17.4 — 修复后台压缩去重:已注入旧前缀的请求不再重复触发 haiku 压缩

### 背景
会话高频往返时,请求 A 触发后台压缩后,紧随的请求 B 注入旧前缀转发上游;B 的响应回来时,压缩已由其他会话 promote 转正,
但触发判断中 `redundant = (not injected) and store.covers(msg_hashes)` 因 `not injected` 短路了 covers 检查,
看不到已落地的新压缩,导致对同一段消息再发一次几乎相同的 haiku 压缩。

### 变更(`proxy/server.py`)
- **covers() 加 exclude 参数**:允许已注入请求排除自己注入所用的条目,只检查有无**更新**条目覆盖同一段。
  `oh` 链扩展为 `original_hashes → pending_hashes → intent_hashes`,覆盖从声明意图到转正的全生命周期。
- **redundant 改为 `store.covers(msg_hashes, exclude=injected_via)`**:未注入时 exclude=None(等价全量检查);
  已注入时排除注入条目——有更新条目覆盖则冗余(跳过),仅有注入条目自身覆盖则是正当的"需进一步压缩"。
- **intent_hashes 意图登记**:store.add() 后、起线程前立即写入 `entry["intent_hashes"] = msg_hashes`,
  关闭从声明压缩到线程写 pending_hashes 的空窗;线程完成或失败时清空。
- **already_compressing 加 pending/intent 检查**:覆盖 thread alive → pending 待转正 → intent 已声明的全生命周期。
- **_prune_locked busy() 扩展**:有 intent_hashes 或 pending 的条目也视为在途,不误剪。

### 后续根治(同版本):去重收敛为单一原子判定
上面保留了两道防线——`covers`(精确、内容感知)与全局 `already_compressing`(粗粒度)。审查发现后者两个毛病:
① **跨会话误挡**——某会话后台压缩在跑的整个 30~50s 内,其它会话只要触发判断就看到 `already_compressing=True`
而跳过自己的压缩;② **TOCTOU**——「检查 covers → store.add 登记意图」非原子,两个并发请求可能都通过 covers 后各起
一个线程重复压同一段(全局 already_compressing 也兜不住:第一个请求的线程尚未 start 时第二个就已通过检查)。
- **promote_pending 字段转正收进 `self._lock`**:与 `covers`/`find_match` 同锁互斥,去重判定永远读到一致快照,
  消除「`pending` 已清而 `original_hashes` 尚未就绪」的半态竞争;含 IO 的 `remove`/`persist`/`log` 移到锁外
  (`_lock` 不可重入,且避免持锁做盘/日志 IO 阻塞并发热路径)。
- **新增 `claim_compression(msg_hashes, exclude)`**:一把锁内原子完成「covers 检查 → 建条目 → 写 intent_hashes」,
  返回条目或 None。触发块改用它,**删除全局 `already_compressing`**;`covers` 拆出无锁实现 `_covers_locked` 供复用。
- **效果**:`covers` 成为唯一、精确、无竞争的去重判定——跨会话误挡消失,并发重复触发的 TOCTOU 窗口关闭。
- **测试**:新增 `ClaimCompression` 6 例(空库登记意图 / promoted·pending·在途 intent 三态拦截 / exclude 放行 /
  更新条目挡下);全套 80 测试通过。

## 1.17.0 — 输出明细拆分(thinking/text/tool_use)+ 大输出/长耗时回合整份归档备查

### 背景
排查"为什么有些请求 >100s、输出 10 多 k 但我没感知"时发现:proxy 透明转发、不解析 SSE 内容块,`output_tokens`
只是上游回报的**总量**,无从知道这 10k 里**思考(thinking)/ 正文(text)/ 工具调用(tool_use)** 各占多少——
七成慢请求其实是上游用 ~75 tok/s 真吐了 7k~30k token,大头是 thinking + 工具调用(重 agentic 回合),可见正文很短,
所以"没感知"。同时这些大输出/长耗时回合的**完整内容当下无处可查**,事后想审"它到底输出了啥"只能干瞪眼。

### 变更
**输出明细拆分(`proxy/server.py` + `proxy/stats.py` + `dashboard.html`)**
- 流式响应已全量回给 CC 后,从 `buffer` 副本一趟解析 SSE 内容块:按 `content_block_start` 的 `index→type` +
  `content_block_delta`(`thinking_delta`/`text_delta`/`input_json_delta`/`signature_delta`)累加各段字符数,
  同时攒出有序可读块。抽成纯函数 `_parse_output_blocks`(流式)/ `_parse_output_blocks_json`(非流式)。
- `record` 新增 `out_thinking_chars` / `out_text_chars` / `out_tool_chars`;`stats.recent[]` 透出 `out_thinking`/
  `out_text`/`out_tool`。只读 buffer 副本,不动透传字节流;解析失败包死、不影响请求与落库。
- dashboard 最近请求表新增「明细」列:`💭thinking% 📝text% 🔧tool%` 占比,tooltip 给近似 token 数(字符/4)。

**大请求归档(`proxy/server.py`)**
- 对大输出或长耗时的真实生成回合,把**完整请求体 + 完整响应内容**单独落一份 `*.json.gz` 存档(`meta` 含
  usage/计时/flags/三段明细,`request` 全量,`response` 重建的可读内容块;错误回合附错误体片段)。
- 触发:`_should_archive` —— `output_tokens ≥ ARCHIVE_MIN_OUT` 或 `t_total_ms ≥ ARCHIVE_MIN_MS`,任一即归档
  (out=0 的 502/524/空响应若超时长阈值也归档,请求体 + 错误指纹照样可审)。压缩器自身调用、count 探测不归档。
- 落在响应已回完之后(`finally` 内、`stats.record` 之前),不增可感延迟;整段 `try/except` 包死,**归档失败绝不
  影响请求或落库**。`payload` 兜底递归脱敏(抹 `authorization`/`x-api-key`/`sk-` 串;auth 本在 header 不在 body)。
- 空间封顶:`_prune_archive_dir` 每次写入后按总量(`ARCHIVE_CAP_MB`,默认 200)删最旧文件,回落到上限之下。
- 配置(全走 `_cfg`,默认开,可环境变量覆盖):`ROLLING_CONTEXT_ARCHIVE` / `_ARCHIVE_MIN_OUT`(8000)/
  `_ARCHIVE_MIN_MS`(90000)/ `_ARCHIVE_CAP_MB`(200)。

**归档查看(`dashboard.html` + 两个 GET 路由)**
- `/stats/archive` 列归档文件(名/大小/mtime);`/stats/archive/get?name=<file>` 校验 basename 防穿越、
  `gzip` 解压回 JSON。dashboard 最近表新增「存档」列,有存档的行给 📄 链接,点开即看完整请求 + 响应。

### 测试
- 新增 `proxy/test_archive.py`(11 例):明细解析(流式三段 / signature 归 thinking 不计正文 / 非流式 / 坏行跳过)、
  `_should_archive` 阈值边界(输出 / 时长 / 双不达 / 压缩 kind / 关闭)、`_write_archive` 脱敏 + gzip 往返、
  `_prune_archive_dir` 按总量删最旧留最新。导入隔离:bind 自己的 temp state-dir 后从 `sys.modules` 摘除 + 复原 env,
  保留原导入拓扑(不破坏 test_emergency 的 STATE_DIR 隔离断言)。
- 全套 63 → **74 passed**。

## 1.16.0 — 转发前主动同步压缩消除 resume 冷启动卡顿;修 PowerShell UTF-8 读取腐化 settings.json

### 背景
两件事合并发版。

**(1) resume 第一发「假死」。** 实测发现 resume 一个大会话时,第一发请求会长时间无响应,用户以为死了 Ctrl+C
再 resume 才恢复。排查确认**非死锁,而是冷启动 + 慢上游的假死**:resume 新会话的第一发请求此刻没有可命中
的压缩(压缩要「看到第一发之后」才在后台算),于是把全量 ~257k token 的 transcript 打给第三方上游,缓存全冷,
streaming 嚼完要数十秒(接近 1M 的会话可达数分钟);用户 Ctrl+C 后,后台压缩其实已算完入库,再 resume 就命中
注入、请求缩到 ~44k、缓存转热,恢复正常。根因:第一发付了「全量打慢上游」的代价。

**(2) 我在 1.15.x 引入的 PowerShell 编码回归。** hook 的 `Get-Content` 没带 `-Encoding UTF8`,在中文 Windows 按
默认 cp936 读 UTF-8 的 `settings.json`,把「中文」误读成乱码,再 `ConvertTo-Json` + `WriteAllText` 写回 →
**每个会话腐化一代**,最终某代字节落入 PUA 区(U+E15F)→ `ConvertFrom-Json` 彻底崩、hook 再也改不动
settings.json,fail-open 失效。

### 变更
**主动同步压缩(`proxy/server.py`)**
- 新增开关 `PROACTIVE_COMPRESS`(config `proactive_compress` / env `ROLLING_CONTEXT_PROACTIVE_COMPRESS`,默认开)。
- 转发上游**之前**:未命中缓存(`not injected`)的请求,按 `len(raw_body)//4` 粗估 token(含 system+tools,
  与 breakdown 日志同口径),超 `_effective_trigger` 即当场 `_emergency_compress` 同步压一次、把结果换进请求体
  再发。复用既有同步压缩机制——压完即登记进 store(prefix + key 链)并落盘,**后续请求直接命中、不再付这次
  同步延迟**。第一发账单也从全量(~257k)降到压后(~40k)。
- defense-in-depth:主动压缩按粗估定的 keep 比例可能偏松、压后仍超限,故放宽 emergency 兜底条件为
  `not injected or prewarm`——prewarm 的请求若仍吃 400,用 400 体里上游自报的真实 token 数再压一发,保住
  「CC 永不见 400」。命中真实压缩条目(match)的 injected 请求不放行,它已是 known-good 尺寸。
- 统计 record 新增 `prewarm` 字段;后台压缩父条目回收链补 `proactive_entry`。
- 为何不做「SessionStart 从 transcript 预热」:压缩匹配是消息内容的**精确哈希链命中**,从 transcript JSONL 重建
  CC 第一发的逐条消息极易漂移(CC 重排/插 system-reminder/改写 tool_result)→ 预热大概率不命中、白付一发
  Haiku。转发前主动压缩压的就是请求里这批消息、注入同一发,确定性命中,故选它。

**PowerShell UTF-8 修复(`hooks/start-proxy.ps1`)**
- 三处 `Get-Content | ConvertFrom-Json`(读 rolling-context.json 配置、读 plugin.json 版本、读 settings.json 更新)
  全部补 `-Encoding UTF8`。PS5.1 默认按进程 codepage(中文系统 = cp936)读文件内容,即便加 `-Raw` 也不够,
  必须显式 `-Encoding UTF8`,否则 UTF-8 多字节字符被误读、反复读写腐化成乱码/PUA 字符。
- 注:已被活跃旧客户端腐化的 settings.json 需新版本铺到所有客户端缓存 + 全部重启后才彻底止血(旧 hook 仍在
  每个 SessionStart 重新腐化)。

**无 Python 优雅降级(`hooks/start-proxy.ps1` `hooks/start-proxy.sh` `README.md`)**
- proxy 本体是纯 Python 脚本,没解释器就起不来。旧行为:无 Python 时 hook 仍一路尝试启动 + 轮询,**每次
  SessionStart 白等 ~5 秒**、刷警告;且若曾指向代理、后又没了 Python,重置 settings 的逻辑本身要 Python →
  CC 可能被钉死在死代理上。
- 两个 launcher 开头加「可用 Python」探测——**实跑 `python -c "print('ok')"` 核对输出**,绕开 Windows 微软
  商店那个"命令在、却跑不出东西"的空壳 python3。探不到则:`.ps1` 跳过启动循环、落到既有 section 4 的**纯
  PowerShell fail-open**(把 `ANTHROPIC_BASE_URL` 还原回真上游/官方 API,不依赖 Python)再 exit 0,让
  `powershell ... || bash ...` 短路不落到 `.sh` 重复空等;`.sh`(Mac/Linux 及兜底)log 一行后直接 exit 0,
  跳过 10×0.5s 空等。
- README 加「Prerequisite: Python 3.7+ on PATH」章节:Windows 指 python.org + 勾 Add to PATH、警告别用商店
  空壳、给 `python -c "print('ok')"` 自检;并写明无 Python 也不会坏(自禁 + fail-open,CC 照常用真上游)。

**重复压缩去重(`proxy/server.py`)**
- 现象:同一会话两发连续请求,第一发(慢全量)在途未返回时压缩还在后台跑、没注入 → 原样打上游;~50s 后
  回来,本发自报 token 仍是旧全量值、又超 trigger,而此刻覆盖它的压缩往往已落库 → 响应末尾又触发一条**几乎
  一样的第二次压缩**(白烧一发 Haiku)。
- 修复:`CompressionStore` 新增只读 `covers(msg_hashes)`,同时检查已转正(`original_hashes`)和刚就绪待转正
  (`pending_hashes`)的哈希链是否已覆盖当前消息。响应末尾后台触发块加守卫:`not injected and store.covers(...)`
  即判为冗余、跳过。仅限「未注入」请求——已注入仍超限是「需进一步压缩」的正当场景,不在此列。

**Dashboard 折叠/展开卡顿 + 压缩列语义(`proxy/dashboard.html`)**
- 「最近请求」表从默认 `table-layout:auto` 改 `table-fixed` + `<colgroup>` 定死列宽。auto 下列宽依赖**所有行**内容,
  展开/折叠任一错误明细行、或 15s 自动刷新整表重建,都会触发**全表 reflow**(列宽重算)→ 100 行就卡;fixed 后
  列宽与内容解耦,切换行只局部重绘。
- `renderRecent` 加签名守卫:数据(行数 + 各行 ts/status/压缩/output)没变就跳过整表重建,空闲时 15s 自动刷新不再
  无谓重绘、也不打断正在查看的展开明细。
- 「压缩」列从 `injected`(每个注入请求都打 ✓,满屏 ✓)改为 `first_compressed`(只标压缩**生效的第一个请求**),与
  绿色「✂ 生效」徽标同义、不再刷屏。会话列加 `truncate` 防固定宽下溢出。

**顺手清理**
- 删除 `_request_window` / `_effective_trigger` 的重复定义(1.15.0 遗留的复制粘贴,后定义覆盖前定义,无害但冗余)。

## 1.15.1 — 摘要走 Haiku 时剥掉它不支持的 context-1m beta,修 summarizer 400

### 背景
1.15 后实测发现:CC 在 `model[1m]` 下会给请求带 `anthropic-beta: context-1m-2025-08-07` 头,而压缩器
(`compressor.py`)的摘要请求 `headers = dict(auth_headers)` 原样透传了这个头。问题的本质是**只有 Haiku
模型不支持 1M 长上下文 beta**,而摘要默认就走 Haiku;上游因此以
`400 The long context beta is not yet available for this subscription` 拒掉,导致压缩失败。

### 变更(`proxy/compressor.py`)
- 新增 `_model_supports_1m(model)`:Haiku 判不支持,其余在用模型(Opus 4.x / Sonnet 4.6 / Fable)支持。
- 新增 `_strip_unsupported_1m_beta(headers, model)`:**当目标模型不支持 1M 时**,原地从 `anthropic-beta`
  剥掉 `context-1m` token、保留其余 beta(如 fine-grained-tool-streaming);剥空则删掉该头。判定挂在模型
  上,而非「因为是摘要请求」。
- `_summarize_chunk` 构造摘要请求头后以 `self.summarizer_model` 调用它。**主代理路径不动**——真实 1M
  请求(Opus 等支持 1M 的模型)照常带头走上游。

### 实测(2026-06-28)
`proxy/test_compressor.py` 新增 `StripUnsupported1mBeta`:Haiku 只剥 context-1m / 仅含则删头 / 支持 1M
的模型(opus)原样保留 / 头名大小写 / 无头 noop / `_summarize_chunk` 出站请求确不含 anthropic-beta,
共 6 例。全套 `test_compressor` + `test_emergency` + `test_stats` 合计 **47 例全绿**。

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
