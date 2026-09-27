# 藍色大肥魚 首次安裝（由「首次安裝.bat」執行，可以重複執行，已經裝好的會自動跳過）
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$ConfigPath = Join-Path $Here "config.json"
$PyVersion = "3.12.10"

function Say($text, $color = "White") { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ""; Say "━━ $text ━━" Cyan }
function Ask($question, $default) {
    $hint = if ($default) { "Y/n" } else { "y/N" }
    $answer = Read-Host "$question [$hint]"
    if (-not $answer) { return $default }
    return $answer -match '^[yY是好]'
}
function Do-Step($question) {
    # 每一步都可以跳過：熟悉的人直接按 n
    return Ask "$question（n = 跳過這一步）" $true
}
function Skipped($what) { Say "已跳過。$what" DarkGray }
function Download($url, $out) {
    try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -ErrorAction Stop; return $true }
    catch { Say "下載失敗：$($_.Exception.Message)" Red; return $false }
}
function Find-Exe($name, $fallback) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*\WindowsApps\*" } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    if ($fallback -and (Test-Path $fallback)) { return $fallback }
    return $null
}
function Run-Installer($exe, $arguments, [switch]$Admin) {
    # 不用 Start-Process -Wait：PowerShell 5 會連安裝程式打開的 App 一起等，Ollama/Docker 會卡住永遠不結束
    $p = if ($Admin) { Start-Process $exe -ArgumentList $arguments -Verb RunAs -PassThru } else { Start-Process $exe -ArgumentList $arguments -PassThru }
    if ($p) { $p.WaitForExit() }
}
function Test-Url($url) {
    try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null; return $true } catch { return $false }
}
function Set-Config($values) {
    $cfg = @{}
    if (Test-Path $ConfigPath) {
        try { (Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } } catch {}
    }
    foreach ($k in $values.Keys) { $cfg[$k] = $values[$k] }
    $cfg | ConvertTo-Json -Depth 6 | Set-Content $ConfigPath -Encoding UTF8
}

function Find-Python {
    $list = @()
    foreach ($root in "HKCU:\Software\Python\PythonCore", "HKLM:\Software\Python\PythonCore") {
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $p = Get-ItemProperty (Join-Path $_.PSPath "InstallPath") -ErrorAction SilentlyContinue
            if ($p.ExecutablePath) { $list += $p.ExecutablePath }
        }
    }
    $list += (Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    foreach ($exe in $list) {
        if ($exe -like "*\WindowsApps\*" -or -not (Test-Path $exe)) { continue }   # Windows 內建的假 python 只會打開 Microsoft Store
        $ok = & $exe -c "import sys, tkinter; print(int(sys.version_info >= (3, 10)))" 2>$null
        if ($ok -eq "1") { return $exe }
    }
    return $null
}

# ---------- 系統需求 ----------
Clear-Host
Say "🐟 藍色大肥魚 首次安裝" Cyan
Say "這個程式會幫你把本魚需要的東西裝好。可以重複執行，已經裝好的會自動跳過。" Gray
Say "每一步都會先問你，直接按 Enter 就是「好」，熟悉的人想自己來的步驟按 n 就能跳過。" Gray
Step "系統需求"
Say "  基本功能（管理、遊戲、音樂）：Windows 10 / 11 64 位元、記憶體 4 GB、硬碟 1 GB"
Say "  AI 聊天（線上 AI）：電腦不用好，有網路就行"
Say "  AI 聊天（本機 AI）：建議 NVIDIA 顯示卡 8 GB 以上、記憶體 16 GB、硬碟 10 GB（沒有獨立顯卡也能跑，但很慢）"
Say "  上網查資料：不用另外裝東西（進階使用者想改用 Docker + SearXNG 的話，BIOS 要開啟 CPU 虛擬化，額外吃 2～4 GB 記憶體）"

$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$drive = "?"; $diskGB = 0
try { $drive = (Split-Path -Qualifier $Here).TrimEnd(':'); $diskGB = [math]::Round((Get-PSDrive $drive).Free / 1GB) } catch {}
$vramGB = 0; $gpuName = "沒有偵測到 NVIDIA 顯示卡"
try {
    $smi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    if ($smi) { $parts = $smi -split ','; $gpuName = $parts[0].Trim(); $vramGB = [math]::Round([int]$parts[1] / 1024) }
} catch {}
$is64 = [Environment]::Is64BitOperatingSystem
Step "你的電腦"
Say ("  作業系統：{0}（{1}）" -f (Get-CimInstance Win32_OperatingSystem).Caption, $(if ($is64) { "64 位元" } else { "32 位元，不支援" })) $(if ($is64) { "Green" } else { "Red" })
Say "  記憶體：$ramGB GB" $(if ($ramGB -ge 16) { "Green" } elseif ($ramGB -ge 8) { "Yellow" } else { "Red" })
Say "  顯示卡：$gpuName$(if ($vramGB) { "（$vramGB GB）" })" $(if ($vramGB -ge 8) { "Green" } else { "Yellow" })
Say "  $drive 槽剩餘空間：$diskGB GB" $(if ($diskGB -ge 20) { "Green" } else { "Yellow" })
$aiRecommended = ($vramGB -ge 6) -or ($ramGB -ge 16)

function Set-AiKey($provider, $key) {
    # 金鑰跟 Discord Token 一樣要保密，存在 ai_keys.json，不放進 config.json
    $keysPath = Join-Path $Here "ai_keys.json"
    $keys = @{}
    if (Test-Path $keysPath) {
        try { (Get-Content $keysPath -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $keys[$_.Name] = $_.Value } } catch {}
    }
    $keys[$provider] = $key
    $keys | ConvertTo-Json | Set-Content $keysPath -Encoding UTF8
}
function Test-GeminiKey($key) {
    $body = '{"model":"gemini-flash-latest","messages":[{"role":"user","content":"Reply with OK"}]}'
    try {
        Invoke-RestMethod "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" -Method Post `
            -Headers @{ Authorization = "Bearer $key" } -ContentType "application/json" -Body $body -TimeoutSec 60 | Out-Null
        return $true
    } catch { Say "金鑰測試失敗：$($_.Exception.Message)" Red; return $false }
}

# ---------- 選功能 ----------
Step "要用哪些功能？"
Say "管理、遊戲、等級、音樂這些基本功能一定會裝。AI 聊天可以選一種「大腦」（之後在控制面板的「AI 來源」分頁隨時可以換）：" Gray
Say "  1. 免費線上 AI：什麼都不用申請，最簡單。缺點是大家共用，常常要等 10～50 秒"
Say "  2. Google Gemini：用 Google 帳號拿一把免費金鑰，又快又穩（推薦，大約多花 1 分鐘）"
Say "  3. 本機 AI（Ollama）：免費、對話不會送出去，但要好的顯示卡，會下載 3～10 GB 的模型"
Say "  4. 先不要 AI 聊天"
if (-not $aiRecommended) { Say "（你的電腦跑本機 AI 會比較吃力，建議選 1 或 2）" Yellow }
$aiChoice = "$(Read-Host "選 1、2、3 或 4（直接按 Enter = 1）")".Trim()
if ($aiChoice -notmatch '^[1-4]$') { $aiChoice = "1" }

$wantAI = $aiChoice -ne "4"
$wantOllama = $aiChoice -eq "3"
if ($aiChoice -eq "2") {
    Say ""
    Say "拿 Gemini 金鑰的步驟：" Cyan
    Say "  ① 等一下會打開 Google AI Studio，用 Google 帳號登入"
    Say "  ② 按「Create API key」（第一次可能要先同意條款、選或建立一個專案）"
    Say "  ③ 按金鑰旁邊的複製按鈕，回到這個視窗按右鍵貼上"
    Start-Process "https://aistudio.google.com/app/apikey"
    $geminiKey = "$(Read-Host "把 Gemini 金鑰貼在這裡（按右鍵貼上；直接按 Enter 就先改用免費線上 AI）")".Trim()
    if ($geminiKey -and (Test-GeminiKey $geminiKey)) {
        Set-AiKey "gemini" $geminiKey
        Set-Config @{ ai_provider = "gemini"; online_models = @{ gemini = "gemini-flash-latest" } }
        Say "Gemini 設定好了！" Green
    } else {
        Say "先改用免費線上 AI。之後拿到金鑰，在控制面板「AI 來源」分頁按「貼上並使用」就好。" Yellow
        $aiChoice = "1"
    }
}
if ($aiChoice -eq "1") { Set-Config @{ ai_provider = "free" } }
if ($aiChoice -eq "3") { Set-Config @{ ai_provider = "ollama" } }

$wantSearch = $false
$wantSearxng = $false
if ($wantAI) {
    $wantSearch = Ask "要讓 AI 能上網查資料嗎？（問到新聞、天氣這類問題會先上網查，不用另外裝東西）" $true
    if ($wantSearch) {
        # 內建搜尋不用裝任何東西。上次選過 SearXNG（例如裝完 Docker 重開機後再跑一次）或已經架好的人，預設繼續用它
        $prevEngine = $null
        if (Test-Path $ConfigPath) { try { $prevEngine = (Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).search_engine } catch {} }
        $useSearxng = ($prevEngine -eq "searxng") -or (Test-Url "http://127.0.0.1:8080/search?q=test&format=json")
        $wantSearxng = Ask "要改用 Docker + SearXNG 當搜尋引擎嗎？（進階，要裝 Docker，大部分人不需要）" $useSearxng
    }
}
Set-Config @{ ai_enabled = $wantAI; search_enabled = $wantSearch; search_engine = $(if ($wantSearxng) { "searxng" } else { "builtin" }) }

# ---------- Python ----------
Step "1. Python"
$python = Find-Python
if ($python) { Say "已經裝好了：$python" Green }
elseif (-not (Do-Step "沒有找到 Python 3.10 以上的版本，要自動下載安裝 Python $PyVersion 嗎？")) {
    Skipped "請自己安裝 Python 3.10 以上（要勾選 tcl/tk 和 Add to PATH），裝好後再執行一次。需要 Python 的步驟會先跳過。"
}
else {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $installer = Join-Path $env:TEMP "python-$PyVersion-$arch.exe"
    Say "下載 Python $PyVersion（約 25 MB）…"
    if (-not (Download "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-$arch.exe" $installer)) { Read-Host "按 Enter 結束"; exit 1 }
    Say "安裝中，不用點任何東西（約 1～2 分鐘）…"
    Run-Installer $installer "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_tcltk=1 Include_pip=1 Include_test=0"
    Remove-Item $installer -ErrorAction SilentlyContinue
    $python = Find-Python
    if (-not $python) { Say "Python 安裝失敗，請到 https://www.python.org/downloads/ 手動安裝後再執行一次。" Red; Read-Host "按 Enter 結束"; exit 1 }
    Say "Python 安裝完成！" Green
}

# ---------- 套件 ----------
Step "2. Python 套件"
if (-not $python) { Skipped "沒有 Python，沒辦法裝套件。" }
elseif (Do-Step "要安裝／更新本魚需要的 Python 套件嗎？") {
    & $python -m pip install --upgrade pip --disable-pip-version-check -q
    & $python -m pip install -r (Join-Path $Here "requirements.txt") --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Say "套件安裝失敗，請確認網路後再執行一次。" Red }
    else { Say "套件都裝好了！" Green }
} else { Skipped "之後可以在控制面板的「小工具」按「更新元件」。" }

# ---------- ffmpeg ----------
Step "3. ffmpeg（播放音樂用）"
$ffmpeg = Join-Path $Here "ffmpeg.exe"
if (Test-Path $ffmpeg) { Say "已經有了。" Green }
elseif (-not (Do-Step "要自動下載 ffmpeg 嗎？（約 100 MB）")) { Skipped "沒有 ffmpeg 就不能放音樂，也可以自己把 ffmpeg.exe 放進這個資料夾。" }
else {
    $zip = Join-Path $env:TEMP "ffmpeg.zip"
    Say "下載 ffmpeg（約 100 MB）…"
    if (Download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        $entry = $archive.Entries | Where-Object { $_.FullName -like "*/bin/ffmpeg.exe" } | Select-Object -First 1
        if ($entry) { [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $ffmpeg, $true) }
        $archive.Dispose(); Remove-Item $zip
        if (Test-Path $ffmpeg) { Say "ffmpeg 裝好了！" Green } else { Say "解壓縮失敗，之後再執行一次試試看。" Yellow }
    } else { Say "先跳過，沒有 ffmpeg 就不能放音樂，之後再執行一次就好。" Yellow }
}

# ---------- Deno ----------
Step "4. Deno（YouTube 點歌用）"
# YouTube 會出「驗證題」，要用 JavaScript 程式才解得開。電腦裡本來就有 Node.js 或 Bun 也能用，就不用再裝
$denoExe = Join-Path $env:USERPROFILE ".deno\bin\deno.exe"
$jsRuntime = Find-Exe "deno" $denoExe
if (-not $jsRuntime) { $jsRuntime = Find-Exe "node" "C:\Program Files\nodejs\node.exe" }
if (-not $jsRuntime) { $jsRuntime = Find-Exe "bun" (Join-Path $env:USERPROFILE ".bun\bin\bun.exe") }
if ($jsRuntime) { Say "已經有了：$jsRuntime" Green }
elseif (-not (Do-Step "要自動下載 Deno 嗎？（約 45 MB，YouTube 點歌解析影片要用）")) { Skipped "沒有 Deno 的話，有些 YouTube 影片可能會放不了。" }
else {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
    $zip = Join-Path $env:TEMP "deno.zip"
    Say "下載 Deno（約 45 MB）…"
    if (Download "https://github.com/denoland/deno/releases/latest/download/deno-$arch-pc-windows-msvc.zip" $zip) {
        New-Item -ItemType Directory -Force (Split-Path $denoExe) | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        $entry = $archive.Entries | Where-Object { $_.Name -eq "deno.exe" } | Select-Object -First 1
        if ($entry) { [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $denoExe, $true) }
        $archive.Dispose(); Remove-Item $zip
        if (Test-Path $denoExe) { Say "Deno 裝好了！" Green } else { Say "解壓縮失敗，之後再執行一次試試看。" Yellow }
    } else { Say "先跳過，之後再執行一次就好。" Yellow }
}

# ---------- Discord Token ----------
Step "5. Discord 機器人"
$tokenPath = Join-Path $Here "token.txt"
if ((Test-Path $tokenPath) -and "$(Get-Content $tokenPath -Raw)".Trim()) { Say "已經有 Token 了。" Green }
else {
    Say "還沒有機器人的話，照這樣做：" Gray
    Say "  ① 到 Discord Developer Portal 按「New Application」取名字"
    Say "  ② 左邊選「Bot」，按「Reset Token」並複製"
    Say "  ③ 同一頁往下，把「Message Content Intent」和「Server Members Intent」打開，按 Save（沒開的話本魚一啟動就會關掉！）"
    if (Ask "要幫你打開 Developer Portal 嗎？（已經有機器人的話按 n）" $true) { Start-Process "https://discord.com/developers/applications" }
    $token = Read-Host "把 Token 貼在這裡（按右鍵就能貼上，還沒有就直接按 Enter，之後在控制面板的「設定」填）"
    if ("$token".Trim()) { Set-Content $tokenPath "$token".Trim() -NoNewline -Encoding ASCII; Say "Token 存好了！" Green }
}

# ---------- AI ----------
if ($wantOllama) {
    Step "6. AI 聊天（Ollama）"
    $ollama = Find-Exe "ollama" "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if ($ollama) { Say "Ollama 已經裝好了。" Green }
    elseif (-not (Do-Step "要自動下載安裝 Ollama 嗎？")) { Skipped "可以自己到 https://ollama.com 下載。" }
    else {
        $setupExe = Join-Path $env:TEMP "OllamaSetup.exe"
        Say "下載 Ollama（檔案比較大，要等幾分鐘）…"
        if (Download "https://ollama.com/download/OllamaSetup.exe" $setupExe) {
            Say "安裝 Ollama 中…"
            Run-Installer $setupExe "/SILENT"
            Remove-Item $setupExe -ErrorAction SilentlyContinue
            $ollama = Find-Exe "ollama" "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
        }
    }
    if ($ollama) {
        if (-not (Test-Url "http://localhost:11434")) {
            Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
            for ($i = 0; $i -lt 20 -and -not (Test-Url "http://localhost:11434"); $i++) { Start-Sleep 1 }
        }
        $model = if ($vramGB -ge 11) { "qwen3:14b" } elseif ($vramGB -ge 7) { "qwen3:8b" } else { "qwen3:4b" }
        if (Ask "要現在下載推薦給你的模型 $model 嗎？（依你的顯示卡挑的，之後在控制面板的「本機模型」分頁可以換）" $true) {
            & $ollama pull $model
            if ($LASTEXITCODE -eq 0) { Set-Config @{ model_name = $model }; Say "模型 $model 裝好了！" Green }
        }
    } else { Say "現在沒有 Ollama，裝好之後在控制面板的「本機模型」分頁下載模型就好。" Yellow }
}

# ---------- 搜尋 ----------
# 內建搜尋不用裝東西；選了 SearXNG 才要 Docker
if ($wantSearxng) {
    Step "7. 上網查資料（Docker + SearXNG）"
    $docker = Find-Exe "docker" "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if (-not $docker -and -not (Do-Step "要自動下載安裝 Docker Desktop 嗎？（約 600 MB，需要系統管理員權限）")) {
        Skipped "可以自己到 https://www.docker.com/products/docker-desktop/ 下載，裝好後再執行一次首次安裝。"
    }
    elseif (-not $docker) {
        $setupExe = Join-Path $env:TEMP "DockerDesktopInstaller.exe"
        Say "下載 Docker Desktop（約 600 MB）…"
        if (Download "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" $setupExe) {
            Say "安裝 Docker Desktop 中（Windows 會問你要不要允許，請按「是」）…"
            Run-Installer $setupExe "install --quiet --accept-license" -Admin
        }
        Say "Docker 裝好之後通常要重新開機。開機後打開 Docker Desktop，等它跑起來，再雙擊一次「首次安裝.bat」就會自動建立搜尋服務。" Yellow
    } elseif (-not (Do-Step "要自動建立搜尋服務（SearXNG）嗎？")) {
        Skipped "已經自己架好 SearXNG 的話，記得 settings.yml 要開 json 格式，並在控制面板「設定」填網址。"
    } else {
        $ready = $false
        for ($i = 0; $i -lt 90; $i++) {
            & $docker info *> $null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            if ($i -eq 0) { Say "等 Docker 啟動中（最多 90 秒）…"; Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe" -ErrorAction SilentlyContinue }
            Start-Sleep 1
        }
        if (-not $ready) { Say "Docker 沒有啟動成功，請打開 Docker Desktop 確認狀態後，再執行一次首次安裝。" Yellow }
        else {
            $conf = Join-Path $Here "searxng"
            New-Item -ItemType Directory -Force $conf | Out-Null
            $settings = Join-Path $conf "settings.yml"
            if (-not (Test-Path $settings)) {
                $secret = -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
                @("use_default_settings: true", "server:", "  secret_key: `"$secret`"", "  limiter: false", "search:", "  formats:", "    - html", "    - json") |
                    Set-Content $settings -Encoding ASCII
            }
            $existing = & $docker ps -a --filter "name=^/searxng$" --format "{{.Names}}"
            if ($existing) { & $docker start searxng | Out-Null }
            else {
                Say "下載並建立 SearXNG（第一次要下載約 200 MB）…"
                & $docker run -d --name searxng --restart unless-stopped -p 127.0.0.1:8080:8080 -v "${conf}:/etc/searxng" searxng/searxng:latest | Out-Null
            }
            $ok = $false
            for ($i = 0; $i -lt 30 -and -not $ok; $i++) { Start-Sleep 2; $ok = Test-Url "http://127.0.0.1:8080/search?q=test&format=json" }
            if ($ok) { Set-Config @{ searxng_url = "http://localhost:8080/search" }; Say "搜尋服務建好了！" Green }
            elseif ($existing) { Say "你之前建的 searxng 容器沒有開 JSON 格式。到 Docker Desktop 把它刪掉，再執行一次首次安裝就會重建。" Yellow }
            else { Say "搜尋服務還沒回應，可能還在初始化，等一下在控制面板看「SearXNG」燈號。" Yellow }
        }
    }
}

# ---------- 邀請連結 ----------
Step "8. 把本魚加進你的伺服器"
# Bot Token 的第一段就是機器人 ID（Base64），有 Token 就能直接做出邀請連結
$appId = $null
if ((Test-Path $tokenPath) -and ($tok = "$(Get-Content $tokenPath -Raw)".Trim())) {
    try {
        $seg = ($tok.Split('.')[0]).Replace('-', '+').Replace('_', '/')
        $seg += '=' * ((4 - $seg.Length % 4) % 4)
        $decoded = [Text.Encoding]::ASCII.GetString([Convert]::FromBase64String($seg))
        if ($decoded -match '^\d{17,20}$') { $appId = $decoded }
    } catch {}
}
if (-not $appId) {
    $typed = Read-Host "沒有 Token 也可以：貼上 Developer Portal「General Information」裡的 Application ID（直接按 Enter 跳過）"
    if ("$typed".Trim() -match '^\d{17,20}$') { $appId = $typed.Trim() }
}
if (-not $appId) { Skipped "之後在控制面板的「伺服器」分頁按「複製邀請連結」也可以。" }
else {
    # 權限跟機器人裡的 /邀請 一樣：本魚需要的權限，不是整個管理員
    $invite = "https://discord.com/oauth2/authorize?client_id=$appId&permissions=564324631702614&scope=bot%20applications.commands"
    try { Set-Clipboard $invite } catch {}
    Say "邀請連結（已經複製好了，可以直接貼給別人）：" Green
    Say "  $invite"
    if (Ask "要現在用瀏覽器打開，把本魚加進你的伺服器嗎？" $true) { Start-Process $invite }
}

# ---------- 完成 ----------
Step "完成！"
if ($python) {
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { $pythonw = $python }
    $panel = Join-Path $Here "panel.pyw"
    if (Do-Step "要建立「藍色大肥魚控制面板」的捷徑嗎？（開始功能表和桌面各一個，可以釘選到工作列）") {
        # 捷徑由控制面板自己建：直接指向剛剛裝好套件的那個 Python（電腦裡有好幾個 Python 也不會開錯），
        # 配上肥魚圖示，而且能釘選到開始和工作列
        & $python $panel --create-shortcuts | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Say "開始功能表和桌面都多了「藍色大肥魚控制面板」，以後都從這裡打開。" Green
            Say "想放在工作列：在開始功能表搜尋「藍色大肥魚」→ 在圖示上按右鍵 →「釘選到工作列」。" Green
        } else { Say "建立捷徑失敗，之後可以在控制面板的「設定」按「建立捷徑」。" Yellow }
    }
    Say "接下來：在控制面板按「▶ 啟動」，本魚就會上線～在 Discord 打 /-使用說明 可以看新手教學。" Green
    if (Ask "要現在打開控制面板嗎？" $true) { Start-Process $pythonw -ArgumentList "`"$panel`"" -WorkingDirectory $Here }
} else {
    Say "裝好 Python 之後再執行一次首次安裝，就能完成剩下的步驟。" Yellow
}
Start-Sleep 3
