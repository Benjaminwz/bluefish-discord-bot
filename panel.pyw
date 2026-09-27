import os
import sys
import ast
import json
import time
import importlib
import importlib.util
import queue
import socket
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

import ctypes
import shlex
import plistlib

# 同一份面板在 Windows 和 Mac 都能用，只有捷徑、開機啟動、找程式這些地方不一樣
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""
SETUP_NAME = "首次安裝.bat" if IS_WINDOWS else "首次安裝.command"
# Mac 用 Homebrew 裝的程式（ffmpeg、deno、ollama）放在這兩個地方
UNIX_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
if not IS_WINDOWS:
    # 從 Finder 或 Dock 打開時 PATH 很短，補上 Homebrew 的位置才找得到程式（開機器人時也會傳下去）
    _path = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join([d for d in UNIX_BIN_DIRS if d not in _path] + _path)

# 工作列用這個名字認人：開始功能表的捷徑和開著的面板用同一個，釘選在工作列的肥魚圖示
# 打開面板後才會合在同一格，不會另外多出一個 Python 圖示
APP_ID = "BlueFish.ControlPanel"

# 宣告支援高解析度螢幕，要在開任何視窗之前做。沒宣告的話，在 125%～175% 縮放的螢幕上
# Windows 會把整個視窗硬拉大，字會糊掉
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

# 螢幕縮放比例（100% = 1.0，175% = 1.75），在 App 建立後算出來。
# 字型大小用「點」會自動跟著縮放，但間距、欄寬這些像素值要自己乘上它
UI_SCALE = 1.0


def px(*values):
    scaled = tuple(round(v * UI_SCALE) for v in values)
    return scaled[0] if len(scaled) == 1 else scaled

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(BASE_DIR, "deepseek_discord_bot.py")
PANEL_SCRIPT = os.path.join(BASE_DIR, "panel.pyw")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.txt")
REQUIREMENTS_PATH = os.path.join(BASE_DIR, "requirements.txt")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def shell_folder(csidl, fallback):
    """問 Windows 特殊資料夾（桌面、開始功能表…）真正的位置，桌面被 OneDrive 搬走時也找得到。"""
    buf = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return fallback


APPDATA_PROGRAMS = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
START_MENU_DIR = shell_folder(0x02, APPDATA_PROGRAMS)                          # CSIDL_PROGRAMS
STARTUP_DIR = shell_folder(0x07, os.path.join(APPDATA_PROGRAMS, "Startup"))    # CSIDL_STARTUP
SHORTCUT_NAME = "藍色大肥魚控制面板.lnk"
STARTUP_LAUNCHER = os.path.join(STARTUP_DIR, SHORTCUT_NAME)
OLD_STARTUP_BAT = os.path.join(STARTUP_DIR, "BlueFishPanel.bat")   # 舊版用 .bat 開機啟動，開機時會閃一下黑色視窗
ICON_PATH = os.path.join(BASE_DIR, "bluefish.ico")
AVATAR_PATH = os.path.join(BASE_DIR, "avatar.png")  # 左上角的圓形大頭貼
COVER_PATH = os.path.join(BASE_DIR, "cover.png")   # 側邊欄的封面（透明底的本魚）
DOCK_ICON_PATH = os.path.join(BASE_DIR, "bluefish.png")   # Mac 的 Dock 圖示
ICNS_PATH = os.path.join(BASE_DIR, "bluefish.icns")       # Mac 的 App 圖示
MAC_APP_PATH = os.path.join(os.path.expanduser("~"), "Applications", "藍色大肥魚控制面板.app")
MAC_LAUNCH_AGENT = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.bluefish.panel.plist")
PANEL_LOCK_PORT = 47832   # 控制面板自己的「只開一個」鎖；機器人用的是 47831
MAX_LOG_LINES = 3000
INSTANCE_LOCK_PORT = 47831
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "知識庫")

# 知識庫的讀檔和搜尋跟機器人共用同一個 knowledge.py
sys.path.insert(0, BASE_DIR)
import knowledge  # noqa: E402

DEFAULT_CONFIG = {
    "model_name": "deepseek-r1:14b",
    "max_history_turns": 6,
    "searxng_url": "http://localhost:8080/search",
    "docker_desktop_path": r"C:\Program Files\Docker\Docker\Docker Desktop.exe" if IS_WINDOWS else "/Applications/Docker.app",
    "persona": "",
    "autostart_bot": False,
    "enable_thinking": True,
    "ai_enabled": True,
}
GRACEFUL_STOP_TIMEOUT = 12

COLOR_ON = "#22a35a"
COLOR_WAIT = "#e0a100"
COLOR_OFF = "#a0a7b4"
COLOR_BAD = "#e04848"

# 介面配色：海洋藍
FONT = "Microsoft JhengHei UI" if IS_WINDOWS else "PingFang TC"
SHORTCUT_BUTTON = "🐟 建立捷徑（開始功能表和桌面）" if IS_WINDOWS else "🐟 建立 App（應用程式資料夾）"
MONO_FONT = "Consolas" if IS_WINDOWS else "Menlo"
BG = "#f4f6fc"
CARD = "#ffffff"
TEXT = "#1f2a3d"
MUTED = "#6b7688"
BORDER = "#dde2f1"
ACCENT = "#3558d4"
ACCENT_DARK = "#2542ad"
ACCENT_LIGHT = "#b9c7f4"
DANGER = "#d64545"
FIELD_DISABLED = "#f2f4fa"
ROW_STRIPE = "#f6f8fe"
LOG_BG = "#f7f9fc"  # 執行紀錄用淺色底，看起來不會像工程師的黑色終端機
# 左邊導覽列：配色跟封面的本魚一樣（深藍洋裝、寶藍頭髮）
SIDEBAR_BG = "#1a2656"
SIDEBAR_HOVER = "#25336c"
SIDEBAR_ACTIVE = "#2f3f84"
SIDEBAR_BAR = "#8fb2ff"
SIDEBAR_TEXT = "#c1cbee"
SIDEBAR_MUTED = "#8793c4"
# 右上角機器人狀態膠囊：(底色, 文字色)
PILL_TONES = {"on": ("#e1f5e8", "#16794a"), "wait": ("#fff3d6", "#946200"), "off": ("#e8ecf2", "#5b6678")}
GUILDS_SNAPSHOT_PATH = os.path.join(BASE_DIR, "guilds.json")
# 用 127.0.0.1 不用 localhost：Windows 會先試 IPv6，Ollama 只聽 IPv4，每次都要被拒絕重試 2 秒
OLLAMA_URL = "http://127.0.0.1:11434"
STATUS_INTERVAL_MS = 4000
DOCKER_CHECK_SECONDS = 30   # docker info 要開一個新程序，最多 30 秒查一次
AI_KEYS_PATH = os.path.join(BASE_DIR, "ai_keys.json")
# 跟機器人的 AI_PROVIDERS 對應。代碼: (顯示名稱, 格式, API 網址, 建議模型, 申請金鑰的網址, 說明)
AI_PROVIDERS = {
    "ollama": ("本機 Ollama", "ollama", "", "", "",
               "免費，模型跑在你自己的電腦上，對話不會送出去。要好的顯示卡（8 GB 以上），模型在「本機模型」分頁下載。"),
    "free": ("免費線上 AI（不用金鑰）", "openai", "https://text.pollinations.ai/openai", "openai-fast", "",
             "完全不用申請，選了就能用。缺點是大家共用，常常要等 10～50 秒，人多時偶爾會沒回應。對話會送到 Pollinations。"),
    "gemini": ("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest",
               "https://aistudio.google.com/app/apikey",
               "有免費額度，小伺服器通常用不完，又快又穩。用 Google 帳號就能拿到金鑰。"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat",
                 "https://platform.deepseek.com/api_keys", "中文很好又非常便宜，要先在官網儲值（最低幾十塊台幣）。"),
    "openai": ("OpenAI（ChatGPT）", "openai", "https://api.openai.com/v1", "gpt-5-mini",
               "https://platform.openai.com/api-keys", "穩定、品質好，要綁信用卡依用量付費。"),
    "claude": ("Anthropic Claude", "anthropic", "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001",
               "https://console.anthropic.com/settings/keys", "對話自然、中文好，要先儲值依用量付費。"),
    "openrouter": ("OpenRouter", "openai", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat",
                   "https://openrouter.ai/settings/keys", "一把金鑰就能用上百種模型，有些模型免費（名稱結尾是 :free）。"),
    "groq": ("Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
             "https://console.groq.com/keys", "速度超快，有免費額度，但每分鐘能問的次數比較少。"),
    "custom": ("自訂（OpenAI 相容）", "openai", "", "", "",
               "給進階使用者：任何支援 OpenAI 格式的服務（例如 LM Studio、xAI），自己填 API 網址。"),
}
# 模型清單裡跟聊天無關的（畫圖、語音、向量…）不要列出來
NON_CHAT_MODEL_WORDS = ("embed", "tts", "whisper", "dall-e", "imagen", "image", "veo", "audio", "moderation",
                        "transcribe", "realtime", "search", "aqa", "lyria")


def load_ai_keys():
    try:
        with open(AI_KEYS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ai_keys(keys):
    temp_path = AI_KEYS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in keys.items() if v}, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, AI_KEYS_PATH)


def ai_http(url, headers=None, body=None, timeout=60):
    """面板用 urllib 打線上 AI（不依賴 requests）。回傳 (HTTP 狀態碼, 解析後的 JSON 或原始文字)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "User-Agent": "BlueFishPanel",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", errors="replace")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def ai_error_text(status, data):
    detail = data
    if isinstance(data, dict):
        err = data.get("error")
        detail = err.get("message") if isinstance(err, dict) else err or data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        detail = (data[0].get("error") or {}).get("message") or data[0]
    detail = str(detail)
    if "<html" in detail.lower() or "<!doctype" in detail.lower():
        detail = "伺服器回了一個錯誤網頁"
    if status in (401, 403) or "api key" in detail.lower():
        return "金鑰不對或沒有權限，請確認有完整複製（前後不要有空白）。"
    if status == 402 or "balance" in detail.lower() or "insufficient" in detail.lower():
        return "帳戶額度用完了，要到官網儲值。"
    if status == 429:
        return "問太快或免費額度用完了，等一下再試。"
    if status >= 500:
        return f"服務暫時故障（HTTP {status}），等一下再試。"
    return f"HTTP {status}：{detail[:200]}"


def ai_auth_headers(style, key):
    if style == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def fetch_online_models(provider, base, key):
    style = AI_PROVIDERS[provider][1]
    if provider == "free":
        status, data = ai_http("https://text.pollinations.ai/models", timeout=20)
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(ai_error_text(status, data))
        return [m.get("name") for m in data if isinstance(m, dict) and m.get("name")]
    status, data = ai_http(f"{base}/models", headers=ai_auth_headers(style, key), timeout=20)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(ai_error_text(status, data))
    ids = []
    for m in data.get("data") or data.get("models") or []:
        model_id = str(m.get("id") or m.get("name") or "")
        model_id = model_id[len("models/"):] if model_id.startswith("models/") else model_id  # Gemini 會加 models/ 開頭
        if model_id and not any(w in model_id.lower() for w in NON_CHAT_MODEL_WORDS):
            ids.append(model_id)
    return sorted(set(ids))


def test_online_ai(provider, base, key, model):
    """問一句很短的話，確認金鑰、網址、模型都對。回傳模型的回答。"""
    style = AI_PROVIDERS[provider][1]
    messages = [{"role": "user", "content": "請只回覆「OK」兩個字。"}]
    if style == "anthropic":
        status, data = ai_http(f"{base}/messages", headers=ai_auth_headers(style, key),
                               body={"model": model, "max_tokens": 50, "messages": messages}, timeout=60)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(ai_error_text(status, data))
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    url = base if provider == "free" else f"{base}/chat/completions"
    status, data = ai_http(url, headers=ai_auth_headers(style, key),
                           body={"model": model, "messages": messages}, timeout=90)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(ai_error_text(status, data))
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "（有回應，但內容是空的）"


# 跟機器人的 CHANNEL_FEATURES 對應，可以在每個頻道個別關掉的功能
CHANNEL_FEATURES = (
    ("ai", "AI 聊天"), ("music", "點歌"), ("games", "小遊戲"),
    ("levels", "聊天經驗值"), ("auto_reply", "自動回覆"), ("sports", "運動比分"),
    ("knowledge", "知識庫"),
)


def console_python():
    folder, name = os.path.split(sys.executable)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def windowed_python():
    folder = os.path.dirname(sys.executable)
    candidate = os.path.join(folder, "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def load_config():
    data = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    return data


def save_config(data):
    temp_path = CONFIG_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, CONFIG_PATH)


def read_token():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_token(token):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(token.strip())


def read_default_persona():
    try:
        with open(BOT_SCRIPT, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "PERSONA" for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    except Exception:
        pass
    return ""


def http_alive(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def base_url(url):
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc
    if netloc.lower() == "localhost" or netloc.lower().startswith("localhost:"):
        netloc = "127.0.0.1" + netloc[len("localhost"):]
    return f"{parts.scheme}://{netloc}"


def find_program(name, fallback):
    """剛裝好的程式在重開機前常常不在 PATH 裡，找不到就去預設安裝位置找。"""
    return shutil.which(name) or (fallback if os.path.exists(fallback) else name)


def ollama_exe():
    if not IS_WINDOWS:
        return find_program("ollama", "/Applications/Ollama.app/Contents/Resources/ollama")
    return find_program("ollama", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"))


def docker_exe():
    if not IS_WINDOWS:
        return find_program("docker", "/Applications/Docker.app/Contents/Resources/bin/docker")
    return find_program("docker", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")


def open_path(path):
    """用檔案總管（Mac 是 Finder）打開資料夾，或用預設的程式打開檔案。"""
    if IS_WINDOWS:
        os.startfile(path)
    else:
        subprocess.Popen(["open", path])


def docker_ready():
    try:
        result = subprocess.run(
            [docker_exe(), "info"], capture_output=True, timeout=6, creationflags=CREATE_NO_WINDOW
        )
        return result.returncode == 0
    except Exception:
        return False


# 跟機器人的 SEARCH_ENGINES 對應：上網查資料的方式
SEARCH_ENGINES = {"builtin": "內建搜尋（免安裝，推薦）", "searxng": "SearXNG（要 Docker，進階）"}


def configured_search_engine(cfg):
    """跟機器人的 load_search_engine 一樣：舊設定檔沒有這一項時，有開上網查資料的人就是在用 SearXNG。"""
    engine = cfg.get("search_engine")
    if engine in SEARCH_ENGINES:
        return engine
    return "searxng" if cfg.get("search_enabled", True) else "builtin"


def package_installed(name):
    importlib.invalidate_caches()  # 剛用「更新元件」裝好的也要找得到
    return importlib.util.find_spec(name) is not None


def js_runtime_name():
    """YouTube 點歌要用哪個 JavaScript 執行環境解析，跟機器人的 find_js_runtimes 一樣的找法和順序。"""
    home = os.path.expanduser("~")
    candidates = (
        ("Deno", "deno", (os.path.join(home, ".deno", "bin", "deno" + EXE),
                          os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "deno.exe"),
                          os.path.join(BASE_DIR, "deno" + EXE))),
        ("Node.js", "node", (r"C:\Program Files\nodejs\node.exe",)),
        ("Bun", "bun", (os.path.join(home, ".bun", "bin", "bun" + EXE),)),
    )
    for label, exe, fallbacks in candidates:
        if shutil.which(exe) or any(os.path.exists(path) for path in fallbacks):
            return label
    return None


def ffmpeg_available():
    return os.path.exists(os.path.join(BASE_DIR, "ffmpeg" + EXE)) or bool(shutil.which("ffmpeg"))


def bot_lock_in_use():
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_LOCK_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def send_bot_command(command):
    """送指令給正在跑的本魚。它有回 ok 才回傳 True（舊版本魚不懂這些指令，會回傳 False）。"""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_LOCK_PORT), timeout=1) as conn:
            conn.settimeout(3)
            conn.sendall(command)
            return conn.recv(16).strip() == b"ok"
    except OSError:
        return False


def request_graceful_shutdown():
    """請正在跑的本魚自己安全關機（存好等級資料、離開語音頻道）。"""
    return send_bot_command(b"shutdown")


def wait_for_lock_release(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not bot_lock_in_use():
            return True
        time.sleep(0.5)
    return False


def kill_all_bots():
    if not IS_WINDOWS:
        result = subprocess.run(["pgrep", "-f", "deepseek_discord_bot"], capture_output=True, timeout=10)
        killed = []
        for pid in result.stdout.decode(errors="ignore").split():
            if pid.isdigit() and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), 9)
                    killed.append(pid)
                except OSError:
                    pass
        return killed
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and ("
        "($_.CommandLine -like '*deepseek_discord_bot*') -or ($_.Name -eq 'bluefish_bot.exe')) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW,
    )
    output = result.stdout.decode(errors="ignore")
    return [line.strip() for line in output.splitlines() if line.strip().isdigit()]


def fetch_ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:
        return []


OLLAMA_API = f"{OLLAMA_URL}/api"
OLLAMA_LIBRARY_URL = "https://ollama.com/library"

# (名稱, 下載大小 GB, 建議顯示卡記憶體 GB, 支援深度思考, 說明)
# 需要的顯示卡記憶體 = 模型本身 + 對話用的空間，是大約的數字
RECOMMENDED_MODELS = [
    ("qwen3:4b", 2.6, 4, True, "最輕快，小顯卡或筆電用，聊天品質普通"),
    ("qwen3:8b", 5.2, 7, True, "輕量款，中文不錯"),
    ("deepseek-r1:8b", 5.2, 7, True, "DeepSeek 輕量款"),
    ("qwen3:14b", 9.3, 11, True, "中文最自然，12 GB 以上顯卡推薦"),
    ("deepseek-r1:14b", 9.0, 11, True, "本魚原本用的模型"),
    ("gemma3:12b", 8.1, 10, False, "Google 出的，不支援深度思考"),
    ("qwen3:30b", 19.0, 21, True, "大顯卡用，架構特殊所以跑起來比同級快"),
    ("deepseek-r1:32b", 20.0, 22, True, "最聰明但最慢，24 GB 顯卡用"),
]


def detect_gpu():
    """用 nvidia-smi 查顯示卡名稱和記憶體（GB）。不是 NVIDIA 或查不到就回傳 (None, None)。"""
    if IS_MAC:
        return detect_apple_chip()
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return None, None
        best_name, best_mb = None, 0
        for line in result.stdout.decode(errors="ignore").splitlines():
            name, _, mb = line.rpartition(",")
            if mb.strip().isdigit() and int(mb) > best_mb:
                best_name, best_mb = name.strip(), int(mb)
        return (best_name, best_mb / 1024) if best_name else (None, None)
    except Exception:
        return None, None


def fetch_installed_models():
    """回傳 {模型名稱: 大小(bytes)}，Ollama 沒開就回傳 None。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    installed = {}
    for m in data.get("models", []):
        name = m.get("name") or m.get("model")
        if name:
            installed[name] = m.get("size", 0)
    return installed


def normalize_model_name(name):
    name = name.strip()
    return name if ":" in name else name + ":latest"


def delete_model(name):
    body = json.dumps({"model": name, "name": name}).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_API}/delete", data=body, method="DELETE",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30):
        pass


def format_size(num_bytes):
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.0f} MB"
    # 知識庫的文件常常只有幾 KB，不要都顯示成 0 MB
    return f"{max(num_bytes / 1024, 0.1):.1f} KB" if num_bytes < 100 * 1024 else f"{num_bytes / 1024:.0f} KB"


def detect_apple_chip():
    """Mac 的 M 系列晶片：顯示卡跟系統共用記憶體，大約三分之二能拿來跑模型。"""
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, timeout=5).stdout.decode(errors="ignore").strip()
        memory = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, timeout=5).stdout)
    except Exception:
        return None, None
    if not chip.startswith("Apple"):
        return None, None  # 舊的 Intel Mac 跑本機模型會很慢，當成沒有顯示卡
    return chip, memory / 1024 ** 3 * 2 / 3


INVITE_PERMISSIONS = 564324631702614  # 跟機器人的 /邀請 同一組權限


def invite_url_from_token():
    """Bot Token 的第一段就是機器人 ID（Base64），不用等機器人上線就能做出邀請連結。"""
    import base64
    try:
        first = read_token().split(".")[0]
        app_id = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)).decode("ascii")
    except Exception:
        return ""
    if not (app_id.isdigit() and 17 <= len(app_id) <= 20):
        return ""
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&permissions={INVITE_PERMISSIONS}&scope=bot%20applications.commands")


def read_guild_snapshot():
    try:
        with open(GUILDS_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------- 捷徑（.lnk）----------
# 以前的捷徑是 .bat 檔，Windows 不讓 .bat 釘選到開始或工作列。現在改成真正的 Windows 捷徑，
# 直接指向 pythonw.exe（打開面板時不會有黑色視窗），配上肥魚圖示和 APP_ID，就能像一般應用程式一樣釘選。
# 用 Windows 內建的 COM 介面（IShellLinkW）建立，不用另外裝套件。

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    # 只會放字串（VT_LPWSTR）；最後補一格讓大小跟 Windows 的 PROPVARIANT 一樣
    _fields_ = [("vt", ctypes.c_ushort), ("reserved1", ctypes.c_ushort), ("reserved2", ctypes.c_ushort),
                ("reserved3", ctypes.c_ushort), ("pwszVal", ctypes.c_wchar_p), ("padding", ctypes.c_void_p)]


def _guid(text):
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _com_call(obj, index, *argtypes):
    """呼叫 COM 物件的第 index 個方法（照 Windows SDK 標頭檔裡的順序）。失敗會丟 OSError。"""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])
    return lambda *args: method(obj, *args)


def _com_release(obj):
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _com_query(obj, iid):
    result = ctypes.c_void_p()
    _com_call(obj, 0, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(_guid(iid)), ctypes.byref(result))
    return result


def create_shortcut(path, target, arguments="", workdir="", icon="", description="", app_id=""):
    """建立（或蓋掉）一個 Windows 捷徑。app_id 會寫進捷徑，讓工作列知道它跟面板視窗是同一個程式。"""
    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitialize(None)
    except OSError:
        pass  # 這個執行緒已經用別的模式初始化過 COM，直接用就好
    link = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,  # CLSID_ShellLink
                           ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")), ctypes.byref(link))  # IShellLinkW
    text = ctypes.c_wchar_p
    try:
        _com_call(link, 20, text)(target)                       # SetPath
        _com_call(link, 11, text)(arguments)                    # SetArguments
        _com_call(link, 9, text)(workdir)                       # SetWorkingDirectory
        _com_call(link, 7, text)(description)                   # SetDescription
        if icon:
            _com_call(link, 17, text, ctypes.c_int)(icon, 0)    # SetIconLocation
        if app_id:
            store = _com_query(link, "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")  # IPropertyStore
            try:
                key = _PROPERTYKEY(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)  # PKEY_AppUserModel_ID
                value = _PROPVARIANT(vt=31, pwszVal=app_id)                              # 31 = VT_LPWSTR
                _com_call(store, 6, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(key), ctypes.byref(value))  # SetValue
                _com_call(store, 7)()                                                     # Commit
            finally:
                _com_release(store)
        persist = _com_query(link, "{0000010B-0000-0000-C000-000000000046}")  # IPersistFile
        try:
            _com_call(persist, 6, text, ctypes.c_long)(path, True)            # Save
        finally:
            _com_release(persist)
    finally:
        _com_release(link)


def find_desktop():
    folder = shell_folder(0x10, os.path.join(os.path.expanduser("~"), "Desktop"))  # CSIDL_DESKTOPDIRECTORY
    return folder if os.path.isdir(folder) else None


def make_panel_shortcut(path):
    """指向控制面板的捷徑：用 pythonw.exe 打開 panel.pyw（不會有黑色視窗），配上肥魚圖示。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    create_shortcut(path, windowed_python(), f'"{PANEL_SCRIPT}"', BASE_DIR,
                    ICON_PATH if os.path.exists(ICON_PATH) else "", "藍色大肥魚 Discord 機器人的控制面板", APP_ID)


def remove_old_launcher(path):
    """刪掉舊版面板做的 .bat 啟動檔（只刪內容真的是打開這個控制面板的，別人的檔案不動）。"""
    try:
        with open(path, "r", encoding="mbcs" if os.name == "nt" else "utf-8", errors="replace") as f:
            content = f.read(2000)
        if "panel.pyw" in content and len(content) < 1000:
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def make_mac_app(path):
    """Mac 版的捷徑：在「應用程式」資料夾做一個小小的 .app，雙擊就打開控制面板，也能留在 Dock。"""
    contents = os.path.join(path, "Contents")
    for sub in ("MacOS", "Resources"):
        os.makedirs(os.path.join(contents, sub), exist_ok=True)
    launcher = os.path.join(contents, "MacOS", "bluefish-panel")
    with open(launcher, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/bash\n"
                f"export PATH={shlex.quote(os.environ.get('PATH', ''))}\n"
                f"cd {shlex.quote(BASE_DIR)}\n"
                f"exec {shlex.quote(sys.executable)} {shlex.quote(PANEL_SCRIPT)}\n")
    os.chmod(launcher, 0o755)
    if os.path.exists(ICNS_PATH):
        shutil.copyfile(ICNS_PATH, os.path.join(contents, "Resources", "bluefish.icns"))
    info = {
        "CFBundleName": "藍色大肥魚控制面板",
        "CFBundleDisplayName": "藍色大肥魚控制面板",
        "CFBundleIdentifier": "com.bluefish.panel",
        "CFBundleExecutable": "bluefish-panel",
        "CFBundleIconFile": "bluefish",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
    }
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    return path


def boot_enabled():
    if IS_WINDOWS:
        return os.path.exists(STARTUP_LAUNCHER) or os.path.exists(OLD_STARTUP_BAT)
    return os.path.exists(MAC_LAUNCH_AGENT)


def set_boot(enabled):
    """電腦開機時自動打開控制面板：Windows 放捷徑到「啟動」資料夾，Mac 放一個登入時執行的設定檔。"""
    if IS_WINDOWS:
        if enabled:
            make_panel_shortcut(STARTUP_LAUNCHER)
            remove_old_launcher(OLD_STARTUP_BAT)
        else:
            for path in (STARTUP_LAUNCHER, OLD_STARTUP_BAT):
                if os.path.exists(path):
                    os.remove(path)
        return
    if not enabled:
        if os.path.exists(MAC_LAUNCH_AGENT):
            os.remove(MAC_LAUNCH_AGENT)
        return
    os.makedirs(os.path.dirname(MAC_LAUNCH_AGENT), exist_ok=True)
    agent = {
        "Label": "com.bluefish.panel",
        "ProgramArguments": [sys.executable, PANEL_SCRIPT],
        "WorkingDirectory": BASE_DIR,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    with open(MAC_LAUNCH_AGENT, "wb") as f:
        plistlib.dump(agent, f)


def install_shortcuts(desktop=True):
    """在開始功能表（和桌面）放「藍色大肥魚控制面板」捷徑，順便把舊版做的 .bat 捷徑換掉。回傳建好的捷徑路徑。
    Mac 則是在「應用程式」資料夾放一個 .app。"""
    if not IS_WINDOWS:
        return [make_mac_app(MAC_APP_PATH)]
    made = []
    path = os.path.join(START_MENU_DIR, SHORTCUT_NAME)
    make_panel_shortcut(path)
    made.append(path)
    folder = find_desktop() if desktop else None
    if folder:
        path = os.path.join(folder, SHORTCUT_NAME)
        make_panel_shortcut(path)
        made.append(path)
        remove_old_launcher(os.path.join(folder, "藍色大肥魚控制面板.bat"))
    return made


class StatusLight:
    """主控台上方的狀態卡片：左邊一條顏色帶加圓點，一眼看出正常（綠）、等待（黃）、有問題（紅）。"""

    def __init__(self, parent, title):
        self.frame = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        self.stripe = tk.Frame(self.frame, bg=COLOR_OFF, width=px(4))
        self.stripe.pack(side="left", fill="y")
        body = tk.Frame(self.frame, bg=CARD)
        body.pack(side="left", fill="both", expand=True, padx=px(12), pady=px(10))
        top = tk.Frame(body, bg=CARD)
        top.pack(anchor="w")
        size = px(11)
        self.canvas = tk.Canvas(top, width=size, height=size, bg=CARD, highlightthickness=0, bd=0)
        self.dot = self.canvas.create_oval(0, 0, size - 1, size - 1, fill=COLOR_OFF, outline="")
        self.canvas.pack(side="left", padx=px(0, 8))
        tk.Label(top, text=title, bg=CARD, fg=TEXT, font=(FONT, 10, "bold")).pack(side="left")
        self.detail = tk.Label(body, text="檢查中…", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w")
        self.detail.pack(anchor="w", pady=px(3, 0))

    def set(self, color, detail):
        self.canvas.itemconfigure(self.dot, fill=color)
        self.stripe.configure(bg=color)
        self.detail.configure(text=detail)


class App(tk.Tk):
    def __init__(self):
        global UI_SCALE
        super().__init__()
        if IS_MAC:
            # Mac 的 Tk 把 1 點當 1 像素，字會比 Windows 小一號，調成跟 Windows 100% 一樣大
            self.tk.call("tk", "scaling", 96 / 72)
        UI_SCALE = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("藍色大肥魚 控制面板")
        try:
            if IS_WINDOWS and os.path.exists(ICON_PATH):
                self.iconbitmap(default=ICON_PATH)  # 視窗左上角和工作列都用肥魚圖示（對話框也會跟著用）
            elif os.path.exists(DOCK_ICON_PATH):
                self._dock_icon = tk.PhotoImage(file=DOCK_ICON_PATH)
                self.iconphoto(True, self._dock_icon)  # Mac：Dock 上顯示本魚，不是 Python 的火箭
        except tk.TclError:
            pass
        self._fit_window(1140, 780, 940, 660)
        self.configure(bg=BG)
        self._setup_theme()

        self.config_data = load_config()
        self.bot_proc = None
        self.bot_online = False
        self.task_running = False
        self.status_checking = False
        self.gpu_name = None
        self.gpu_vram = None
        self.installed_models = {}
        self.pulling = False
        self.pull_cancel = False
        self.guild_snapshot = {}
        self.guild_snapshot_mtime = None
        self.last_bot_lock = False
        self.docker_checked_at = -DOCKER_CHECK_SECONDS
        self.docker_last = False
        self.hints_shown = set()
        self.log_queue = queue.Queue()

        self._build_ui()
        self.after(100, self._drain_log)
        self.after(300, self._refresh_status)
        self.after(500, self.refresh_models)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if IS_MAC:
            self.createcommand("::tk::mac::Quit", self.on_close)  # 按 Cmd+Q 也要先問、先把本魚關好
            self.createcommand("::tk::mac::ReopenApplication", self._bring_to_front)  # 點 Dock 圖示叫回視窗

        self.log("控制面板已開啟。")
        if self.config_data.get("autostart_bot"):
            self.after(800, self.start_bot)
        self.after(1500, self._upgrade_shortcuts)
        self.after(200, self._fit_cover)  # 剛打開時版面還沒排好，等一下再決定封面放不放得下

    def _upgrade_shortcuts(self):
        """舊版的開機啟動是 .bat（開機時會閃黑色視窗），換成捷徑；第一次打開新版面板時，
        在開始功能表放一個捷徑（只做一次，之後自己刪掉就不會再加回來）。"""
        if IS_WINDOWS and os.path.exists(OLD_STARTUP_BAT):
            try:
                make_panel_shortcut(STARTUP_LAUNCHER)
                if remove_old_launcher(OLD_STARTUP_BAT):
                    self.log("開機自動打開控制面板改用新的捷徑了（開機時不會再閃一下黑色視窗）。", "info")
            except Exception as e:
                self.log(f"更新開機啟動的捷徑失敗：{e}", "error")
        if self.config_data.get("shortcuts_created"):
            return
        try:
            install_shortcuts(desktop=False)
        except Exception as e:
            self.log(f"建立捷徑失敗：{e}（可以到「設定」按「{SHORTCUT_BUTTON.split(' ', 1)[1]}」再試一次）", "error")
            return
        self.config_data["shortcuts_created"] = True
        try:
            save_config(self.config_data)
        except Exception:
            pass
        if IS_WINDOWS:
            self.log("開始功能表多了「藍色大肥魚控制面板」：搜尋「藍色大肥魚」就找得到，"
                     "在圖示上按右鍵可以釘選到開始或工作列。", "good")
        else:
            self.log("「應用程式」資料夾多了「藍色大肥魚控制面板」：用 Spotlight 搜尋「藍色大肥魚」就找得到，"
                     "打開後在 Dock 的圖示上按右鍵 →「選項」→「保留在 Dock」就能常駐。", "good")

    def listen_for_show(self, lock):
        """已經開著面板時再點一次捷徑，新開的那個會通知這裡，把這個視窗叫到最前面，不會開出第二個面板。"""
        def worker():
            while True:
                try:
                    conn, _ = lock.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(2)
                    if conn.recv(16).strip() == b"show":
                        conn.sendall(b"ok")
                        self.after(0, self._bring_to_front)
                except OSError:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=worker, name="panel-show", daemon=True).start()

    def _bring_to_front(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _fit_window(self, width, height, min_width, min_height):
        """視窗照縮放比例放大，但不超過螢幕的 92%（筆電小螢幕也放得下），並擺在螢幕中間。"""
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(px(width), int(screen_w * 0.92))
        h = min(px(height), int(screen_h * 0.86))
        self.minsize(min(px(min_width), w), min(px(min_height), h))
        self.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{max(0, (screen_h - h) // 2 - px(16))}")

    def _setup_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")  # clam 才能自訂顏色；vista 主題的按鈕顏色改不動
        except tk.TclError:
            pass
        base = (FONT, 10)
        style.configure(".", background=BG, foreground=TEXT, font=base, bordercolor=BORDER,
                        lightcolor=BG, darkcolor=BG, troughcolor="#e1e8f2", focuscolor=ACCENT_LIGHT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Warn.TLabel", background=BG, foreground="#b26a00")
        # 白色卡片裡的字：底色要跟卡片一樣是白的，不然字後面會有一塊灰底
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=(FONT, 11, "bold"))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("CardWarn.TLabel", background=CARD, foreground="#b26a00", font=(FONT, 9))
        # 可以點的文字（例如「▸ 進階設定」）
        style.configure("Link.TLabel", background=BG, foreground=ACCENT_DARK, font=(FONT, 10, "bold"))

        style.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT_DARK, font=(FONT, 10, "bold"))

        style.configure("TButton", padding=px(14, 7), background="#e2e9f3", foreground=TEXT,
                        borderwidth=0, relief="flat", lightcolor="#e2e9f3", darkcolor="#e2e9f3")
        style.map("TButton",
                  background=[("disabled", "#eef1f6"), ("pressed", "#c9d5e6"), ("active", "#d4dfee")],
                  foreground=[("disabled", "#a3abb8")],
                  lightcolor=[("pressed", "#c9d5e6"), ("active", "#d4dfee")],
                  darkcolor=[("pressed", "#c9d5e6"), ("active", "#d4dfee")])
        for name, bg, active, fg in (
            ("Accent.TButton", ACCENT, ACCENT_DARK, "#ffffff"),
            ("Danger.TButton", "#fbe4e4", "#f6cccc", DANGER),
            ("On.TButton", "#dcf3e5", "#c6ead4", "#17804a"),
            ("Off.TButton", "#eceff4", "#dde2ea", MUTED),
        ):
            style.configure(name, background=bg, foreground=fg, font=(FONT, 10, "bold"),
                            lightcolor=bg, darkcolor=bg)
            style.map(name,
                      background=[("disabled", "#e6ebf2"), ("pressed", active), ("active", active)],
                      foreground=[("disabled", "#a3abb8")],
                      lightcolor=[("active", active)], darkcolor=[("active", active)])
        # 空間比較小的地方（AI 來源的卡片）用的緊湊按鈕；「Compact.Accent.TButton」會繼承 Accent 的顏色
        style.configure("Compact.TButton", padding=px(8, 6))
        style.configure("Compact.Accent.TButton", padding=px(8, 6))

        # 分頁用左邊的導覽列切換，Notebook 自己的分頁標籤藏起來
        style.layout("Pages.TNotebook.Tab", [])
        style.configure("Pages.TNotebook", background=BG, borderwidth=0, tabmargins=0,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)

        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(widget, fieldbackground=CARD, background=CARD, bordercolor=BORDER,
                            lightcolor=CARD, darkcolor=CARD, padding=px(5), arrowcolor=ACCENT_DARK,
                            arrowsize=px(12), insertcolor=TEXT)
            style.map(widget,
                      fieldbackground=[("disabled", FIELD_DISABLED), ("readonly", CARD)],
                      foreground=[("disabled", "#8a94a6")],
                      bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        # 唯讀下拉選單選到的文字不要整塊反白成藍色
        style.map("TCombobox", selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)])
        style.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=CARD,
                        indicatorforeground=ACCENT, indicatorsize=px(13), indicatormargin=px(0, 0, 8, 0))
        style.map("TCheckbutton", background=[("active", BG)],
                  indicatorbackground=[("selected", CARD)])
        style.configure("Card.TCheckbutton", background=CARD)
        style.map("Card.TCheckbutton", background=[("active", CARD)],
                  indicatorbackground=[("selected", CARD)])

        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=px(32), bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        borderwidth=1, font=base)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background="#eaeff6", foreground=TEXT, relief="flat",
                        font=(FONT, 10, "bold"), padding=px(6, 7), lightcolor="#eaeff6", darkcolor="#eaeff6",
                        bordercolor=BORDER)
        style.map("Treeview.Heading", background=[("active", "#dde5f0")])

        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e1e8f2",
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(12))
        style.configure("TSeparator", background=BORDER)
        for bar in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(bar, background="#d5deea", troughcolor=BG, bordercolor=BG,
                            arrowcolor=MUTED, lightcolor="#d5deea", darkcolor="#d5deea", arrowsize=px(13),
                            gripcount=0)
            style.map(bar, background=[("active", "#c2cfe0")])

    def _build_shell(self):
        """左邊深藍色導覽列；右邊上方是頁面標題和機器人狀態，下面放各頁內容。回傳右邊的內容區。"""
        side = tk.Frame(self, bg=SIDEBAR_BG, width=px(212))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=SIDEBAR_BG)
        brand.pack(fill="x", padx=px(18), pady=px(22, 20))
        # 左上角放本魚的圓形大頭貼（Tk 只能整數倍縮放：縮放 140% 以上用原尺寸，其他用一半）；圖不見就退回魚的符號
        self.avatar_image = None
        if os.path.exists(AVATAR_PATH):
            try:
                image = tk.PhotoImage(file=AVATAR_PATH)
                self.avatar_image = image if UI_SCALE >= 1.4 else image.subsample(2)
            except tk.TclError:
                self.avatar_image = None
        if self.avatar_image:
            tk.Label(brand, image=self.avatar_image, bg=SIDEBAR_BG, bd=0).pack(side="left")
        else:
            tk.Label(brand, text="🐟", bg=SIDEBAR_BG, fg="#ffffff", font=(FONT, 22)).pack(side="left")
        names = tk.Frame(brand, bg=SIDEBAR_BG)
        names.pack(side="left", padx=px(10, 0))
        tk.Label(names, text="藍色大肥魚", bg=SIDEBAR_BG, fg="#ffffff", font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(names, text="控制面板", bg=SIDEBAR_BG, fg=SIDEBAR_MUTED, font=(FONT, 9)).pack(anchor="w")
        self.nav_frame = tk.Frame(side, bg=SIDEBAR_BG)
        self.nav_frame.pack(fill="x")
        self.header_sub = tk.Label(side, bg=SIDEBAR_BG, fg=SIDEBAR_MUTED, font=(FONT, 9), anchor="w", justify="left")
        self.header_sub.pack(side="bottom", fill="x", padx=px(22), pady=px(0, 18))
        # 不管在哪一頁，都能一鍵打開知識庫資料夾
        folder_link = tk.Label(side, text="📂  開啟知識庫資料夾", bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 10),
                               anchor="w", cursor="hand2", padx=px(12), pady=px(7))
        folder_link.pack(side="bottom", fill="x", padx=px(10), pady=px(0, 12))
        folder_link.bind("<Button-1>", lambda e: self.open_knowledge_folder())
        folder_link.bind("<Enter>", lambda e: folder_link.configure(bg=SIDEBAR_HOVER, fg="#ffffff"))
        folder_link.bind("<Leave>", lambda e: folder_link.configure(bg=SIDEBAR_BG, fg=SIDEBAR_TEXT))
        self.folder_link = folder_link

        # 封面：本魚本人，放在選單和底下連結中間的空位。下面一行小字跟著機器人的狀態變
        self.cover_images = []
        self.cover_box = tk.Frame(side, bg=SIDEBAR_BG)
        if os.path.exists(COVER_PATH):
            try:
                full = tk.PhotoImage(file=COVER_PATH)
                # Tk 只能整數倍縮放：螢幕縮放 120% 以上用原尺寸，放不下再用一半大小
                self.cover_images = ([full] if UI_SCALE >= 1.2 else []) + [full.subsample(2)]
            except tk.TclError:
                self.cover_images = []
        if self.cover_images:
            self.cover_label = tk.Label(self.cover_box, image=self.cover_images[0], bg=SIDEBAR_BG, bd=0)
            self.cover_label.pack()
            self.cover_caption = tk.Label(self.cover_box, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 9))
            self.cover_caption.pack(pady=px(2, 0))
            side.bind("<Configure>", lambda e: self._fit_cover(), add="+")

        content = tk.Frame(self, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        top = tk.Frame(content, bg=BG)
        top.pack(fill="x", padx=px(26), pady=px(18, 2))
        self.header_pill = tk.Label(top, font=(FONT, 10, "bold"), padx=px(14), pady=px(6))
        self.header_pill.pack(side="right", anchor="n", pady=px(4, 0))
        self.page_title = tk.Label(top, bg=BG, fg=TEXT, font=(FONT, 17, "bold"), anchor="w")
        self.page_title.pack(anchor="w")
        self.page_sub = tk.Label(top, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w")
        self.page_sub.pack(anchor="w", pady=px(2, 0))
        self._set_header_pill("檢查中…", "off")
        return content

    def _set_header_pill(self, text, tone):
        bg, fg = PILL_TONES[tone]
        self.header_pill.configure(text=f"●  {text}", bg=bg, fg=fg)
        if self.cover_images:
            caption = {"on": "本魚上班中～", "wait": "本魚起床中…", "off": "本魚在睡覺… zzz"}[tone]
            self.cover_caption.configure(text="本魚在背景工作中" if "背景" in text else caption)

    def _fit_cover(self):
        """封面放得下原尺寸就用原尺寸，不然用一半大小，還是放不下（視窗太矮）就先藏起來。"""
        if not self.cover_images:
            return
        free = self.folder_link.winfo_y() - (self.nav_frame.winfo_y() + self.nav_frame.winfo_height()) - px(16)
        need = self.cover_caption.winfo_reqheight() + px(10)
        choice = next((img for img in self.cover_images if img.height() + need <= free), None)
        if choice is None:
            self.cover_box.pack_forget()
            return
        if self.cover_label.cget("image") != str(choice):
            self.cover_label.configure(image=choice)
        if not self.cover_box.winfo_manager():
            self.cover_box.pack(side="bottom", pady=px(0, 8))

    def _card(self, parent, title=None, note=None, padding=(16, 12)):
        """白色卡片，跟主控台上方的狀態卡片同一種樣子。有標題就放第一行，標題列右邊還可以再放按鈕。
        回傳 (卡片, 放內容的框, 標題列)。卡片裡的字要用 Card 開頭的樣式，底色才會是白的。"""
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=px(padding[0]), pady=px(padding[1]))
        head = None
        if title:
            head = ttk.Frame(body, style="Card.TFrame")
            head.pack(fill="x")
            ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
        if note:
            ttk.Label(body, text=note, style="CardMuted.TLabel", justify="left").pack(
                anchor="w", pady=px(4 if title else 0, 0))
        return card, body, head

    def _build_nav(self, pages):
        self.nav_items = {}
        for tab, icon, label in pages:
            item = tk.Frame(self.nav_frame, bg=SIDEBAR_BG, cursor="hand2")
            item.pack(fill="x", padx=px(10), pady=px(1))
            bar = tk.Frame(item, bg=SIDEBAR_BG, width=px(4))
            bar.pack(side="left", fill="y")
            icon_label = tk.Label(item, text=icon, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 12), width=2)
            icon_label.pack(side="left", padx=px(10, 6), pady=px(8))
            text_label = tk.Label(item, text=label, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT, font=(FONT, 11), anchor="w")
            text_label.pack(side="left", fill="x", expand=True)
            key = str(tab)
            self.nav_items[key] = ((item, icon_label, text_label), bar)
            for widget in (item, icon_label, text_label):
                widget.bind("<Button-1>", lambda e, t=tab: self.notebook.select(t))
                widget.bind("<Enter>", lambda e, k=key: self._hover_nav(k, True))
                widget.bind("<Leave>", lambda e, k=key: self._hover_nav(k, False))

    def _paint_nav(self, key, bg, fg, bar_color, bold=False):
        (item, icon_label, text_label), bar = self.nav_items[key]
        for widget in (item, icon_label, text_label):
            widget.configure(bg=bg)
        icon_label.configure(fg=fg)
        text_label.configure(fg=fg, font=(FONT, 11, "bold" if bold else "normal"))
        bar.configure(bg=bar_color)

    def _hover_nav(self, key, inside):
        if key != self.notebook.select():
            color = SIDEBAR_HOVER if inside else SIDEBAR_BG
            self._paint_nav(key, color, "#ffffff" if inside else SIDEBAR_TEXT, color)

    def _update_nav(self):
        current = self.notebook.select()
        for key in self.nav_items:
            if key == current:
                self._paint_nav(key, SIDEBAR_ACTIVE, "#ffffff", SIDEBAR_BAR, bold=True)
            else:
                self._paint_nav(key, SIDEBAR_BG, SIDEBAR_TEXT, SIDEBAR_BG)
        title, subtitle = self.page_info.get(current, ("", ""))
        self.page_title.configure(text=title)
        self.page_sub.configure(text=subtitle)

    def _build_ui(self):
        content = self._build_shell()
        notebook = ttk.Notebook(content, style="Pages.TNotebook")
        notebook.pack(fill="both", expand=True, padx=px(12), pady=px(2, 12))
        self.notebook = notebook

        self.tab_main = ttk.Frame(notebook, padding=px(14))
        self.tab_settings = ttk.Frame(notebook, padding=px(14))
        self.tab_persona = ttk.Frame(notebook, padding=px(14))
        self.tab_models = ttk.Frame(notebook, padding=px(14))
        self.tab_servers = ttk.Frame(notebook, padding=px(14))
        self.tab_channels = ttk.Frame(notebook, padding=px(14))
        self.tab_ai = ttk.Frame(notebook, padding=px(14))
        self.tab_knowledge = ttk.Frame(notebook, padding=px(14))
        pages = (
            (self.tab_main, "🏠", "主控台", "啟動本魚，看看她現在好不好"),
            (self.tab_ai, "🧠", "AI 來源", "選本魚聊天要用哪個 AI「大腦」"),
            (self.tab_knowledge, "📚", "知識庫", "放文件給本魚參考，回答問題時她會自己去查"),
            (self.tab_servers, "🌐", "伺服器", "本魚在哪些伺服器工作、邀請她到新的伺服器"),
            (self.tab_channels, "＃", "頻道功能", "每個頻道可以個別開關本魚的功能"),
            # 用線上 AI 時這頁會從選單藏起來（見 _update_nav_visibility）
            (self.tab_models, "📦", "本機模型", "下載、切換本機 AI（Ollama）用的模型"),
            (self.tab_settings, "⚙", "設定", "機器人帳號、聊天和開機設定"),
            (self.tab_persona, "🎭", "人設", "本魚的個性和說話方式"),
        )
        self.page_info = {}
        for tab, icon, label, subtitle in pages:
            notebook.add(tab, text=f"  {label}  ")
            self.page_info[str(tab)] = (label, subtitle)
        self._build_nav([(tab, icon, label) for tab, icon, label, _ in pages])

        self._build_main_tab()
        self._build_settings_tab()
        self._build_persona_tab()
        self._build_models_tab()
        self._build_ai_tab()
        self._build_knowledge_tab()
        self._build_channels_tab()  # 要比伺服器分頁先建，伺服器清單更新時會順便更新頻道表格
        self._build_servers_tab()
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._update_nav()

    def _on_tab_changed(self, event=None):
        self._update_nav()
        current = self.notebook.select()
        if current == str(self.tab_models) and not self.pulling:
            self.refresh_model_table()
        elif current == str(self.tab_knowledge):
            self.refresh_knowledge_page()
        elif current in (str(self.tab_servers), str(self.tab_channels)):
            self.refresh_guild_table()

    def _build_main_tab(self):
        status_box = ttk.Frame(self.tab_main)
        status_box.pack(fill="x")
        self.light_bot = StatusLight(status_box, "機器人")
        self.light_ollama = StatusLight(status_box, "AI 聊天")
        self.light_search = StatusLight(status_box, "上網查資料")
        self.light_music = StatusLight(status_box, "點歌")
        for i, light in enumerate((self.light_bot, self.light_ollama, self.light_search, self.light_music)):
            light.frame.grid(row=0, column=i, sticky="nsew", padx=px(0 if i == 0 else 10, 0))
            status_box.columnconfigure(i, weight=1, uniform="lights")

        bot_card, bot_box, _ = self._card(self.tab_main, "🐟 本魚")
        bot_card.pack(fill="x", pady=px(14, 0))
        buttons = ttk.Frame(bot_box, style="Card.TFrame")
        buttons.pack(fill="x", pady=px(10, 0))
        self.btn_start = ttk.Button(buttons, text="▶ 啟動", style="Accent.TButton", command=self.start_bot)
        self.btn_stop = ttk.Button(buttons, text="■ 停止", style="Danger.TButton", command=self.stop_bot)
        self.btn_restart = ttk.Button(buttons, text="⟳ 重新啟動", command=self.restart_bot)
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=px(6))
        self.btn_restart.pack(side="left")
        ttk.Button(buttons, text="🐟 邀請到伺服器", command=self.quick_invite).pack(side="left", padx=px(6, 0))

        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=px(14))
        self.var_ai = tk.BooleanVar(value=bool(self.config_data.get("ai_enabled", True)))
        self.btn_ai = ttk.Button(buttons, width=16, command=self.toggle_ai)
        self.btn_ai.pack(side="left")
        # 說明放在按鈕下面一行，視窗窄的時候才不會被切掉
        self.label_ai = ttk.Label(bot_box, style="CardMuted.TLabel")
        self.label_ai.pack(anchor="w", pady=px(8, 0))

        tools_card, tools_box, _ = self._card(self.tab_main, "🧰 小工具")
        tools_card.pack(fill="x", pady=px(12, 0))
        tools = ttk.Frame(tools_box, style="Card.TFrame")
        tools.pack(fill="x", pady=px(10, 0))
        # 只顯示現在用得到的按鈕（見 _update_tool_buttons），不會一排看不懂的技術名詞
        self.btn_install = ttk.Button(tools, text="🔄 更新元件", command=self.install_packages)
        self.btn_wake_ollama = ttk.Button(tools, text="叫醒 Ollama", command=self.start_ollama)
        self.btn_wake_searxng = ttk.Button(tools, text="叫醒 SearXNG", command=self.start_searxng)
        self.btn_open_folder = ttk.Button(tools, text="📂 打開本魚資料夾", command=lambda: open_path(BASE_DIR))
        ttk.Button(tools, text="強制關閉所有本魚", command=self.kill_background_bots).pack(side="right")
        self._update_ai_widgets()

        log_card, log_box, log_head = self._card(self.tab_main, "📝 執行紀錄")
        log_card.pack(fill="both", expand=True, pady=px(12, 0))
        ttk.Button(log_head, text="清除", style="Compact.TButton", command=self.clear_log).pack(side="right")
        self.log_view = ScrolledText(
            log_box, height=8, wrap="word", state="disabled",
            font=(FONT, 10), background=LOG_BG, foreground=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=0, highlightthickness=0, padx=px(12), pady=px(10),
            spacing1=px(2), spacing3=px(2),
        )
        self.log_view.vbar.configure(width=px(12))
        self.log_view.pack(fill="both", expand=True, pady=px(10, 0))
        self.log_view.tag_configure("stamp", foreground="#98a2b3", font=(MONO_FONT, 9))
        self.log_view.tag_configure("error", foreground="#d64545")
        self.log_view.tag_configure("good", foreground="#1f8a4c")
        self.log_view.tag_configure("info", foreground="#2f6fc4")

    def _build_settings_tab(self):
        frame = self.tab_settings
        # 本機模型改到「本機模型」分頁選，這裡只記著目前用哪個，儲存設定時才不會蓋掉
        self.var_model = tk.StringVar(value=self.config_data.get("model_name", DEFAULT_CONFIG["model_name"]))

        # 儲存按鈕先放在最下面，視窗矮的時候被擠掉的是上面的空白，不是按鈕
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", anchor="w", pady=px(14, 0))
        ttk.Button(buttons, text="儲存設定", style="Accent.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(buttons, text=SHORTCUT_BUTTON, command=self.create_shortcuts).pack(side="left", padx=px(8))

        card, body, _ = self._card(frame, "🔑 Discord 機器人")
        card.pack(fill="x")
        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x", pady=px(10, 0))
        ttk.Label(row, text="Token", style="Card.TLabel").pack(side="left")
        self.var_token = tk.StringVar(value=read_token())
        self.entry_token = ttk.Entry(row, textvariable=self.var_token, show="•")
        self.entry_token.pack(side="left", fill="x", expand=True, padx=px(12, 0))
        self.btn_show_token = ttk.Button(row, text="顯示", width=6, command=self.toggle_token)
        self.btn_show_token.pack(side="left", padx=px(6, 0))
        ttk.Label(body, text="在 Discord Developer Portal 的「Bot」頁按「Reset Token」拿到的那一串，不能給別人看。",
                  style="CardMuted.TLabel").pack(anchor="w", pady=px(6, 0))

        card, body, _ = self._card(frame, "💬 聊天")
        card.pack(fill="x", pady=px(12, 0))
        grid = ttk.Frame(body, style="Card.TFrame")
        grid.pack(fill="x", pady=px(6, 0))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="記住最近幾輪對話", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=px(4))
        history_row = ttk.Frame(grid, style="Card.TFrame")
        history_row.grid(row=0, column=1, columnspan=2, sticky="w", padx=px(12, 0), pady=px(4))
        self.var_history = tk.IntVar(value=int(self.config_data.get("max_history_turns", 6)))
        ttk.Spinbox(history_row, from_=1, to=20, textvariable=self.var_history, width=5).pack(side="left")
        ttk.Label(history_row, text="越多越記得前面聊過什麼，但回答會稍微慢一點",
                  style="CardMuted.TLabel").pack(side="left", padx=px(10, 0))

        self.var_search = tk.BooleanVar(value=bool(self.config_data.get("search_enabled", True)))
        ttk.Checkbutton(grid, text="上網查資料（問到新聞、天氣、最新消息時，會先上網查再回答）", style="Card.TCheckbutton",
                        variable=self.var_search, command=self._update_search_fields).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=px(8, 2))

        ttk.Label(grid, text="搜尋方式", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=px(4))
        self.var_search_engine = tk.StringVar(value=SEARCH_ENGINES[configured_search_engine(self.config_data)])
        self.combo_search_engine = ttk.Combobox(grid, textvariable=self.var_search_engine, state="readonly",
                                                values=list(SEARCH_ENGINES.values()), width=30)
        self.combo_search_engine.grid(row=2, column=1, sticky="w", padx=px(12, 0), pady=px(4))
        self.combo_search_engine.bind("<<ComboboxSelected>>", lambda e: self._update_search_fields())

        # 下面兩格只有選 SearXNG 才用得到，選內建搜尋時整個藏起來（見 _update_search_fields）
        label_url = ttk.Label(grid, text="SearXNG 網址", style="Card.TLabel")
        label_url.grid(row=3, column=0, sticky="w", pady=px(4))
        self.var_searxng = tk.StringVar(value=self.config_data.get("searxng_url", DEFAULT_CONFIG["searxng_url"]))
        self.entry_searxng = ttk.Entry(grid, textvariable=self.var_searxng)
        self.entry_searxng.grid(row=3, column=1, sticky="ew", padx=px(12, 0), pady=px(4))
        label_docker = ttk.Label(grid, text="Docker 位置", style="Card.TLabel")
        label_docker.grid(row=4, column=0, sticky="w", pady=px(4))
        self.var_docker = tk.StringVar(value=self.config_data.get("docker_desktop_path", DEFAULT_CONFIG["docker_desktop_path"]))
        self.entry_docker = ttk.Entry(grid, textvariable=self.var_docker)
        self.entry_docker.grid(row=4, column=1, sticky="ew", padx=px(12, 0), pady=px(4))
        self.btn_browse_docker = ttk.Button(grid, text="瀏覽…", style="Compact.TButton", command=self.browse_docker)
        self.btn_browse_docker.grid(row=4, column=2, padx=px(6, 0))
        self.searxng_widgets = (label_url, self.entry_searxng, label_docker, self.entry_docker, self.btn_browse_docker)

        self.var_thinking = tk.BooleanVar(value=bool(self.config_data.get("enable_thinking", True)))
        self.check_thinking = ttk.Checkbutton(grid, text="深度思考（本機模型才有：回答比較準，但會慢很多）",
                                              style="Card.TCheckbutton", variable=self.var_thinking)
        self.check_thinking.grid(row=5, column=0, columnspan=3, sticky="w", pady=px(8, 0))
        self._update_search_fields()
        self._update_thinking_visibility()

        card, body, _ = self._card(frame, "🚀 開機")
        card.pack(fill="x", pady=px(12, 0))
        self.var_autostart_bot = tk.BooleanVar(value=bool(self.config_data.get("autostart_bot")))
        ttk.Checkbutton(body, text="打開控制面板時，自動啟動本魚", style="Card.TCheckbutton",
                        variable=self.var_autostart_bot).pack(anchor="w", pady=px(8, 2))
        self.var_boot = tk.BooleanVar(value=boot_enabled())
        ttk.Checkbutton(body, text="電腦開機時，自動打開控制面板", style="Card.TCheckbutton",
                        variable=self.var_boot, command=self.toggle_boot).pack(anchor="w", pady=px(2))
        ttk.Label(body, text="兩個都勾起來，電腦一開機本魚就會自己上線。",
                  style="CardMuted.TLabel").pack(anchor="w", pady=px(4, 0))

    def _build_persona_tab(self):
        frame = self.tab_persona
        ttk.Label(
            frame,
            text="這裡寫的是本魚的個性和說話規則，可以直接改。改完按「儲存人設」，重新啟動本魚就會生效。",
            style="Muted.TLabel",
        ).pack(anchor="w")
        self.persona_text = ScrolledText(frame, wrap="word", font=(FONT, 10), undo=True, relief="flat",
                                         borderwidth=0, padx=px(10), pady=px(8), background=CARD, foreground=TEXT,
                                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.persona_text.pack(fill="both", expand=True, pady=px(8))
        persona = self.config_data.get("persona") or read_default_persona()
        self.persona_text.insert("1.0", persona)

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="儲存人設", style="Accent.TButton", command=self.save_persona).pack(side="left")
        ttk.Button(buttons, text="還原成預設人設", command=self.reset_persona).pack(side="left", padx=px(8))

    # ---------- 伺服器分頁 ----------

    def _build_servers_tab(self):
        frame = self.tab_servers
        ttk.Label(
            frame,
            text="只有「啟用」的伺服器本魚才會工作，改了馬上生效。停用的伺服器裡，本魚會完全安靜。",
            style="Muted.TLabel",
        ).pack(anchor="w")
        self.label_guild_info = ttk.Label(frame, font=(FONT, 10, "bold"))
        self.label_guild_info.pack(anchor="w", pady=px(8, 6))

        table_box = ttk.Frame(frame)
        table_box.pack(fill="both", expand=True)
        columns = ("state", "name", "members", "id")
        self.guild_table = ttk.Treeview(table_box, columns=columns, show="headings", selectmode="extended", height=5)
        for col, title, width, stretch in (
            ("state", "狀態", 90, False), ("name", "伺服器", 320, True),
            ("members", "成員數", 90, False), ("id", "伺服器 ID", 190, False),
        ):
            self.guild_table.heading(col, text=title)
            self.guild_table.column(col, width=px(width), anchor="w", stretch=stretch)
        # 伺服器 ID 一般人用不到，不顯示（資料還是留在表格裡，程式要用）
        self.guild_table.configure(displaycolumns=("state", "name", "members"))
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.guild_table.yview)
        self.guild_table.configure(yscrollcommand=scroll.set)
        self.guild_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.guild_table.tag_configure("stripe", background=ROW_STRIPE)
        self.guild_table.tag_configure("enabled", foreground="#17804a")
        self.guild_table.tag_configure("disabled", foreground="#9aa3b2")
        self.guild_table.bind("<Double-1>", lambda e: self.toggle_selected_guilds())
        self.guild_table.bind("<<TreeviewSelect>>", lambda e: self._update_guild_buttons())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=px(8, 0))
        self.btn_guild_on = ttk.Button(buttons, text="✔ 啟用", style="Accent.TButton",
                                       command=lambda: self.set_selected_guilds(True))
        self.btn_guild_off = ttk.Button(buttons, text="✕ 停用", command=lambda: self.set_selected_guilds(False))
        self.btn_guild_on.pack(side="left")
        self.btn_guild_off.pack(side="left", padx=px(6))
        ttk.Button(buttons, text="全部啟用", command=self.enable_all_guilds).pack(side="left")
        self.btn_guild_leave = ttk.Button(buttons, text="讓本魚離開這個伺服器", style="Danger.TButton",
                                          command=self.leave_selected_guild)
        self.btn_guild_leave.pack(side="left", padx=px(18, 0))
        ttk.Button(buttons, text="重新整理", command=self.refresh_guild_table).pack(side="right")
        ttk.Label(frame, text="小提示：雙擊一列可以快速切換；按住 Ctrl 可以一次選好幾個。",
                  style="Muted.TLabel").pack(anchor="w", pady=px(6, 0))

        self.var_new_guild_default = tk.BooleanVar(value=bool(self.config_data.get("new_guild_default", True)))
        ttk.Checkbutton(frame, text="新加入的伺服器自動啟用（取消勾選的話，要到這裡手動啟用本魚才會工作）",
                        variable=self.var_new_guild_default, command=self.save_new_guild_default).pack(anchor="w", pady=px(10, 0))

        invite_card, invite_box, _ = self._card(
            frame, "💌 邀請本魚到其他伺服器", "把邀請連結傳給別人，或請他們私訊本魚說「邀請」，本魚會回一個邀請按鈕。")
        invite_card.pack(fill="x", pady=px(12, 0))
        invite_buttons = ttk.Frame(invite_box, style="Card.TFrame")
        invite_buttons.pack(anchor="w", pady=px(10, 0))
        self.btn_copy_invite = ttk.Button(invite_buttons, text="複製邀請連結", command=self.copy_invite_url)
        self.btn_open_invite = ttk.Button(invite_buttons, text="用瀏覽器打開", command=self.open_invite_url)
        self.btn_copy_invite.pack(side="left")
        self.btn_open_invite.pack(side="left", padx=px(6))
        self.label_invite_warn = ttk.Label(invite_box, style="CardWarn.TLabel", wraplength=px(880), justify="left")
        self.label_invite_warn.pack(anchor="w", pady=px(6, 0))
        self.refresh_guild_table()

    def _guild_default(self):
        return bool(self.config_data.get("new_guild_default", True))

    def _guild_enabled(self, guild_id):
        return bool((self.config_data.get("guild_access") or {}).get(guild_id, self._guild_default()))

    def refresh_guild_table(self):
        try:
            self.guild_snapshot_mtime = os.path.getmtime(GUILDS_SNAPSHOT_PATH)
        except OSError:
            self.guild_snapshot_mtime = None
        snap = read_guild_snapshot()
        self.guild_snapshot = snap
        guilds = snap.get("guilds") or []
        selected = set(self.guild_table.selection())
        self.guild_table.delete(*self.guild_table.get_children())
        enabled_count = 0
        for index, g in enumerate(guilds):
            gid = str(g.get("id", ""))
            enabled = self._guild_enabled(gid)
            enabled_count += enabled
            self.guild_table.insert(
                "", "end", iid=gid, tags=("enabled" if enabled else "disabled",) + (("stripe",) if index % 2 else ()),
                values=("✔ 啟用" if enabled else "✕ 停用", g.get("name", "?"), g.get("members", ""), gid),
            )
        keep = [i for i in selected if self.guild_table.exists(i)]
        if keep:
            self.guild_table.selection_set(keep)

        if not guilds and not snap:
            info = "還沒有伺服器清單。啟動機器人一次，本魚上線後就會出現在這裡。"
        elif not guilds:
            info = "本魚目前不在任何伺服器裡，用下面的邀請連結把她加進去吧～"
        else:
            info = f"本魚在 {len(guilds)} 個伺服器裡，{enabled_count} 個啟用中"
            updated = snap.get("updated")
            if updated and not self.last_bot_lock:
                info += f"（機器人沒在跑，這是 {time.strftime('%m/%d %H:%M', time.localtime(updated))} 的清單）"
        self.label_guild_info.configure(text=info)
        if guilds:
            self.header_sub.configure(text=f"{len(guilds)} 個伺服器\n{enabled_count} 個啟用中")

        self.invite_url = snap.get("invite_url") or invite_url_from_token()
        has_invite = bool(self.invite_url)
        state = "normal" if has_invite else "disabled"
        self.btn_copy_invite.configure(state=state)
        self.btn_open_invite.configure(state=state)
        if not has_invite:
            warn = "先到「設定」分頁填好 Discord Bot Token，就能產生邀請連結。"
        elif snap.get("public") is False:
            warn = ("⚠ 這隻機器人目前不是公開的，只有你自己能用這個連結。想讓別人也能邀請，"
                    "要到 Discord Developer Portal → 你的應用程式 → Bot，把「Public Bot」打開。")
        else:
            warn = ""
        self.label_invite_warn.configure(text=warn)
        # 沒有要提醒的事就整行藏起來，卡片下面才不會多一塊空白
        if warn:
            self.label_invite_warn.pack(anchor="w", pady=px(6, 0))
        else:
            self.label_invite_warn.pack_forget()
        self._update_guild_buttons()
        self.refresh_channel_table()

    def _update_guild_buttons(self):
        selection = self.guild_table.selection()
        state = "normal" if selection else "disabled"
        self.btn_guild_on.configure(state=state)
        self.btn_guild_off.configure(state=state)
        self.btn_guild_leave.configure(state="normal" if len(selection) == 1 else "disabled")

    def _save_guild_access(self, changes):
        access = dict(self.config_data.get("guild_access") or {})
        default = self._guild_default()
        # 把目前清單上的每個伺服器都明確寫下來，之後改「新伺服器預設」才不會連帶改到它們
        for g in self.guild_snapshot.get("guilds") or []:
            access.setdefault(str(g.get("id")), default)
        access.update(changes)
        self.config_data["guild_access"] = access
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return False
        self.refresh_guild_table()
        self._notify_guild_reload()
        return True

    def _notify_guild_reload(self):
        def worker():
            if not bot_lock_in_use():
                self.log("伺服器啟用設定已儲存，下次啟動機器人時生效。", "info")
            elif send_bot_command(b"guilds_reload"):
                self.log("已通知機器人套用新的伺服器啟用設定。", "good")
            else:
                self.log("機器人沒有回應（可能是舊版），伺服器啟用設定要重新啟動機器人後才會生效。", "error")

        threading.Thread(target=worker, daemon=True).start()

    def set_selected_guilds(self, enabled):
        ids = self.guild_table.selection()
        if not ids:
            return
        if self._save_guild_access({gid: enabled for gid in ids}):
            names = [self.guild_table.set(gid, "name") for gid in ids]
            self.log(f"{'啟用' if enabled else '停用'}：{'、'.join(names)}", "info")

    def toggle_selected_guilds(self):
        ids = self.guild_table.selection()
        if ids:
            self.set_selected_guilds(not self._guild_enabled(ids[0]))

    def enable_all_guilds(self):
        guilds = self.guild_snapshot.get("guilds") or []
        if guilds and self._save_guild_access({str(g.get("id")): True for g in guilds}):
            self.log("所有伺服器都啟用了。", "info")

    def save_new_guild_default(self):
        old_default = self._guild_default()
        access = dict(self.config_data.get("guild_access") or {})
        for g in self.guild_snapshot.get("guilds") or []:
            access.setdefault(str(g.get("id")), old_default)
        self.config_data["guild_access"] = access
        self.config_data["new_guild_default"] = bool(self.var_new_guild_default.get())
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        self.log("新伺服器會" + ("自動啟用。" if self.var_new_guild_default.get() else "先停用，要到「伺服器」分頁手動啟用。"), "info")
        self._notify_guild_reload()

    def leave_selected_guild(self):
        ids = self.guild_table.selection()
        if len(ids) != 1:
            return
        gid = ids[0]
        name = self.guild_table.set(gid, "name")
        if not self.last_bot_lock and not bot_lock_in_use():
            messagebox.showinfo("機器人沒在跑", "要先啟動機器人，才能讓她離開伺服器。")
            return
        if not messagebox.askyesno(
            "讓本魚離開伺服器",
            f"確定要讓本魚離開「{name}」嗎？\n\n離開之後要有人重新邀請才能回去，"
            "而且那個伺服器的斜線指令會消失。\n（只是暫時不想讓她在那裡工作的話，用「停用」就好。）",
        ):
            return

        def worker():
            if send_bot_command(f"leave:{gid}".encode("utf-8")):
                self.log(f"已請本魚離開「{name}」。", "good")
            else:
                self.log(f"機器人沒有回應（可能是舊版），沒辦法離開「{name}」。", "error")

        threading.Thread(target=worker, daemon=True).start()

    def copy_invite_url(self):
        url = self.invite_url
        if not url:
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self.log("邀請連結已複製，可以直接貼給別人。", "good")

    def open_invite_url(self):
        url = self.invite_url
        if url:
            webbrowser.open(url)

    def quick_invite(self):
        # 主畫面的快捷鍵：打開邀請頁，同時把連結複製起來方便貼給別人
        url = getattr(self, "invite_url", "") or invite_url_from_token()
        if not url:
            messagebox.showwarning("還不能邀請", "先到「設定」分頁填好 Discord Bot Token，才能產生邀請連結。")
            return
        self.invite_url = url
        self.clipboard_clear()
        self.clipboard_append(url)
        webbrowser.open(url)
        self.log("已打開邀請頁面，連結也複製好了，可以直接貼給別人。", "good")

    # ---------- AI 來源分頁 ----------

    def _build_ai_tab(self):
        frame = self.tab_ai
        self.ai_keys = load_ai_keys()
        self.ai_provider_codes = list(AI_PROVIDERS)
        self.ai_current_code = None
        ttk.Label(frame, text="挑一個就好。不知道選哪個的話，選「Google Gemini」最快最穩。",
                  font=(FONT, 11, "bold")).pack(anchor="w")

        quick = ttk.Frame(frame)
        quick.pack(fill="x", pady=px(10, 0))
        # 說明每行都短短的，視窗縮小、卡片變窄時也不會被切掉。
        # 每張卡片：(方案代碼, 標籤, 標題, 說明, 其他按鈕, 主要按鈕文字, 主要按鈕動作)
        cards = (
            ("free", "🆓 最簡單", "免費線上 AI", "不用申請，按一下就能用。\n缺點：大家共用，常要等\n10～50 秒，偶爾沒回應。",
             (), "使用這個", self.quick_use_free),
            ("gemini", "⭐ 推薦", "Google Gemini", "① 按「取得金鑰」登入 Google\n② 按「Create API key」再複製\n③ 回來按「貼上並使用」",
             (("取得金鑰", lambda: webbrowser.open(AI_PROVIDERS["gemini"][4])),), "貼上並使用", self.quick_use_gemini),
            ("ollama", "💻 本機", "本機 Ollama", "免費、對話不會送出去。\n要 8 GB 以上的顯示卡，\n模型在「本機模型」分頁下載。",
             (), "使用這個", self.quick_use_ollama),
        )
        # 正在用的那張卡片外框變藍色、按鈕變成「✔ 使用中」，由 _refresh_ai_cards 決定
        self.ai_card_buttons = {}
        self.ai_card_frames = {}
        for i, (code, badge, title, body, extra, action_text, action) in enumerate(cards):
            card = tk.Frame(quick, bg=CARD, highlightthickness=2, highlightbackground=BORDER)
            card.grid(row=0, column=i, sticky="nsew", padx=px(0 if i == 0 else 10, 0))
            quick.columnconfigure(i, weight=1, uniform="ai_cards")
            inner = ttk.Frame(card, style="Card.TFrame")
            inner.pack(fill="both", expand=True, padx=px(14), pady=px(12))
            tk.Label(inner, text=badge, bg="#e9edfd", fg=ACCENT_DARK, font=(FONT, 9, "bold"),
                     padx=px(8), pady=px(2)).pack(anchor="w")
            ttk.Label(inner, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=px(8, 0))
            ttk.Label(inner, text=body, style="CardMuted.TLabel", justify="left").pack(anchor="w", pady=px(4, 10))
            row = ttk.Frame(inner, style="Card.TFrame")
            row.pack(anchor="w", side="bottom")
            for label, command in extra:
                ttk.Button(row, text=label, style="Compact.TButton", command=command).pack(side="left", padx=px(0, 6))
            button = ttk.Button(row)
            button.pack(side="left")
            self.ai_card_buttons[code] = (button, action_text, action)
            self.ai_card_frames[code] = card

        self.label_ai_current = ttk.Label(frame, font=(FONT, 10, "bold"))
        self.label_ai_current.pack(anchor="w", pady=px(12, 0))

        # 進階設定平常收起來，一般人用上面三張卡片就夠了
        self.adv_toggle = ttk.Label(frame, style="Link.TLabel", cursor="hand2")
        self.adv_toggle.pack(anchor="w", pady=px(12, 0))
        self.adv_toggle.bind("<Button-1>", lambda e: self._toggle_ai_advanced())
        self.adv_card, adv, _ = self._card(frame)
        adv.columnconfigure(1, weight=1)
        ttk.Label(adv, text="服務", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=px(3))
        self.combo_ai_provider = ttk.Combobox(adv, state="readonly", width=34,
                                              values=[AI_PROVIDERS[c][0] for c in self.ai_provider_codes])
        self.combo_ai_provider.grid(row=0, column=1, sticky="w", padx=px(12, 0), pady=px(3))
        self.combo_ai_provider.bind("<<ComboboxSelected>>", lambda e: self._on_ai_provider_changed())
        self.label_ai_note = ttk.Label(adv, style="CardMuted.TLabel", wraplength=px(820), justify="left")
        self.label_ai_note.grid(row=1, column=1, columnspan=3, sticky="w", padx=px(12, 0), pady=px(0, 6))

        ttk.Label(adv, text="API 金鑰", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=px(3))
        self.var_ai_key = tk.StringVar()
        self.entry_ai_key = ttk.Entry(adv, textvariable=self.var_ai_key, show="•")
        self.entry_ai_key.grid(row=2, column=1, sticky="ew", padx=px(12, 0), pady=px(3))
        self.btn_ai_key_show = ttk.Button(adv, text="顯示", width=6, command=self._toggle_ai_key)
        self.btn_ai_key_show.grid(row=2, column=2, padx=px(6, 0))
        self.btn_ai_signup = ttk.Button(adv, text="取得金鑰", command=self._open_ai_signup)
        self.btn_ai_signup.grid(row=2, column=3, padx=px(6, 0))

        ttk.Label(adv, text="模型", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=px(3))
        self.var_ai_model = tk.StringVar()
        self.combo_ai_model = ttk.Combobox(adv, textvariable=self.var_ai_model)
        self.combo_ai_model.grid(row=3, column=1, sticky="ew", padx=px(12, 0), pady=px(3))
        self.btn_ai_models = ttk.Button(adv, text="抓模型清單", command=self.fetch_ai_models)
        self.btn_ai_models.grid(row=3, column=2, columnspan=2, sticky="w", padx=px(6, 0))

        # API 網址只有「自訂」服務要自己填，其他服務藏起來（見 _on_ai_provider_changed）
        self.label_ai_base = ttk.Label(adv, text="API 網址", style="Card.TLabel")
        self.label_ai_base.grid(row=4, column=0, sticky="w", pady=px(3))
        self.var_ai_base = tk.StringVar()
        self.entry_ai_base = ttk.Entry(adv, textvariable=self.var_ai_base)
        self.entry_ai_base.grid(row=4, column=1, sticky="ew", padx=px(12, 0), pady=px(3))

        ttk.Label(adv, text="提問上限", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=px(3))
        limit_row = ttk.Frame(adv, style="Card.TFrame")
        limit_row.grid(row=5, column=1, columnspan=3, sticky="w", padx=px(12, 0), pady=px(3))
        self.var_ai_limit = tk.IntVar(value=int(self.config_data.get("ai_user_limit", 5) or 0))
        ttk.Spinbox(limit_row, from_=0, to=60, width=5, textvariable=self.var_ai_limit).pack(side="left")
        ttk.Label(limit_row, text="每個人每分鐘最多問幾次（0 = 不限），免得有人一直洗、把額度用光",
                  style="CardMuted.TLabel").pack(side="left", padx=px(8, 0))

        buttons = ttk.Frame(adv, style="Card.TFrame")
        buttons.grid(row=6, column=0, columnspan=4, sticky="w", pady=px(10, 0))
        self.btn_ai_test = ttk.Button(buttons, text="測試連線", command=lambda: self._run_ai_test(save_after=False))
        self.btn_ai_test.pack(side="left")
        ttk.Button(buttons, text="✔ 儲存並套用", style="Accent.TButton",
                   command=self.save_ai_settings).pack(side="left", padx=px(6))

        ttk.Label(frame, text="⚠ 金鑰要保密：把資料夾分享給別人之前，記得先刪掉 token.txt 和 ai_keys.json。",
                  style="Warn.TLabel").pack(side="bottom", anchor="w", pady=px(8, 0))

        current = self.config_data.get("ai_provider") or "ollama"
        self._select_ai_provider(current if current in AI_PROVIDERS else "ollama")
        self._update_ai_current_label()
        # 正在用上面三張卡片以外的服務（例如 DeepSeek、ChatGPT）的人，進階設定直接打開
        self.adv_open = False
        self._toggle_ai_advanced(open_it=current not in ("free", "gemini", "ollama"))

    def _toggle_ai_advanced(self, open_it=None):
        self.adv_open = (not self.adv_open) if open_it is None else open_it
        arrow = "▾" if self.adv_open else "▸"
        self.adv_toggle.configure(text=f"{arrow} 進階設定：換其他 AI 服務（DeepSeek、ChatGPT、Claude…）、自己挑模型")
        if self.adv_open:
            self.adv_card.pack(fill="x", pady=px(8, 0), after=self.adv_toggle)
        else:
            self.adv_card.pack_forget()

    def _ai_selected_code(self):
        index = self.combo_ai_provider.current()
        return self.ai_provider_codes[index] if index >= 0 else "ollama"

    def _select_ai_provider(self, code):
        self.combo_ai_provider.current(self.ai_provider_codes.index(code))
        self._on_ai_provider_changed()

    def _on_ai_provider_changed(self):
        # 切換前先把剛剛打的金鑰暫存起來，切回來還在（按「儲存並套用」才會寫進檔案）
        if self.ai_current_code:
            self.ai_keys[self.ai_current_code] = self.var_ai_key.get().strip()
        code = self._ai_selected_code()
        self.ai_current_code = code
        name, style, base, default_model, signup, note = AI_PROVIDERS[code]
        self.label_ai_note.configure(text=note)
        online = style != "ollama"
        needs_key = online and code != "free"
        self.var_ai_key.set(self.ai_keys.get(code, "") if needs_key or code == "custom" else "")
        self.entry_ai_key.configure(state="normal" if needs_key or code == "custom" else "disabled")
        self.btn_ai_signup.configure(state="normal" if signup else "disabled")
        saved_model = (self.config_data.get("online_models") or {}).get(code)
        if code == "ollama":
            self.var_ai_model.set(self.config_data.get("model_name") or DEFAULT_CONFIG["model_name"])
        else:
            self.var_ai_model.set(saved_model or default_model)
        self.combo_ai_model.configure(state="disabled" if code == "ollama" else "normal", values=())
        self.btn_ai_models.configure(state="normal" if online else "disabled")
        self.var_ai_base.set((self.config_data.get("custom_base_url") or "") if code == "custom" else base)
        for widget in (self.label_ai_base, self.entry_ai_base):
            if code == "custom":
                widget.grid()
            else:
                widget.grid_remove()
        self.btn_ai_test.configure(state="normal" if online else "disabled")

    def _update_ai_current_label(self):
        code = self.config_data.get("ai_provider") or "ollama"
        code = code if code in AI_PROVIDERS else "ollama"
        if code == "ollama":
            model = self.config_data.get("model_name") or DEFAULT_CONFIG["model_name"]
        else:
            model = (self.config_data.get("online_models") or {}).get(code) or AI_PROVIDERS[code][3]
        state = "" if self.var_ai.get() else "（AI 聊天目前是關閉的，到「主控台」打開）"
        self.label_ai_current.configure(text=f"目前使用：{AI_PROVIDERS[code][0]}　·　{model}{state}")
        self._refresh_ai_cards()

    def _refresh_ai_cards(self):
        """正在用的方案，卡片外框變藍色、按鈕變成白色的「✔ 使用中」；其他方案的按鈕是藍色，按了就換過去。"""
        current = self.config_data.get("ai_provider") or "ollama"
        for code, card in self.ai_card_frames.items():
            card.configure(highlightbackground=ACCENT if code == current else BORDER)
        for code, (button, text, action) in self.ai_card_buttons.items():
            if code == current:
                name = AI_PROVIDERS[code][0]
                button.configure(text="✔ 使用中", style="Compact.TButton",
                                 command=lambda n=name: self.log(f"目前已經在用「{n}」了。", "info"))
            else:
                # 存過 Gemini 金鑰的話，按一下就換回去，不用再貼一次
                if code == "gemini" and (self.ai_keys.get("gemini") or "").strip():
                    text = "使用這個"
                button.configure(text=text, style="Compact.Accent.TButton", command=action)

    def _toggle_ai_key(self):
        showing = not self.entry_ai_key.cget("show")
        self.entry_ai_key.configure(show="•" if showing else "")
        self.btn_ai_key_show.configure(text="顯示" if showing else "隱藏")

    def _open_ai_signup(self):
        url = AI_PROVIDERS[self._ai_selected_code()][4]
        if url:
            webbrowser.open(url)

    def _ai_form(self):
        code = self._ai_selected_code()
        return code, self.var_ai_base.get().strip().rstrip("/"), self.var_ai_key.get().strip(), self.var_ai_model.get().strip()

    def _check_ai_form(self, code, base, key, model):
        if code == "ollama":
            return True
        name = AI_PROVIDERS[code][0]
        if code not in ("free", "custom") and not key:
            messagebox.showwarning("還沒填金鑰", f"要先填 {name} 的 API 金鑰。\n按「取得金鑰」可以打開申請頁面。")
            return False
        if code == "custom" and not base.startswith("http"):
            messagebox.showwarning("還沒填網址", "自訂服務要填 API 網址，例如 http://localhost:1234/v1")
            return False
        if not model:
            messagebox.showwarning("還沒選模型", "要填模型名稱，或按「抓模型清單」選一個。")
            return False
        return True

    def fetch_ai_models(self):
        code, base, key, model = self._ai_form()
        if code not in ("free", "custom") and not key:
            messagebox.showwarning("還沒填金鑰", "抓模型清單要先填 API 金鑰。")
            return
        self.btn_ai_models.configure(state="disabled", text="抓取中…")

        def worker():
            try:
                models = fetch_online_models(code, base, key)
                error = None
            except Exception as e:
                models, error = [], str(e)

            def done():
                self.btn_ai_models.configure(state="normal", text="抓模型清單")
                if error:
                    messagebox.showerror("抓不到模型清單", error)
                    return
                self.combo_ai_model.configure(values=models)
                self.log(f"{AI_PROVIDERS[code][0]} 有 {len(models)} 個模型可以選，點「模型」欄的下拉選單挑一個。", "good")
                if models and self.var_ai_model.get().strip() not in models:
                    messagebox.showinfo("模型清單", f"找到 {len(models)} 個模型。\n目前填的「{self.var_ai_model.get()}」不在清單裡，"
                                                     "請從「模型」欄的下拉選單重新挑一個。")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _run_ai_test(self, save_after):
        code, base, key, model = self._ai_form()
        if not self._check_ai_form(code, base, key, model):
            return
        self.btn_ai_test.configure(state="disabled", text="測試中…")
        self.log(f"正在測試 {AI_PROVIDERS[code][0]}（{model}）…" + ("免費服務可能要等幾十秒。" if code == "free" else ""), "info")

        def worker():
            try:
                answer, error = test_online_ai(code, base, key, model), None
            except Exception as e:
                answer, error = None, str(e)

            def done():
                self.btn_ai_test.configure(state="normal", text="測試連線")
                if error:
                    self.log(f"測試失敗：{error}", "error")
                    messagebox.showerror("連線失敗", f"{AI_PROVIDERS[code][0]} 連線失敗：\n\n{error}")
                    return
                self.log(f"測試成功！{AI_PROVIDERS[code][0]} 回答：{answer.strip()[:60]}", "good")
                if save_after:
                    self.save_ai_settings(skip_check=True)
                else:
                    messagebox.showinfo("連線成功", f"連線成功！模型回答：\n\n{answer.strip()[:200]}\n\n記得按「儲存並套用」。")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def save_ai_settings(self, skip_check=False):
        code, base, key, model = self._ai_form()
        if not skip_check and not self._check_ai_form(code, base, key, model):
            return
        try:
            limit = max(0, min(60, int(self.var_ai_limit.get())))
        except (tk.TclError, ValueError):
            limit = 5
        self.ai_keys[code] = key if code not in ("ollama", "free") else ""
        self.config_data["ai_provider"] = code
        self.config_data["ai_user_limit"] = limit
        if code != "ollama":
            self.config_data.setdefault("online_models", {})[code] = model
        if code == "custom":
            self.config_data["custom_base_url"] = base
        try:
            save_ai_keys(self.ai_keys)
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        self._update_ai_widgets()
        self.log(f"AI 來源改成：{AI_PROVIDERS[code][0]}" + (f"（{model}）" if code != "ollama" else ""), "good")

        def worker():
            if not bot_lock_in_use():
                self.log("AI 來源已儲存，下次啟動機器人時生效。", "info")
            elif send_bot_command(b"ai_reload"):
                self.log("已通知機器人改用新的 AI 來源，立刻生效。", "good")
            else:
                self.log("機器人沒有回應（可能是舊版），要重新啟動機器人才會改用新的 AI 來源。", "error")

        threading.Thread(target=worker, daemon=True).start()
        if not self.var_ai.get() and messagebox.askyesno("打開 AI 聊天？", "AI 聊天現在是關閉的，要順便打開嗎？"):
            self.toggle_ai()

    def quick_use_free(self):
        self._select_ai_provider("free")
        self.var_ai_model.set(AI_PROVIDERS["free"][3])
        self.save_ai_settings()

    def quick_use_gemini(self):
        try:
            key = self.clipboard_get().strip()
        except tk.TclError:
            key = ""
        saved = (self.ai_keys.get("gemini") or "").strip()
        # 以前存過金鑰的話直接換回 Gemini，不用再去拿一次；剪貼簿裡有一把新的 Gemini 金鑰（AIza 開頭）才換新的
        if saved and not (key.startswith("AIza") and key != saved):
            self._select_ai_provider("gemini")
            self.var_ai_key.set(saved)
            self.var_ai_model.set((self.config_data.get("online_models") or {}).get("gemini") or AI_PROVIDERS["gemini"][3])
            self.save_ai_settings()
            return
        if len(key) < 20 or " " in key or "\n" in key:
            messagebox.showwarning("剪貼簿裡沒有金鑰",
                                   "剪貼簿裡看起來不是 Gemini 金鑰。\n\n請先按「取得金鑰」，在 Google AI Studio 按「Create API key」，"
                                   "再按金鑰旁邊的複製按鈕，然後回來按「貼上並使用」。")
            return
        self._select_ai_provider("gemini")
        self.var_ai_key.set(key)
        self.var_ai_model.set(AI_PROVIDERS["gemini"][3])
        # 先測試金鑰能不能用，成功才存，免得存了一把壞掉的金鑰
        self._run_ai_test(save_after=True)

    def quick_use_ollama(self):
        self._select_ai_provider("ollama")
        self.save_ai_settings()
        self.log("本機 Ollama 要先在「本機模型」分頁下載模型，再按「使用這個模型」。", "info")
        self.notebook.select(self.tab_models)  # 直接帶去下載模型的那一頁

    # ---------- 知識庫分頁 ----------

    def _build_knowledge_tab(self):
        frame = self.tab_knowledge
        knowledge.ensure_folder(KNOWLEDGE_DIR)
        self.panel_kb = knowledge.KnowledgeBase(KNOWLEDGE_DIR)
        self.kb_loading = False
        self.kb_scope_ids = [None]
        ttk.Label(frame, text="把文件丟進知識庫資料夾就好，Word、Excel、PowerPoint、PDF、txt 都可以。\n"
                              "改了檔案不用重開本魚，她大約 20 秒內就會讀到新的內容。",
                  style="Muted.TLabel", justify="left").pack(anchor="w")

        top = ttk.Frame(frame)
        top.pack(fill="x", pady=px(10, 0))
        ttk.Button(top, text="📂 開啟知識庫資料夾", style="Accent.TButton",
                   command=self.open_knowledge_folder).pack(side="left")
        ttk.Button(top, text="＋ 加入檔案…", command=self.add_knowledge_files).pack(side="left", padx=px(6))
        ttk.Button(top, text="⟳ 重新讀取", command=self.refresh_knowledge_page).pack(side="left")
        self.var_knowledge = tk.BooleanVar(value=bool(self.config_data.get("knowledge_enabled", True)))
        ttk.Checkbutton(top, text="啟用知識庫（AI 聊天時自動參考）", variable=self.var_knowledge,
                        command=self.toggle_knowledge).pack(side="right")

        self.label_kb_summary = ttk.Label(frame, font=(FONT, 10, "bold"))
        self.label_kb_summary.pack(anchor="w", pady=px(10, 6))

        # 下面兩個區塊先放（side=bottom），視窗矮的時候被壓縮的是檔案表格，不是按鈕
        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=px(10, 0))
        bottom.columnconfigure(0, weight=1, uniform="kb_bottom")
        bottom.columnconfigure(1, weight=1, uniform="kb_bottom")

        scope_card, scope_box, _ = self._card(
            bottom, "🔒 伺服器專用資料", "放進伺服器專用資料夾的檔案，只有那個伺服器能用；\n其他檔案所有伺服器（和私訊）都能用。")
        scope_card.grid(row=0, column=0, sticky="nsew", padx=px(0, 6))
        scope_row = ttk.Frame(scope_box, style="Card.TFrame")
        scope_row.pack(fill="x", pady=px(10, 0))
        self.combo_kb_scope = ttk.Combobox(scope_row, state="readonly", width=24)
        self.combo_kb_scope.pack(side="left", fill="x", expand=True)
        ttk.Button(scope_row, text="建立資料夾", style="Compact.TButton",
                   command=self.create_server_knowledge_folder).pack(side="left", padx=px(6, 0))

        test_card, test_box, _ = self._card(bottom, "🔍 試試看：本魚會找到哪些資料？")
        test_card.grid(row=0, column=1, sticky="nsew", padx=px(6, 0))
        test_row = ttk.Frame(test_box, style="Card.TFrame")
        test_row.pack(fill="x", pady=px(10, 0))
        self.var_kb_query = tk.StringVar()
        entry = ttk.Entry(test_row, textvariable=self.var_kb_query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.run_knowledge_test())
        ttk.Button(test_row, text="搜尋", style="Compact.Accent.TButton",
                   command=self.run_knowledge_test).pack(side="left", padx=px(6, 0))
        self.text_kb_result = tk.Text(test_box, height=4, wrap="word", relief="flat", borderwidth=0,
                                      background=LOG_BG, foreground=TEXT, font=(FONT, 9), padx=px(8), pady=px(6),
                                      highlightthickness=0, state="disabled")
        self.text_kb_result.pack(fill="both", expand=True, pady=px(8, 0))
        self.text_kb_result.tag_configure("file", foreground=ACCENT_DARK, font=(FONT, 9, "bold"))
        self.text_kb_result.tag_configure("muted", foreground=MUTED)
        self._set_kb_result([("輸入一個問題按「搜尋」，看看本魚會參考哪些段落（例如：伺服器規則是什麼）", "muted")])

        table_box = ttk.Frame(frame)
        table_box.pack(fill="both", expand=True)
        columns = ("kind", "size", "scope", "state")
        self.kb_table = ttk.Treeview(table_box, columns=columns, show="tree headings", selectmode="browse", height=4)
        self.kb_table.heading("#0", text="檔案（雙擊打開）")
        self.kb_table.column("#0", width=px(240), minwidth=px(120), stretch=True)
        for col, title, width, stretch in (("kind", "格式", 60, False), ("size", "大小", 80, False),
                                           ("scope", "可用範圍", 150, False), ("state", "狀態", 220, True)):
            self.kb_table.heading(col, text=title)
            self.kb_table.column(col, width=px(width), anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.kb_table.yview)
        self.kb_table.configure(yscrollcommand=scroll.set)
        self.kb_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.kb_table.tag_configure("stripe", background=ROW_STRIPE)
        self.kb_table.tag_configure("error", foreground=DANGER)
        self.kb_table.tag_configure("skipped", foreground="#b26a00")
        self.kb_table.bind("<Double-1>", self._open_selected_knowledge_file)

    def open_knowledge_folder(self):
        knowledge.ensure_folder(KNOWLEDGE_DIR)
        open_path(KNOWLEDGE_DIR)

    def _scope_name(self, scope):
        for g in self.guild_snapshot.get("guilds") or []:
            if str(g.get("id")) == scope:
                return g.get("name", scope)
        return f"伺服器 {scope}"

    def refresh_knowledge_page(self):
        guilds = self.guild_snapshot.get("guilds") or []
        current = self.combo_kb_scope.current()
        self.kb_scope_ids = [None] + [str(g.get("id")) for g in guilds]
        self.combo_kb_scope.configure(values=["所有伺服器共用（私訊也能用）"] + [g.get("name", "?") for g in guilds])
        self.combo_kb_scope.current(current if 0 <= current < len(self.kb_scope_ids) else 0)
        if self.kb_loading:
            return
        self.kb_loading = True
        self.label_kb_summary.configure(text="讀取中…")

        def worker():
            try:
                self.panel_kb.refresh()
                error = None
            except Exception as e:
                error = str(e)
            self.after(0, lambda: self._fill_knowledge_table(error))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_knowledge_table(self, error=None):
        self.kb_loading = False
        table = self.kb_table
        table.delete(*table.get_children())
        if error:
            self.label_kb_summary.configure(text=f"讀取知識庫失敗：{error}")
            return
        files = self.panel_kb.files(include_all_scopes=True)
        problems = 0
        for index, (rel, doc) in enumerate(files):
            if doc.get("skipped"):
                state, tag = knowledge.TOO_MUCH_TEXT, "skipped"
            elif doc["error"]:
                state, tag = doc["error"], "error"
            else:
                state, tag = f"✔ 可以用（{len(doc['chunks'])} 段）", ""
            problems += bool(tag)
            scope = f"🔒 {self._scope_name(doc['scope'])}" if doc["scope"] else "所有伺服器"
            tags = tuple(t for t in (tag, "stripe" if index % 2 else "") if t)
            table.insert("", "end", iid=rel, text=rel, tags=tags,
                         values=(doc["ext"].lstrip(".").upper(), format_size(doc["size"]), scope, state))
        s = self.panel_kb.stats()
        if not files:
            text = "資料夾裡還沒有資料：按「開啟知識庫資料夾」把文件放進去，或按「加入檔案…」"
        else:
            text = f"{s['usable']} 個檔案可以用，共 {s['chunks']} 段、約 {s['chars']:,} 字"
            if problems:
                text += f"　·　{problems} 個檔案讀不到，看下面的「狀態」"
        if not self.var_knowledge.get():
            text += "　（知識庫目前關閉中）"
        self.label_kb_summary.configure(text=text)

    def _open_selected_knowledge_file(self, event=None):
        selection = self.kb_table.selection()
        if selection:
            path = os.path.join(KNOWLEDGE_DIR, *selection[0].split("/"))
            if os.path.exists(path):
                open_path(path)

    def _notify_knowledge_reload(self, message):
        def worker():
            if bot_lock_in_use() and send_bot_command(b"knowledge_reload"):
                self.log(message + "，已通知機器人馬上重新讀取。", "good")
            else:
                self.log(message + "，機器人下次啟動（或 20 秒內）就會讀到。", "info")

        threading.Thread(target=worker, daemon=True).start()

    def add_knowledge_files(self):
        exts = " ".join(f"*{e}" for e in sorted(knowledge.SUPPORTED_EXTS))
        paths = filedialog.askopenfilenames(title="選擇要加入知識庫的檔案",
                                            filetypes=[("知識庫支援的檔案", exts), ("所有檔案", "*.*")])
        if not paths:
            return
        knowledge.ensure_folder(KNOWLEDGE_DIR)
        added = []
        for path in paths:
            target = os.path.join(KNOWLEDGE_DIR, os.path.basename(path))
            if os.path.abspath(path) == os.path.abspath(target):
                continue
            if os.path.exists(target) and not messagebox.askyesno(
                    "檔案已經存在", f"知識庫裡已經有「{os.path.basename(path)}」了，要用新的蓋掉嗎？"):
                continue
            try:
                shutil.copy2(path, target)
                added.append(os.path.basename(path))
            except OSError as e:
                messagebox.showerror("加入失敗", f"「{os.path.basename(path)}」複製失敗：{e}")
        if added:
            self.refresh_knowledge_page()
            self._notify_knowledge_reload(f"已加入知識庫：{'、'.join(added)}")

    def create_server_knowledge_folder(self):
        index = self.combo_kb_scope.current()
        scope = self.kb_scope_ids[index] if 0 <= index < len(self.kb_scope_ids) else None
        if not scope:
            messagebox.showinfo("先選伺服器", "先在左邊的選單選一個伺服器（伺服器清單要啟動過機器人才會有）。")
            return
        # 資料夾名稱裡的 [伺服器ID] 才是重點，名字只是方便辨認；去掉 Windows 檔名不能用的字
        name = "".join(ch for ch in self._scope_name(scope) if ch not in '<>:"/\\|?*').strip(" .") or "伺服器"
        existing = next((d for d in os.listdir(KNOWLEDGE_DIR) if f"[{scope}]" in d
                         and os.path.isdir(os.path.join(KNOWLEDGE_DIR, d))), None)
        folder = os.path.join(KNOWLEDGE_DIR, existing or f"{name} [{scope}]")
        os.makedirs(folder, exist_ok=True)
        open_path(folder)
        self.log(f"「{name}」專用的知識庫資料夾：{folder}（放進去的檔案只有那個伺服器能用）", "info")

    def toggle_knowledge(self):
        enabled = bool(self.var_knowledge.get())
        self.config_data["knowledge_enabled"] = enabled
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        self._notify_knowledge_reload(f"知識庫已{'開啟' if enabled else '關閉'}")
        self.refresh_knowledge_page()

    def _set_kb_result(self, parts):
        self.text_kb_result.configure(state="normal")
        self.text_kb_result.delete("1.0", "end")
        for text, tag in parts:
            self.text_kb_result.insert("end", text, tag)
        self.text_kb_result.configure(state="disabled")

    def run_knowledge_test(self):
        query = self.var_kb_query.get().strip()
        if not query:
            return
        index = self.combo_kb_scope.current()
        scope = self.kb_scope_ids[index] if 0 <= index < len(self.kb_scope_ids) else None
        self._set_kb_result([("搜尋中…", "muted")])

        def worker():
            try:
                self.panel_kb.refresh_if_stale(5)
                hits = self.panel_kb.search(query, scope)
            except Exception as e:
                self.after(0, lambda: self._set_kb_result([(f"搜尋失敗：{e}", "muted")]))
                return

            def show():
                if not hits:
                    self._set_kb_result([("找不到相關的資料。本魚回答這個問題時不會參考知識庫。", "muted")])
                    return
                parts = []
                for i, hit in enumerate(hits, 1):
                    where = hit["file"] + (f" › {hit['head']}" if hit["head"] else "")
                    strong = "，很相關" if i == 1 and knowledge.is_strong(hits) else ""
                    parts.append((f"{i}. {where}", "file"))
                    parts.append((f"（涵蓋 {hit['coverage']:.0%}{strong}）\n", "muted"))
                    # 每一行用「／」隔開，表格的每一列才分得出來
                    snippet = " ／ ".join(" ".join(line.split()) for line in hit["text"].splitlines() if line.strip())
                    parts.append((snippet[:140] + ("…" if len(snippet) > 140 else "") + "\n", ""))
                self._set_kb_result(parts)

            self.after(0, show)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 頻道功能分頁 ----------

    def _build_channels_tab(self):
        frame = self.tab_channels
        self.channel_guild_id = None
        self.channel_guild_ids = []
        ttk.Label(
            frame,
            text="點表格裡的 ✔／✕ 就能開關，點分類那一列會整個分類一起切換，改了馬上生效。\n"
                 "管理指令（踢人、警告…）不受影響，不怕把自己鎖在外面。",
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w")

        top = ttk.Frame(frame)
        top.pack(fill="x", pady=px(8, 6))
        ttk.Label(top, text="伺服器：").pack(side="left")
        self.combo_channel_guild = ttk.Combobox(top, state="readonly", width=40)
        self.combo_channel_guild.pack(side="left")
        self.combo_channel_guild.bind("<<ComboboxSelected>>", self._on_channel_guild_selected)
        self.label_channel_info = ttk.Label(top, style="Muted.TLabel")
        self.label_channel_info.pack(side="left", padx=px(12, 0))

        table_box = ttk.Frame(frame)
        table_box.pack(fill="both", expand=True)
        # 頻道名稱前面的 # 和 🔊 已經看得出是文字還是語音頻道，不用再多一欄「類型」
        columns = tuple(key for key, _ in CHANNEL_FEATURES)
        self.channel_table = ttk.Treeview(table_box, columns=columns, show="tree headings",
                                          selectmode="extended", height=12)
        self.channel_table.heading("#0", text="頻道")
        self.channel_table.column("#0", width=px(200), minwidth=px(130), stretch=True)
        for key, label in CHANNEL_FEATURES:
            self.channel_table.heading(key, text=label)
            self.channel_table.column(key, width=px(80), anchor="center", stretch=False)
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.channel_table.yview)
        # 功能欄位比較多，視窗窄的時候可以左右捲動，不會有欄位被擠出畫面
        xscroll = ttk.Scrollbar(table_box, orient="horizontal", command=self.channel_table.xview)
        self.channel_table.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        table_box.rowconfigure(0, weight=1)
        table_box.columnconfigure(0, weight=1)
        self.channel_table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.channel_table.tag_configure("stripe", background=ROW_STRIPE)
        self.channel_table.tag_configure("category", font=(FONT, 10, "bold"), background="#e8eef7")
        self.channel_table.tag_configure("limited", foreground="#b26a00")
        self.channel_table.bind("<ButtonRelease-1>", self._on_channel_click)

        # 按鈕和小提示排在表格前面（before=table_box），視窗矮的時候被壓縮的是表格，按鈕一定看得到
        ttk.Label(frame, text="小提示：按住 Ctrl 或 Shift 可以一次選好幾個頻道。新開的頻道預設全部開啟。",
                  style="Muted.TLabel").pack(side="bottom", anchor="w", pady=px(6, 0), before=table_box)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=px(8, 0), before=table_box)
        ttk.Button(buttons, text="✔ 選取的頻道全部開啟", style="Accent.TButton",
                   command=lambda: self._set_selected_channels(True)).pack(side="left")
        ttk.Button(buttons, text="✕ 選取的頻道全部關閉",
                   command=lambda: self._set_selected_channels(False)).pack(side="left", padx=px(6))
        ttk.Button(buttons, text="這個伺服器全部還原成開啟", command=self._reset_guild_channels).pack(side="left")
        ttk.Button(buttons, text="重新整理", command=self.refresh_guild_table).pack(side="right")

    def _channel_disabled(self):
        raw = self.config_data.get("channel_features") or {}
        return {str(k): set(v) for k, v in raw.items() if isinstance(v, list)}

    def _on_channel_guild_selected(self, event=None):
        index = self.combo_channel_guild.current()
        if 0 <= index < len(self.channel_guild_ids):
            self.channel_guild_id = self.channel_guild_ids[index]
        self.refresh_channel_table()

    def _selected_guild_channels(self):
        for g in self.guild_snapshot.get("guilds") or []:
            if str(g.get("id")) == self.channel_guild_id:
                return g.get("channels")
        return None

    def refresh_channel_table(self):
        guilds = self.guild_snapshot.get("guilds") or []
        self.channel_guild_ids = [str(g.get("id")) for g in guilds]
        self.combo_channel_guild.configure(values=[g.get("name", "?") for g in guilds])
        if self.channel_guild_id not in self.channel_guild_ids:
            self.channel_guild_id = self.channel_guild_ids[0] if guilds else None
        if self.channel_guild_id:
            self.combo_channel_guild.current(self.channel_guild_ids.index(self.channel_guild_id))
        else:
            self.combo_channel_guild.set("")

        table = self.channel_table
        selected = set(table.selection())
        closed = {iid for iid in table.get_children() if table.get_children(iid) and not table.item(iid, "open")}
        table.delete(*table.get_children())

        channels = self._selected_guild_channels()
        if not guilds:
            self.label_channel_info.configure(text="還沒有伺服器清單，先啟動機器人一次。")
            return
        if channels is None:
            self.label_channel_info.configure(text="還沒有頻道清單。用新版機器人啟動一次，頻道就會出現在這裡。")
            return

        disabled = self._channel_disabled()
        limited_count = 0
        categories = {}
        for index, ch in enumerate(channels):
            cid = str(ch.get("id"))
            off = disabled.get(cid, set())
            limited_count += bool(off)
            parent = ""
            cat_name = ch.get("category") or ""
            if cat_name:
                parent = categories.get(cat_name)
                if parent is None:
                    parent = f"cat:{len(categories)}:{cat_name}"
                    categories[cat_name] = parent
                    table.insert("", "end", iid=parent, text=f"📁 {cat_name}", open=parent not in closed,
                                 tags=("category",))
            prefix = "🔊 " if ch.get("type") in ("語音", "舞台") else "# "
            table.insert(parent, "end", iid=cid, text=prefix + ch.get("name", "?"),
                         values=tuple("✕" if key in off else "✔" for key, _ in CHANNEL_FEATURES),
                         tags=(("limited",) if off else ()) + (("stripe",) if index % 2 else ()))

        # 分類那一列顯示整個分類的狀態：全開 ✔、全關 ✕、有開有關 ◐
        for parent in categories.values():
            kids = table.get_children(parent)
            marks = []
            for key, _ in CHANNEL_FEATURES:
                offs = sum(1 for kid in kids if key in disabled.get(kid, set()))
                marks.append("✔" if offs == 0 else "✕" if offs == len(kids) else "◐")
            table.item(parent, values=tuple(marks))

        keep = [i for i in selected if table.exists(i)]
        if keep:
            table.selection_set(keep)
        info = f"{len(channels)} 個頻道"
        info += f"，其中 {limited_count} 個有關掉的功能（橘色）" if limited_count else "，全部功能都開著"
        self.label_channel_info.configure(text=info)

    def _expand_channel_rows(self, rows):
        ids = []
        for row in rows:
            kids = self.channel_table.get_children(row)
            for cid in (kids if row.startswith("cat:") else (row,)):
                if cid not in ids:
                    ids.append(cid)
        return ids

    def _on_channel_click(self, event):
        table = self.channel_table
        if table.identify_region(event.x, event.y) != "cell":
            return
        row = table.identify_row(event.y)
        column = table.identify_column(event.x)
        if not row or column == "#0":
            return
        key = table["columns"][int(column[1:]) - 1]
        ids = self._expand_channel_rows([row])
        if not ids:
            return
        disabled = self._channel_disabled()
        # 裡面只要有一個是關的就全部打開，全部都開著才全部關掉
        enable = any(key in disabled.get(cid, set()) for cid in ids)
        self._save_channel_features(ids, {key: enable})

    def _set_selected_channels(self, enabled):
        ids = self._expand_channel_rows(self.channel_table.selection())
        if not ids:
            messagebox.showinfo("還沒選頻道", "先在表格裡點選要設定的頻道（按住 Ctrl 可以選好幾個）。")
            return
        self._save_channel_features(ids, {key: enabled for key, _ in CHANNEL_FEATURES})

    def _reset_guild_channels(self):
        channels = self._selected_guild_channels() or []
        ids = [str(ch.get("id")) for ch in channels]
        disabled = self._channel_disabled()
        if not any(disabled.get(cid) for cid in ids):
            self.log("這個伺服器的頻道本來就全部開著。", "info")
            return
        if messagebox.askyesno("全部還原", "要把這個伺服器所有頻道的功能都打開嗎？"):
            self._save_channel_features(ids, {key: True for key, _ in CHANNEL_FEATURES})

    def _save_channel_features(self, channel_ids, changes):
        data = {k: sorted(v) for k, v in self._channel_disabled().items()}
        for cid in channel_ids:
            off = set(data.get(cid, []))
            for key, enabled in changes.items():
                if enabled:
                    off.discard(key)
                else:
                    off.add(key)
            if off:
                data[cid] = sorted(off)
            else:
                data.pop(cid, None)
        self.config_data["channel_features"] = data
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        labels = dict(CHANNEL_FEATURES)
        names = [self.channel_table.item(cid, "text").lstrip("#🔊 ") for cid in channel_ids if self.channel_table.exists(cid)]
        shown = "、".join(names[:5]) + (f" 等 {len(names)} 個頻道" if len(names) > 5 else "")
        what = "、".join(f"{'開啟' if on else '關閉'}「{labels[k]}」" for k, on in changes.items())
        if len(changes) == len(CHANNEL_FEATURES):
            what = "全部功能" + ("開啟" if all(changes.values()) else "關閉")
        self.log(f"{shown}：{what}", "info")
        self.refresh_channel_table()

        def worker():
            if not bot_lock_in_use():
                self.log("頻道功能設定已儲存，下次啟動機器人時生效。", "info")
            elif send_bot_command(b"channels_reload"):
                self.log("已通知機器人套用新的頻道功能設定。", "good")
            else:
                self.log("機器人沒有回應（可能是舊版），頻道功能設定要重新啟動機器人後才會生效。", "error")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 模型分頁 ----------

    def _build_models_tab(self):
        frame = self.tab_models
        top = ttk.Frame(frame)
        top.pack(fill="x")
        self.label_gpu = ttk.Label(top, text="顯示卡：偵測中…")
        self.label_gpu.pack(side="left")
        self.label_current_model = ttk.Label(top, font=(FONT, 10, "bold"))
        self.label_current_model.pack(side="right")

        ttk.Label(
            frame,
            text="選一個模型按「下載」，下載好再按「使用這個模型」就換過去了。\n"
                 "「你的電腦」那一欄會告訴你顯示卡跑不跑得動，✅ 的都可以放心選。",
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=px(6, 8))

        columns = ("name", "size", "need", "fit", "think", "status", "note")
        table_box = ttk.Frame(frame)
        table_box.pack(fill="both", expand=True)
        self.model_table = ttk.Treeview(table_box, columns=columns, show="headings", selectmode="browse", height=8)
        headings = {
            "name": ("模型", 150), "size": ("下載大小", 80), "need": ("需要顯示卡", 90),
            "fit": ("你的電腦", 120), "think": ("深度思考", 70), "status": ("狀態", 90), "note": ("說明", 280),
        }
        for col, (title, width) in headings.items():
            self.model_table.heading(col, text=title)
            self.model_table.column(col, width=px(width), anchor="w", stretch=(col == "note"))
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.model_table.yview)
        self.model_table.configure(yscrollcommand=scroll.set)
        self.model_table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.model_table.tag_configure("stripe", background=ROW_STRIPE)
        self.model_table.tag_configure("current", foreground=COLOR_ON)
        self.model_table.tag_configure("toobig", foreground="#999999")
        self.model_table.bind("<<TreeviewSelect>>", lambda e: self._update_model_buttons())
        self.model_table.bind("<Double-1>", lambda e: self.use_selected_model())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=px(8, 0))
        self.btn_pull = ttk.Button(buttons, text="⬇ 下載", command=self.pull_selected_model)
        self.btn_use = ttk.Button(buttons, text="✔ 使用這個模型", style="Accent.TButton", command=self.use_selected_model)
        self.btn_delete = ttk.Button(buttons, text="✕ 刪除", style="Danger.TButton", command=self.delete_selected_model)
        self.btn_pull.pack(side="left")
        self.btn_use.pack(side="left", padx=px(6))
        self.btn_delete.pack(side="left")
        ttk.Button(buttons, text="重新整理", command=self.refresh_model_table).pack(side="right")

        custom = ttk.Frame(frame)
        custom.pack(fill="x", pady=px(8, 0))
        ttk.Label(custom, text="其他模型：").pack(side="left")
        self.var_custom_model = tk.StringVar()
        entry = ttk.Entry(custom, textvariable=self.var_custom_model, width=28)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self.pull_custom_model())
        ttk.Button(custom, text="下載", command=self.pull_custom_model).pack(side="left", padx=px(6))
        ttk.Button(custom, text="到 Ollama 模型庫找…", command=lambda: webbrowser.open(OLLAMA_LIBRARY_URL)).pack(side="left")
        ttk.Label(custom, text="（例如 qwen3:14b）", style="Muted.TLabel").pack(side="left", padx=px(6))

        progress_box = ttk.Frame(frame)
        progress_box.pack(fill="x", pady=px(10, 0))
        self.pull_progress = ttk.Progressbar(progress_box, mode="determinate", maximum=1000)
        self.pull_progress.pack(side="left", fill="x", expand=True)
        self.btn_cancel_pull = ttk.Button(progress_box, text="取消下載", command=self.cancel_pull, state="disabled")
        self.btn_cancel_pull.pack(side="left", padx=px(6, 0))
        self.label_pull = ttk.Label(frame, text="", style="Muted.TLabel")
        self.label_pull.pack(anchor="w", pady=px(4, 0))

        self._update_current_model_label()
        self._update_model_buttons()
        threading.Thread(target=self._detect_gpu_worker, daemon=True).start()

    def _detect_gpu_worker(self):
        name, vram = detect_gpu()

        def apply():
            self.gpu_name, self.gpu_vram = name, vram
            if name:
                self.label_gpu.configure(text=f"顯示卡：{name}（{vram:.0f} GB）")
            else:
                self.label_gpu.configure(text="顯示卡：偵測不到 NVIDIA 顯示卡，「你的電腦」欄會顯示「？」")
            self.refresh_model_table()

        self.after(0, apply)

    def _update_current_model_label(self):
        self.label_current_model.configure(text=f"目前使用：{self.config_data.get('model_name') or DEFAULT_CONFIG['model_name']}")

    def _fit_text(self, need_gb):
        if need_gb is None:
            return "", ""
        if not self.gpu_vram:
            return "？", ""
        if need_gb <= self.gpu_vram:
            return "✅ 跑得動", ""
        if need_gb <= self.gpu_vram * 1.3:
            return "⚠️ 會變慢", ""
        return "❌ 太大，會很慢", "toobig"

    def refresh_model_table(self):
        def worker():
            installed = fetch_installed_models()
            self.after(0, lambda: self._fill_model_table(installed))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_model_table(self, installed):
        ollama_up = installed is not None
        self.installed_models = installed or {}
        selected = self._selected_model()
        current = normalize_model_name(self.config_data.get("model_name") or DEFAULT_CONFIG["model_name"])
        installed_norm = {normalize_model_name(n): size for n, size in self.installed_models.items()}

        self.model_table.delete(*self.model_table.get_children())
        rows = []
        known = set()
        for name, size_gb, need_gb, thinking, note in RECOMMENDED_MODELS:
            known.add(normalize_model_name(name))
            rows.append((name, f"{size_gb:.1f} GB", need_gb, "✔" if thinking else "✘", note))
        for name, size in sorted(installed_norm.items()):
            if name not in known:
                gb = size / (1024 ** 3)
                need = round(gb + 2) if size else None
                display = name[:-len(":latest")] if name.endswith(":latest") else name
                rows.append((display, format_size(size) if size else "", need, "", "你自己裝的模型"))

        for index, (name, size_text, need_gb, think_text, note) in enumerate(rows):
            norm = normalize_model_name(name)
            fit, fit_tag = self._fit_text(need_gb)
            if norm == current:
                status = "● 使用中" if norm in installed_norm else "● 使用中（沒裝）"
            elif norm in installed_norm:
                status = "已安裝"
            else:
                status = "未安裝" if ollama_up else "？"
            tags = ("current",) if norm == current else ((fit_tag,) if fit_tag else ())
            tags += ("stripe",) if index % 2 else ()
            need_text = f"約 {need_gb} GB" if need_gb else ""
            self.model_table.insert("", "end", iid=name, values=(name, size_text, need_text, fit, think_text, status, note), tags=tags)

        if selected and self.model_table.exists(selected):
            self.model_table.selection_set(selected)
        if not ollama_up and not self.pulling:
            self.label_pull.configure(text="Ollama 沒有在跑，看不到哪些已經裝好。按「下載」時會自動幫你叫醒它。")
        elif not self.pulling:
            self.label_pull.configure(text="")
        self._update_current_model_label()
        self._update_model_buttons()

    def _selected_model(self):
        sel = self.model_table.selection()
        return sel[0] if sel else None

    def _is_installed(self, name):
        norm = normalize_model_name(name)
        return any(normalize_model_name(n) == norm for n in self.installed_models)

    def _update_model_buttons(self):
        name = self._selected_model()
        installed = bool(name) and self._is_installed(name)
        self.btn_pull.configure(state="normal" if name and not installed and not self.pulling else "disabled")
        self.btn_use.configure(state="normal" if installed else "disabled")
        self.btn_delete.configure(state="normal" if installed and not self.pulling else "disabled")

    def _ensure_ollama_blocking(self, timeout=20):
        if http_alive(OLLAMA_URL):
            return True
        self.log("Ollama 沒在跑，先幫你叫醒它…", "info")
        try:
            subprocess.Popen([ollama_exe(), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.log(f"啟動 Ollama 失敗（可能沒裝或不在 PATH 裡）：{e}", "error")
            return False
        for _ in range(timeout):
            time.sleep(1)
            if http_alive(OLLAMA_URL):
                return True
        self.log("Ollama 一直沒有回應，請自己打開 Ollama 再試一次。", "error")
        return False

    def pull_selected_model(self):
        name = self._selected_model()
        if name:
            self.start_pull(name)

    def pull_custom_model(self):
        name = self.var_custom_model.get().strip()
        if not name:
            return
        if not all(c.isalnum() or c in "._-:/" for c in name):
            messagebox.showerror("名稱不對", "模型名稱只能有英文、數字和 . _ - : / 這些符號。")
            return
        self.start_pull(name)

    def start_pull(self, name):
        if self.pulling:
            messagebox.showinfo("下載中", "已經有一個模型在下載了，等它完成或先取消。")
            return
        need = next((n for m, _, n, _, _ in RECOMMENDED_MODELS if m == name), None)
        if need and self.gpu_vram and need > self.gpu_vram * 1.3:
            if not messagebox.askyesno(
                "模型可能太大",
                f"「{name}」大約需要 {need} GB 顯示卡記憶體，你的顯示卡只有 {self.gpu_vram:.0f} GB。\n"
                "裝得起來，但放不進顯示卡的部分會用 CPU 跑，回答會慢很多。\n\n還是要下載嗎？",
            ):
                return
        self.pulling = True
        self.pull_cancel = False
        self.pull_progress.configure(value=0)
        self.btn_cancel_pull.configure(state="normal")
        self._update_model_buttons()
        self.label_pull.configure(text=f"準備下載 {name}…")
        threading.Thread(target=self._pull_worker, args=(name,), daemon=True).start()

    def cancel_pull(self):
        self.pull_cancel = True
        self.label_pull.configure(text="取消中…（已經下載的部分會留著，下次再按下載會接著下載）")

    def _pull_worker(self, name):
        def ui(fn):
            self.after(0, fn)

        ok, message = False, ""
        try:
            if not self._ensure_ollama_blocking():
                message = "Ollama 沒有在跑，沒辦法下載。"
                return
            self.log(f"開始下載模型 {name}…", "info")
            body = json.dumps({"model": name, "name": name, "stream": True}).encode("utf-8")
            req = urllib.request.Request(f"{OLLAMA_API}/pull", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            layers = {}
            last_bytes, last_time, speed = 0, time.time(), 0.0
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    if self.pull_cancel:
                        message = "已取消下載。"
                        return
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("error"):
                        message = f"下載失敗：{event['error']}"
                        return
                    status = event.get("status", "")
                    if event.get("digest") and event.get("total"):
                        layers[event["digest"]] = (event.get("completed", 0), event["total"])
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    now = time.time()
                    if now - last_time >= 1:
                        speed = (done - last_bytes) / (now - last_time)
                        last_bytes, last_time = done, now
                    if event.get("digest") and total:
                        fraction = done / total
                        text = f"下載 {name}：{format_size(done)} / {format_size(total)}（{fraction * 100:.0f}%）"
                        if speed > 0:
                            remaining = (total - done) / speed
                            text += f"　{format_size(speed)}/秒，大約還要 {int(remaining // 60)} 分 {int(remaining % 60)} 秒"
                        ui(lambda t=text, f=fraction: (self.pull_progress.configure(value=f * 1000), self.label_pull.configure(text=t)))
                    elif status:
                        friendly = {
                            "pulling manifest": "取得模型資訊中…",
                            "verifying sha256 digest": "檢查檔案完整性…",
                            "writing manifest": "收尾中…",
                            "success": "完成！",
                        }.get(status, status)
                        ui(lambda t=f"{name}：{friendly}": self.label_pull.configure(text=t))
                    if status == "success":
                        ok = True
            if not ok:
                message = "下載沒有正常結束，可以再按一次下載接著下載。"
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            message = f"下載失敗：{detail or e}"
            if "not found" in detail.lower() or "does not exist" in detail.lower() or e.code == 404:
                message += "\n找不到這個模型，請確認名稱跟 Ollama 模型庫上的一模一樣。"
        except Exception as e:
            message = f"下載失敗：{e}"
        finally:
            def finish():
                self.pulling = False
                self.btn_cancel_pull.configure(state="disabled")
                if ok:
                    self.pull_progress.configure(value=1000)
                    self.label_pull.configure(text=f"{name} 下載好了！選它再按「使用這個模型」就能換過去。")
                    self.log(f"模型 {name} 下載完成。", "good")
                else:
                    self.pull_progress.configure(value=0)
                    self.label_pull.configure(text=message)
                    self.log(message, "info" if self.pull_cancel else "error")
                self.refresh_model_table()
                self.refresh_models()

            ui(finish)

    def use_selected_model(self):
        name = self._selected_model()
        if not name:
            return
        if not self._is_installed(name):
            if messagebox.askyesno("還沒下載", f"「{name}」還沒下載，要現在下載嗎？"):
                self.start_pull(name)
            return
        self.apply_model_choice(name)

    def apply_model_choice(self, name):
        self.config_data["model_name"] = name
        self.var_model.set(name)
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        self.refresh_model_table()
        self._update_ai_current_label()

        def worker():
            if not bot_lock_in_use():
                self.log(f"已改用模型 {name}，下次啟動機器人時生效。", "good")
                return
            if send_bot_command(f"model:{name}".encode("utf-8")):
                self.log(f"已通知機器人改用 {name}，正在載入新模型（大模型要等十幾秒）。", "good")
            else:
                self.log(f"機器人沒有回應（可能是舊版），要重新啟動機器人才會改用 {name}。", "error")

        threading.Thread(target=worker, daemon=True).start()

    def delete_selected_model(self):
        name = self._selected_model()
        if not name or not self._is_installed(name):
            return
        current = normalize_model_name(self.config_data.get("model_name") or DEFAULT_CONFIG["model_name"])
        warning = ""
        if normalize_model_name(name) == current:
            warning = "\n\n⚠️ 這是本魚現在正在用的模型！刪掉之後要先換一個模型，本魚才能聊天。"
        if not messagebox.askyesno("刪除模型", f"確定要刪除「{name}」嗎？會釋放硬碟空間，之後想用要重新下載。{warning}"):
            return

        def worker():
            try:
                delete_model(name)
                self.log(f"已刪除模型 {name}。", "good")
            except Exception as e:
                self.log(f"刪除模型失敗：{e}", "error")
            self.after(0, self.refresh_model_table)
            self.after(0, self.refresh_models)

        threading.Thread(target=worker, daemon=True).start()

    def log(self, text, tag=None):
        self.log_queue.put((text, tag))

    def _guess_tag(self, line):
        lowered = line.lower()
        if "error" in lowered or "traceback" in lowered or "失敗" in line or "出錯" in line or "exception" in lowered:
            return "error"
        if "成功" in line or "上線" in line or "已經在跑" in line:
            return "good"
        return None

    def _drain_log(self):
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass

        if lines:
            self.log_view.configure(state="normal")
            for text, tag in lines:
                if "目前登入身份" in text:
                    self.bot_online = True
                self.log_view.insert("end", time.strftime("%H:%M:%S  "), "stamp")
                self.log_view.insert("end", f"{text}\n", tag or self._guess_tag(text))
            line_count = int(self.log_view.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                self.log_view.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
            self.log_view.see("end")
            self.log_view.configure(state="disabled")

        self.after(150, self._drain_log)

    def clear_log(self):
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")

    def _pump_output(self, proc, name):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.log(line)
        proc.wait()
        self.log(f"{name}已結束（代碼 {proc.returncode}）", "info")

    def _child_env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def bot_running(self):
        return self.bot_proc is not None and self.bot_proc.poll() is None

    def kill_background_bots(self, then_start=False):
        def worker():
            if self.bot_running():
                self._stop_bot_blocking()
            elif bot_lock_in_use() and request_graceful_shutdown():
                self.log("已請背景的本魚安全關機，等它存檔…", "info")
                wait_for_lock_release(GRACEFUL_STOP_TIMEOUT)
            try:
                killed = kill_all_bots()
            except Exception as e:
                self.log(f"清除背景機器人失敗：{e}", "error")
                return
            if killed:
                self.log(f"已關閉 {len(killed)} 隻背景機器人（PID：{', '.join(killed)}）。", "good")
            else:
                self.log("沒有找到在背景執行的機器人。", "info")
            if then_start:
                time.sleep(2)
                self.after(0, self.start_bot)

        if not then_start and not messagebox.askyesno(
            "強制關閉所有本魚",
            "會把這台電腦上所有正在執行的本魚都關掉（包含這個面板開的）。\n"
            "通常是本魚每句話都回兩次、或是關不掉的時候才需要用。\n確定嗎？",
        ):
            return
        threading.Thread(target=worker, daemon=True).start()

    def start_bot(self):
        if self.bot_running():
            self.log("機器人已經在跑了。", "info")
            return
        if bot_lock_in_use():
            if messagebox.askyesno(
                "已經有一隻在跑了",
                "偵測到已經有一隻本魚在背景執行（不是這個面板開的）。\n"
                "兩隻同時跑會每句話都回兩次、音樂也會卡。\n\n要把它關掉，再由面板重新啟動嗎？",
            ):
                self.kill_background_bots(then_start=True)
            return
        if not os.path.exists(BOT_SCRIPT):
            messagebox.showerror("找不到主程式", f"找不到 {BOT_SCRIPT}\n請確認 deepseek_discord_bot.py 跟面板放在同一個資料夾。")
            return
        if not read_token():
            messagebox.showwarning("還沒填權杖", "請先到「設定」分頁填入 Discord Bot Token 並儲存。")
            self.notebook.select(self.tab_settings)
            return

        self.bot_online = False
        self.log("正在啟動機器人…", "info")
        try:
            self.bot_proc = subprocess.Popen(
                [console_python(), "-u", BOT_SCRIPT],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._child_env(),
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.log(f"啟動失敗：{e}", "error")
            return
        threading.Thread(target=self._pump_output, args=(self.bot_proc, "機器人"), daemon=True).start()

    def _stop_bot_blocking(self):
        proc = self.bot_proc
        if proc is None or proc.poll() is not None:
            return
        # 先請它自己安全關機（會存檔、離開語音頻道），等太久才強制結束
        if request_graceful_shutdown():
            try:
                proc.wait(timeout=GRACEFUL_STOP_TIMEOUT)
                self.bot_online = False
                return
            except subprocess.TimeoutExpired:
                self.log("機器人沒有在時間內關好，改成強制結束。", "error")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self.bot_online = False

    def stop_bot(self):
        if not self.bot_running():
            self.log("機器人本來就沒在跑。", "info")
            return
        self.log("正在停止機器人…", "info")
        threading.Thread(target=self._stop_bot_blocking, daemon=True).start()

    def restart_bot(self):
        def worker():
            if self.bot_running():
                self.log("正在停止機器人…", "info")
                self._stop_bot_blocking()
            self.after(0, self.start_bot)

        threading.Thread(target=worker, daemon=True).start()

    def _using_online_ai(self):
        return (self.config_data.get("ai_provider") or "ollama") not in ("ollama", "")

    def _update_ai_widgets(self):
        online = self._using_online_ai()
        if self.var_ai.get():
            self.btn_ai.configure(text="● AI 聊天：開", style="On.TButton")
            self.label_ai.configure(text="按一下關閉 AI（音樂和管理功能照常）" if online
                                    else "按一下關閉 AI（會釋放顯示卡記憶體，音樂和管理功能照常）")
        else:
            self.btn_ai.configure(text="○ AI 聊天：關", style="Off.TButton")
            self.label_ai.configure(text="按一下打開 AI（用「AI 來源」分頁設定的線上 AI）" if online
                                    else "按一下打開 AI（會載入模型，第一次要等十幾秒）")
        if hasattr(self, "label_ai_current"):
            self._update_ai_current_label()
        self._update_tool_buttons()
        self._update_nav_visibility()
        self._update_thinking_visibility()

    def _update_tool_buttons(self):
        """「小工具」只放現在用得到的：用線上 AI 就不用叫醒 Ollama，用內建搜尋就不用叫醒 SearXNG。"""
        if not hasattr(self, "btn_install"):
            return
        use_searxng = (bool(self.config_data.get("search_enabled", True))
                       and configured_search_engine(self.config_data) == "searxng")
        wanted = [self.btn_install]
        if not self._using_online_ai():
            wanted.append(self.btn_wake_ollama)
        if use_searxng:
            wanted.append(self.btn_wake_searxng)
        wanted.append(self.btn_open_folder)
        for button in (self.btn_install, self.btn_wake_ollama, self.btn_wake_searxng, self.btn_open_folder):
            button.pack_forget()
        for i, button in enumerate(wanted):
            button.pack(side="left", padx=px(0 if i == 0 else 6, 0))

    def _update_nav_visibility(self):
        """「本機模型」只有用本機 Ollama 才用得到，用線上 AI 時從左邊的選單藏起來。"""
        if not hasattr(self, "nav_items"):
            return
        (item, _, _), _ = self.nav_items[str(self.tab_models)]
        show = not self._using_online_ai()
        if show == getattr(self, "models_nav_shown", True):
            return
        self.models_nav_shown = show
        if show:
            (after_item, _, _), _ = self.nav_items[str(self.tab_settings)]
            item.pack(fill="x", padx=px(10), pady=px(1), before=after_item)
        else:
            item.pack_forget()
            if self.notebook.select() == str(self.tab_models):
                self.notebook.select(self.tab_main)
        self.after_idle(self._fit_cover)  # 選單變長或變短，封面的空位也跟著變

    def _update_thinking_visibility(self):
        """「深度思考」只對本機模型有用，線上 AI 不管勾不勾都一樣，就不顯示了。"""
        if hasattr(self, "check_thinking"):
            if self._using_online_ai():
                self.check_thinking.grid_remove()
            else:
                self.check_thinking.grid()

    def toggle_ai(self):
        enabled = not self.var_ai.get()
        self.var_ai.set(enabled)
        self._update_ai_widgets()
        self.config_data["ai_enabled"] = enabled
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        label = "開啟" if enabled else "關閉"

        def worker():
            if not bot_lock_in_use():
                self.log(f"AI 聊天功能已設為{label}，下次啟動機器人時生效。", "info")
                return
            if send_bot_command(b"ai_on" if enabled else b"ai_off"):
                self.log(f"已通知機器人{label} AI 聊天功能，立刻生效。", "good")
            else:
                self.log(f"機器人沒有回應（可能是舊版），AI 聊天功能要重新啟動機器人後才會{label}。", "error")

        threading.Thread(target=worker, daemon=True).start()

    def install_packages(self):
        if self.task_running:
            self.log("已經有安裝工作在跑了，等它結束。", "info")
            return
        if not os.path.exists(REQUIREMENTS_PATH):
            messagebox.showerror("找不到元件清單", "找不到 requirements.txt，請確認它跟控制面板放在同一個資料夾。")
            return

        def worker():
            self.task_running = True
            self.after(0, lambda: self.btn_install.configure(state="disabled"))
            self.log("開始更新元件，第一次可能要幾分鐘…", "info")
            try:
                proc = subprocess.Popen(
                    [console_python(), "-m", "pip", "install", "-U", "-r", REQUIREMENTS_PATH],
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=self._child_env(),
                    creationflags=CREATE_NO_WINDOW,
                )
                self._pump_output(proc, "更新元件")
                if proc.returncode == 0:
                    self.log("元件都更新好了！重新啟動本魚就會用新的。", "good")
                else:
                    self.log("更新元件失敗，請看上面的紅字訊息（通常是網路不穩，再按一次試試看）。", "error")
            except Exception as e:
                self.log(f"更新元件失敗：{e}", "error")
            finally:
                self.task_running = False
                self.after(0, lambda: self.btn_install.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def start_ollama(self):
        if http_alive(OLLAMA_URL):
            self.log("Ollama 已經在跑了。", "good")
            return
        try:
            subprocess.Popen(
                [ollama_exe(), "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            self.log("已送出啟動 Ollama 的指令，稍等幾秒狀態燈就會變綠。", "info")
        except Exception as e:
            self.log(f"啟動 Ollama 失敗（可能沒裝或不在 PATH 裡）：{e}", "error")

    def start_searxng(self):
        docker_path = self.var_docker.get().strip() or DEFAULT_CONFIG["docker_desktop_path"]
        searxng_base = base_url(self.var_searxng.get().strip() or DEFAULT_CONFIG["searxng_url"])

        def worker():
            if http_alive(searxng_base):
                self.log("SearXNG 已經在跑了。", "good")
                return
            if not docker_ready():
                self.log("Docker 還沒啟動，正在打開 Docker Desktop…", "info")
                try:
                    subprocess.Popen([docker_path] if IS_WINDOWS else ["open", "-a", docker_path])
                except Exception as e:
                    self.log(f"打不開 Docker Desktop：{e}。請到「設定」確認它的位置。", "error")
                    return
                for _ in range(90):
                    if docker_ready():
                        break
                    time.sleep(1)
                else:
                    self.log("等了 90 秒 Docker 還沒好，請自己看一下 Docker Desktop。", "error")
                    return
            self.log("Docker 就緒，正在啟動 SearXNG 容器…", "info")
            try:
                result = subprocess.run(
                    [docker_exe(), "start", "searxng"], capture_output=True, timeout=30,
                    creationflags=CREATE_NO_WINDOW,
                )
                if result.returncode != 0:
                    self.log(f"啟動容器失敗：{result.stderr.decode(errors='ignore').strip()}", "error")
                    return
            except Exception as e:
                self.log(f"執行 docker start 失敗：{e}", "error")
                return
            for _ in range(20):
                if http_alive(searxng_base):
                    self.log("SearXNG 啟動成功。", "good")
                    return
                time.sleep(1)
            self.log("容器啟動了，但 SearXNG 還沒回應，可能還在初始化。", "info")

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_status(self):
        if not self.status_checking:
            self.status_checking = True
            searxng_base = base_url(self.var_searxng.get().strip() or DEFAULT_CONFIG["searxng_url"])
            # 只檢查真的會用到的服務：用線上 AI 不查 Ollama，用內建搜尋不查 SearXNG／Docker
            check_ollama = not self._using_online_ai()
            search_on = bool(self.var_ai.get()) and bool(self.config_data.get("search_enabled", True))
            engine = configured_search_engine(self.config_data)
            docker_due = time.monotonic() - self.docker_checked_at > DOCKER_CHECK_SECONDS

            def worker():
                searxng_ok = http_alive(searxng_base) if search_on and engine == "searxng" else None
                docker = None
                if searxng_ok is False:
                    # SearXNG 沒回應時才去問 Docker（它在 Docker 裡跑），docker info 很慢，最多 30 秒問一次
                    if docker_due:
                        docker = docker_ready()
                        self.docker_checked_at = time.monotonic()
                        self.docker_last = docker
                    else:
                        docker = self.docker_last
                result = {
                    "bot_lock": bot_lock_in_use(),
                    "ollama": http_alive(OLLAMA_URL) if check_ollama else None,
                    "search_on": search_on,
                    "engine": engine,
                    "searxng": searxng_ok,
                    "docker": docker,
                    "ddgs": package_installed("ddgs") if search_on else None,
                    "js_runtime": js_runtime_name(),
                    "ffmpeg": ffmpeg_available(),
                }
                self.after(0, lambda: self._apply_status(result))

            threading.Thread(target=worker, daemon=True).start()
        self.after(STATUS_INTERVAL_MS, self._refresh_status)

    def _apply_status(self, result):
        self.status_checking = False
        lock_changed = self.last_bot_lock != bool(result.get("bot_lock"))
        self.last_bot_lock = bool(result.get("bot_lock"))
        if self.bot_running():
            if self.bot_online:
                self._set_header_pill("本魚上線中", "on")
            else:
                self._set_header_pill("啟動中…", "wait")
        elif result.get("bot_lock"):
            self._set_header_pill("在背景執行", "wait")
        else:
            self._set_header_pill("已停止", "off")
        try:
            mtime = os.path.getmtime(GUILDS_SNAPSHOT_PATH)
        except OSError:
            mtime = None
        if mtime != self.guild_snapshot_mtime or lock_changed:
            self.refresh_guild_table()

        if self.bot_running():
            if self.bot_online:
                self.light_bot.set(COLOR_ON, "線上")
            else:
                self.light_bot.set(COLOR_WAIT, "啟動中…")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        elif result.get("bot_lock"):
            self.bot_online = False
            self.light_bot.set(COLOR_WAIT, "在背景執行（別處開的）")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        else:
            self.bot_online = False
            self.light_bot.set(COLOR_OFF, "已停止")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

        provider = self.config_data.get("ai_provider") or "ollama"
        if not self.var_ai.get():
            self.light_ollama.set(COLOR_OFF, "已關閉")
        elif self._using_online_ai():
            name = AI_PROVIDERS.get(provider, AI_PROVIDERS["free"])[0].split("（")[0]
            self.light_ollama.set(ACCENT, f"線上：{name}")
        elif result["ollama"]:
            self.light_ollama.set(COLOR_ON, "本機 Ollama 運作中")
        else:
            self.light_ollama.set(COLOR_BAD, "本機 Ollama 沒在跑")
        self._apply_search_light(result)
        self._apply_music_light(result)

    def _hint_once(self, key, text):
        """缺東西的提示只在執行紀錄寫一次，不要每 4 秒洗一次。"""
        if key not in self.hints_shown:
            self.hints_shown.add(key)
            self.log(text, "error")

    def _apply_search_light(self, result):
        if not result["search_on"]:
            self.light_search.set(COLOR_OFF, "已關閉" if self.var_ai.get() else "AI 關著，用不到")
        elif result["engine"] == "builtin":
            if result["ddgs"]:
                self.light_search.set(COLOR_ON, "內建搜尋")
            else:
                self.light_search.set(COLOR_BAD, "缺少元件")
                self._hint_once("ddgs", "上網查資料要用的元件還沒裝：按「小工具」裡的「更新元件」就會裝好。")
        elif result["searxng"]:
            self.light_search.set(COLOR_ON, "SearXNG 運作中")
        elif result["ddgs"]:
            # 機器人查不到 SearXNG 時會自動改用內建搜尋
            self.light_search.set(COLOR_WAIT, "SearXNG 沒開，先用內建")
        else:
            self.light_search.set(COLOR_BAD, "SearXNG 沒在跑" + ("（Docker 沒開）" if result["docker"] is False else ""))

    def _apply_music_light(self, result):
        if not result["ffmpeg"]:
            self.light_music.set(COLOR_BAD, "缺 ffmpeg，不能放歌")
            self._hint_once("ffmpeg", f"點歌要用的 ffmpeg 不見了：再雙擊一次「{SETUP_NAME}」就會幫你下載。")
        elif not result["js_runtime"]:
            self.light_music.set(COLOR_WAIT, "YouTube 缺 Deno")
            self._hint_once("deno", "YouTube 點歌需要 Deno 才能穩定解析（沒有的話有些歌會放不了）："
                                    f"再雙擊一次「{SETUP_NAME}」就會幫你裝好。")
        else:
            self.light_music.set(COLOR_ON, "一切正常")

    def refresh_models(self):
        def worker():
            models = fetch_ollama_models()
            self.after(0, lambda: self._apply_models(models))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_models(self, models):
        # 只有用本機 Ollama 才需要提醒；用線上 AI 的話本機模型有沒有下載都沒差
        current = self.var_model.get()
        if models and current and current not in models and not self._using_online_ai():
            self.log(f"注意：設定的模型「{current}」還沒下載，到「本機模型」分頁下載或換一個。", "error")

    def toggle_token(self):
        if self.entry_token.cget("show"):
            self.entry_token.configure(show="")
            self.btn_show_token.configure(text="隱藏")
        else:
            self.entry_token.configure(show="•")
            self.btn_show_token.configure(text="顯示")

    def _selected_search_engine(self):
        chosen = self.var_search_engine.get()
        return next((key for key, label in SEARCH_ENGINES.items() if label == chosen), "builtin")

    def _update_search_fields(self):
        """SearXNG 網址和 Docker 位置只有選 SearXNG 才出現；沒開上網查資料的話，搜尋方式也不用選。"""
        searching = bool(self.var_search.get())
        self.combo_search_engine.configure(state="readonly" if searching else "disabled")
        show = searching and self._selected_search_engine() == "searxng"
        for widget in self.searxng_widgets:
            if show:
                widget.grid()
            else:
                widget.grid_remove()

    def browse_docker(self):
        path = filedialog.askopenfilename(
            title="選擇 Docker Desktop.exe" if IS_WINDOWS else "選擇 Docker",
            filetypes=[("程式", "*.exe"), ("所有檔案", "*.*")] if IS_WINDOWS else [("所有檔案", "*")],
        )
        if path:
            self.var_docker.set(os.path.normpath(path))

    def _ask_restart(self, message):
        if self.bot_running():
            if messagebox.askyesno("已儲存", message + "\n\n機器人正在執行，要現在重新啟動套用嗎？"):
                self.restart_bot()
        else:
            messagebox.showinfo("已儲存", message)

    def save_settings(self):
        try:
            history = int(self.var_history.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("格式錯誤", "記住的對話輪數要是數字。")
            return

        self.config_data.update({
            "model_name": self.var_model.get().strip() or DEFAULT_CONFIG["model_name"],
            "max_history_turns": max(1, min(20, history)),
            "searxng_url": self.var_searxng.get().strip() or DEFAULT_CONFIG["searxng_url"],
            "docker_desktop_path": self.var_docker.get().strip() or DEFAULT_CONFIG["docker_desktop_path"],
            "autostart_bot": bool(self.var_autostart_bot.get()),
            "enable_thinking": bool(self.var_thinking.get()),
            "search_enabled": bool(self.var_search.get()),
            "search_engine": self._selected_search_engine(),
        })
        try:
            save_config(self.config_data)
            write_token(self.var_token.get())
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        # 要先存好設定再通知機器人，它收到後會重新讀設定檔裡的搜尋方式
        if bot_lock_in_use():
            send_bot_command(b"search_on" if self.var_search.get() else b"search_off")
        self.log("設定已儲存。", "good")
        self._update_tool_buttons()  # 換了搜尋方式，「叫醒 SearXNG」要跟著出現或藏起來
        self.refresh_model_table()
        self.refresh_guild_table()
        self._ask_restart("設定已儲存。")

    def save_persona(self):
        text = self.persona_text.get("1.0", "end").strip()
        default = read_default_persona().strip()
        self.config_data["persona"] = "" if text == default else text
        try:
            save_config(self.config_data)
        except Exception as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        self.log("人設已儲存。", "good")
        self._ask_restart("人設已儲存。")

    def reset_persona(self):
        if not messagebox.askyesno("還原人設", "確定要把人設還原成程式內建的預設內容嗎？"):
            return
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", read_default_persona())

    def toggle_boot(self):
        try:
            set_boot(self.var_boot.get())
            if self.var_boot.get():
                self.log("已設定開機自動打開控制面板。", "good")
            else:
                self.log("已取消開機自動打開控制面板。", "info")
        except Exception as e:
            messagebox.showerror("設定失敗", str(e))
            self.var_boot.set(boot_enabled())

    def create_shortcuts(self):
        try:
            made = install_shortcuts(desktop=True)
        except Exception as e:
            messagebox.showerror("建立失敗", f"建立捷徑失敗：{e}")
            return
        self.config_data["shortcuts_created"] = True
        try:
            save_config(self.config_data)
        except Exception:
            pass
        self.log("捷徑建好了：" + "、".join(made), "good")
        if IS_WINDOWS:
            messagebox.showinfo(
                "捷徑建好了",
                "開始功能表和桌面都有「藍色大肥魚控制面板」了，圖示是本魚的大頭貼。\n\n"
                "想放到工作列或開始畫面：\n在開始功能表搜尋「藍色大肥魚」→ 在圖示上按右鍵 →「釘選到工作列」或「釘選到開始畫面」。",
            )
        else:
            messagebox.showinfo(
                "App 建好了",
                "「應用程式」資料夾裡有「藍色大肥魚控制面板」了，用 Spotlight 搜尋「藍色大肥魚」也找得到。\n\n"
                "想放在 Dock：打開後在 Dock 的圖示上按右鍵 →「選項」→「保留在 Dock」。",
            )

    def on_close(self):
        if self.bot_running():
            if not messagebox.askyesno("關閉面板", "機器人還在執行中，關掉面板會一起停止機器人。\n確定要關閉嗎？"):
                return
            self._stop_bot_blocking()
        self.destroy()


def acquire_panel_lock():
    """同時只開一個控制面板。回傳 (要不要直接結束, 鎖)：已經有一個開著的話，請它跳到最前面，這個就不用開了。"""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Mac：剛關掉的連接埠才能馬上再用
    try:
        lock.bind(("127.0.0.1", PANEL_LOCK_PORT))
        lock.listen(5)
        return False, lock
    except OSError:
        lock.close()
    try:
        with socket.create_connection(("127.0.0.1", PANEL_LOCK_PORT), timeout=1) as conn:
            conn.settimeout(2)
            conn.sendall(b"show")
            if conn.recv(16).strip() == b"ok":
                return True, None
    except OSError:
        pass
    return False, None  # 連接埠被別的程式占走了：照常打開，只是沒辦法防止重複開啟


if __name__ == "__main__":
    if "--create-shortcuts" in sys.argv:
        # 首次安裝用的：只建立捷徑，不打開視窗
        made = install_shortcuts(desktop="--no-desktop" not in sys.argv)
        try:
            config = load_config()
            config["shortcuts_created"] = True
            save_config(config)
        except Exception:
            pass
        for path in made:
            print(path)
        sys.exit(0)
    should_exit, panel_lock = acquire_panel_lock()
    if should_exit:
        sys.exit(0)
    app = App()
    if panel_lock is not None:
        app.listen_for_show(panel_lock)
    app.mainloop()
