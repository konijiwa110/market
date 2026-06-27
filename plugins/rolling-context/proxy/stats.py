"""
Rolling Context Proxy — request statistics.

Records one entry per real /v1/messages generation call: token usage
(input / cache-read / cache-create / output), latency breakdown
(proxy overhead, prefill = time-to-first-token, generation, total), and a
few flags. Entries live in an in-memory ring buffer and are appended to a
JSONL file so the numbers survive a proxy restart. All aggregation for the
dashboard happens here.

Pure stdlib — no external dependencies.
"""

import os
import json
import threading
import collections

STATS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-stats.jsonl")
MAX_RECORDS = 50000

# 单条 Anthropic 流的真实输出速率上限大约 50~100 tok/s(Haiku 偶尔更高),持续高于此值
# 在物理上不可能源自真流式 —— 几乎一定是上游(sub2api)把整条 SSE 缓冲后一次性吐出,
# 导致首字=末字、生成耗时塌缩、tok/s 虚高。超过这个阈值或只用极少数据块送达的记录标为
# "bursty"(疑似缓冲),从吞吐统计里隔离出去,避免污染真实速率。
BURST_TPS = 250


def _is_bursty(out, gen_ms, chunks):
    """判断一条记录的 tok/s 是否是上游缓冲突发造成的虚高(而非真实生成速率)。"""
    if out < 20 or gen_ms <= 0:
        return False
    if out / (gen_ms / 1000.0) > BURST_TPS:
        return True
    # stream_chunks 已记录(>0)且整条响应只用了 1~2 个数据块 —— 真流式不可能,必是缓冲后突发。
    if 0 < chunks <= 2 and out >= 50:
        return True
    return False


def _percentile(sorted_vals, pct):
    """Linear-interpolated percentile of a pre-sorted list."""
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


class StatsCollector:
    """In-memory ring buffer of per-request records, mirrored to a JSONL file."""

    def __init__(self, path=STATS_PATH, max_records=MAX_RECORDS):
        self._path = path
        self._lock = threading.Lock()
        self._records = collections.deque(maxlen=max_records)
        self._load()

    def _load(self):
        """Load the tail of the JSONL file so history survives a restart."""
        try:
            with open(self._path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return
        for line in lines[-self._records.maxlen:]:
            line = line.strip()
            if not line:
                continue
            try:
                self._records.append(json.loads(line))
            except Exception:
                continue

    def record(self, rec: dict):
        """Append one request record to memory and the JSONL file."""
        with self._lock:
            self._records.append(rec)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _bucket_seconds(span: float) -> int:
        """Pick a time-bucket size that yields a readable number of points."""
        if span <= 2 * 3600:
            return 60            # 1 minute
        if span <= 12 * 3600:
            return 300           # 5 minutes
        if span <= 3 * 86400:
            return 3600          # 1 hour
        if span <= 30 * 86400:
            return 6 * 3600      # 6 hours
        return 86400             # 1 day

    def aggregate(self, hours, now, extra=None) -> dict:
        """Aggregate records within the last `hours` (None = all) into the
        structure consumed by the dashboard."""
        with self._lock:
            records = list(self._records)
        if hours is not None:
            cutoff = now - hours * 3600
            records = [r for r in records if r.get("ts", 0) >= cutoff]

        totals = {
            "requests": 0, "input_tokens": 0, "cache_read": 0, "cache_create": 0,
            "output_tokens": 0, "injected_requests": 0, "errors": 0,
            "req_bytes": 0, "resp_bytes": 0,
            # 错误来源拆分 + 并发统计(见 server._capture_error_source / 并发检测)。
            "errors_cf": 0, "errors_upstream": 0, "errors_other": 0,
            "concurrent_requests": 0,
            # 疑似上游缓冲突发(tok/s 虚高)的请求数 —— 这些样本不计入吞吐统计。
            "bursty_requests": 0,
        }
        total_ms_list, prefill_list, gen_list, tps_list = [], [], [], []
        # 单请求(非并发期)吞吐单独成列,与全部吞吐对比 —— 并发期内吞吐会互相挤占失真。
        tps_single_list = []

        span = (now - min((r.get("ts", now) for r in records), default=now))
        bucket = self._bucket_seconds(max(span, 60))
        buckets = {}
        models = {}
        sessions = {}

        for r in records:
            ts = r.get("ts", 0)
            inp = r.get("input_tokens", 0) or 0
            cr = r.get("cache_read", 0) or 0
            cc = r.get("cache_create", 0) or 0
            out = r.get("output_tokens", 0) or 0
            tot_ms = r.get("t_total_ms", 0) or 0
            pre_ms = r.get("t_prefill_ms", 0) or 0
            gen_ms = r.get("t_gen_ms", 0) or 0
            status = r.get("status", 0) or 0
            billed = inp + cr + cc
            concurrent = bool(r.get("concurrent"))
            chunks = r.get("stream_chunks", 0) or 0
            bursty = _is_bursty(out, gen_ms, chunks)

            totals["requests"] += 1
            totals["input_tokens"] += inp
            totals["cache_read"] += cr
            totals["cache_create"] += cc
            totals["output_tokens"] += out
            totals["req_bytes"] += r.get("req_bytes", 0) or 0
            totals["resp_bytes"] += r.get("resp_bytes", 0) or 0
            if r.get("injected"):
                totals["injected_requests"] += 1
            if concurrent:
                totals["concurrent_requests"] += 1
            if bursty:
                totals["bursty_requests"] += 1
            if status >= 400:
                totals["errors"] += 1
                src = r.get("err_source")
                if src == "cloudflare":
                    totals["errors_cf"] += 1
                elif src == "upstream":
                    totals["errors_upstream"] += 1
                else:
                    totals["errors_other"] += 1
            if tot_ms:
                total_ms_list.append(tot_ms)
            if pre_ms:
                prefill_list.append(pre_ms)
            if gen_ms:
                gen_list.append(gen_ms)
            # 吞吐统计排除 bursty(疑似缓冲)样本,否则真实速率会被虚高值拉爆。
            if gen_ms and out and not bursty:
                tps = out / (gen_ms / 1000.0)
                tps_list.append(tps)
                if not concurrent:
                    tps_single_list.append(tps)

            bstart = int(ts // bucket) * bucket
            b = buckets.setdefault(bstart, {
                "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
                "requests": 0, "total_ms": 0, "prefill_ms": 0, "gen_ms": 0,
                "lat_n": 0, "tps_sum": 0, "tps_n": 0, "cr": 0, "billed": 0,
                "tps_single_sum": 0, "tps_single_n": 0,
                "tps_conc_sum": 0, "tps_conc_n": 0,
            })
            b["input"] += inp
            b["cache_create"] += cc
            b["cache_read"] += cr
            b["output"] += out
            b["requests"] += 1
            b["cr"] += cr
            b["billed"] += billed
            if tot_ms:
                b["total_ms"] += tot_ms
                b["prefill_ms"] += pre_ms
                b["gen_ms"] += gen_ms
                b["lat_n"] += 1
            if gen_ms and out and not bursty:
                _tps = out / (gen_ms / 1000.0)
                b["tps_sum"] += _tps
                b["tps_n"] += 1
                if concurrent:
                    b["tps_conc_sum"] += _tps
                    b["tps_conc_n"] += 1
                else:
                    b["tps_single_sum"] += _tps
                    b["tps_single_n"] += 1

            m = models.setdefault(r.get("model", "unknown"),
                                  {"input": 0, "output": 0, "requests": 0})
            m["input"] += billed
            m["output"] += out
            m["requests"] += 1

            s = sessions.setdefault(r.get("session", "--------"),
                                    {"total": 0, "requests": 0})
            s["total"] += billed + out
            s["requests"] += 1

        # Build continuous time series (fill empty buckets with zeros).
        labels = []
        series = {k: [] for k in (
            "input_fresh", "cache_create", "cache_read", "output", "billed",
            "requests", "total_ms", "prefill_ms", "gen_ms", "output_tps",
            "output_tps_single", "output_tps_concurrent", "cache_hit_pct",
        )}
        if buckets:
            first, last = min(buckets), max(buckets)
            steps = int((last - first) / bucket) + 1
            keys = ([first + i * bucket for i in range(steps)]
                    if steps <= 5000 else sorted(buckets))
            for k in keys:
                labels.append(k)
                b = buckets.get(k)
                if not b:
                    for key in series:
                        series[key].append(0)
                    continue
                series["input_fresh"].append(b["input"])
                series["cache_create"].append(b["cache_create"])
                series["cache_read"].append(b["cache_read"])
                series["output"].append(b["output"])
                series["billed"].append(b["billed"])
                series["requests"].append(b["requests"])
                n = b["lat_n"]
                series["total_ms"].append(round(b["total_ms"] / n, 1) if n else 0)
                series["prefill_ms"].append(round(b["prefill_ms"] / n, 1) if n else 0)
                series["gen_ms"].append(round(b["gen_ms"] / n, 1) if n else 0)
                series["output_tps"].append(round(b["tps_sum"] / b["tps_n"], 1) if b["tps_n"] else 0)
                series["output_tps_single"].append(
                    round(b["tps_single_sum"] / b["tps_single_n"], 1) if b["tps_single_n"] else 0)
                series["output_tps_concurrent"].append(
                    round(b["tps_conc_sum"] / b["tps_conc_n"], 1) if b["tps_conc_n"] else 0)
                series["cache_hit_pct"].append(round(100 * b["cr"] / b["billed"], 1) if b["billed"] else 0)

        billed_total = totals["input_tokens"] + totals["cache_read"] + totals["cache_create"]
        totals["total_input_billed"] = billed_total
        totals["cache_hit_pct"] = round(100 * totals["cache_read"] / billed_total, 1) if billed_total else 0
        total_ms_list.sort()
        prefill_list.sort()
        gen_list.sort()
        totals["avg_total_ms"] = round(sum(total_ms_list) / len(total_ms_list), 1) if total_ms_list else 0
        totals["avg_prefill_ms"] = round(sum(prefill_list) / len(prefill_list), 1) if prefill_list else 0
        totals["avg_gen_ms"] = round(sum(gen_list) / len(gen_list), 1) if gen_list else 0
        totals["avg_output_tps"] = round(sum(tps_list) / len(tps_list), 1) if tps_list else 0
        totals["avg_output_tps_single"] = round(
            sum(tps_single_list) / len(tps_single_list), 1) if tps_single_list else 0
        totals["p50_total_ms"] = round(_percentile(total_ms_list, 50), 1)
        totals["p90_total_ms"] = round(_percentile(total_ms_list, 90), 1)
        totals["p99_total_ms"] = round(_percentile(total_ms_list, 99), 1)

        latency = {
            "labels": ["p50", "p90", "p99"],
            "total": [round(_percentile(total_ms_list, p), 1) for p in (50, 90, 99)],
            "prefill": [round(_percentile(prefill_list, p), 1) for p in (50, 90, 99)],
            "gen": [round(_percentile(gen_list, p), 1) for p in (50, 90, 99)],
        }

        by_model = sorted(
            ({"model": k, "input": v["input"], "output": v["output"],
              "requests": v["requests"], "total": v["input"] + v["output"]}
             for k, v in models.items()),
            key=lambda x: x["total"], reverse=True,
        )
        by_session = sorted(
            ({"session": k, "total": v["total"], "requests": v["requests"]}
             for k, v in sessions.items()),
            key=lambda x: x["total"], reverse=True,
        )[:12]

        recent = []
        for r in records[-100:][::-1]:
            r_out = r.get("output_tokens", 0) or 0
            r_gen = r.get("t_gen_ms", 0) or 0
            r_chunks = r.get("stream_chunks", 0) or 0
            recent.append({
                "ts": r.get("ts", 0), "model": r.get("model", "unknown"),
                "session": r.get("session", "--------"),
                "input": r.get("input_tokens", 0), "cache_read": r.get("cache_read", 0),
                "cache_create": r.get("cache_create", 0), "output": r_out,
                "total_ms": r.get("t_total_ms", 0), "prefill_ms": r.get("t_prefill_ms", 0),
                "gen_ms": r_gen, "injected": r.get("injected", False),
                "status": r.get("status", 0),
                # 单请求输出速度(tok/s)、数据块数、是否疑似缓冲、并发标记、错误来源指纹与响应明细。
                "tps": round(r_out / (r_gen / 1000.0), 1) if (r_gen and r_out) else 0,
                "chunks": r_chunks,
                "bursty": _is_bursty(r_out, r_gen, r_chunks),
                "concurrent": bool(r.get("concurrent")),
                "err_source": r.get("err_source", ""),
                "err_retry_after": r.get("err_retry_after", ""),
                "err_server": r.get("err_server", ""),
                "err_ctype": r.get("err_ctype", ""),
                "err_cf_ray": r.get("err_cf_ray", ""),
                "err_snippet": r.get("err_snippet", ""),
            })

        return {
            "generated_at": now,
            "window_hours": hours,
            "bucket_seconds": bucket,
            "config": extra or {},
            "totals": totals,
            "series": {"labels": labels, **series},
            "latency": latency,
            "by_model": by_model,
            "by_session": by_session,
            "recent": recent,
        }
