"""以固定清單檢查 Git 追蹤的發布內容，避免誤發布個人檔案。"""
from pathlib import Path
import re
import subprocess
import zipfile
import argparse
import sys

# 新增發布檔案時必須明確更新清單；不使用遞迴複製工作資料夾。
ROOT_FILES = {
    "codex_account.py", "codex_batch.py", "codex_batch_transport.py",
    "codex_translate.py", "pdf_preview.py", "pdf_translate_gui.py",
    "protected_translate.py", "qwen36_translate.py", "traditional_chinese.py",
    "requirements.txt", "README.md", ".gitignore", "Install.ps1",
    "Translate-PDF.ps1", "Start-翻譯GUI.cmd", ".github/workflows/ci.yml",
    "scripts/check_release.py",
}
TEST_FILES = {"test_batch_transport.py", "test_codex_account.py", "test_codex_batch.py",
              "test_codex_translation.py", "test_gui_quality.py",
              "test_translation_quality.py", "test_traditional_output.py"}

def checked_files(root):
    files = subprocess.check_output(["git", "-c", "core.quotepath=false", "ls-files"],
                                    cwd=root, encoding="utf-8").splitlines()
    if not files:
        raise SystemExit("沒有可發布的 Git 追蹤檔案。")
    for name in files:
        if name not in ROOT_FILES and not (name.startswith("tests/") and name[6:] in TEST_FILES):
            raise SystemExit(f"發布清單未允許此檔案：{name}")
        path = root / name
        if path.is_symlink():
            raise SystemExit(f"發布內容不可包含符號連結：{name}")
        text = path.read_text(encoding="utf-8-sig")
        # 只回報檔名，不在 CI 中回顯可能包含機密的內容。
        patterns = (r"[A-Z]:[/\\]Users[/\\](?!Public\b|<|\{)[^/\\\s]+",
                    r"sk-[A-Za-z0-9_-]{20,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
                    r"github_pat_[A-Za-z0-9_]{20,}",
                    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
        if any(re.search(pattern, text) for pattern in patterns):
            raise SystemExit(f"可能包含個人路徑或機密：{name}")
    return files

if __name__ == "__main__":
    # 本機與英文 Windows runner 都使用 UTF-8 顯示繁體診斷。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = checked_files(root)
    if args.archive:
        # ZIP 僅使用已通過檢查的清單，排除本機環境與測試產物。
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(root / name, "paper-translate-codex-cli/" + name)
    print(f"發布檢查通過：{len(files)} 個檔案。")
