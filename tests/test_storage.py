"""storage / window_titles / collector のロジックを検証するユニットテスト.

外部 API (Win32 / mss / Pillow) に依存しないコードパスのみを対象とする。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


# src/ を import パスに通す
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _isolate_appdata(tmp: Path) -> None:
    """config.app_data_dir() がテスト用ディレクトリを指すよう APPDATA を差し替える."""
    os.environ["APPDATA"] = str(tmp)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        _isolate_appdata(Path(self._tmp.name))
        # config モジュールはモジュールレベルで APPDATA を読まないので、
        # 関数呼び出し時点で env を見るため差し替えで OK
        # storage を新規インポート / リロード
        if "storage" in sys.modules:
            del sys.modules["storage"]
        if "config" in sys.modules:
            del sys.modules["config"]
        global storage, config  # type: ignore
        import config  # type: ignore  # noqa: F401
        import storage  # type: ignore  # noqa: F401
        self.storage = storage
        self.config = config

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sample_event(self, seq: int = 1) -> dict:
        return {
            "session_id": "test-session",
            "event_seq": seq,
            "ts": "2026-05-04T10:23:45.123+09:00",
            "event_type": "window_focus",
            "app": {
                "process_name": "Receipt.exe",
                "process_path": "C:\\Program Files\\Receipt\\Receipt.exe",
                "pid": 1234,
            },
            "window": {
                "title": "[マスク済み]",
                "title_raw_hash": "abcd1234abcd1234",
                "hwnd": 65432,
                "rect": [0, 0, 1920, 1080],
                "monitor": 1,
            },
            "dwell_ms_prev": 8420,
            "screenshot": {
                "filename": "2026-05-04T10-23-45-123_abc12345.jpg",
                "width": 1920,
                "height": 1080,
                "ocr_text_summary": "処方入力 患者ID:[MASKED]",
                "ocr_token_count": 5,
                "mask_applied_count": 1,
                "mask_categories": ["patient_id"],
            },
            "transition_from_app": "Explorer.EXE",
        }

    def test_event_store_append_and_read(self) -> None:
        store = self.storage.EventStore()
        ev = self._sample_event(1)
        path = store.append(ev)
        self.assertTrue(path.exists())
        self.assertTrue(str(path).endswith("2026-05-04.jsonl"))
        store.close()

        # 読み戻し
        store2 = self.storage.EventStore()
        rows = store2.read_day("2026-05-04")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "test-session")
        self.assertEqual(rows[0]["screenshot"]["mask_categories"], ["patient_id"])

    def test_event_store_multiple_days_routing(self) -> None:
        store = self.storage.EventStore()
        ev_a = self._sample_event(1)
        ev_b = self._sample_event(2)
        ev_b["ts"] = "2026-05-05T01:00:00.000+09:00"
        store.append(ev_a)
        store.append(ev_b)
        store.close()
        rows_a = store.read_day("2026-05-04")
        rows_b = store.read_day("2026-05-05")
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(len(rows_b), 1)

    def test_event_store_jsonl_format(self) -> None:
        store = self.storage.EventStore()
        store.append(self._sample_event(1))
        store.append(self._sample_event(2))
        store.close()
        p = self.config.events_dir() / "2026-05-04.jsonl"
        lines = p.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)
        for line in lines:
            obj = json.loads(line)
            # 必須フィールドが揃っているか
            self.assertIn("session_id", obj)
            self.assertIn("event_seq", obj)
            self.assertIn("ts", obj)
            self.assertIn("event_type", obj)
            self.assertIn("app", obj)
            self.assertIn("window", obj)
            self.assertIn("dwell_ms_prev", obj)
            self.assertIn("screenshot", obj)
            self.assertIn("transition_from_app", obj)

    def test_cleanup_old_data(self) -> None:
        # 古いファイルを作る
        old_event = self.config.events_dir() / "2024-01-01.jsonl"
        old_event.write_text("{}\n", encoding="utf-8")
        old_shot = self.config.screenshots_dir() / "2024-01-01T00-00-00-000_deadbeef.jpg"
        old_shot.write_bytes(b"fake")
        # mtime を 200 日前に
        past = time.time() - 200 * 86400
        os.utime(old_event, (past, past))
        os.utime(old_shot, (past, past))
        # 新しいファイル
        new_event = self.config.events_dir() / "2026-05-04.jsonl"
        new_event.write_text("{}\n", encoding="utf-8")

        cfg = self.config.CollectorConfig(keep_screenshots_days=30, keep_events_days=90)
        result = self.storage.cleanup_old_data(cfg)
        self.assertEqual(result["screenshots"], 1)
        self.assertEqual(result["events"], 1)
        self.assertFalse(old_event.exists())
        self.assertFalse(old_shot.exists())
        self.assertTrue(new_event.exists())

    def test_get_stats(self) -> None:
        store = self.storage.EventStore()
        ev = self._sample_event(1)
        # 今日の日付に上書き
        today_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + ".000+09:00"
        ev["ts"] = today_iso
        store.append(ev)
        store.close()

        stats = self.storage.get_stats(self.storage.EventStore())
        self.assertEqual(stats["today_event_count"], 1)
        self.assertEqual(len(stats["recent"]), 1)
        self.assertGreaterEqual(stats["bytes_total"], 0)


class WindowTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        if "window_titles" not in sys.modules:
            import window_titles  # type: ignore
        from window_titles import (  # type: ignore
            mask_window_title,
            is_blocklisted,
        )
        self.mask = mask_window_title
        self.blocked = is_blocklisted

    def test_mask_person_name(self) -> None:
        masked, h = self.mask("処方箋 - 田中太郎様 のレセプト")
        self.assertIn("[MASKED]", masked)
        self.assertNotIn("田中太郎", masked)
        self.assertEqual(len(h), 16)

    def test_mask_long_digits(self) -> None:
        masked, _ = self.mask("患者ID 123456789 入力中")
        self.assertIn("[MASKED]", masked)
        self.assertNotIn("123456789", masked)

    def test_mask_birthdate(self) -> None:
        masked, _ = self.mask("生年月日 1980-04-01 検索")
        self.assertIn("[MASKED]", masked)
        masked2, _ = self.mask("昭和55年4月1日 患者")
        self.assertIn("[MASKED]", masked2)

    def test_mask_empty(self) -> None:
        masked, h = self.mask("")
        self.assertEqual(masked, "")
        self.assertEqual(h, "")

    def test_blocklist_process(self) -> None:
        self.assertTrue(self.blocked(
            "vault", "1Password.exe",
            ["1password"], [],
        ))

    def test_blocklist_title(self) -> None:
        self.assertTrue(self.blocked(
            "ログイン情報の確認", "Notepad.exe",
            [], ["ログイン情報"],
        ))

    def test_blocklist_negative(self) -> None:
        self.assertFalse(self.blocked(
            "Receipt 入力", "Receipt.exe",
            ["1password"], ["password"],
        ))


class CollectorFilterTests(unittest.TestCase):
    """Collector のフィルタロジック (quiet_hours / blocklist / min_dwell)."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        _isolate_appdata(Path(self._tmp.name))
        self._collectors: list = []
        for m in ("storage", "collector", "config", "window_titles"):
            sys.modules.pop(m, None)
        import collector  # type: ignore
        import config  # type: ignore
        self.collector_mod = collector
        self.config = config

    def tearDown(self) -> None:
        for c in self._collectors:
            try:
                c.stop()
            except Exception:
                pass
        self._tmp.cleanup()

    def _make(self, **cfg_kwargs):
        cfg = self.config.CollectorConfig(**cfg_kwargs)
        c = self.collector_mod.Collector(cfg=cfg)
        if not hasattr(self, "_collectors"):
            self._collectors = []
        self._collectors.append(c)
        return c

    def _info(self, hwnd=1, title="Receipt 入力", proc="Receipt.exe"):
        return self.collector_mod.WindowInfo(
            hwnd=hwnd,
            title=title,
            process_name=proc,
            process_path=f"C:\\{proc}",
            pid=999,
            rect=(0, 0, 1920, 1080),
            monitor=1,
        )

    def test_quiet_hours_skip(self) -> None:
        from collector import _in_quiet_hours  # type: ignore

        cfg = self.config.CollectorConfig(quiet_hours_start=22, quiet_hours_end=6)
        # 23:00 は skip 範囲内
        self.assertTrue(_in_quiet_hours(cfg, datetime(2026, 5, 4, 23, 0)))
        # 5:00 も範囲内
        self.assertTrue(_in_quiet_hours(cfg, datetime(2026, 5, 4, 5, 0)))
        # 12:00 は範囲外
        self.assertFalse(_in_quiet_hours(cfg, datetime(2026, 5, 4, 12, 0)))
        # 範囲指定なしなら常に False
        cfg2 = self.config.CollectorConfig()
        self.assertFalse(_in_quiet_hours(cfg2, datetime(2026, 5, 4, 23, 0)))

    def test_blocklist_skips_capture(self) -> None:
        c = self._make(blocklist_processes=["1password"])
        info = self._info(hwnd=1, proc="1Password.exe", title="vault")
        # 初回は前回フォーカスがないので dwell=0 でフィルタ通過 → blocklist で弾かれる想定
        # ただし initial seq は 0 のまま
        result = c.process(info)
        self.assertIsNone(result)
        self.assertEqual(c._seq, 0)

    def test_min_dwell_seconds_skips_quick_switch(self) -> None:
        c = self._make(min_dwell_seconds_for_capture=2.0)
        info_a = self._info(hwnd=1, proc="A.exe", title="A")
        info_b = self._info(hwnd=2, proc="B.exe", title="B")
        # A をフォーカス → 直後 B に切替（dwell < 2s）
        c.process(info_a)  # 初回は dwell=0 で書かれる可能性
        # ここで A の処理時にイベントが書かれる場合あり、それは仕様通り
        time.sleep(0.05)
        result_b = c.process(info_b)
        # 0.05秒しか滞在していないので min_dwell でスキップされ event は出ない
        self.assertIsNone(result_b)

    def test_pause_flag_skips(self) -> None:
        c = self._make()
        # PAUSED ファイルを作る
        from config import pause_flag_file  # type: ignore
        pause_flag_file().write_text("paused", encoding="utf-8")
        info = self._info(hwnd=10)
        result = c.process(info)
        self.assertIsNone(result)

    def test_event_emitted_when_filters_pass(self) -> None:
        # mss/PIL なしでもメタデータのみのイベントが書かれるはず
        c = self._make(min_dwell_seconds_for_capture=0.0, max_capture_per_minute=60)
        info_a = self._info(hwnd=1, proc="A.exe", title="A")
        info_b = self._info(hwnd=2, proc="B.exe", title="田中太郎様 処方")
        c.process(info_a)
        # 少し滞在
        c._focus.focus_since -= 1.0  # 1 秒前にフォーカスしたことに
        result = c.process(info_b)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["event_type"], "window_focus")
        # タイトルがマスクされていること
        self.assertIn("[MASKED]", result["window"]["title"])
        self.assertNotIn("田中太郎", result["window"]["title"])
        self.assertEqual(result["transition_from_app"], "A.exe")
        self.assertGreaterEqual(result["dwell_ms_prev"], 1000)


if __name__ == "__main__":
    unittest.main()
