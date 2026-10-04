"""驗證繁體輸出與模型命令的金鑰隔離，不呼叫任何服務。"""
import sys
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from traditional_chinese import to_traditional
import pdf_translate_gui as gui

class TraditionalOutputTests(unittest.TestCase):
    def test_prose_conversion_preserves_protected_content(self):
        # 保護 token 屬性、數學、程式碼與數字，僅將敘述轉為繁體。
        source = '<style title="软件">软件与数据</style> {v3} $变量+1$ `软件` 1.25'
        self.assertEqual(to_traditional(source),
                         '<style title="软件">軟體與資料</style> {v3} $变量+1$ `软件` 1.25')

    def test_api_key_is_absent_from_process_command(self):
        # 合成測試金鑰不應出現在子程序命令、日誌或版本控制內容。
        app = object.__new__(gui.TranslatorGUI)
        app.job = dict(mode="deepseek", model="", effort="none", parallel=1,
                       dual=True, api_key="synthetic-test-key", deepseek_model="deepseek-chat")
        command = app._build_command(Path("paper.pdf"), Path("output"))
        self.assertNotIn("synthetic-test-key", " ".join(command))
        self.assertNotIn("--deepseek-api-key", command)
        self.assertEqual(command[command.index("--lang-out") + 1], "zh-TW")

if __name__ == "__main__":
    unittest.main()
