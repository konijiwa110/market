# ChangeLog

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
