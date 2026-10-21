# kvm-hid: RP2350-USB-A を KVM の HID にするファームウェア

Waveshare RP2350-USB-A を、KVM ホスト（Tinker Board）からの 1 行コマンドで、操作される側 PC のキーボードとマウスとして動かす。2 種類をビルドする。

| UF2 | ホストとのつなぎ方 | 操作される側 PC へ | 必要なもの |
|---|---|---|---|
| `kvm_hid_uart.uf2` | UART（GP0 = TX, GP1 = RX, 3.3 V）115200 bps | **USB-C**（ネイティブ USB, TinyUSB） | ジャンパ線 3 本 |
| `kvm_hid_cdc.uf2` | **USB-C**（CDC ACM, `/dev/ttyACM*`） | **USB-A**（PIO-USB, GP12/13, Full-Speed） | VBUS を切った A–A ケーブル |

どちらも、相手からは「ブートキーボード＋絶対座標マウス＋ブートマウス（相対）」の複合 HID に見える。BIOS/UEFI では相対マウスを使う。

## 配線

### UART 版
| Tinker Board（40 ピン） | RP2350-USB-A |
|---|---|
| 8 番（UART1 TX, `/dev/ttyS1`） | GP1（UART0 RX） |
| 10 番（UART1 RX） | GP0（UART0 TX） |
| 6 番（GND） | GND |

- 電源線（5V / 3V3）はつながない。RP2350 は相手 PC の USB-C から給電される。
- Tinker のシリアルコンソールは UART2（ttyS2）なので、UART1 と競合しない。

### CDC 版
- Tinker の USB-A → RP2350 の USB-C（普通のケーブル。RP2350 は Tinker から給電される）。
- RP2350 の USB-A → 相手 PC の USB-A: **ボードの USB-A はホスト用で 5V を出している。A–A ケーブルは VBUS（1 番ピン）を切ったものを使うこと。** 切らないと Tinker 側と相手 PC 側の 5V がつながってしまう。

## ビルド（WSL）
```
bash firmware/kvm-hid/build.sh          # -> firmware/kvm-hid/out/*.uf2
KVM_UART_BAUD=921600 bash firmware/kvm-hid/build.sh   # UART を速くする場合
```
- Docker イメージ `kvm-hid-pico`（arm-none-eabi-gcc）を使う。pico-sdk 2.3.1 と Pico-PIO-USB 0.7.2 は `~/kvm-hid-build` に取得する。
- プロトコルとキューのホスト側テスト: `cc -Isrc -o /tmp/t test/test_proto.c src/proto.c src/hid_reports.c && /tmp/t`

## 書き込み
BOOT ボタンを押しながら USB-C を PC につなぐ → `RP2350` ドライブに UF2 をコピーする。

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
- 両方とも、ビルドとホスト側テストまで（2026-10-21）。実機での列挙、BIOS での動作、PIO-USB の安定性は未確認。
- WS2812 の色の並び（GRB を仮定）。
