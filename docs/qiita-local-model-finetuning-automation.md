<!--
Qiita title: ローカルモデルのファインチューニングを完全自動化する方法
Suggested tags: LLM, Python, 機械学習, FineTuning, 自動化
-->

# ローカルモデルのファインチューニングを完全自動化する方法

ローカルモデルのファインチューニングでは、学習を一度実行して終わり、ということはほとんどありません。

学習、マージ、変換、評価、結果の判定、再学習。途中で一時的な失敗が起きればログを読み、どこで止まったかを調べ、再開する必要があります。モデルの品質が足りないのか、評価ハーネスが落ちたのか、単にファイル転送が失敗したのかも区別しなければなりません。

この記事では、この流れを「上限付き・スコアゲート付き」の自動化シーケンスにする考え方と、Windows 向けの OSS ツール [`training-candidate-automation`](https://github.com/VolcaneGD/training-candidate-automation) を紹介します。

> 先に重要な注意点です。このツールは**学習候補を自動化する**ためのものです。モデルの有効化、manifest の変更、本番配布、モデル削除は意図的に自動化しません。評価結果を人が確認してから行うべき変更を、学習ループに混ぜないためです。

## 完全自動化で必要になる状態遷移

「シェルスクリプトで学習コマンドを呼ぶ」だけでは、再現性のある自動化には足りません。最低限、次の状態を扱う必要があります。

```text
候補 N を開始
  ↓
学習 → マージ → 変換 → 評価
  ↓
スコアが全件合格？
  ├─ はい: COMPLETE
  └─ いいえ: データ修復/再準備 → 候補 N+1
                                      ↓
                              上限到達なら STOPPED
```

ここでのポイントは3つです。

1. **候補数に上限を設ける** — クラウド課金や GPU 時間を無制限に消費しない。
2. **スコアは機械可読な JSON で判定する** — ログの文言を grep して成功扱いにしない。
3. **モデル品質と基盤障害を分ける** — 壊れた評価レポート、ハーネス障害、パーサ障害を「追加学習が必要」という根拠にしない。

## `training-candidate-automation` とは

`training-candidate-automation` は、学習フレームワークやモデル提供元に依存しない Python 製の小さな自動化ランナーです。

- コマンド配列を指定順に実行
- 各工程のログと状態を保存
- JSON の `passed` / `cases`（または dotted key）で合格判定
- 未達なら任意の `repair_commands` を実行し、次の候補へ進行
- 一時的な工程失敗は、工程ごとの回数・待機時間で自動再試行
- Windows の単一モニターで進捗、経過時間、LIVE LOG を表示
- Codex Skills を同梱し、設定作成と監督手順を再利用可能にする

MIT ライセンスで公開しています。

```powershell
git clone https://github.com/VolcaneGD/training-candidate-automation.git
cd training-candidate-automation
python -m pip install -e .
```

標準ライブラリ中心の構成なので、ランナーとモニター自体に追加の Python 依存はありません。

## 設定は「文字列のシェル」ではなくコマンド配列にする

設定ファイルは JSON です。重要なのは `command` を一つのシェル文字列ではなく、引数配列として書くことです。

```json
{
  "max_candidates": 2,
  "release_template": "my-model-v{candidate}",
  "workdir": "C:\\path\\to\\project",
  "stages": [
    {
      "name": "train",
      "command": ["python", "train.py", "--output", "artifacts/{release_name}"]
    },
    {
      "name": "evaluate",
      "command": [
        "python", "evaluate.py",
        "--model", "artifacts/{release_name}",
        "--output", "scores/{release_name}.json"
      ],
      "allow_nonzero": true
    }
  ],
  "score_reports": [
    {
      "path": "scores/{release_name}.json",
      "passed_key": "passed",
      "cases_key": "cases"
    }
  ],
  "repair_commands": [
    ["python", "refresh_training_data.py"]
  ]
}
```

`allow_nonzero` は、評価プログラムが「未達」を非ゼロ終了コードで表す一方で、JSON レポートは正しく書き出す場合だけに使います。学習や変換の失敗まで成功扱いにするためのオプションではありません。

## スコアレポートをゲートにする

評価スクリプトは、少なくとも次のような JSON を出力します。

```json
{
  "passed": 18,
  "cases": 20
}
```

`passed == cases` のときだけ候補は合格です。未達なら `repair_commands` を実行して次の候補へ進みます。

評価がクラッシュした、JSON が壊れている、必須キーがない、といった場合は合格にも未達にも分類せず、停止して原因を残します。この区別がないと、評価基盤の不具合に対して不要な追加学習を繰り返すことになります。

## Windows モニターで「止まっていない」ことを見える化する

長時間学習では、無反応に見える時間が不安になります。付属モニターは1秒ごとに更新し、工程、候補番号、経過時間、成果物サイズ、LIVE LOG を表示します。

| 状態 | 表示 | 意味 |
| --- | --- | --- |
| 実行中 | `RUNNING`（明滅） | 工程が実行中 |
| 再試行中 | `RETRYING`（黄） | エラー表示後、待機または再試行中 |
| 停止 | `STOPPED`（赤） | 安全上の理由で自動実行を停止 |
| 完了 | `COMPLETE`（緑） | 全スコアレポートが合格 |

Windows では、次のランチャーを使うと、学習ループを非表示で起動し、同じ実行を監視するモニターを一つだけ開けます。

```powershell
& .\scripts\launch_candidate_loop.ps1 `
  -ConfigPath C:\runs\candidate-config.json `
  -RunDir C:\runs\candidate-automation `
  -Title 'My model candidates'
```

成功時は結果を LIVE LOG に表示し、5秒後にモニターを閉じます。失敗工程はログにエラーを残してから、設定された `retry_attempts` と `retry_delay_seconds` に従って再試行します。

さらに、明示的に指定した Windows Scheduled Task を `-RecoveryTask` として渡せば、モニターが停止状態を検知したときに復旧要求を送れます。デフォルトでは何も起動しません。復旧タスクは、必ず同じ上限付き設定を再開する自分のタスクだけを指定してください。

## Codex Skills も同梱する理由

自動化を運用すると、失敗したときに AI エージェントが「とりあえず再学習」と判断してしまう危険があります。そこで同梱の Skills は次の境界を明確にします。

- `automated-training-candidates`: 上限付き設定の作成、起動、結果の分類
- `training-job-monitor`: 長時間工程のモニター起動と状態確認

どちらも、モデルの有効化・削除・本番反映をループの外に置く設計です。自動化の目的は「人の判断をなくす」ことではなく、「毎回同じ安全な手順を確実に実行する」ことだと考えています。

## 実運用でのチェックリスト

最後に、導入時のチェックリストです。

- [ ] `max_candidates` が正の整数で設定されている
- [ ] 各工程がコマンド配列で定義されている
- [ ] 評価が整数のスコア JSON を出力する
- [ ] `allow_nonzero` は評価の未達表現に限定している
- [ ] 学習成果物、ログ、トークン、`.env` を Git に含めていない
- [ ] モデルの有効化は独立した検証の後に行う
- [ ] 停止時は `monitor_state.json`、工程ログ、スコア JSON を確認する

公開リポジトリにはサンプル設定、テスト、配布用 wheel 設定も含めています。自分の学習フレームワークに合わせて `examples/score-gated-local.json` から始めてみてください。

<details><summary>参考リンク</summary>

- [training-candidate-automation](https://github.com/VolcaneGD/training-candidate-automation)
- [Qiita: 記事を投稿する](https://help.qiita.com/ja/articles/qiita-post)
- [Qiita Markdown](https://help.qiita.com/ja/articles/qiita-markdown)

</details>
