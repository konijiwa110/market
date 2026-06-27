"""Unit tests for StatsCollector JSONL compaction — stdlib only, hermetic (temp file).

Covers the "stats file never grows without bound" fix in stats.py:

  - runtime compaction: after every `max_records` appends, the file is rewritten from the
    in-memory ring buffer, so a long-lived proxy keeps the file at ~2x cap, never unbounded
  - load-time compaction: an already-oversized history file is trimmed on construction, so the
    next _load never has to readlines() a huge file into memory
  - persistence is preserved: records below the cap survive a reopen unchanged

stats.py touches no files on import (STATS_PATH is just a constant), so no HOME redirect needed.

Run:  python -m unittest test_stats      (from this proxy/ dir)
   or  python test_stats.py
"""
import json
import os
import tempfile
import unittest

import stats


class StatsCompaction(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="rc-stats-", suffix=".jsonl")
        os.close(fd)
        os.unlink(self.path)  # start with no file at all

    def tearDown(self):
        for p in (self.path, self.path + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]

    def test_runtime_compaction_caps_file_growth(self):
        cap = 10
        s = stats.StatsCollector(path=self.path, max_records=cap)
        for i in range(5 * cap):
            s.record({"ts": i, "i": i})
        # 文件稳态 ≤ ~2×cap 行,绝不随写入无界增长。
        self.assertLessEqual(len(self._lines()), 2 * cap)
        # 内存只保留尾部 cap 条,最新一条正确。
        self.assertEqual(len(s._records), cap)
        self.assertEqual(s._records[-1]["i"], 5 * cap - 1)

    def test_load_compacts_oversized_history(self):
        cap = 10
        # 预写一个远超上限的历史文件(模拟旧版长期追加攒下的大文件)。
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(7 * cap):
                f.write(json.dumps({"ts": i, "i": i}) + "\n")
        s = stats.StatsCollector(path=self.path, max_records=cap)
        # 构造即压实:文件被截到 ≤ cap 行,且保留的是最新尾部。
        lines = self._lines()
        self.assertLessEqual(len(lines), cap)
        self.assertEqual(json.loads(lines[-1])["i"], 7 * cap - 1)
        self.assertEqual(len(s._records), cap)

    def test_records_below_cap_survive_reopen(self):
        cap = 50
        s = stats.StatsCollector(path=self.path, max_records=cap)
        for i in range(20):
            s.record({"ts": i, "i": i})
        # 重新打开:未达上限不压实,历史完整恢复。
        s2 = stats.StatsCollector(path=self.path, max_records=cap)
        self.assertEqual(len(s2._records), 20)
        self.assertEqual(s2._records[-1]["i"], 19)


if __name__ == "__main__":
    unittest.main(verbosity=2)
