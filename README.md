# 論文翻譯／支援 Codex CLI

Windows 本機 PDF 翻譯工具，介面與中文譯文使用臺灣繁體中文。支援 Codex 訂閱額度、DeepSeek API，以及 Ollama 本機模型；提供段落批次並行、進度與高解析度預覽。

## 安裝與啟動

先安裝 Python 3.12（勾選 Python Launcher）、Git；Codex 模式另需 Node.js 與官方 Codex CLI。

```powershell
git clone https://github.com/anguslin08200/paper-translate-codex-cli.git
cd paper-translate-codex-cli
powershell -ExecutionPolicy Bypass -File .\Install.ps1
npm install -g @openai/codex
codex login
.\Start-翻譯GUI.cmd
```

選擇 PDF 與翻譯服務後開始翻譯。中文及中英雙語 PDF 輸出到輸入檔案旁；公式、圖表內的原始文字及英文專有名稱會保留。首次使用可能需下載版面模型與字型。

中文負責連接與解釋，基礎技術詞及完整片語保留原文英文，例如 register、high-dimensional latent state、SSM、continuous-time、discrete-time、discretization、convolution 與 attention；不額外重複中文譯名。既有 PDF 不會自動改寫，新政策使用獨立快取。

- **Codex**：啟動時自動讀取本機 CLI 帳號的可用模型及訂閱額度，也可按「重新整理模型／額度」。額度與其他 Codex 工作共用；讀取失敗顯示未知，不會假裝有額度。建議先用 low、標準檔位；並行可依帳號情況調整。
- **DeepSeek**：在介面輸入自己的 API 金鑰及模型名稱，維持 API 計費。金鑰以 Windows DPAPI 加密存於目前使用者的本機設定，透過子程序環境傳遞，不放在啟動命令或 GitHub。
- **本機模型**：自行安裝 Ollama，執行 `ollama pull <介面選定的模型>`。程式連線到 `127.0.0.1:11434`，不需要 Codex 或 DeepSeek 金鑰。

純命令列範例：

```powershell
.\Translate-PDF.ps1 -InputPdf .\paper.pdf -Provider Codex -Effort low -Parallel 8
```

## CI 與部署

GitHub Actions 在推送及 PR 時執行離線測試、程式檢查與發布內容掃描；通過後提供乾淨的原始碼 ZIP。下載 ZIP 或 clone，再照上面的步驟在 Windows 安裝即可。這是桌面工具，CI 不部署網站，也不登入 Codex、不查你的額度、不呼叫模型或 DeepSeek API，**不需要設定任何 API Secret**。本次不包含 Cloud 部署。

## 資料與隱私

公開儲存庫僅包含程式、離線測試與教學。PDF、譯文、術語表、設定、登入憑證、金鑰、快取、紀錄及虛擬環境都不包含在發布檔案內；請勿使用 `git add -f` 加入這些檔案。

「本機執行」不代表雲端翻譯完全離線：Codex 模式會把待翻譯文字交給 OpenAI，DeepSeek 模式交給 DeepSeek；敏感文件請選擇 Ollama 本機模式。原始 PDF、圖表及公式保護不會因字形轉換而重寫。

PDF 處理依賴 [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) 與 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，請遵循各依賴專案的授權。
