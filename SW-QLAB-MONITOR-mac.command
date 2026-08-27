#!/bin/bash
# macOS で QLab と同じ Mac 上で動かす場合はこれをダブルクリック
cd "$(dirname "$0")"
python3 sw_qlab_monitor.py --host 127.0.0.1 --console
