# システム構成

![CyberWire Discord Digestの構成図](./architecture.svg)

Discordや資料への貼り付けには、[PNG版](./architecture.png)を利用できます。

## 処理の流れ

1. GitHub Actionsが6時間ごと、または手動テストでPythonプログラムを起動します。
2. CyberWire DailyとHacking Humansの公式RSSを取得します。
3. `episodes.json` の最終公開日、エピソード番号、`seen_ids` と比較して新着だけを選びます。
4. 公式Transcriptを優先し、なければRSSの公式音声、音声処理ができなければ公式Show Notesを使います。
5. Gemini APIで初学者向けの日本語解説を生成します。高需要や一時障害の場合は、3.8、3.7、3.6、3.5 Flashの順に切り替えます。
6. Discord Incoming Webhookへ投稿します。Embed投稿が失敗した場合は、1,800文字以下の通常メッセージへ自動的に切り替えます。
7. Discord投稿が成功した場合だけ `episodes.json` を更新し、GitHubへコミットします。

## 外部サービスとAPI

| サービス | 役割 | 接続方法 |
|---|---|---|
| CyberWire / Megaphone | 番組情報、Show Notes、音声の提供 | 公開RSS・HTTPS |
| CyberWire公式サイト | Transcriptの提供 | HTTPS |
| Gemini API | 英語資料・音声から日本語解説を生成 | `google-genai` SDK / HTTPS API |
| Discord | 解説の通知先 | Incoming Webhook |
| GitHub Actions | 定期実行環境 | cron / workflow_dispatch |
| GitHub Repository | コードと処理済み状態の保存 | Checkout / Git commit |

APIキーとWebhook URLはGitHub Secretsから実行時に注入し、ソースコードには保存しません。
