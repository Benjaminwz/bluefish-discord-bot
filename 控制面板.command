#!/bin/bash
# 打開控制面板的備用方法（平常用「應用程式」裡的「藍色大肥魚控制面板」就好）。還沒裝好的話會先跑首次安裝。
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    bash ./setup.sh
    exit
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
nohup .venv/bin/python panel.pyw >/dev/null 2>&1 &
echo "控制面板打開了，這個視窗可以關掉。"
