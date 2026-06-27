# 网关统计看板(`/stats`)

代理在转发每个 `/v1/messages` 时旁路记一条统计,落盘 `~/.claude/rolling-context-stats.jsonl`
(一行一条,追加写,重启自动从文件尾回载)。浏览器打开 **`http://127.0.0.1:<端口>/stats`** 看图表。
统计是「旁路」逻辑:不改转发/压缩行为,出错被吞,绝不影响请求本身。

## 端点

| 路径 | 说明 |
| --- | --- |
| `GET /stats` | 看板页面(单文件 `dashboard.html`,暗色,Tailwind + Chart.js via CDN) |
| `GET /stats/data?hours=<n>` | 聚合后的 JSON;`hours` 省略或 `all` = 全部历史,否则取最近 n 小时 |

看板支持 1h / 6h / 24h / 7天 / 30天 / 全部 时间窗,15s 自动刷新(可关)。

## 每条记录(server.py 埋点)

只记真实生成调用;`count_tokens` 探测调用**不计**,避免污染。字段:

- **token**:`input_tokens` / `cache_read` / `cache_create` / `output_tokens`(统一从 SSE usage 解析)。
- **字节**:`req_bytes` / `resp_bytes`。
- **四段耗时**(`time.perf_counter`):
  - `t_overhead_ms` 代理开销(进入 → 转发);
  - `t_prefill_ms` 首字(转发 → 首字节,≈ 输入处理时间);
  - `t_gen_ms` 生成(首字 → 末字,≈ 输出时间);
  - `t_total_ms` 总响应时间。
- **`stream_chunks`**:流式响应实际收到的数据块数(见下「tok/s 与缓冲突发」)。
- **`concurrent`**:记录时有其它生成请求同时在途(用在途登记表判定)。
- **`injected`**:本次是否注入了压缩前缀。`status`:上游响应码。
- **错误指纹**(仅 status ≥ 400 时填,见 `_capture_error_source`):
  `err_source`(`cloudflare` / `upstream` / `other`)、`err_server`、`err_cf_ray`、
  `err_ctype`、`err_retry_after`、`err_snippet`(响应体前 500 字)。

## 错误归因:Cloudflare 边缘 vs 上游 origin

429/5xx 到底是谁限的?关键陷阱:**sub2api 上游也挂在 Cloudflare 后面**,所以
`Server: cloudflare` / `CF-RAY` 几乎人人都有,不能据此判定是 CF 拦的。判定依据:

- **cloudflare**:CF 自身拦截 —— `text/html` 错误页,或带 `cf-mitigated`。
- **upstream**:上游 origin 生成 —— JSON 错误体(`{"error":{...}}`)+ 源站 `Via` / `X-Request-Id`。
- **other**:都不匹配。

看板:「请求数」KPI 给出 `CF x · 上游 y` 拆分;最近请求表的错误行可**点击展开**,
显示来源、指纹与原始响应体,直接看上游回了什么。

## tok/s 与缓冲突发(bursty)

`tok/s = output_tokens ÷ t_gen_ms`。单条 Anthropic 流真实速率约 **50~100 tok/s**;持续高于
~250 在物理上不可能源自真流式 —— 几乎一定是上游把整条 SSE 缓冲后**一次性吐出**:首字=末字、
`t_gen_ms` 塌缩、tok/s 虚高。识别办法是看 `stream_chunks`:真流式有几十~上百块,缓冲突发只有 1~2 块。

当 `tok/s > 250` **或** 整条响应仅 1~2 块送达时,该样本判为 **bursty**,从所有吞吐统计里隔离
(否则真实速率会被虚高值拉爆),表里以琥珀色 `⚡` 标注,KPI 给出「疑似缓冲 N 次」。
因此「输出吞吐(单请求)」反映的是排除并发挤占、排除缓冲突发后的**真实生成速率**。
阈值常量 `BURST_TPS` 在 `stats.py` 顶部,可改。

## 聚合产物(stats.aggregate)

`totals`(KPI)、`series`(按时间分桶的折线,桶大小随窗口自适应 1m→1d)、`latency`
(p50/p90/p99)、`by_model` / `by_session`(占比)、`recent`(最新 100 条明细)。
分位用线性插值;全部纯标准库,无外部依赖。

## 日志大小

- `rolling-context-debug.log` 按大小滚动:单文件 10MB、保留 5 个历史(≈60MB 封顶)。
  环境变量 `ROLLING_CONTEXT_LOG_MB` / `ROLLING_CONTEXT_LOG_BACKUPS` 可覆写。
- stdout(被 start-proxy 重定向到 `rolling-context-proxy.log`)降到 INFO 级,体量约为 DEBUG 的 1/10;
  完整 DEBUG 仍写入会滚动的 debug.log。两边都不再无界增长。

## 隐私 / 网络

- 新增本地文件 `~/.claude/rolling-context-stats.jsonl`(纯统计,不含消息正文;`err_snippet` 是错误响应体片段)。
- 看板的 Tailwind / Chart.js 走公网 CDN(Chart.js 已加 SRI 校验)。本机离线时图表不渲染,但数据接口不受影响。
