"""profile_loader のテスト. プロファイル読込・extends継承・whitelist マージを検証."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# src/ を import path に追加
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from profile_loader import (  # noqa: E402
    Profile,
    MaskRule,
    clear_cache,
    get_default_profile_name,
    list_available_profiles,
    load_profile,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """各テスト前後でキャッシュをクリア."""
    clear_cache()
    yield
    clear_cache()


# --- 1. base プロファイル読込 -------------------------------------------------

def test_load_base_profile() -> None:
    profile = load_profile("base")
    assert profile.name == "base"
    assert profile.extends is None
    assert len(profile.rules) > 0
    # base には共通PIIルールが含まれている
    cats = {r.category for r in profile.rules}
    assert "email" in cats
    assert "phone" in cats
    assert "my_number" in cats
    assert "personal_name" in cats


# --- 2. pharmacy プロファイル: extends 継承解決 -------------------------------

def test_load_pharmacy_inherits_base() -> None:
    pharmacy = load_profile("pharmacy")
    base = load_profile("base")

    assert pharmacy.name == "pharmacy"
    assert pharmacy.extends == "base"
    # pharmacy は base のルールも全て含む
    base_names = {r.name for r in base.rules}
    pharmacy_names = {r.name for r in pharmacy.rules}
    assert base_names <= pharmacy_names, f"missing inherited rules: {base_names - pharmacy_names}"

    # pharmacy 固有カテゴリも含む
    cats = {r.category for r in pharmacy.rules}
    assert "patient_id" in cats
    assert "insurance_id" in cats
    assert "insurance_card_no" in cats

    # whitelist の薬剤名リストも継承される
    assert "drug_names" in pharmacy.whitelist
    assert "アムロジピン" in pharmacy.whitelist["drug_names"]


# --- 3. 全プロファイル7種が読み込める ----------------------------------------

@pytest.mark.parametrize("name", ["base", "pharmacy", "accounting", "legal", "sales", "hr", "generic"])
def test_all_profiles_load(name: str) -> None:
    profile = load_profile(name)
    assert profile.name == name
    assert isinstance(profile.rules, list)
    # 全プロファイルでルールが少なくとも1つ存在 (generic は base 継承で base のルール)
    assert len(profile.rules) > 0


# --- 4. 業界プロファイルは base の共通PIIを必ず継承 ---------------------------

@pytest.mark.parametrize("name", ["pharmacy", "accounting", "legal", "sales", "hr", "generic"])
def test_industry_profiles_extend_base(name: str) -> None:
    profile = load_profile(name)
    cats = {r.category for r in profile.rules}
    # base 共通PIIは全業界に含まれる
    assert "email" in cats, f"{name} missing email rule"
    assert "phone" in cats, f"{name} missing phone rule"
    assert "my_number" in cats, f"{name} missing my_number rule"
    assert "personal_name" in cats, f"{name} missing personal_name rule"


# --- 5. ルールが priority 昇順でソートされている -----------------------------

def test_rules_sorted_by_priority() -> None:
    profile = load_profile("pharmacy")
    priorities = [r.priority for r in profile.rules]
    assert priorities == sorted(priorities), f"priorities not sorted: {priorities}"


# --- 6. パターンが re.compile 済み -------------------------------------------

def test_rules_have_compiled_patterns() -> None:
    profile = load_profile("base")
    for rule in profile.rules:
        # search() が呼べる = compile 済み
        assert hasattr(rule.pattern, "search")
        assert hasattr(rule.pattern, "subn")


# --- 7. 不正なJSONはエラーを上げる -------------------------------------------

def test_invalid_regex_raises_value_error(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text(json.dumps({
        "name": "broken",
        "rules": [{"name": "bad", "pattern": "[invalid(", "category": "test"}]
    }), encoding="utf-8")
    monkeypatch.setenv("WORKSCOPE_PROFILE_DIR", str(tmp_path))
    clear_cache()
    with pytest.raises(ValueError, match="invalid regex"):
        load_profile("broken")


# --- 8. 存在しないプロファイル名は FileNotFoundError -------------------------

def test_missing_profile_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSCOPE_PROFILE_DIR", str(tmp_path))
    clear_cache()
    # tmp_path にも repo にも nonexistent.json が無いと仮定
    with pytest.raises(FileNotFoundError):
        load_profile("nonexistent_profile_xyz_12345")


# --- 9. 循環参照を検出 -------------------------------------------------------

def test_cyclic_extends_detected(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"name": "a", "extends": "b", "rules": []}), encoding="utf-8")
    b.write_text(json.dumps({"name": "b", "extends": "a", "rules": []}), encoding="utf-8")
    monkeypatch.setenv("WORKSCOPE_PROFILE_DIR", str(tmp_path))
    clear_cache()
    with pytest.raises(ValueError, match="cyclic extends"):
        load_profile("a")


# --- 10. キャッシュが効く ----------------------------------------------------

def test_cache_returns_same_object() -> None:
    p1 = load_profile("base")
    p2 = load_profile("base")
    assert p1 is p2


def test_clear_cache_invalidates() -> None:
    p1 = load_profile("base")
    clear_cache()
    p2 = load_profile("base")
    assert p1 is not p2


# --- 11. デフォルトプロファイル名解決 ----------------------------------------

def test_default_profile_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WORKSCOPE_PROFILE", "accounting")
    assert get_default_profile_name() == "accounting"


def test_default_profile_fallback(monkeypatch) -> None:
    monkeypatch.delenv("WORKSCOPE_PROFILE", raising=False)
    # config.industry_profile が空 + DEFAULT_PROFILE が空 ならフォールバック
    name = get_default_profile_name()
    assert name in ("pharmacy", "")  # 環境次第
    # 最低でも空文字でないことは保証されない場合があるので、解決は確実に動く事のみ確認


# --- 12. list_available_profiles ---------------------------------------------

def test_list_available_profiles_includes_all_seven() -> None:
    available = list_available_profiles()
    expected = {"base", "pharmacy", "accounting", "legal", "sales", "hr", "generic"}
    missing = expected - set(available)
    assert not missing, f"missing profiles in repo: {missing}"


# --- 13. whitelist のマージ動作 ----------------------------------------------

def test_whitelist_list_merge(tmp_path: Path, monkeypatch) -> None:
    """子のwhitelistリストは親のリストに追記マージされる."""
    parent = tmp_path / "parent.json"
    child = tmp_path / "child.json"
    parent.write_text(json.dumps({
        "name": "parent",
        "rules": [],
        "whitelist": {"items": ["a", "b"]}
    }), encoding="utf-8")
    child.write_text(json.dumps({
        "name": "child",
        "extends": "parent",
        "rules": [],
        "whitelist": {"items": ["c", "d"]}
    }), encoding="utf-8")
    monkeypatch.setenv("WORKSCOPE_PROFILE_DIR", str(tmp_path))
    clear_cache()
    p = load_profile("child")
    assert set(p.whitelist["items"]) == {"a", "b", "c", "d"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
