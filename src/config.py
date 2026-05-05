"""WorkScope Collector configuration.

All settings are centralized here. Pharmacy-specific tuning happens via
%APPDATA%\\WorkScope\\config.json (overrides defaults below).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_NAME = "WorkScope"
APP_VERSION = "0.1.0"

# ビルド時埋め込み（顧客別ビルドで scripts/build_for_customer.sh が書き換える）
# 空文字の場合はデフォルト "pharmacy" にフォールバック（v0.1.0互換）
DEFAULT_PROFILE = ""
CUSTOMER_NAME = ""
UPLOAD_ENDPOINT = ""


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_root() -> Path:
    p = app_data_dir() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = app_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def screenshots_dir() -> Path:
    p = data_root() / "screenshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_dir() -> Path:
    p = data_root() / "events"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_file() -> Path:
    return app_data_dir() / "state.json"


def pause_flag_file() -> Path:
    return app_data_dir() / "PAUSED"


@dataclass
class CollectorConfig:
    # Trigger behavior
    trigger_on_window_change: bool = True
    min_dwell_seconds_for_capture: float = 0.8  # ignore quick alt-tabs
    max_capture_per_minute: int = 30  # safety cap

    # Screenshot
    capture_active_monitor_only: bool = True
    jpeg_quality: int = 70  # masked image quality

    # OCR / masking
    ocr_languages: list[str] = field(default_factory=lambda: ["japan", "en"])
    ocr_max_image_side: int = 1920  # downscale before OCR for speed
    mask_strict_mode: bool = True  # err on side of masking when in doubt
    drop_image_if_unmaskable: bool = True

    # 業界プロファイル: "pharmacy" / "accounting" / "legal" / "sales" / "hr" / "generic"
    # 空文字の場合は profile_loader.get_default_profile_name() がフォールバック解決
    industry_profile: str = ""

    # Storage
    keep_screenshots_days: int = 30
    keep_events_days: int = 90

    # Quiet hours (collector still runs but skips capture; e.g., night)
    quiet_hours_start: int | None = None  # 22 = 22:00
    quiet_hours_end: int | None = None    # 6  = 06:00

    # Apps to never capture (case-insensitive substring match on process name or title)
    blocklist_processes: list[str] = field(default_factory=lambda: [
        "1password", "keepass", "bitwarden",
    ])
    blocklist_title_substrings: list[str] = field(default_factory=lambda: [
        "パスワード", "password", "ログイン情報",
    ])

    # Apps to always capture even if otherwise filtered (helpful for receipt computer)
    allowlist_processes: list[str] = field(default_factory=list)


def load_config() -> CollectorConfig:
    cfg_path = app_data_dir() / "config.json"
    cfg = CollectorConfig()
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_config(cfg: CollectorConfig) -> None:
    cfg_path = app_data_dir() / "config.json"
    cfg_path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
