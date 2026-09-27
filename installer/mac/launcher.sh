#!/bin/bash
# 藍色大肥魚.app 的啟動程式（打包時會放在 Contents/MacOS/bluefish）。
# 第一次打開時用 uv 下載本魚專用的 Python（放在自己的資料夾，不影響電腦裡其他的 Python），
# 之後打開安裝精靈或控制面板。程式和資料都放在「應用程式支援」資料夾，換新版 App 時資料不會不見。
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
DATA="$HOME/Library/Application Support/藍色大肥魚"
LOG="$DATA/install-log.txt"
PY="$DATA/.venv/bin/python"
export PATH="$DATA/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_PYTHON_INSTALL_DIR="$DATA/python" UV_PYTHON_PREFERENCE=only-managed UV_NO_CACHE=1
mkdir -p "$DATA/bin" || exit 1

alert() {
    osascript -e "display alert \"藍色大肥魚\" message \"$1\" as critical" >/dev/null 2>&1
}

new_version="$(cat "$RES/app/VERSION")"
if [ "$new_version" != "$(cat "$DATA/VERSION" 2>/dev/null)" ]; then
    # 第一次或換了新版：更新程式檔案。設定、Token、等級、知識庫這些資料不在 App 裡，不會被蓋掉
    cp -R "$RES/app/." "$DATA/"
    rm -f "$DATA/VERSION"   # 套件裝好才寫上版本，中途失敗的話下次會重來
fi
[ -f "$DATA/config.json" ] || cp "$DATA/default_config.json" "$DATA/config.json"

if [ ! -f "$DATA/VERSION" ] || ! "$PY" -c "import tkinter, discord" >/dev/null 2>&1; then
    osascript -e 'display dialog "本魚正在準備需要的程式（第一次大約 1～3 分鐘），好了會自動打開。" with title "藍色大肥魚" buttons {"好"} default button 1 giving up after 900' >/dev/null 2>&1 &
    dialog=$!
    {
        echo "[$(date)] 準備 $new_version"
        if [ ! -x "$DATA/bin/uv" ]; then
            if [ "$(uname -m)" = "arm64" ]; then target=aarch64-apple-darwin; else target=x86_64-apple-darwin; fi
            curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$target.tar.gz" |
                tar -xz -C "$DATA/bin" --strip-components 1
        fi &&
        "$DATA/bin/uv" venv --python 3.13 --seed --allow-existing "$DATA/.venv" &&
        "$DATA/bin/uv" pip install --python "$PY" -U -r "$DATA/requirements.txt"
    } >>"$LOG" 2>&1
    status=$?
    kill "$dialog" 2>/dev/null
    if [ $status -ne 0 ]; then
        alert "準備失敗了，請確認有連上網路，再打開一次本魚。\n\n詳細紀錄：$LOG"
        exit 1
    fi
    echo "$new_version" > "$DATA/VERSION"
fi

cd "$DATA" || exit 1
if grep -q '"setup_done": *true' config.json 2>/dev/null; then
    exec "$PY" panel.pyw
fi
exec "$PY" setup_wizard.py
