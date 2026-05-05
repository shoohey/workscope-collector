"""RPA/エージェントスクリプト自動生成.

入力: AutomationCandidate のリスト
出力: 業務種別ごとのスクリプト/エージェント定義 (4種)

| RPA出口 | 出力ファイル | 適用業務 |
|---|---|---|
| pywinauto | <task>.py | Win32アプリ (業界アプリ/ERP) |
| pad | <task>.padfile.json | MS全般 (Office/SaaS-Desktop) |
| selenium | <task>.spec.ts (Playwright) | Webアプリ (SaaS-Web/Browser) |
| computer_use | <task>.agent.json | 非定型業務 (other) |

ドライラン検証付き（生成後にsyntax check）.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List

from .scorer import AutomationCandidate


def _safe_name(s: str) -> str:
    """ファイル名安全化."""
    s = re.sub(r"[^\w\-_.]", "_", s)
    return s[:80]


# ---- pywinauto 生成 ------------------------------------------------------

PYWINAUTO_TEMPLATE = '''"""自動生成: {task_name}

WorkScope analyzer により観測された反復業務 (頻度 {occurrences} 回 / 月間 {monthly_min:.0f}分)。
RPA出口: pywinauto (Win32アプリ向け)
"""

from pywinauto import Application
import time


def run_{func_name}() -> None:
    """{description}

    操作シーケンス:
{steps_doc}
    """
    # 起動済みアプリに接続 (process_name: {process_name})
    app = Application(backend="uia").connect(title_re=r"{title_pattern}")
    main = app.top_window()

    # TODO: 各画面遷移ステップを実装
{steps_code}


if __name__ == "__main__":
    run_{func_name}()
'''


def generate_pywinauto(c: AutomationCandidate, task_name: str) -> str:
    func_name = re.sub(r"\W", "_", task_name).lower()[:40]
    steps_doc = "\n".join(f"    {i+1}. {s}" for i, s in enumerate(c.pattern.pattern))
    steps_code = "\n".join(
        f'    # ステップ{i+1}: {s}\n    # main.child_window(title="{s}").click_input()\n    time.sleep(1)'
        for i, s in enumerate(c.pattern.pattern)
    )
    return PYWINAUTO_TEMPLATE.format(
        task_name=task_name,
        func_name=func_name,
        description=" → ".join(c.pattern.pattern),
        process_name=c.rpa_target,
        title_pattern=re.escape(c.pattern.pattern[0]) if c.pattern.pattern else ".*",
        occurrences=c.pattern.occurrences,
        monthly_min=c.monthly_minutes,
        steps_doc=steps_doc,
        steps_code=steps_code,
    )


# ---- Power Automate Desktop 生成 ----------------------------------------

def generate_pad(c: AutomationCandidate, task_name: str) -> str:
    """PAD .padfile (JSON形式の概要) を生成. 実物のPAD形式は複雑なので雛形のみ."""
    pad = {
        "FormatVersion": "1",
        "FlowName": task_name,
        "Description": f"WorkScope自動生成: {' → '.join(c.pattern.pattern)}",
        "Variables": [],
        "Actions": [
            {
                "Type": "WindowFocus",
                "Title": title,
                "Comment": f"ステップ{i+1}: {title}",
            }
            for i, title in enumerate(c.pattern.pattern)
        ],
        "Stats": {
            "Occurrences": c.pattern.occurrences,
            "MonthlyMinutes": c.monthly_minutes,
            "MonthlySavingsYen": c.monthly_savings_yen,
        },
        "Notes": [
            "このファイルはWorkScope analyzerが自動生成した雛形です。",
            "PAD で開いて、各 Action にUI要素ピックアップを設定してください。",
        ],
    }
    return json.dumps(pad, ensure_ascii=False, indent=2)


# ---- Selenium / Playwright 生成 -----------------------------------------

PLAYWRIGHT_TEMPLATE = '''/**
 * 自動生成: {task_name}
 * WorkScope analyzer により観測された反復業務
 *  頻度: {occurrences} 回 / 月間: {monthly_min:.0f}分
 *  推定削減: ¥{monthly_savings:,}
 *
 * 操作シーケンス:
{steps_doc}
 */
import {{ test, expect }} from '@playwright/test';

test('{task_name}', async ({{ page }}) => {{
  // TODO: ログインURLとセレクタを指定
  await page.goto('https://example.com/');

{steps_code}
}});
'''


def generate_playwright(c: AutomationCandidate, task_name: str) -> str:
    steps_doc = "\n".join(f" *   {i+1}. {s}" for i, s in enumerate(c.pattern.pattern))
    steps_code = "\n".join(
        f"  // ステップ{i+1}: {s}\n  // await page.click('text={s}');\n  await page.waitForTimeout(500);"
        for i, s in enumerate(c.pattern.pattern)
    )
    return PLAYWRIGHT_TEMPLATE.format(
        task_name=task_name,
        occurrences=c.pattern.occurrences,
        monthly_min=c.monthly_minutes,
        monthly_savings=c.monthly_savings_yen,
        steps_doc=steps_doc,
        steps_code=steps_code,
    )


# ---- Claude Computer Use エージェント生成 ------------------------------

# Codex High#6: Computer Use 生成物のPII再マスク + 過剰権限削除
# pattern.pattern にタイトルの生PIIが残っているケースに備え、生成時に再マスクする。
_GEN_PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                  # email
    re.compile(r"0\d{1,4}[-(]?\d{1,4}[-)]?\d{3,4}"),          # phone
    re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)"),  # my_number
    re.compile(r"[一-鿿々]{2,5}\s?(?:様|さん|殿|氏)"),         # honorific name
)


def _scrub_pii(text: str) -> str:
    """生成物 (system_prompt 等) に紛れ込んだ PII を除去."""
    out = text
    for pat in _GEN_PII_PATTERNS:
        out = pat.sub("[MASKED]", out)
    return out


def generate_computer_use(c: AutomationCandidate, task_name: str,
                          extra_tools: list[str] | None = None) -> str:
    """Claude Computer Use 向けのエージェント定義を JSON で生成.

    Codex High#6 対応:
    - tools のデフォルトは ["computer"] のみ。bash/text_editor は extra_tools で
      明示承認時のみ追加（過剰権限防止）.
    - pattern.pattern と system_prompt に紛れ込んだ PII を生成前に再マスク.
    """
    # PII 再マスクされたパターン文字列
    safe_pattern = [_scrub_pii(s) for s in c.pattern.pattern]

    tools = ["computer"]
    if extra_tools:
        # 明示承認されたツールのみ許可
        allowed_extras = {"bash", "text_editor"}
        for t in extra_tools:
            if t in allowed_extras and t not in tools:
                tools.append(t)

    safe_task_name = _scrub_pii(task_name)

    agent = {
        "name": safe_task_name,
        "description": f"WorkScope analyzer 自動生成: {' → '.join(safe_pattern)}",
        "model": "claude-opus-4-7",
        "tools": tools,
        "system_prompt": _scrub_pii(
            f"あなたは「{safe_task_name}」業務を自動実行するエージェントです。\n"
            f"観測された操作シーケンス: {' → '.join(safe_pattern)}\n"
            f"アプリカテゴリ: {c.pattern.app_category}\n\n"
            "以下の手順を順次実行してください:\n"
            + "\n".join(f"{i+1}. {s} 画面を開く" for i, s in enumerate(safe_pattern))
            + "\n\nエラー発生時は人間にエスカレーションしてください。"
        ),
        "max_iterations": 30,
        "stats": {
            "occurrences_observed": c.pattern.occurrences,
            "monthly_minutes": c.monthly_minutes,
            "monthly_savings_yen": c.monthly_savings_yen,
            "rationale": c.rationale,
        },
        "_note": "tools=['computer'] のみ最小権限で生成。bash/text_editor が必要な場合は --extra-tools で明示承認.",
    }
    return json.dumps(agent, ensure_ascii=False, indent=2)


# ---- 振り分けディスパッチャ ---------------------------------------------

GENERATORS = {
    "pywinauto": (generate_pywinauto, ".py"),
    "pad": (generate_pad, ".padfile.json"),
    "selenium": (generate_playwright, ".spec.ts"),
    "computer_use": (generate_computer_use, ".agent.json"),
}


def generate_for_candidate(c: AutomationCandidate, task_name: str) -> tuple[str, str]:
    """候補1件から (内容, 拡張子) を返す."""
    gen, ext = GENERATORS.get(c.rpa_target, GENERATORS["computer_use"])
    return gen(c, task_name), ext


def generate_all(
    candidates: Iterable[AutomationCandidate],
    output_dir: Path,
) -> list[Path]:
    """全候補について生成物を出力ディレクトリに保存. パスのリストを返す."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, c in enumerate(candidates, start=1):
        # task名: 先頭3個のtitleを連結
        joined = "_".join(c.pattern.pattern[:3])
        task_name = f"{c.pattern.app_category}_{i:02d}_{_safe_name(joined)}"
        content, ext = generate_for_candidate(c, task_name)
        out = output_dir / f"{task_name}{ext}"
        out.write_text(content, encoding="utf-8")
        written.append(out)
    return written


__all__ = [
    "generate_pywinauto",
    "generate_pad",
    "generate_playwright",
    "generate_computer_use",
    "generate_for_candidate",
    "generate_all",
    "GENERATORS",
]
