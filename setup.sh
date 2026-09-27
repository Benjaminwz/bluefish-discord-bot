#!/bin/bash
# 藍色大肥魚 首次安裝（Mac 版）：由「首次安裝.command」執行，可以重複執行，已經裝好的會自動跳過。
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
VENV_PY="$HERE/.venv/bin/python"
PY_VERSION="3.13"
TOKEN_PATH="$HERE/token.txt"

cyan=$'\033[36m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; gray=$'\033[90m'; reset=$'\033[0m'
say()     { printf '%s%s%s\n' "${2:-}" "$1" "$reset"; }
step()    { echo; say "━━ $1 ━━" "$cyan"; }
skipped() { say "已跳過。$1" "$gray"; }
# ask "問題" y|n：直接按 Enter 就是預設值
ask() {
    local hint="Y/n" answer
    [ "$2" = "n" ] && hint="y/N"
    read -r -p "$1 [$hint] " answer
    answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
    [ -z "$answer" ] && answer="$2"
    [ "$answer" = "y" ] || [ "$answer" = "yes" ]
}
url_ok() { curl -fs --max-time 3 "$1" >/dev/null 2>&1; }
have() { command -v "$1" >/dev/null 2>&1; }

# 從網路下載的資料夾會被 Mac 標成「來自網路」，裡面每個檔案打開時都會跳警告，這裡一次清掉
xattr -dr com.apple.quarantine "$HERE" 2>/dev/null
# 有些解壓縮程式會弄丟「可以執行」的標記，補回去，之後雙擊「控制面板.command」才打得開
chmod +x "$HERE"/*.command "$HERE/setup.sh" 2>/dev/null

clear
say "🐟 藍色大肥魚 首次安裝（Mac 版）" "$cyan"
say "這個程式會幫你把本魚需要的東西裝好。可以重複執行，已經裝好的會自動跳過。" "$gray"
say "每一步都會先問你，直接按 Enter 就是「好」，想自己來的步驟按 n 就能跳過。" "$gray"
say "中途要輸入密碼的話，就是這台 Mac 的登入密碼（打字時畫面不會顯示，打完按 Enter 就好）。" "$gray"

# ---------- 系統需求 ----------
step "系統需求"
say "  基本功能（管理、遊戲、音樂）：macOS 13 以上、記憶體 4 GB、硬碟 3 GB"
say "  AI 聊天（線上 AI）：電腦不用好，有網路就行"
say "  AI 聊天（本機 AI）：建議 M 系列晶片、記憶體 16 GB 以上、硬碟 10 GB"

ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
disk_gb="$(df -g "$HERE" | awk 'NR==2 {print $4}')"
step "你的電腦"
say "  系統：macOS $(sw_vers -productVersion)"
if [[ "$chip" == Apple* ]]; then say "  晶片：$chip" "$green"; else say "  晶片：$chip（跑本機 AI 會很慢）" "$yellow"; fi
if [ "$ram_gb" -ge 16 ]; then say "  記憶體：$ram_gb GB" "$green"; elif [ "$ram_gb" -ge 8 ]; then say "  記憶體：$ram_gb GB" "$yellow"; else say "  記憶體：$ram_gb GB" "$red"; fi
if [ "${disk_gb:-0}" -ge 20 ]; then say "  剩餘空間：$disk_gb GB" "$green"; else say "  剩餘空間：$disk_gb GB" "$yellow"; fi
ai_recommended=false
[[ "$chip" == Apple* ]] && [ "$ram_gb" -ge 16 ] && ai_recommended=true

# ---------- 選功能（答案先記著，裝好 Python 後才寫進設定檔） ----------
step "要用哪些功能？"
say "管理、遊戲、等級、音樂這些基本功能一定會裝。AI 聊天可以選一種「大腦」（之後在控制面板的「AI 來源」分頁隨時可以換）：" "$gray"
say "  1. 免費線上 AI：什麼都不用申請，最簡單。缺點是大家共用，常常要等 10～50 秒"
say "  2. Google Gemini：用 Google 帳號拿一把免費金鑰，又快又穩（推薦，大約多花 1 分鐘）"
say "  3. 本機 AI（Ollama）：免費、對話不會送出去，但要 M 系列晶片和夠大的記憶體，會下載 3～10 GB 的模型"
say "  4. 先不要 AI 聊天"
$ai_recommended || say "（你的 Mac 跑本機 AI 會比較吃力，建議選 1 或 2）" "$yellow"
read -r -p "選 1、2、3 或 4（直接按 Enter = 1） " ai_choice
[[ "$ai_choice" =~ ^[1-4]$ ]] || ai_choice=1

gemini_key=""
if [ "$ai_choice" = "2" ]; then
    echo
    say "拿 Gemini 金鑰的步驟：" "$cyan"
    say "  ① 等一下會打開 Google AI Studio，用 Google 帳號登入"
    say "  ② 按「Create API key」（第一次可能要先同意條款、選或建立一個專案）"
    say "  ③ 按金鑰旁邊的複製按鈕，回到這個視窗按 Command + V 貼上"
    open "https://aistudio.google.com/app/apikey"
    read -r -p "把 Gemini 金鑰貼在這裡（直接按 Enter 就先改用免費線上 AI）： " gemini_key
    gemini_key="$(printf '%s' "$gemini_key" | tr -d '[:space:]')"
    if [ -n "$gemini_key" ] && curl -fs --max-time 60 "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" \
            -H "Authorization: Bearer $gemini_key" -H "Content-Type: application/json" \
            -d '{"model":"gemini-flash-latest","messages":[{"role":"user","content":"Reply with OK"}]}' >/dev/null; then
        say "Gemini 金鑰可以用！" "$green"
    else
        [ -n "$gemini_key" ] && say "金鑰測試失敗。" "$red"
        say "先改用免費線上 AI。之後拿到金鑰，在控制面板「AI 來源」分頁按「貼上並使用」就好。" "$yellow"
        gemini_key=""; ai_choice=1
    fi
fi

want_search=false; want_searxng=false
if [ "$ai_choice" != "4" ]; then
    if ask "要讓 AI 能上網查資料嗎？（問到新聞、天氣這類問題會先上網查，不用另外裝東西）" y; then
        want_search=true
        prev_default=n
        grep -q '"search_engine": *"searxng"' "$HERE/config.json" 2>/dev/null && prev_default=y
        url_ok "http://127.0.0.1:8080/search?q=test&format=json" && prev_default=y
        ask "要改用 Docker + SearXNG 當搜尋引擎嗎？（進階，要裝 Docker，大部分人不需要）" "$prev_default" && want_searxng=true
    fi
fi

# ---------- Homebrew ----------
step "1. Homebrew（Mac 上裝軟體的工具）"
find_brew() {
    local b
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { echo "$b"; return; }; done
    command -v brew 2>/dev/null
}
BREW="$(find_brew)"
if [ -n "$BREW" ]; then
    say "Homebrew 已經裝好了。" "$green"
elif ask "要安裝 Homebrew 嗎？（Python、ffmpeg 這些都靠它裝；會要你輸入 Mac 的登入密碼，第一次要等 5～15 分鐘）" y; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    BREW="$(find_brew)"
    [ -z "$BREW" ] && say "Homebrew 安裝失敗，請到 https://brew.sh 照說明安裝後，再執行一次首次安裝。" "$red"
else
    skipped "沒有 Homebrew 的話，Python、ffmpeg、Deno 都要自己裝。"
fi
[ -n "$BREW" ] && eval "$("$BREW" shellenv)"

# ---------- Python ----------
step "2. Python"
find_python() {
    local c p
    for c in "$VENV_PY" python3.14 python3.13 python3.12 python3.11 python3.10 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
        p="$(command -v "$c" 2>/dev/null)" || continue
        # Mac 內建的 python3 太舊，而且沒裝開發工具時一執行就會跳視窗
        [ "$p" = "/usr/bin/python3" ] && continue
        if [ "$("$p" -c 'import sys, tkinter; tkinter.Tcl(); print(int(sys.version_info >= (3, 10)))' 2>/dev/null)" = "1" ]; then
            echo "$p"; return
        fi
    done
}
PY="$(find_python)"
if [ -z "$PY" ] && [ -n "$BREW" ] && ask "沒有找到能用的 Python，要用 Homebrew 安裝 Python $PY_VERSION 嗎？" y; then
    "$BREW" install "python@$PY_VERSION" "python-tk@$PY_VERSION"
    PY="$(find_python)"
fi
if [ -z "$PY" ]; then
    say "沒有 Python 3.10 以上（要有 tkinter），需要 Python 的步驟會先跳過。" "$red"
    say "可以到 https://www.python.org/downloads/macos/ 下載安裝，裝好後再執行一次首次安裝。" "$yellow"
elif [ "$PY" != "$VENV_PY" ]; then
    say "建立本魚專用的 Python 環境（資料夾裡的 .venv）…"
    if "$PY" -m venv --clear "$HERE/.venv"; then PY="$VENV_PY"; else say "建立失敗，改用 $PY" "$yellow"; fi
fi
[ -n "$PY" ] && say "使用 Python：$("$PY" --version 2>&1)" "$green"

# 寫設定檔：set_config '{"ai_enabled": true}'（跟控制面板存的格式一樣）
set_config() {
    [ -n "$PY" ] || return 1
    "$PY" - "$1" <<'PYEOF'
import json, sys
path = "config.json"
try:
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
for key, value in json.loads(sys.argv[1]).items():
    if isinstance(value, dict) and isinstance(cfg.get(key), dict):
        cfg[key].update(value)
    else:
        cfg[key] = value
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYEOF
}
# 金鑰跟 Discord Token 一樣要保密，存在 ai_keys.json，不放進 config.json
set_ai_key() {
    [ -n "$PY" ] || return 1
    "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
path = "ai_keys.json"
try:
    with open(path, encoding="utf-8-sig") as f:
        keys = json.load(f)
except Exception:
    keys = {}
keys[sys.argv[1]] = sys.argv[2]
with open(path, "w", encoding="utf-8") as f:
    json.dump(keys, f, ensure_ascii=False, indent=2)
PYEOF
}

if [ -n "$PY" ]; then
    case "$ai_choice" in
        1) set_config '{"ai_provider": "free"}' ;;
        2) set_ai_key gemini "$gemini_key"
           set_config '{"ai_provider": "gemini", "online_models": {"gemini": "gemini-flash-latest"}}'
           say "Gemini 設定好了！" "$green" ;;
        3) set_config '{"ai_provider": "ollama"}' ;;
    esac
    engine=builtin; $want_searxng && engine=searxng
    ai_on=true; [ "$ai_choice" = "4" ] && ai_on=false
    set_config "{\"ai_enabled\": $ai_on, \"search_enabled\": $want_search, \"search_engine\": \"$engine\"}"
fi

# ---------- Python 套件 ----------
step "3. Python 套件"
if [ -z "$PY" ]; then skipped "要先有 Python。"
elif ask "要安裝／更新本魚需要的 Python 套件嗎？" y; then
    "$PY" -m pip install -q -U pip
    if "$PY" -m pip install -U -r "$HERE/requirements.txt"; then say "套件裝好了！" "$green"
    else say "有套件安裝失敗，請確認網路後再執行一次。" "$red"; fi
fi

# ---------- ffmpeg ----------
step "4. ffmpeg（播放音樂用）"
if have ffmpeg; then say "ffmpeg 已經裝好了。" "$green"
elif [ -z "$BREW" ]; then skipped "要先有 Homebrew。沒有 ffmpeg 就不能放音樂。"
elif ask "要安裝 ffmpeg 嗎？（要等幾分鐘）" y; then
    "$BREW" install ffmpeg && say "ffmpeg 裝好了！" "$green"
else skipped "沒有 ffmpeg 就不能放音樂。"; fi

# ---------- Deno ----------
step "5. Deno（YouTube 點歌用）"
if have deno || [ -x "$HOME/.deno/bin/deno" ]; then say "Deno 已經裝好了。" "$green"
elif have node; then say "電腦裡已經有 Node.js，YouTube 點歌會用它，不用再裝 Deno。" "$green"
elif [ -z "$BREW" ]; then skipped "要先有 Homebrew。沒有 Deno 的話，有些 YouTube 影片可能會放不了。"
elif ask "要安裝 Deno 嗎？（YouTube 點歌解析影片要用）" y; then
    "$BREW" install deno && say "Deno 裝好了！" "$green"
else skipped "沒有 Deno 的話，有些 YouTube 影片可能會放不了。"; fi

# ---------- Discord Token ----------
step "6. Discord 機器人"
if [ -s "$TOKEN_PATH" ] && [ -n "$(tr -d '[:space:]' < "$TOKEN_PATH")" ]; then say "已經有 Token 了。" "$green"
else
    say "還沒有機器人的話，照這樣做：" "$gray"
    say "  ① 到 Discord Developer Portal 按「New Application」取名字"
    say "  ② 左邊選「Bot」，按「Reset Token」並複製"
    say "  ③ 同一頁往下，把「Message Content Intent」和「Server Members Intent」打開，按 Save（沒開的話本魚一啟動就會關掉！）"
    ask "要幫你打開 Developer Portal 嗎？（已經有機器人的話按 n）" y && open "https://discord.com/developers/applications"
    read -r -p "把 Token 貼在這裡（按 Command + V 貼上；還沒有就直接按 Enter，之後在控制面板的「設定」填）： " token
    token="$(printf '%s' "$token" | tr -d '[:space:]')"
    if [ -n "$token" ]; then printf '%s' "$token" > "$TOKEN_PATH"; chmod 600 "$TOKEN_PATH"; say "Token 存好了！" "$green"; fi
fi

# ---------- 本機 AI ----------
if [ "$ai_choice" = "3" ]; then
    step "7. AI 聊天（Ollama）"
    OLLAMA="$(command -v ollama 2>/dev/null)"
    [ -z "$OLLAMA" ] && [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && OLLAMA=/Applications/Ollama.app/Contents/Resources/ollama
    if [ -n "$OLLAMA" ]; then say "Ollama 已經裝好了。" "$green"
    elif [ -z "$BREW" ]; then skipped "可以自己到 https://ollama.com 下載。"
    elif ask "要安裝 Ollama 嗎？" y; then
        "$BREW" install ollama
        OLLAMA="$(command -v ollama 2>/dev/null)"
    else skipped "可以自己到 https://ollama.com 下載。"; fi
    if [ -n "$OLLAMA" ]; then
        if ! url_ok "http://localhost:11434"; then
            nohup "$OLLAMA" serve >/dev/null 2>&1 &
            for _ in $(seq 20); do url_ok "http://localhost:11434" && break; sleep 1; done
        fi
        # M 系列晶片的顯示卡跟系統共用記憶體，大約三分之二能拿來跑模型
        if [ "$ram_gb" -ge 24 ]; then model="qwen3:14b"; elif [ "$ram_gb" -ge 16 ]; then model="qwen3:8b"; else model="qwen3:4b"; fi
        if ask "要現在下載推薦給你的模型 $model 嗎？（依你的記憶體挑的，之後在控制面板的「本機模型」分頁可以換）" y; then
            "$OLLAMA" pull "$model" && set_config "{\"model_name\": \"$model\"}" && say "模型 $model 裝好了！" "$green"
        fi
    else say "現在沒有 Ollama，裝好之後在控制面板的「本機模型」分頁下載模型就好。" "$yellow"; fi
fi

# ---------- 搜尋 ----------
# 內建搜尋不用裝東西；選了 SearXNG 才要 Docker
if $want_searxng; then
    step "8. 上網查資料（Docker + SearXNG）"
    DOCKER="$(command -v docker 2>/dev/null)"
    [ -z "$DOCKER" ] && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ] && DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
    if [ -z "$DOCKER" ]; then
        if [ -n "$BREW" ] && ask "要安裝 Docker Desktop 嗎？（約 600 MB，會要你輸入 Mac 的登入密碼）" y; then
            "$BREW" install --cask docker
            say "Docker 裝好後，打開「應用程式」裡的 Docker，照畫面同意條款、等它跑起來，再雙擊一次「首次安裝.command」就會自動建立搜尋服務。" "$yellow"
        else
            skipped "可以自己到 https://www.docker.com/products/docker-desktop/ 下載，裝好後再執行一次首次安裝。"
        fi
    elif ! ask "要自動建立搜尋服務（SearXNG）嗎？" y; then
        skipped "已經自己架好 SearXNG 的話，記得 settings.yml 要開 json 格式，並在控制面板「設定」填網址。"
    else
        ready=false
        for i in $(seq 90); do
            "$DOCKER" info >/dev/null 2>&1 && { ready=true; break; }
            [ "$i" = 1 ] && { say "等 Docker 啟動中（最多 90 秒）…"; open -a Docker 2>/dev/null; }
            sleep 1
        done
        if ! $ready; then say "Docker 沒有啟動成功，請打開 Docker 確認狀態後，再執行一次首次安裝。" "$yellow"
        else
            conf="$HERE/searxng"
            mkdir -p "$conf"
            if [ ! -f "$conf/settings.yml" ]; then
                secret="$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 32)"
                printf 'use_default_settings: true\nserver:\n  secret_key: "%s"\n  limiter: false\nsearch:\n  formats:\n    - html\n    - json\n' "$secret" > "$conf/settings.yml"
            fi
            existing="$("$DOCKER" ps -a --filter "name=^/searxng$" --format "{{.Names}}")"
            if [ -n "$existing" ]; then "$DOCKER" start searxng >/dev/null
            else
                say "下載並建立 SearXNG（第一次要下載約 200 MB）…"
                "$DOCKER" run -d --name searxng --restart unless-stopped -p 127.0.0.1:8080:8080 -v "$conf:/etc/searxng" searxng/searxng:latest >/dev/null
            fi
            ok=false
            for _ in $(seq 30); do sleep 2; url_ok "http://127.0.0.1:8080/search?q=test&format=json" && { ok=true; break; }; done
            if $ok; then set_config '{"searxng_url": "http://localhost:8080/search"}'; say "搜尋服務建好了！" "$green"
            elif [ -n "$existing" ]; then say "你之前建的 searxng 容器沒有開 JSON 格式。到 Docker 把它刪掉，再執行一次首次安裝就會重建。" "$yellow"
            else say "搜尋服務還沒回應，可能還在初始化，等一下在控制面板看「上網查資料」燈號。" "$yellow"; fi
        fi
    fi
fi

# ---------- 邀請連結 ----------
step "9. 把本魚加進你的伺服器"
# Bot Token 的第一段就是機器人 ID（Base64），有 Token 就能直接做出邀請連結
app_id=""
if [ -s "$TOKEN_PATH" ] && [ -n "$PY" ]; then
    app_id="$("$PY" -c 'import base64, sys
first = open(sys.argv[1]).read().strip().split(".")[0]
try:
    print(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)).decode("ascii"))
except Exception:
    pass' "$TOKEN_PATH" 2>/dev/null)"
fi
[[ "$app_id" =~ ^[0-9]{17,20}$ ]] || app_id=""
if [ -z "$app_id" ]; then
    read -r -p "沒有 Token 也可以：貼上 Developer Portal「General Information」裡的 Application ID（直接按 Enter 跳過）： " typed
    typed="$(printf '%s' "$typed" | tr -d '[:space:]')"
    [[ "$typed" =~ ^[0-9]{17,20}$ ]] && app_id="$typed"
fi
if [ -z "$app_id" ]; then skipped "之後在控制面板的「伺服器」分頁按「複製邀請連結」也可以。"
else
    # 權限跟機器人裡的 /邀請 一樣：本魚需要的權限，不是整個管理員
    invite="https://discord.com/oauth2/authorize?client_id=$app_id&permissions=564324631702614&scope=bot%20applications.commands"
    printf '%s' "$invite" | pbcopy
    say "邀請連結（已經複製好了，可以直接貼給別人）：" "$green"
    say "  $invite"
    ask "要現在用瀏覽器打開，把本魚加進你的伺服器嗎？" y && open "$invite"
fi

# ---------- 完成 ----------
step "完成！"
if [ -n "$PY" ]; then
    APP="$HOME/Applications/藍色大肥魚控制面板.app"
    if ask "要在「應用程式」資料夾建立「藍色大肥魚控制面板」嗎？（可以放到 Dock，之後都從這裡打開）" y; then
        # App 由控制面板自己建：指向剛剛裝好套件的這個 Python，配上本魚的圖示
        if "$PY" "$HERE/panel.pyw" --create-shortcuts >/dev/null; then
            say "「應用程式」資料夾多了「藍色大肥魚控制面板」，用 Spotlight（Command + 空白鍵）搜尋「藍色大肥魚」也找得到。" "$green"
            say "想放在 Dock：打開後在 Dock 的圖示上按右鍵 →「選項」→「保留在 Dock」。" "$green"
        else say "建立失敗，之後可以雙擊資料夾裡的「控制面板.command」打開，再到「設定」按「建立 App」。" "$yellow"; fi
    fi
    say "接下來：在控制面板按「▶ 啟動」，本魚就會上線～在 Discord 打 /-使用說明 可以看新手教學。" "$green"
    if ask "要現在打開控制面板嗎？" y; then
        if [ -d "$APP" ]; then open "$APP"
        else (cd "$HERE" && nohup "$PY" panel.pyw >/dev/null 2>&1 &); fi
    fi
else
    say "裝好 Python 之後再執行一次首次安裝，就能完成剩下的步驟。" "$yellow"
fi
