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
UPLOAD_API_KEY = ""

# ビルド時の raw_capture_mode 既定値（USB回収顧客向けビルドで True に書き換え）。
# 安全側の既定は False。クラウドアップロード運用に切り替える顧客では絶対に True にしない。
RAW_CAPTURE_MODE_DEFAULT: bool = False


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

    # USB回収前提のデータ取得最優先モード（既定: False = 安全側）。
    # True の場合、OCR が初期化失敗・0件返し・マスク例外・unmaskable 検出
    # のいずれの経路でもスクショを破棄せず保存する。
    # OCR が動いた場合はその結果で黒塗りマスクを適用、動かなかった場合は
    # 生スクショ（マスク無し）を保存する。
    #
    # 有効化する経路は2通り（明示的な opt-in 必須）:
    #   1. ビルド時: scripts/build_for_customer.sh --raw-capture で
    #      RAW_CAPTURE_MODE_DEFAULT=True が _build_constants.py に焼き付けられる。
    #   2. 運用時: %APPDATA%/WorkScope/config.json に "raw_capture_mode": true。
    #
    # PII 漏洩リスクは「USB物理回収＋手元目視検査＋同意書」で担保する前提。
    # クラウド送信運用 (upload_enabled=True) と raw_capture_mode=True の併用は禁止。
    # （load_config() で組み合わせを検出した場合は警告ログ + raw_capture_mode を強制 False に戻す）
    raw_capture_mode: bool = False

    # 業界プロファイル: "pharmacy" / "accounting" / "legal" / "sales" / "hr" / "generic"
    # 空文字の場合は profile_loader.get_default_profile_name() がフォールバック解決
    industry_profile: str = ""

    # クラウドアップロード設定（uploader.py が使用）
    # 空文字なら uploader 起動しない (USB回収モード)
    upload_enabled: bool = False
    upload_interval_hours: float = 24.0
    upload_quiet_hours_only: bool = True  # 営業時間外のみ送信
    upload_max_retry: int = 5
    upload_max_archive_mb: int = 200

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

    # ビルド時定数（_build_constants.py 経由で書き換わる RAW_CAPTURE_MODE_DEFAULT）
    # を既定値として適用。config.json で明示的に上書き可能。
    cfg.raw_capture_mode = bool(RAW_CAPTURE_MODE_DEFAULT)

    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except (OSError, json.JSONDecodeError):
            pass

    # 安全ガード: クラウドアップロード有効と raw_capture_mode=True の併用は禁止。
    # この組み合わせは未マスクスクショがクラウドに送信されるリスクがあるため、
    # 強制的に raw_capture_mode を False に戻す。
    if cfg.upload_enabled and cfg.raw_capture_mode:
        import logging
        logging.getLogger(__name__).error(
            "raw_capture_mode=True is forbidden when upload_enabled=True; "
            "forcing raw_capture_mode=False to prevent unmasked PII from leaving the device"
        )
        cfg.raw_capture_mode = False

    return cfg


def save_config(cfg: CollectorConfig) -> None:
    cfg_path = app_data_dir() / "config.json"
    cfg_path.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
