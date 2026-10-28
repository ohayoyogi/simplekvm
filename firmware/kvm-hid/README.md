# kvm-hid: RP2350-USB-A / RP2040-Zero を KVM の HID にするファームウェア

Waveshare RP2350-USB-A（または RP2040-Zero）を、KVM ホスト（Tinker Board）からの 1 行コマンドで、操作される側 PC のキーボードとマウスとして動かす。2 種類をビルドする。

| UF2 | ホストとのつなぎ方 | 操作される側 PC へ | 必要なもの |
|---|---|---|---|
| `kvm_hid_uart.uf2` | UART（GP0 = TX, GP1 = RX, 3.3 V）115200 bps | **USB-C**（ネイティブ USB, TinyUSB） | ジャンパ線 3 本 |
| `kvm_hid_cdc.uf2` | **USB-C**（CDC ACM, `/dev/ttyACM*`） | **USB-A**（PIO-USB, GP12/13, Full-Speed） | VBUS を切った A–A ケーブル |

RP2040-Zero 用は `kvm_hid_uart_rp2040_zero.uf2` と `kvm_hid_cdc_rp2040_zero.uf2`（下記「RP2040-Zero」。実機では未確認）。

相手からは次の複合 HID に見える。BIOS/UEFI では相対マウスを使う。
- UART 版: ブートキーボード＋絶対座標マウス＋ブートマウス（相対）の 3 インターフェース（1 ms ごとのポーリング）。
- CDC 版: 同じ 3 インターフェースで、ポーリングは 8 / 4 / 4 ms（PIO-USB はソフトで受信するので、問い合わせを減らしている）。ブートプロトコル（BIOS）が選ばれたら、相対マウスは 3 バイトのブートレポートを送る。入力がない間は、250 ms ごとに今の状態を送り直す（下記）。
  - 相対と絶対を 1 つのインターフェースにまとめると、Linux は 2 つ目のマウスのボタンとホイールを捨てる（同じ用途の重複）ので、分けている。

## 配線

### UART 版
| Tinker Board（40 ピン） | RP2350-USB-A |
|---|---|
| 8 番（UART1 TX, `/dev/ttyS1`） | GP1（UART0 RX） |
| 10 番（UART1 RX） | GP0（UART0 TX） |
| 6 番（GND） | GND |

- 電源線（5V / 3V3）はつながない。RP2350 は相手 PC の USB-C から給電される。
- Tinker のシリアルコンソールは、Armbian の標準では UART2（ttyS2）なので、UART1 と競合しない。コンソールを UART1 に移している場合は競合するので、CDC 版を使うか、別の UART を使う。

### CDC 版
- Tinker の USB-A → RP2350 の USB-C（普通のケーブル。RP2350 は Tinker から給電される）。
- RP2350 の USB-A → 相手 PC の USB-A: **ボードの USB-A はホスト用で 5V を出している。A–A ケーブルは VBUS（1 番ピン）を切ったものを使うこと。** 切らないと Tinker 側と相手 PC 側の 5V がつながってしまう。

### RP2040-Zero
Waveshare RP2040-Zero には USB-A がないので、相手 PC への USB は線をつないで作る。

- UART 版（`kvm_hid_uart_rp2040_zero.uf2`）: 配線は RP2350-USB-A と同じ（GP0 / GP1 / GND）。USB-C が相手 PC への HID。
- CDC 版（`kvm_hid_cdc_rp2040_zero.uf2`）: USB-C → KVM ホスト。相手 PC へは、USB ケーブル（相手側が A オス）を切って次のようにつなぐ。

| USB ケーブル | RP2040-Zero |
|---|---|
| D+（緑） | GP14、**さらに 1.5 kΩ で 3V3 へ** |
| D−（白） | GP15 |
| GND（黒） | GND |
| VBUS（赤） | **つながない**（RP2040-Zero は KVM ホストの USB-C から給電） |

- **D+ のプルアップ（1.5 kΩ、GP14–3V3）は必須。** Pico-PIO-USB のデバイスモードは内蔵していない（RP2350-USB-A はボードに R13 がある）。ないと、相手 PC は何もつながっていないと判断する。
- D+ / D− に直列に 22〜27 Ω を入れるのが本来の形（RP2350-USB-A は 27 Ω）。短い線なら、なくても動くことが多い。
- ピンは `KVM_PIO_USB_DP_PIN`（D− はその次の番号）で変えられる。
- RP2350 版との違い:
  - **受信器は Pico-PIO-USB の元のもの**（エッジ検出器＋NRZI デコーダ）。`usb_dev_rx` は RP2350 の PIO の機能（IN_COUNT）を使うので、RP2040 では動かない。元の受信器はパケットの間に CPU の再開を待つので、続けて届くパケット（ハブの TT が SOF の直後に送る割り込み IN など）を取りこぼしうる。RP2350 で Tinker のハブ経由の折り返しに使ったときは、これでリセットが起きていた。**PC の USB ポートに直結**して使うのがよい。
  - クロックは既定で 120 MHz（RP2040 の定格内）。RP2350 で元の受信器を使っていたときは、120 MHz ではハブ経由でトークンの約 1/3 を読み損ねた。不安定なら `KVM_SYS_KHZ=240000` で 240 MHz にできる（RP2040 では定格を超えるオーバークロック。コア電圧を 1.15 V に上げてから切り替える）。
  - LED（WS2812, GP16）: RP2040 の PIO は 2 つで、どちらも PIO-USB が使う。PIO-USB の準備のあと pio0 に空きがあれば点灯し、なければ点かない。

## ビルド（WSL）
```
bash firmware/kvm-hid/build.sh          # -> firmware/kvm-hid/out/*.uf2（RP2350-USB-A と RP2040-Zero の両方）
KVM_BOARDS=rp2040_zero bash firmware/kvm-hid/build.sh   # 片方だけ
KVM_UART_BAUD=921600 bash firmware/kvm-hid/build.sh   # UART を速くする場合
```
- Docker イメージ `kvm-hid-pico`（arm-none-eabi-gcc）を使う。pico-sdk 2.3.1 と Pico-PIO-USB 0.7.3（`patches/` のパッチを当てる）は `~/kvm-hid-build` に取得する。
- プロトコルとキューのホスト側テスト: `cc -Isrc -o /tmp/t test/test_proto.c src/proto.c src/hid_reports.c && /tmp/t`
- PIO 受信器のシミュレーション（元の受信器の弱点と `usb_dev_rx` の検証。NRZI、ビットスタッフィング、クロックの誤差、立ち上がり時間、同期の遅れを含む）: `python3 test/pio_rx_sim.py [回数]`

## 書き込み
BOOT ボタンを押しながら USB-C を PC につなぐ → `RP2350`（RP2040 なら `RPI-RP2`）ドライブに UF2 をコピーする。

CDC 版が動いていれば、ポートを 1200 bps で開くだけで書き込みモードに入る（Pico SDK の stdio_usb と同じ）。Tinker からなら:
```
stty -F /dev/ttyACM0 1200          # -> RP2350 ドライブ（/dev/sdX1）が現れる
sudo mount /dev/sdX1 /mnt && sudo cp kvm_hid_cdc.uf2 /mnt/ && sync && sudo umount /mnt
```

## Pico-PIO-USB への修正（CDC 版）
`patches/pico-pio-usb-0.7.3-device-fixes.patch`（build.sh が自動で当てる。`PIOUSB_PATCHED=1` で飛ばす）。デバイスモードの次の不具合を直している。
- 64 バイトを超える IN 転送: ACK のあと次のパケットを準備するまでの間、前のパケットを送り直してデータが飛んでいた（84 バイトの設定記述子が読めなかった）。準備ができるまで NAK を返す。
- バスリセットのときに、保留中の「続き」や後回しの記述子の要求を消していなかった。
- **受信器の作り直し（`usb_dev_rx`）**: 元の受信器（エッジ検出器＋NRZI デコーダ）は、パケットの EOP ごとに止まって CPU の再開を待ち、さらに EOP で 1 回余分にトリガーを出すので、CPU がデコーダを戻す必要があった。次のパケットは EOP の約 330 ns 後に始まりうる（ハブの TT は SOF の直後に割り込み IN を送る）ので、間に合わないと壊れた。新しい受信器は、検出と復号を 1 つのステートマシン（96 MHz、32 命令、PIO1）にまとめ、パケットごとに状態を初期化し、EOP で区切りの印 0xffffffff を FIFO に入れる。CPU を待たない。割り込み処理は FIFO からパケット単位で読む。送信中は受信器を止める。RP2350 専用（`mov x, pins` の IN_COUNT を使う）。RP2040 でビルドすると、元の受信器が使われる（`PIO_USB_DEVICE_RX_SM`。ほかの修正は両方に入る）。検証は `test/pio_rx_sim.py`（下記）。
- **バスリセットの見張りが受信を壊していた**: `pio_usb_bus_get_line_state()` は RP2350 で、E9 の不具合への対処として D+/D− の入力を一瞬無効にする。受信器からは偽の SE0 に見える。メインループで休みなく呼ばれていたので、CPU が待機中に届いたパケット（主に SOF）の先頭が壊れていた。デバイスモードでは入力を切り替えずに読む（USB の線は駆動されているので E9 の対処は不要）。
- HID のプロトコル: SET_PROTOCOL の値をインターフェースごとに `pio_usb_device_hid_protocol[]` に記録する（ブートプロトコルなら 3 バイトのレポートを送るため）。GET_PROTOCOL と GET_IDLE に答える（以前は応答せず、ホストがタイムアウトしていた）。

あわせて、CDC 版は **120 MHz**（RP2350 の定格内。PIO-USB は 120 MHz か 240 MHz が前提）で動かし、全体を RAM で動かす（`copy_to_ram`）。元の受信器は CPU が急いで再開させる必要があり、120 MHz ではトークンの約 1/3 を読み損ねたので一時 240 MHz にしていたが、`usb_dev_rx` は CPU を待たないので 120 MHz で足りる（折り返しで 120 秒、エラー 0）。`KVM_SYS_KHZ=240000` で 240 MHz にもできる。

## 動作確認
1. LED の色:
   - 赤: 相手 PC に未接続（UART 版）／ホストのポートが閉じている（CDC 版）
   - 緑: 動作中
   - 青: コマンドを受信した
   - 暗い黄: 相手 PC がサスペンド中
2. ホストから:
   ```
   python3 kvm.py --hid /dev/ttyS1          # UART 版
   python3 kvm.py                           # CDC 版（/dev/serial/by-id/*kvm-hid* を自動検出）
   ```
3. 手で試すとき（UART 版）: `stty -F /dev/ttyS1 115200 raw -echo; printf '?\n' > /dev/ttyS1` → `OK kvm-hid uart 0.1` が返る。`printf 'K 00 04 00 00 00 00 00\nK 00 00 00 00 00 00 00\n' > /dev/ttyS1` で「a」が入力される。

## プロトコル
`src/proto.h` を参照。1 行 1 コマンド（16 進）。
- `K mm k1..k6`: キーボード
- `A bb xxxx yyyy ww`: 絶対座標マウス
- `R bb dx dy ww`: 相対マウス
- `Z`: 全部離す
- `?`: 識別

RP2350 からは `L xx`（キーボード LED）が返る。CDC 版は Pico-PIO-USB が SET_REPORT のデータを捨てるので、LED は返らない。

## 実機で未確認のこと
- UART 版は、ビルドとホスト側テストまで（2026-10-21）。実機では未確認。
- CDC 版を実機で試した（2026-10-24）:
  - USB-C 側の CDC は動いた。1200 bps でポートを開くと書き込みモードに入るので、BOOT ボタンなしで書き換えられる。`?` に `OK kvm-hid cdc 0.1` が返る。
  - USB-A 側（PIO-USB）: 素の 0.7.2 / 120 MHz では、84 バイトの設定記述子が読めず、ほぼ列挙できなかった。上記の修正と 240 MHz で、**列挙でき、キー・絶対座標・相対マウスが届く**ようになった。
  - 元の受信器のままでは、Tinker のハブ（Genesys 05e3:0610、TT 1 個）経由の折り返しで割り込み IN がときどき失敗し（`-71`）、Linux の usbhid がデバイスをリセットした（120 秒で約 20 回）。
  - **受信器の作り直しとバスリセットの見張りの修正のあと（2026-10-25）**: 同じ折り返しで、**120 秒間リセット 0、エラー 0、失敗した転送 0**（3 つの入力デバイスを開き、全エンドポイントが問い合わせられる状態）。SOF は 10 秒で 10200 回すべて正しく復号、PID の誤りも 0。列挙のやり直し 5 回もすべて成功。キー（JIS の ¥ も）、絶対座標（位置・クリック・ホイール）、相対（移動・クリック・ホイール）が届く。
  - 状態の再送（`KEEPALIVE_MS` = 250 ms）: usbhid はエラーのあとデータが届くまで再試行の状態を解かず、入力のない間にエラーが 2 回（1.0〜1.5 秒あけて）起きるとリセットする（`hid_io_error`）。同じ状態を送り直して、その状態を解き、リセット後の復帰も兼ねる。
  - 本来の使い方（PC の USB ポートに直結）と BIOS での動作は未確認。
  - プルアップが常にかかっているので、ファームを書き換えても「抜き差し」にならない。ホストのハブが列挙をあきらめていたら、ポートを無効→有効にする（Linux なら `/sys/bus/usb/devices/<hub>:1.0/<hub>-portN/disable` に 1 → 0）か、ケーブルを抜き差しする。
- LED の色の並び: RP2350-USB-A の LED は、一般的な WS2812（緑・赤・青）ではなく赤・緑・青の順だった（2026-10-28、動作中の緑が赤に見えた）。`KVM_LED_RGB=1`（既定）で赤から送る。RP2040-Zero も同じと言われているが未確認。違ったら `-DKVM_LED_RGB=0`。
- RP2040-Zero（2026-10-28 追加）: 両方の版がビルドできることまで。実機では未確認。RP2040 向けに受信器を切り替えたパッチで作り直した RP2350 の CDC 版は、Tinker のハブ経由の折り返しで、3 つの入力デバイスを開いて 70 秒間リセット 0・エラー 0、キーが届くことを確認した（受信器は RP2350 では元のまま）。
