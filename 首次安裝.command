#!/bin/bash
# 首次安裝：雙擊就會打開「終端機」，把本魚需要的東西裝好。可以重複執行，已經裝好的會自動跳過。
cd "$(dirname "$0")" || exit 1
bash ./setup.sh
echo
read -r -n 1 -s -p "按任意鍵關閉…"
echo
