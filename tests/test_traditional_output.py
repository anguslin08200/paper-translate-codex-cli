"""驗證繁體輸出與模型命令的金鑰隔離，不呼叫任何服務。"""
import sys
from pathlib import Path
from unittest.mock import patch
import unittest
import logging
import os
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from traditional_chinese import to_traditional
import pdf_translate_gui as gui
import protected_translate as protection

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

    def test_spawned_worker_redacts_secret_without_changing_arguments(self):
        # Windows worker 必須安裝相同日誌防護，不能把金鑰重複附加到命令參數。
        captured = {}
        with patch.dict(os.environ, {"PDF_TRANSLATE_DEEPSEEK_API_KEY": "synthetic-test-key"}), \
             patch.object(protection, "__name__", "__mp_main__"), \
             patch.object(logging, "getLogRecordFactory", return_value=logging.LogRecord), \
             patch.object(logging, "setLogRecordFactory", side_effect=lambda f: captured.update(factory=f)), \
             patch.object(sys, "argv", ["protected_translate.py"]):
            protection.inject_api_secret()
            self.assertEqual(sys.argv, ["protected_translate.py"])
        record = captured["factory"]("test", logging.ERROR, "test.py", 1,
                                     "key=%s", ("synthetic-test-key",), None)
        self.assertNotIn("synthetic-test-key", record.getMessage())

    def test_api_and_cache_text_are_normalized_before_typesetting(self):
        from pdf2zh_next.translator.base_translator import BaseTranslator
        # 模擬 API 回應與快取命中，確認兩條輸出路徑都會轉為繁體。
        with patch.object(BaseTranslator, "translate", return_value="软件 {v2}"), \
             patch.object(BaseTranslator, "llm_translate", return_value="数据 {v2}"):
            protection.install_traditional_output()
            self.assertEqual(BaseTranslator.translate(None, "source"), "軟體 {v2}")
            self.assertEqual(BaseTranslator.llm_translate(None, "source"), "資料 {v2}")

if __name__ == "__main__":
    unittest.main()
