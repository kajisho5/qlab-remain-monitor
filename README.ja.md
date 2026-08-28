# SW QLAB MONITOR v1.1.0

![QLab Monitor](docs/qlab-screenshot.png)

QLab（4 / 5）を LAN 経由で監視する読み取り専用モニター。
再生中のキュー・REMAIN（残り）・キューリスト全体をブラウザに表示する。
SW PIXERA MONITOR の QLab 版で、画面構成と操作は同じ。

---

## 1. QLab 側の設定（1回だけ）

QLab 5:
1. ワークスペース設定（右下の歯車）→ **Network** → **OSC Access** タブ
2. **Allow OSC Connections** にチェック
3. **No Passcode** の行の 3つのチェックボックス（view / edit / control）を有効に
   （view だけでも本ツールは動く。パスコードを使う場合は設定ウィンドウに入力）
4. **OSC Listening Port** が **53000** であることを確認

QLab 4: 設定 → **OSC** → **Use OSC Controls** にチェック。

※ 本ツールは取得系メッセージしか送らない（GO/STOP は一切送らない）。
※ 同じ Mac 上で動かす場合は IP に `127.0.0.1` を指定。

## 2. 起動

**Windows**: `SW-QLAB-MONITOR.bat` をダブルクリック → 設定ウィンドウ
- `QLab の IP` に Mac の IP（カンマ区切りで複数台可）
- IP が分からなければ **Bonjourで検索** ボタン → 同一LAN上のQLabをワークスペース名で
  自動検出し、選ぶとIP/ポートが入る（公式のQLab Remoteアプリと同じ `_qlab._tcp.local.` の
  仕組みを使用。追加ライブラリ不要）
- パスコードを設定している場合のみ `パスコード` 欄に入力
- **接続してモニター起動** でブラウザにモニター画面

**macOS**（QLab と同じ Mac で動かす）: `SW-QLAB-MONITOR-mac.command` をダブルクリック。
初回は右クリック → 開く で実行を許可。

実機なしの確認は `SW-QLAB-MONITOR-DEMO.bat`(ダミー QLab 内蔵)。

繋がらないときは `SW-QLAB-TEST.bat` に `192.168.0.30` を入れると、
TCP が届くか・QLab が応答するか・ワークスペース名まで出る。

コマンドラインなら:

```
python sw_qlab_monitor.py --host 192.168.0.30 --console
python sw_qlab_monitor.py --host 192.168.0.30 --passcode 1234 --console
python sw_qlab_monitor.py --demo
python sw_qlab_monitor.py --discover   # Bonjourで検索して一覧表示
```

モニター画面は `http://localhost:8780`。
同一 LAN の別端末（タブレット等）からは `http://<このPCのIP>:8780/`。

## 3. 画面

| 表示 | 内容 |
|---|---|
| REMAIN | 再生中キューの残り時間（残60秒で琥珀／10秒で赤）。複数走っている場合は最も長いもの |
| ELAPSED | 経過時間 |
| NOW | 再生中のキュー番号と名前＋素材ファイル名 |
| NEXT (PLAYHEAD) | 次に GO で出るキュー（プレイヘッド位置） |
| END AT | 終了予定の実時刻 |
| STATE | PLAYING / PAUSED / STANDBY |
| NOW PLAYING | 走っているキューを全部カード表示。種別バッジ・キュー番号・キュー名・**素材ファイル名**・残り/経過/尺・進捗バー。QLabで付けたキューカラーが左端と バッジに反映される。残60秒で琥珀・10秒で赤、PAUSED は琥珀枠 |
| 下部ストリップ | キューリスト全体。尺に比例した幅で、再生中は明るく＋進捗バー、プレイヘッドは白枠 |

- `F` キー：全画面フォーカスモード（数字だけ）／`Esc` で解除

## 4. Companion / Stream Deck に REMAIN を出す

設定ウィンドウの **Companion** 欄に Companion の PC の IP（既定ポート 8000）。
Companion 側で **Custom Variables** に同名の変数を先に作っておくこと。

| 変数名 | 内容 |
|---|---|
| `qlab_remain` / `qlab_elapsed` | 残り / 経過 |
| `qlab_state` | PLAYING / PAUSED / STANDBY / NO LINK |
| `qlab_cue` / `qlab_cue_number` | 再生中のキュー |
| `qlab_next` | プレイヘッドのキュー |
| `qlab_running_count` | 走っているキュー数 |
| `qlab_end_at` | 終了予定の実時刻 |
| `qlab_workspace` | ワークスペース名 |
| `qlab_connected` / `qlab_devices` | OK・NG / 接続台数 |

ボタンのテキストに `$(custom:qlab_remain)` と書けば Stream Deck に残り時間が出る。
複数台監視のときは `qlab1_remain` `qlab2_remain` … に分かれる。

## 5. 仕組み

- QLab OSC API（OSC 1.1 / TCP / SLIP フレーム RFC1055 / 既定ポート 53000）
- 接続時に `/version` → `/workspaces` → `/workspace/{id}/connect` → `/alwaysReply 1`
- 30Hz で `runningOrPausedCues/shallow` と各キューの `actionElapsed` `currentDuration`
  `isPaused`、プレイヘッドを取得（`--rate` で 1-60Hz に変更可）
- 5秒ごとに `cueLists`（Groupキューの中身を含む完全版）でキューリスト構造をスキャンし、
  Groupキュー（入れ子含む）は展開してキュー一覧に表示する
- SSE でブラウザに push、ブラウザ側は 60fps で自走しサーバ値へ滑らかに追従

## 6. 既知の制限

- QLab の OSC API には映像・音声の実データが無い（波形やスコープは取得できない）
- キューのサムネイルを返す API も無いため、PIXERA 版のような絵は出ない
  （代わりに素材ファイル名・キュー種別・キューカラーで識別する）
- 素材ファイル名は `fileTarget` を再生開始時に1回だけ取得してキャッシュする
- 複数キューが同時に走っている場合、REMAIN は最も残りが長いものを表示する
  （全部は NOW PLAYING のカードで確認できる）
- ストリップは最初のキューリストのみ表示

---

MIT License / Copyright (c) 2026 SEVENTHWELL
