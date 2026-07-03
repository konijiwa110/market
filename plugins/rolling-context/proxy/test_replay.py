"""Replay tests — 用「真实事故的形状」当 golden fixture,回放匹配层的不变量。

每个 fixture 都对应一次真实线上事故(形状取自请求归档,内容合成、不含真实对话):
  1. ResumeThinkingStrip  — 2026-07-03 fa861c68:CC resume 剥掉历史 assistant 的 thinking 块,
                            深条目 221 条哈希链中 61 条失配 → 480k 裸奔(1.20.3 根治)。
  2. SameTurnRePrompt     — 同日同会话:模型对文字优先的插话回了 tool_use,CC 原样重发同轮、
                            在最后一条 user 消息追加 ~6KB CRITICAL 指令块。尾部改写不得影响
                            前缀条目命中。
  3. SystemTailRequest    — 1.20.1 压缩风暴触发形状:CC 把任务提醒等作为独立 role:"system"
                            消息挂在 messages 末尾。匹配与注入不得被 system 尾巴干扰。
  4. ArchiveReplay        — 机会性回放:本机存在真实归档(~/.claude/rolling-context-archive)
                            时,逐份校验哈希不变量;归档不存在(CI/他机)自动跳过。
                            真实对话永不提交进仓库。

改动哈希 / 匹配 / 注入逻辑时,这套测试必须全绿;新事故修复后应在此补一个对应形状的 fixture。

Run:  python -m unittest test_replay      (from this proxy/ dir)
"""
import glob
import gzip
import json
import os
import sys
import tempfile
import unittest

# 先记下真实 HOME 下的归档目录(供机会性回放);随后再做导入隔离。
# 若本模块在别的测试模块之后加载(env 已被指到 tmp),该目录不存在 → ArchiveReplay 自动跳过。
_REAL_ARCHIVE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-archive")

if "server" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="rc-replay-")
    os.makedirs(os.path.join(_TMP, ".claude"), exist_ok=True)
    os.environ["USERPROFILE"] = _TMP
    os.environ["HOME"] = _TMP
    os.environ["ROLLING_CONTEXT_STATE_DIR"] = _TMP
    os.environ.setdefault("ANTHROPIC_BASE_URL", "https://example.invalid")

import server  # noqa: E402


def _strip_thinking(messages):
    """模拟 CC resume:剥掉 assistant 消息里的 thinking/redacted_thinking 块。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c
                 if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
        out.append({**m, "content": c})
    return out


def _make_conversation(n_turns):
    """合成带 thinking + 工具调用的 agentic 对话:每轮 user 提问 → assistant(thinking+text+tool_use)
    → user(tool_result)。形状与真实归档一致。"""
    msgs = [{"role": "user", "content": "帮我部署 newapi 服务"}]
    for i in range(n_turns):
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"第 {i} 步:检查服务状态再决定", "signature": f"SIG{i}"},
            {"type": "text", "text": f"执行第 {i} 步检查。"},
            {"type": "tool_use", "id": f"toolu_{i:04d}", "name": "Bash",
             "input": {"command": f"docker ps | grep svc{i}"}},
        ]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i:04d}", "content": f"svc{i} running"},
        ]})
    return msgs


def _seed_entry(covered_messages, tag):
    """按后台压缩转正后的形态构造条目:original_hashes 链 + prefix 摘要对。"""
    entry = server.store.add()
    entry["original_hashes"] = server._hash_messages(covered_messages)
    entry["prefix"] = [
        {"role": "user", "content": f"[ROLLING_CONTEXT_SUMMARY] {tag}"},
        {"role": "assistant", "content": "I have the full context. Continuing."},
    ]
    return entry


class ResumeThinkingStrip(unittest.TestCase):
    """事故①:resume 剥 thinking 后,昨天建的深条目必须照常命中。"""

    def setUp(self):
        server.store._compressions = []

    def test_deep_entry_survives_resume(self):
        live = _make_conversation(20)          # 会话进行中(带 thinking)建的条目
        entry = _seed_entry(live[:31], "deep")
        resumed = _strip_thinking(live) + [    # resume 重放(无 thinking)+ 新增一轮
            {"role": "assistant", "content": [{"type": "text", "text": "继续。"}]},
            {"role": "user", "content": "本地部署了吗\n"},
        ]
        best, best_end = server.store.find_match(server._hash_messages(resumed), resumed)
        self.assertIs(best, entry)
        self.assertEqual(best_end, 31)

    def test_mid_conversation_chain_survives_resume(self):
        # 真实形态:链条对应的是「注入视图」的中段消息(entry [24] 命中 raw 526–746 的机制)
        live = _make_conversation(20)
        entry = _seed_entry(live[15:31], "mid-chain")
        resumed = _strip_thinking(live)
        best, best_end = server.store.find_match(server._hash_messages(resumed), resumed)
        self.assertIs(best, entry)
        self.assertEqual(best_end, 31)


class SameTurnRePrompt(unittest.TestCase):
    """事故②:CC 同轮重发、尾部 user 消息追加指令块,前缀条目命中不得受影响。"""

    def setUp(self):
        server.store._compressions = []

    def test_tail_mutation_keeps_prefix_match(self):
        msgs = _make_conversation(10) + [{"role": "user", "content": [
            {"type": "text", "text": "本地部署了吗\n"},
        ]}]
        entry = _seed_entry(msgs[:15], "prefix")
        reprompt = [dict(m) for m in msgs]
        reprompt[-1] = {"role": "user", "content": [
            {"type": "text", "text": "本地部署了吗\n"},
            {"type": "text", "text": "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n" + "x" * 5900},
        ]}
        best, best_end = server.store.find_match(server._hash_messages(reprompt), reprompt)
        self.assertIs(best, entry)
        self.assertEqual(best_end, 15)


class SystemTailRequest(unittest.TestCase):
    """事故③(1.20.1 风暴形状):role:"system" 尾巴消息不得影响匹配与注入安全判定。"""

    def setUp(self):
        server.store._compressions = []

    def test_system_tail_keeps_match(self):
        msgs = _make_conversation(10)
        entry = _seed_entry(msgs[:15], "sys-tail")
        with_tail = msgs + [{"role": "system", "content": "UserPromptSubmit hook additional context"}]
        best, best_end = server.store.find_match(server._hash_messages(with_tail), with_tail)
        self.assertIs(best, entry)
        self.assertEqual(best_end, 15)

    def test_injection_with_system_tail_is_safe(self):
        injected = [
            {"role": "user", "content": "[ROLLING_CONTEXT_SUMMARY] ..."},
            {"role": "assistant", "content": "I have the full context. Continuing."},
            {"role": "user", "content": "继续干活"},
            {"role": "system", "content": "task reminder"},
        ]
        self.assertTrue(server._injection_is_safe(injected))


class ArchiveReplay(unittest.TestCase):
    """机会性回放:拿本机真实归档校验哈希不变量。归档含真实对话,只在本地读、永不入库。"""

    MAX_FILES = 5

    @classmethod
    def _archives(cls):
        if not os.path.isdir(_REAL_ARCHIVE_DIR):
            return []
        files = sorted(glob.glob(os.path.join(_REAL_ARCHIVE_DIR, "*.json.gz")),
                       key=os.path.getmtime, reverse=True)
        return files[:cls.MAX_FILES]

    def _load_messages(self, path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        req = d.get("request") or d
        return req.get("messages") or []

    def test_hash_invariants_on_real_archives(self):
        files = self._archives()
        if not files:
            self.skipTest("no local archives (CI / fresh machine) — fixture classes cover the shapes")
        for path in files:
            msgs = self._load_messages(path)
            if not msgs:
                continue
            with self.subTest(archive=os.path.basename(path)):
                h1 = server._hash_messages(msgs)
                # 不变量 1:JSON 往返(重序列化)不改变哈希
                h2 = server._hash_messages(json.loads(json.dumps(msgs)))
                self.assertEqual(h1, h2)
                # 不变量 2:剥 thinking(模拟 resume)不改变哈希
                h3 = server._hash_messages(_strip_thinking(msgs))
                self.assertEqual(h1, h3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
