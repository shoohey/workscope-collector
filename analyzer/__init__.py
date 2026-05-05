"""WorkScope analyzer: 収集データから業務フローを検出し、業務マップ＋RPA提案を生成する.

主要モジュール:
- detector: 業務単位グルーピング + N-gram 反復パターン検出
- scorer: 自動化候補スコアリング (頻度×時間×複雑度)
- report_generator: 業務マップHTML納品レポート生成
- rpa_generator: pywinauto/PAD/Selenium/Computer Use テンプレート生成
"""
