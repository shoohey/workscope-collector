"""業界プロファイルローダー.

profiles/*.json を読み込んで、extends継承を解決し、MaskRuleオブジェクトの
リストとwhitelist辞書を持つProfileを返す。

設計方針:
- ハードコードされたDEFAULT_RULESを廃止し、プロファイルJSONから動的生成
- extends継承は再帰解決（base → industry の2段が基本、3段以上もOK）
- 同一nameのルールは子側で上書き（priority順で最終評価）
- whitelistはdictマージ（子側で同keyあれば上書き、リストなら追記マージ）
- プロファイル探索パスは: 環境変数 WORKSCOPE_PROFILE_DIR > sys._MEIPASS/profiles > パッケージ同梱 profiles/
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---- データ構造 -----------------------------------------------------------

@dataclass(frozen=True)
class MaskRule:
    """1個のマスキングルール（プロファイルJSONから生成）."""

    name: str
    pattern: re.Pattern[str]
    category: str
    context_keywords: tuple[str, ...] = ()
    priority: int = 100


@dataclass
class Profile:
    """業界プロファイル."""

    name: str
    rules: list[MaskRule] = field(default_factory=list)
    whitelist: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0"
    description: str = ""
    extends: str | None = None


# ---- 探索パス ------------------------------------------------------------

def _candidate_dirs() -> list[Path]:
    """profiles/ の探索先を優先順で返す."""
    out: list[Path] = []

    # 1. 環境変数（テスト/開発で上書き）
    env = os.environ.get("WORKSCOPE_PROFILE_DIR")
    if env:
        out.append(Path(env))

    # 2. PyInstaller frozen 環境（_MEIPASS/profiles）
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            out.append(Path(meipass) / "profiles")

    # 3. パッケージ同梱（src/ または リポジトリルート）
    here = Path(__file__).resolve().parent
    out.append(here.parent / "profiles")  # repo_root/profiles
    out.append(here / "profiles")          # src/profiles (将来用)

    return out


def _find_profile_file(name: str) -> Path:
    """指定名のプロファイルJSONを探索. 見つからなければ FileNotFoundError."""
    for d in _candidate_dirs():
        p = d / f"{name}.json"
        if p.exists():
            return p
    raise FileNotFoundError(
        f"profile '{name}' not found in: {[str(d) for d in _candidate_dirs()]}"
    )


# ---- ローダ本体 ----------------------------------------------------------

# モジュール内キャッシュ（同一プロファイルの再読込を避ける）
_cache: dict[str, Profile] = {}


def _parse_rule(raw: dict[str, Any]) -> MaskRule:
    """生 dict を MaskRule に変換. パターン compile 失敗で ValueError."""
    name = raw["name"]
    pattern_str = raw["pattern"]
    category = raw["category"]
    context_keywords = tuple(raw.get("context_keywords") or ())
    priority = int(raw.get("priority", 100))
    try:
        compiled = re.compile(pattern_str)
    except re.error as e:
        raise ValueError(
            f"profile rule '{name}' has invalid regex: {pattern_str!r} ({e})"
        ) from e
    return MaskRule(
        name=name,
        pattern=compiled,
        category=category,
        context_keywords=context_keywords,
        priority=priority,
    )


def _merge_whitelist(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """whitelist のマージ. 同key配列は連結+重複排除、それ以外は子で上書き."""
    out = dict(parent)
    for k, v in child.items():
        if k in out and isinstance(out[k], list) and isinstance(v, list):
            merged = list(out[k]) + [x for x in v if x not in out[k]]
            out[k] = merged
        else:
            out[k] = v
    return out


def load_profile(name: str, _seen: tuple[str, ...] = ()) -> Profile:
    """プロファイル名から Profile を解決して返す.

    extends を再帰的に解決する。循環参照は ValueError。
    """
    if name in _cache:
        return _cache[name]

    if name in _seen:
        raise ValueError(f"profile cyclic extends detected: {' -> '.join(_seen + (name,))}")

    path = _find_profile_file(name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"profile '{name}' read failed at {path}: {e}") from e

    parent_name = raw.get("extends")
    if parent_name:
        parent = load_profile(parent_name, _seen=_seen + (name,))
        rules = list(parent.rules)
        whitelist = dict(parent.whitelist)
    else:
        rules = []
        whitelist = {}

    # 子のルールを追加（同 name は上書き）
    by_name = {r.name: r for r in rules}
    for raw_rule in raw.get("rules", []):
        rule = _parse_rule(raw_rule)
        by_name[rule.name] = rule

    # priority 昇順でソート
    sorted_rules = sorted(by_name.values(), key=lambda r: (r.priority, r.name))

    # whitelist マージ
    whitelist = _merge_whitelist(whitelist, raw.get("whitelist") or {})

    profile = Profile(
        name=raw.get("name", name),
        rules=sorted_rules,
        whitelist=whitelist,
        version=raw.get("version", "1.0"),
        description=raw.get("description", ""),
        extends=parent_name,
    )
    _cache[name] = profile
    logger.info(
        "loaded profile=%s rules=%d whitelist_keys=%s",
        profile.name, len(profile.rules), list(profile.whitelist.keys()),
    )
    return profile


def clear_cache() -> None:
    """テスト用: ローダキャッシュを空にする."""
    _cache.clear()


def get_default_profile_name() -> str:
    """デフォルトプロファイル名を決定順で返す.

    1. 環境変数 WORKSCOPE_PROFILE
    2. config.json の industry_profile (config.load_config 経由)
    3. ビルド時埋め込み定数 (config.DEFAULT_PROFILE)
    4. フォールバック: "pharmacy" (v0.1.0互換)
    """
    env = os.environ.get("WORKSCOPE_PROFILE")
    if env:
        return env
    try:
        from config import load_config  # type: ignore[import-not-found]
        cfg = load_config()
        if getattr(cfg, "industry_profile", None):
            return cfg.industry_profile  # type: ignore[no-any-return]
    except Exception:
        pass
    try:
        from config import DEFAULT_PROFILE  # type: ignore[import-not-found]
        if DEFAULT_PROFILE:
            return DEFAULT_PROFILE  # type: ignore[no-any-return]
    except Exception:
        pass
    return "pharmacy"


def list_available_profiles() -> list[str]:
    """探索パス内で利用可能な全プロファイル名を返す."""
    seen: set[str] = set()
    for d in _candidate_dirs():
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            seen.add(p.stem)
    return sorted(seen)


__all__ = [
    "MaskRule",
    "Profile",
    "load_profile",
    "clear_cache",
    "get_default_profile_name",
    "list_available_profiles",
]
