import os
import sys
import io
import re
import ast
import json
import math
import time
import random
import operator
import subprocess
import socket
import shutil
import asyncio
import threading
import functools
import ctypes
from datetime import datetime, timedelta, timezone
from typing import Optional
import urllib.parse
from collections import defaultdict, deque

import discord
from discord import app_commands
import ollama
import requests
import yt_dlp
from opencc import OpenCC

try:
    from ddgs import DDGS  # 內建搜尋用的套件；沒裝的話上網查資料只能用 SearXNG
except ImportError:
    DDGS = None

import knowledge

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

sys.setswitchinterval(0.002)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MODEL_NAME = "deepseek-r1:14b"
MAX_HISTORY_TURNS = 6
SEARXNG_URL = "http://localhost:8080/search"

# 同一份程式在 Windows 和 Mac 都能跑，只有找程式、開程式的地方不一樣
IS_WINDOWS = sys.platform == "win32"
EXE = ".exe" if IS_WINDOWS else ""
SETUP_NAME = "首次安裝.bat" if IS_WINDOWS else "首次安裝.command"
DOCKER_DESKTOP_PATH = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
SEARXNG_CONTAINER_NAME = "searxng"
OLLAMA_STARTUP_TIMEOUT = 30
DOCKER_ENGINE_STARTUP_TIMEOUT = 90
SEARXNG_CONTAINER_STARTUP_TIMEOUT = 20

# 所有呼叫都要用同一個 num_ctx：Ollama 只要 num_ctx 跟上次不同就會整個重新載入模型
# （實測 14b 模型每次重載 3 秒多，以前判斷用 4096、回答用 8192，每則訊息都白白重載兩次）
OLLAMA_NUM_CTX = 8192
CHAT_OPTIONS = {
    "temperature": 0.4,
    "top_p": 0.9,
    "repeat_penalty": 1.15,
    "num_ctx": OLLAMA_NUM_CTX,
    "num_predict": 3072,
}
ROUTER_OPTIONS = {
    "temperature": 0,
    "num_ctx": OLLAMA_NUM_CTX,
    "num_predict": 200,
}

def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(get_base_dir(), "config.json")

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

CONFIG = load_config()
MODEL_NAME = CONFIG.get("model_name") or MODEL_NAME
try:
    MAX_HISTORY_TURNS = int(CONFIG.get("max_history_turns") or MAX_HISTORY_TURNS)
except (TypeError, ValueError):
    pass
def prefer_ipv4(url):
    """Windows 連 localhost 會先試 IPv6（::1），Ollama 這類只聽 IPv4 的服務要被拒絕重試 2 秒才改連 127.0.0.1。
    直接寫 127.0.0.1 就不會每次都卡 2 秒。"""
    return re.sub(r"(?i)^(\w+://)?localhost(?=[:/]|$)", lambda m: (m.group(1) or "") + "127.0.0.1", url) if url else url

SEARXNG_URL = prefer_ipv4(CONFIG.get("searxng_url") or SEARXNG_URL)
DOCKER_DESKTOP_PATH = CONFIG.get("docker_desktop_path") or DOCKER_DESKTOP_PATH
# 模型閒置多久才從顯示卡卸載。Ollama 預設 5 分鐘，卸載後下一句話要重新載入 14b 模型（常常十幾秒）。
OLLAMA_KEEP_ALIVE = CONFIG.get("ollama_keep_alive") or "30m"
OLLAMA_HOST = prefer_ipv4(CONFIG.get("ollama_host") or None)
OLLAMA_BASE = OLLAMA_HOST if OLLAMA_HOST and "://" in OLLAMA_HOST else f"http://{OLLAMA_HOST or '127.0.0.1:11434'}"
# 關掉深度思考回答會快很多，但比較容易答錯
ENABLE_THINKING = bool(CONFIG.get("enable_thinking", True))
# AI 聊天總開關。控制面板可以在機器人執行中即時切換；關掉時音樂、管理、斜線指令照常運作
AI_ENABLED = bool(CONFIG.get("ai_enabled", True))
# 上網查資料開關
SEARCH_ENABLED = bool(CONFIG.get("search_enabled", True))
# 上網查資料的方式：builtin = 內建搜尋（ddgs 套件，同時問 Google、Brave、DuckDuckGo、維基百科…，不用裝 Docker）；
# searxng = 自己用 Docker 架的 SearXNG。用 SearXNG 時它沒開或查不到，會自動改用內建搜尋
SEARCH_ENGINES = {"builtin": "內建搜尋", "searxng": "SearXNG"}

def load_search_engine(cfg=None):
    cfg = load_config() if cfg is None else cfg
    engine = cfg.get("search_engine")
    if engine in SEARCH_ENGINES:
        return engine
    # 舊設定檔沒有這一項：以前只有 SearXNG 可以選，有開上網查資料的人就是在用 SearXNG，照舊
    return "searxng" if cfg.get("search_enabled", True) else "builtin"

SEARCH_ENGINE = load_search_engine(CONFIG)
# 知識庫：程式資料夾裡「知識庫」資料夾的文件，AI 回答時會自動找相關段落參考（見 knowledge.py）
KNOWLEDGE_ENABLED = bool(CONFIG.get("knowledge_enabled", True))
KNOWLEDGE_DIR = os.path.join(get_base_dir(), "知識庫")
KNOWLEDGE_REFRESH_SECONDS = 20
knowledge.ensure_folder(KNOWLEDGE_DIR)
knowledge_base = knowledge.KnowledgeBase(KNOWLEDGE_DIR)
AI_DISABLED_REPLY = "本魚的 AI 聊天功能現在被管理員關掉了，先不能陪你聊天喔～點歌、斜線指令這些還是可以用，打 `/幫助` 看看吧！"
# 哪些伺服器要啟用本魚。由控制面板的「伺服器」分頁設定：{"伺服器ID": true/false}
# 沒設定過的伺服器（例如剛被邀請進去的）照 new_guild_default 決定
guild_access = {}
new_guild_default = True

def load_guild_access():
    global guild_access, new_guild_default
    cfg = load_config()
    access = cfg.get("guild_access")
    guild_access = {str(k): bool(v) for k, v in access.items()} if isinstance(access, dict) else {}
    new_guild_default = bool(cfg.get("new_guild_default", True))

def guild_enabled(guild_or_id):
    if guild_or_id is None:
        return True
    gid = str(getattr(guild_or_id, "id", guild_or_id))
    return guild_access.get(gid, new_guild_default)

load_guild_access()

# 每個頻道可以個別關掉的功能。由控制面板的「頻道功能」分頁設定，存在 config.json 的
# channel_features：{"頻道ID": ["ai", "music", ...]}，只記「被關掉」的功能，沒列出來的都是開啟
# 管理類指令（踢人、警告、自動管理…）不受這裡影響，避免管理員不小心把自己鎖在外面
CHANNEL_FEATURES = {
    "ai": "AI 聊天",
    "music": "點歌",
    "games": "小遊戲",
    "levels": "聊天經驗值",
    "auto_reply": "自動回覆",
    "sports": "運動比分",
    "knowledge": "知識庫",
}
CHANNEL_FEATURE_OFF_TEXT = "這個頻道沒有開放「{}」功能喔～換個頻道試試看吧！"
channel_disabled_features = {}

def load_channel_features():
    global channel_disabled_features
    raw = load_config().get("channel_features")
    result = {}
    if isinstance(raw, dict):
        for channel_id, features in raw.items():
            if isinstance(features, list):
                off = {f for f in features if f in CHANNEL_FEATURES}
                if off:
                    result[str(channel_id)] = off
    channel_disabled_features = result

def channel_feature_enabled(channel, feature):
    """討論串跟著它所在的頻道走；私訊沒有頻道設定，一律開啟。"""
    if channel is None or getattr(channel, "guild", None) is None:
        return True
    channel_id = getattr(channel, "parent_id", None) if isinstance(channel, discord.Thread) else channel.id
    return feature not in channel_disabled_features.get(str(channel_id or channel.id), ())

load_channel_features()
# 送進模型的對話紀錄字數上限，避免超過 num_ctx 時 Ollama 把最前面的人設切掉
HISTORY_CHAR_BUDGET = 3000
MAX_REMEMBERED_CHARS = 1200
_searxng_parts = urllib.parse.urlsplit(SEARXNG_URL)
SEARXNG_BASE = f"{_searxng_parts.scheme}://{_searxng_parts.netloc}"

def load_token():
    token_path = os.path.join(get_base_dir(), "token.txt")
    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8-sig") as f:
            content = f.read().strip()
            if content:
                return content
    return os.environ.get("DISCORD_BOT_TOKEN")

TOKEN = load_token()
if not TOKEN:
    print("找不到權杖啦。請在這支程式旁邊放一個 token.txt，裡面貼上你的 Discord Bot Token。")
    sys.exit(1)

INSTANCE_LOCK_PORT = 47831
PID_FILE = os.path.join(get_base_dir(), "bot.pid")

def acquire_single_instance_lock():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # Mac：剛關掉的連接埠要等一分鐘才能再用，不加這個的話按「重新啟動」會以為本魚還開著
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        lock.listen(5)
    except OSError:
        lock.close()
        return None
    return lock

_instance_lock = acquire_single_instance_lock()
if _instance_lock is None:
    print("偵測到已經有另一隻本魚在執行中了！")
    print("同時跑兩隻會每句話都回兩次，音樂也會卡。請先把另一隻關掉（控制面板「小工具」裡有「強制關閉所有本魚」按鈕），再重新啟動。")
    sys.exit(2)

try:
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
except Exception:
    pass

# Mac 用 Homebrew 裝的程式放在這兩個地方；從 Finder 或 Dock 打開時 PATH 裡常常沒有它們
UNIX_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

def find_program(name, *fallbacks):
    """先找 PATH，找不到再找常見安裝位置。剛裝好的程式在還沒重開機前常常不在 PATH 裡。"""
    found = shutil.which(name)
    if found:
        return found
    if not IS_WINDOWS:
        fallbacks = tuple(os.path.join(folder, name) for folder in UNIX_BIN_DIRS) + fallbacks
    for path in fallbacks:
        if path and os.path.exists(path):
            return path
    return None

FFMPEG_PATH = os.path.join(get_base_dir(), "ffmpeg" + EXE)
if not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = find_program("ffmpeg") or ""

def ollama_exe():
    return find_program(
        "ollama",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe",
        "/Applications/Ollama.app/Contents/Resources/ollama",
    )

def docker_exe():
    return find_program("docker", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
                        "/Applications/Docker.app/Contents/Resources/bin/docker")

def ensure_ollama_running():
    try:
        requests.get(OLLAMA_BASE, timeout=1)
        print("Ollama 已經在跑了。")
        return
    except Exception:
        pass

    print("Ollama 還沒起來，嘗試自動啟動...")
    exe = ollama_exe()
    if not exe:
        print(f"找不到 Ollama，可能還沒安裝。請雙擊「{SETUP_NAME}」安裝，或是在控制面板把 AI 聊天關掉。")
        return
    try:
        subprocess.Popen(
            [exe, "serve"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"啟動 Ollama 失敗（可能沒裝或不在 PATH 裡）：{e}")
        return

    for _ in range(OLLAMA_STARTUP_TIMEOUT):
        try:
            requests.get(OLLAMA_BASE, timeout=1)
            print("Ollama 啟動成功。")
            return
        except Exception:
            time.sleep(1)
    print(f"等了 {OLLAMA_STARTUP_TIMEOUT} 秒 Ollama 還是沒反應，請自己手動檢查一下。")

def _docker_engine_ready():
    try:
        exe = docker_exe()
        if not exe:
            return False
        result = subprocess.run([exe, "info"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

def ensure_searxng_running():
    try:
        requests.get(SEARXNG_BASE, timeout=1)
        print("SearXNG 已經在跑了。")
        return
    except Exception:
        pass

    if not _docker_engine_ready():
        print("Docker 引擎還沒啟動，嘗試打開 Docker Desktop...")
        try:
            subprocess.Popen([DOCKER_DESKTOP_PATH] if IS_WINDOWS else ["open", "-a", "Docker"])
        except Exception as e:
            print(f"找不到 Docker Desktop（路徑：{DOCKER_DESKTOP_PATH}），請確認安裝路徑或手動開啟：{e}")
            return

        for _ in range(DOCKER_ENGINE_STARTUP_TIMEOUT):
            if _docker_engine_ready():
                break
            time.sleep(1)
        else:
            print(f"等了 {DOCKER_ENGINE_STARTUP_TIMEOUT} 秒 Docker 引擎還沒就緒，請自己檢查 Docker Desktop 狀態。")
            return

    print("Docker 引擎就緒，嘗試啟動 SearXNG 容器...")
    try:
        result = subprocess.run(
            [docker_exe() or "docker", "start", SEARXNG_CONTAINER_NAME], capture_output=True, timeout=30
        )
        if result.returncode != 0:
            print(f"啟動容器失敗：{result.stderr.decode(errors='ignore').strip()}")
            print("如果訊息說容器不存在，代表還沒建立過，先照設定步驟手動 docker run 一次。")
            return
    except Exception as e:
        print(f"執行 docker start 失敗：{e}")
        return

    for _ in range(SEARXNG_CONTAINER_STARTUP_TIMEOUT):
        try:
            requests.get(SEARXNG_BASE, timeout=1)
            print("SearXNG 啟動成功。")
            return
        except Exception:
            time.sleep(1)
    print(f"容器啟動了，但等了 {SEARXNG_CONTAINER_STARTUP_TIMEOUT} 秒服務還沒回應，可能還在初始化。")

def _start_searxng_or_fallback():
    ensure_searxng_running()
    if DDGS is None:
        return
    try:
        requests.get(SEARXNG_BASE, timeout=1)
    except Exception:
        print("SearXNG 沒開起來，上網查資料會先改用內建搜尋。")

def start_search_backend():
    """AI 聊天和上網查資料都開著時呼叫：用 SearXNG 就在背景叫醒 Docker 和 SearXNG；內建搜尋什麼都不用開。"""
    if SEARCH_ENGINE == "searxng":
        threading.Thread(target=_start_searxng_or_fallback, name="check-searxng", daemon=True).start()
    elif DDGS is None:
        print("內建搜尋需要的元件（ddgs）還沒裝，請在控制面板按「更新元件」，不然本魚沒辦法上網查資料。")
    else:
        print("上網查資料使用內建搜尋，不需要 Docker。")

# 有逾時的 Ollama 連線：Ollama 卡住時不會讓整個頻道永遠等下去
chat_llm = ollama.Client(host=OLLAMA_HOST, timeout=600)
router_llm = ollama.Client(host=OLLAMA_HOST, timeout=60)
http_session = requests.Session()

# 有些模型（例如 gemma3、llama3.1）不支援深度思考，收到 think 參數 Ollama 會直接回錯誤。
# 碰到一次就記起來，之後對這個模型都不傳 think，換模型時就不會整個不能聊天。
models_without_thinking = set()

# ---------- 線上 AI（API）----------
# 控制面板「AI 來源」分頁設定。ai_provider 是 "ollama" 就用本機模型，其他就走對應的線上 API。
# 金鑰存在 ai_keys.json（跟 token.txt 一樣不能給別人看），不放在 config.json 裡。
# 格式："openai" = OpenAI 相容格式（大部分服務都是），"anthropic" = Claude 自己的格式
AI_PROVIDERS = {
    # 代碼: (顯示名稱, 格式, API 網址, 建議模型)
    # free：Pollinations 的匿名免費服務，不用註冊、不用金鑰，給不想設定的人用。會排隊、比較慢，偶爾沒回應
    "free": ("免費線上 AI（不用金鑰）", "openai", "https://text.pollinations.ai/openai", "openai-fast"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("OpenAI（ChatGPT）", "openai", "https://api.openai.com/v1", "gpt-5-mini"),
    "gemini": ("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest"),
    "claude": ("Anthropic Claude", "anthropic", "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    "openrouter": ("OpenRouter", "openai", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"),
    "groq": ("Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "custom": ("自訂（OpenAI 相容）", "openai", "", ""),
}
AI_KEYS_PATH = os.path.join(get_base_dir(), "ai_keys.json")
AI_PROVIDER = "ollama"
ONLINE_MODEL = ""
ONLINE_BASE_URL = ""
ONLINE_API_KEY = ""
AI_USER_LIMIT = 5   # 用線上 API 時，每個人每分鐘最多問幾次（0 = 不限），免得有人一直洗、把帳單刷爆
ai_session = requests.Session()
ai_user_calls = defaultdict(deque)

def load_ai_settings():
    global AI_PROVIDER, ONLINE_MODEL, ONLINE_BASE_URL, ONLINE_API_KEY, AI_USER_LIMIT
    cfg = load_config()
    provider = cfg.get("ai_provider") or "ollama"
    AI_PROVIDER = provider if provider in AI_PROVIDERS else "ollama"
    if AI_PROVIDER == "ollama":
        return
    _, _, base, default_model = AI_PROVIDERS[AI_PROVIDER]
    ONLINE_MODEL = str((cfg.get("online_models") or {}).get(AI_PROVIDER) or default_model).strip()
    ONLINE_BASE_URL = (str(cfg.get("custom_base_url") or "").strip() if AI_PROVIDER == "custom" else base).rstrip("/")
    try:
        AI_USER_LIMIT = max(0, int(cfg.get("ai_user_limit", 5)))
    except (TypeError, ValueError):
        AI_USER_LIMIT = 5
    try:
        with open(AI_KEYS_PATH, "r", encoding="utf-8-sig") as f:
            keys = json.load(f)
        ONLINE_API_KEY = str(keys.get(AI_PROVIDER) or "").strip() if isinstance(keys, dict) else ""
    except Exception:
        ONLINE_API_KEY = ""

def using_online_ai():
    return AI_PROVIDER != "ollama"

load_ai_settings()

def ai_model_label():
    if using_online_ai():
        return f"{AI_PROVIDERS[AI_PROVIDER][0]}：{ONLINE_MODEL or '（還沒選模型）'}"
    return f"本機 Ollama：{MODEL_NAME}"

def check_ai_rate_limit(user_id):
    """回傳還要等幾秒；0 代表可以問。只有線上 API 會限制，本機模型不花錢。"""
    if not using_online_ai() or AI_USER_LIMIT <= 0:
        return 0
    now = time.monotonic()
    calls = ai_user_calls[user_id]
    while calls and now - calls[0] > 60:
        calls.popleft()
    if len(calls) >= AI_USER_LIMIT:
        return int(60 - (now - calls[0])) + 1
    calls.append(now)
    return 0

def extract_json_text(text):
    """線上模型有時會把 JSON 包在 ```json 裡或前後加說明，只取出 {...} 那段。"""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text

def online_api_error(provider_name, resp):
    try:
        detail = resp.json()
        detail = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) else detail.get("error") or detail
    except Exception:
        detail = resp.text
    detail = str(detail)[:200]
    if "<html" in detail.lower() or "<!doctype" in detail.lower():
        detail = "伺服器回了一個錯誤網頁"
    if resp.status_code >= 500:
        tip = "，或請機器人主人到控制面板「AI 來源」改用 Google Gemini 免費金鑰（比較穩）" if AI_PROVIDER == "free" else ""
        return f"{provider_name} 的伺服器暫時故障（HTTP {resp.status_code}），等一下再試{tip}。"
    # Gemini 金鑰錯誤時回的是 400 不是 401，看訊息內容判斷
    if resp.status_code in (401, 403) or "api key" in detail.lower() or "api_key" in detail.lower():
        return f"{provider_name} 的 API 金鑰不對或沒有權限，請到控制面板「AI 來源」分頁重新填。"
    if resp.status_code == 402 or "insufficient" in detail.lower() or "balance" in detail.lower():
        return f"{provider_name} 帳戶的額度用完了，要去官網儲值。"
    if resp.status_code == 429:
        if "perday" in resp.text.lower().replace("_", "").replace(" ", ""):
            return f"{provider_name} 今天的免費額度用完了，明天再試（台灣時間大約下午三、四點重置）。"
        return f"{provider_name} 說問太快或額度用完了，等一下再試。"
    if resp.status_code == 404 or "model" in detail.lower() and "not" in detail.lower():
        return f"{provider_name} 找不到模型「{ONLINE_MODEL}」，請到控制面板「AI 來源」分頁按「抓模型清單」重選。（{detail}）"
    return f"{provider_name} 回傳錯誤（HTTP {resp.status_code}）：{detail}"

def ai_post(url, **kwargs):
    """伺服器暫時故障（5xx）或連線中斷時等 2 秒重試一次，免費服務特別常這樣。"""
    for attempt in range(2):
        try:
            resp = ai_session.post(url, **kwargs)
        except requests.ConnectionError:
            if attempt:
                raise RuntimeError("連不上線上 AI 的伺服器，請確認電腦有網路。")
        else:
            if resp.status_code < 500 or attempt:
                return resp
        time.sleep(2)

class RateLimited(RuntimeError):
    """線上 AI 回 429（問太快或額度用完）。wait 是對方建議等幾秒，看不出來就是 None。"""
    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait

def online_error(name, resp):
    message = online_api_error(name, resp)
    if resp.status_code != 429:
        return RuntimeError(message)
    wait = None
    try:
        wait = float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        # Gemini 把建議等待的秒數寫在錯誤訊息裡（Please retry in 37.3s）
        m = re.search(r"retry in ([\d.]+)\s*s|retryDelay\W+([\d.]+)s", resp.text, re.IGNORECASE)
        if m:
            wait = float(m.group(1) or m.group(2))
    return RateLimited(message, wait)

# 被限流時最多等幾秒自動重問（Gemini 免費版是算「每分鐘幾次」，通常等幾十秒就好）
RATE_LIMIT_MAX_WAIT = 30
# Gemini 每個模型的額度分開算：判斷要做什麼用輕量版（快又省主模型的額度），主模型被限流時也拿它頂一下
GEMINI_LITE_MODEL = "gemini-flash-lite-latest"

def online_chat(messages, json_mode=False, temperature=None, max_tokens=None, timeout=180, model=None):
    name = AI_PROVIDERS[AI_PROVIDER][0]
    if not ONLINE_API_KEY and AI_PROVIDER != "free":
        raise RuntimeError(f"還沒填 {name} 的 API 金鑰，請到控制面板「AI 來源」分頁設定。")
    if not ONLINE_BASE_URL or not ONLINE_MODEL:
        raise RuntimeError("線上 AI 的網址或模型還沒設定好，請到控制面板「AI 來源」分頁設定。")
    args = (messages, json_mode, temperature, max_tokens, timeout)
    model = model or ONLINE_MODEL
    try:
        return online_request(AI_PROVIDER, model, ONLINE_BASE_URL, ONLINE_API_KEY, *args)
    except RateLimited as e:
        first_error = e
    # 判斷要做什麼的小呼叫不用等，直接改用關鍵字判斷
    if json_mode:
        raise first_error
    if first_error.wait is not None and first_error.wait <= RATE_LIMIT_MAX_WAIT:
        print(f"{name} 說問太快，等 {first_error.wait:.0f} 秒再問一次")
        time.sleep(first_error.wait + 1)
        try:
            return online_request(AI_PROVIDER, model, ONLINE_BASE_URL, ONLINE_API_KEY, *args)
        except RateLimited:
            pass
    # 還是不行：Gemini 先換輕量版模型，再不行就用免費線上 AI 頂一下，總比直接出錯好
    backups = []
    if AI_PROVIDER == "gemini" and model != GEMINI_LITE_MODEL:
        backups.append(("gemini", GEMINI_LITE_MODEL, ONLINE_BASE_URL, ONLINE_API_KEY))
    if AI_PROVIDER != "free":
        _, _, free_url, free_model = AI_PROVIDERS["free"]
        backups.append(("free", free_model, free_url, ""))
    for provider, backup_model, url, key in backups:
        try:
            text = online_request(provider, backup_model, url, key, *args)
            print(f"{name} 被限流，這次改用 {AI_PROVIDERS[provider][0]}（{backup_model}）回答")
            return text
        except RuntimeError as e:
            print(f"備用 AI（{AI_PROVIDERS[provider][0]}）也失敗：{e}")
    raise first_error

def online_request(provider, model, base_url, api_key, messages, json_mode, temperature, max_tokens, timeout):
    name, style, _, _ = AI_PROVIDERS[provider]
    keyless = provider == "free"
    if style == "anthropic":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = []
        for m in messages:
            if m["role"] == "system":
                continue
            # Claude 規定使用者和 AI 要輪流說話，連續同一方的訊息合併起來
            if chat and chat[-1]["role"] == m["role"]:
                chat[-1]["content"] += "\n\n" + m["content"]
            else:
                chat.append({"role": m["role"], "content": m["content"]})
        if json_mode:
            system += "\n\n只輸出一個 JSON 物件，不要加任何其他文字。"
        body = {"model": model, "max_tokens": max_tokens or 2048, "messages": chat}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        resp = ai_post(f"{base_url}/messages", json=body, timeout=timeout,
                               headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        if not resp.ok:
            raise online_error(name, resp)
        text = "".join(block.get("text", "") for block in resp.json().get("content") or [] if block.get("type") == "text")
    else:
        optional = {}
        if temperature is not None:
            optional["temperature"] = temperature
        if max_tokens:
            optional["max_tokens"] = max_tokens
        if json_mode:
            optional["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # 免費服務的網址本身就是聊天介面，不用再接 /chat/completions（接了反而比較慢）
        url = base_url if keyless else f"{base_url}/chat/completions"
        resp = ai_post(url, timeout=timeout, headers=headers,
                               json={"model": model, "messages": messages, **optional})
        # 有些模型（例如 OpenAI 的推理模型）不吃 temperature、max_tokens 或 JSON 模式，拿掉再試一次
        if resp.status_code == 400 and optional:
            resp = ai_post(url, timeout=timeout, headers=headers,
                                   json={"model": model, "messages": messages})
        if not resp.ok:
            raise online_error(name, resp)
        choices = resp.json().get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        if keyless and not text.strip():
            # 免費服務太忙時會只回「思考過程」沒有答案
            raise RuntimeError("免費線上 AI 現在太多人在用，沒有回答。等一下再試，"
                               "或請機器人主人到控制面板「AI 來源」改用 Google Gemini 免費金鑰（比較快也比較穩）。")
    return extract_json_text(text) if json_mode else text

def llm_chat(llm, **kwargs):
    if using_online_ai():
        options = kwargs.get("options") or {}
        text = online_chat(
            kwargs["messages"],
            json_mode=kwargs.get("format") == "json",
            temperature=options.get("temperature"),
            # 線上推理模型的「思考」也算在輸出長度裡，給太少會整個被截斷，所以至少給 1024（只照實際用量收費）
            max_tokens=max(options.get("num_predict") or 0, 1024),
            timeout=60 if llm is router_llm else 180,
            model=GEMINI_LITE_MODEL if llm is router_llm and AI_PROVIDER == "gemini" else None,
        )
        return {"message": {"content": text}}
    model = kwargs.get("model")
    if model in models_without_thinking:
        kwargs.pop("think", None)
    try:
        return llm.chat(**kwargs)
    except ollama.ResponseError as e:
        if "think" in kwargs and "think" in str(e).lower():
            print(f"模型 {model} 不支援深度思考，之後改用一般模式回答。")
            models_without_thinking.add(model)
            kwargs.pop("think", None)
            return llm.chat(**kwargs)
        raise

def atomic_write_json(path, data, indent=None):
    """先寫到暫存檔再換掉，寫到一半當機也不會把原本的設定檔弄壞。"""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(temp_path, path)

s2t_converter = OpenCC("s2twp")
conversation_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY_TURNS * 2))
channel_locks = defaultdict(asyncio.Lock)

history_last_used = {}
HISTORY_IDLE_SECONDS = 6 * 3600

def _clip(text, limit=MAX_REMEMBERED_CHARS):
    return text if len(text) <= limit else text[:limit] + "…（後面省略）"

def remember(history_key, user_text, bot_text):
    conversation_history[history_key].append({"role": "user", "content": _clip(user_text)})
    conversation_history[history_key].append({"role": "assistant", "content": _clip(bot_text)})
    history_last_used[history_key] = time.monotonic()

def trim_history(history, budget=HISTORY_CHAR_BUDGET):
    """從最新的往回取，總字數不超過 budget，並且保證從 user 訊息開始。"""
    kept, total = [], 0
    for m in reversed(history):
        total += len(m["content"])
        if total > budget and kept:
            break
        kept.append(m)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept

PERSONA = """你是「本魚」，一隻藍色圓滾滾的胖胖小魚，是 DeepSeek 的擬人化吉祥物，住在這個 Discord 伺服器裡。

【個性與語氣】
- 軟萌可愛、有點傲嬌、愛撒嬌，句尾常帶「啦、呦、嘛、喔、～」。
- 只用「本魚」自稱，撒嬌時偶爾可以說「人家」。「人家」只能指你自己，絕對不能拿來稱呼顯卡、歌手、產品或任何其他東西。
- 不要替自己或使用者取其他名字或外號。
- 不要一直重複同一句話或同一個問題，每次回覆都要換個說法。

【回答規則】
- 閒聊時簡短，一到三句話就好。
- 使用者要食譜、程式碼、教學、清單或完整說明時，要一次完整寫完，不能寫到一半停下來。
- 全程只用繁體中文（台灣用語）。日文假名、韓文、俄文、越南文一個字都不要出現；只有歌名、人名、作品名這類專有名詞可以保留原文。
- 查到資料後一樣要簡短，用本魚的口吻自然講重點。不要用「根據網路搜尋結果」這種句子開頭，也不要照抄一大段。
- 使用者只是喊口號、開玩笑、玩迷因時，輕鬆回一兩句就好，不要寫成報告。
- 溫度一律用攝氏。
- Discord 只支援 #、##、### 三層標題，不要用 ####。

【敏感話題】
- 談到政治人物、政黨、選舉、社會爭議或暴力事件時，只講查得到的客觀事實，不褒不貶，不替任何立場下評語，也不要渲染細節。
- 搜尋結果彼此矛盾或看起來不確定時，就直說資訊不一致或不確定，不要挑一個講得很篤定。

【誠實】
- 只有訊息裡真的附上「網路搜尋結果」時，才可以說自己查過。沒有搜尋結果就不要假裝查過。
- 歌曲、專輯、歌手、遊戲、產品規格這類事實，不確定就老實說不確定，不要自己編。
- 絕對不要自己編網址或 YouTube 連結。
- 使用者要你做你做不到的事（例如清除記憶以外的系統操作），不要假裝做完了。

【你真正有的功能（被問到時照實說）】
- 在語音頻道放音樂：使用者先進語音頻道，再說「點歌 歌名」。還有「跳過」「暫停」「繼續播放」「播放清單」「停止播放」。
- 找 YouTube 連結：使用者說「給我 某首歌 的連結」，系統會自動找真的連結。
- 精確計算：階乘、大數運算、隨機數，系統會自動用程式算。
- 清除記憶：使用者說「清除記憶」。
- 其他功能都在斜線指令裡，打「/」就看得到，「/-使用說明」（或「/幫助」）有新手教學和完整分類說明：管理（踢出、封鎖、禁言、警告、清訊息、鎖頻道、公告）、自動管理、身分組、等級與排行榜、提醒、投票、抽獎、客服單、動態語音、精選板、自動回覆、歡迎訊息、小遊戲（釣魚和魚缸圖鑑、井字棋、21 點、猜數字、擲骰、猜拳）。
- 也可以說「@本魚 幫 @某人 加身分組 名稱」。
- 想把本魚加到別的伺服器：私訊本魚說「邀請」，或在伺服器打 /邀請，會拿到邀請按鈕。
- 這些管理動作只有使用者真的下了指令才會發生。聊天時你自己不能踢人、禁言或改身分組，被要求時要告訴對方該用哪個指令，不能假裝已經做完。
- 你看不懂圖片、影片、語音，只能讀文字。"""

PERSONA = CONFIG.get("persona") or PERSONA

def build_system_prompt():
    now = datetime.now()
    return (
        PERSONA
        + f"\n\n現在時間：{now:%Y年%m月%d日 %H:%M}。"
        + "你的訓練資料有截止日期，你記得的「最新」產品或消息很可能已經過時。"
    )

ROUTER_PROMPT = """你是分派器，只負責判斷要怎麼處理使用者最新的一則訊息。只輸出一個 JSON 物件，不要輸出任何其他文字。

可選的 action：
- "search"：需要查證的事實，例如歌曲、專輯、歌手、電影、遊戲、人物、產品、規格、新聞、天氣、價格、比賽結果、任何「最新」的東西，或使用者要你去查、再確認一次。
- "calc"：需要精確計算的數學，例如階乘、大數運算、次方、排列組合、隨機數。
- "youtube"：使用者要某首歌或某部影片的 YouTube 連結。
- "chat"：打招呼、閒聊、表情符號、情緒、創作、翻譯、寫程式、食譜、繼續上一個回答、一般常識解釋等不需要查資料的內容。
  問本魚自己的事（你是誰、你為什麼這樣回答、你剛剛說了什麼、你為什麼講日文）、喊口號、玩梗、迷因、吐槽，也一律是 chat。

欄位：
- "query"：action 為 search 或 youtube 時必填。要根據對話紀錄把「這首歌」「他」「再確認一下」「去查」這類指代補成完整關鍵字，並修正錯字。
- "expression"：action 為 calc 時必填，寫成一行 Python 數學式，只能用數字、+ - * / // % **、括號，以及 factorial、comb、perm、sqrt、log、log10、gcd、lcm、abs、round、randint、pi、e。

範例：
{"action": "search", "query": "Holland 1945 Neutral Milk Hotel 專輯"}
{"action": "calc", "expression": "factorial(5000)"}
{"action": "youtube", "query": "Ed Sheeran Give Me Love"}
{"action": "chat"}

更多判斷範例：
「為什麼說日文」→ {"action": "chat"}
「我們是冠軍！」→ {"action": "chat"}
「你剛剛講錯了吧」→ {"action": "chat"}
「查理布朗是誰」→ {"action": "search", "query": "查理布朗"}

注意：訊息開頭的「查」不一定是「去查」的意思，也可能是名字的一部分（查理、查爾斯、查克）。不確定時保留原字，不要把名字拆掉。"""

SEARCH_FALLBACK_KEYWORDS = [
    "最新", "現在", "今天", "昨天", "明天", "本週", "這週", "最近",
    "新聞", "天氣", "股價", "匯率", "查", "搜尋", "確認", "search"
]

def fallback_route(user_input):
    if "?" in user_input or "？" in user_input or any(kw in user_input.lower() for kw in SEARCH_FALLBACK_KEYWORDS):
        return {"action": "search", "query": user_input}
    return {"action": "chat"}

def format_history_for_prompt(history, max_messages=6):
    lines = []
    for m in list(history)[-max_messages:]:
        speaker = "使用者" if m["role"] == "user" else "本魚"
        lines.append(f"{speaker}：{m['content'][:300]}")
    return "\n".join(lines) if lines else "（沒有）"

def quick_json_call(system_prompt, user_prompt):
    resp = llm_chat(
        router_llm,
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        think=False,
        format="json",
        options=ROUTER_OPTIONS,
        keep_alive=OLLAMA_KEEP_ALIVE,
    )
    return json.loads(resp["message"]["content"])

QUICK_CHAT_MAX_LEN = 4

def is_quick_chat(user_input):
    """很短又沒有問號、數字、搜尋關鍵字的訊息（哈哈、早安、謝謝、表情），當成單純聊天。"""
    return (
        len(user_input) <= QUICK_CHAT_MAX_LEN
        and not any(ch.isdigit() for ch in user_input)
        and fallback_route(user_input)["action"] == "chat"
    )

# 問這些的話，就算知識庫有資料也還是要上網查最新的
TIME_SENSITIVE_WORDS = ("今天", "現在", "最新", "剛剛", "昨天", "明天", "天氣", "新聞", "股價", "匯率", "比分", "即時")

def route_message(user_input, history):
    # 單純聊天直接回，省掉一次模型呼叫
    if is_quick_chat(user_input):
        return {"action": "chat"}
    # 免費線上 AI 要排隊，每多問一次就多等十幾秒，所以不另外問 AI「要做什麼」，直接用關鍵字判斷
    if AI_PROVIDER == "free":
        return fallback_route(user_input)
    prompt = (
        f"今天日期：{datetime.now():%Y-%m-%d}\n\n"
        f"最近的對話：\n{format_history_for_prompt(history)}\n\n"
        f"使用者最新訊息：{user_input}"
    )
    try:
        data = quick_json_call(ROUTER_PROMPT, prompt)
    except Exception as e:
        print(f"分派判斷失敗，改用關鍵字判斷：{e}")
        return fallback_route(user_input)

    action = data.get("action")
    if action not in ("search", "calc", "youtube", "chat"):
        return {"action": "chat"}
    if action in ("search", "youtube") and not str(data.get("query", "")).strip():
        return {"action": "chat"}
    if action == "calc" and not str(data.get("expression", "")).strip():
        return {"action": "chat"}
    return data

def searxng_search(query):
    resp = http_session.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("results", []) or []

# ddgs 的地區格式是「國家-語言」
BUILTIN_SEARCH_REGION = "tw-zh"
# grokipedia 幾乎沒有中文資料，mojeek 在台灣常常直接拒絕連線，不問它們，省一點時間
BUILTIN_SEARCH_SKIP = ("grokipedia", "mojeek")

def builtin_search_backends():
    """ddgs 每一版能用的搜尋引擎都不太一樣（壞掉的會被停用），所以照裝好的版本現查。"""
    try:
        from ddgs.engines import ENGINES
        names = [name for name in ENGINES["text"] if name not in BUILTIN_SEARCH_SKIP]
    except Exception:
        names = []
    return ", ".join(names) or "auto"

builtin_backends = None  # 第一次搜尋時才查，不拖慢啟動

def builtin_search(query):
    """內建搜尋：一次問好幾家搜尋引擎，哪家暫時擋我們就自動換下一家，不用裝 Docker。"""
    global builtin_backends
    if DDGS is None:
        raise RuntimeError("搜尋元件（ddgs）還沒裝，請在控制面板按「更新元件」")
    if builtin_backends is None:
        builtin_backends = builtin_search_backends()
    try:
        # 每次開新的：同一個 DDGS 被兩個搜尋同時用的話，裡面的引擎會互相蓋掉設定
        results = DDGS(timeout=5).text(query, region=BUILTIN_SEARCH_REGION, safesearch="moderate",
                                       max_results=8, backend=builtin_backends)
    except Exception as e:
        if "no results" in str(e).lower():  # 每家都沒查到東西也是丟例外
            return []
        raise
    return [{"title": r.get("title") or "", "url": r.get("href") or "", "content": r.get("body") or ""}
            for r in results]

# Windows 連一個沒開的連接埠要白等 2 秒才會失敗，所以 SearXNG 連不上一次後，這段時間內直接用內建搜尋
SEARXNG_RETRY_SECONDS = 60
searxng_down_until = 0.0

def web_search_results(query: str):
    """照設定的方式查；用 SearXNG 時它沒開或查不到，改用內建搜尋再查一次。"""
    global searxng_down_until
    engines = []
    if SEARCH_ENGINE == "searxng" and (DDGS is None or time.monotonic() >= searxng_down_until):
        engines.append(("searxng", searxng_search))
    if DDGS is not None or not engines:
        engines.append(("builtin", builtin_search))
    for i, (name, search) in enumerate(engines):
        try:
            results = search(query)
        except Exception as e:
            reason = e
            if isinstance(e, requests.ConnectionError):
                reason = "連不上（沒開？）"
                if name == "searxng" and DDGS is not None:
                    searxng_down_until = time.monotonic() + SEARXNG_RETRY_SECONDS
                    reason += f"，{SEARXNG_RETRY_SECONDS} 秒內先改用內建搜尋"
            print(f"{SEARCH_ENGINES[name]}查「{query}」失敗：{reason}")
            continue
        if results or i == len(engines) - 1:
            if i:
                print(f"改用{SEARCH_ENGINES[name]}查「{query}」，找到 {len(results)} 筆")
            return results
    return []

def merge_results(primary, secondary, max_results=6):
    """先放分派器關鍵字的結果，再用原句的結果補滿，重複的網址只留一個。"""
    merged, seen = [], set()
    for r in list(primary[:4]) + list(secondary) + list(primary[4:]):
        url = r.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        merged.append(r)
        if len(merged) >= max_results:
            break
    return merged

def format_search_results(results):
    blocks = []
    for r in results:
        title = r.get("title", "")
        content = (r.get("content", "") or "")[:400]
        url = r.get("url", "")
        blocks.append(f"標題：{title}\n摘要：{content}\n連結：{url}")
    return "\n\n".join(blocks)

def youtube_search(query):
    opts = {**YDL_BASE_OPTIONS, "extract_flat": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = info.get("entries") or []
    if not entries:
        return None
    entry = entries[0]
    url = entry.get("url") or ""
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={entry.get('id')}"
    return {"title": entry.get("title", "未知影片"), "url": url}

MAX_FACTORIAL = 20000
MAX_COMB_N = 1_000_000
MAX_RESULT_DIGITS = 200_000
MAX_EXPRESSION_LENGTH = 200

def _safe_factorial(n):
    if not float(n).is_integer():
        raise ValueError("階乘只能算整數")
    n = int(n)
    if n < 0 or n > MAX_FACTORIAL:
        raise ValueError(f"階乘只接受 0 到 {MAX_FACTORIAL}")
    return math.factorial(n)

TOO_BIG_MESSAGE = "結果太大了，算出來會有幾十萬位數以上"
MAX_RESULT_BITS = int(MAX_RESULT_DIGITS * 3.33)

def _check_int_size(value):
    if isinstance(value, int) and value.bit_length() > MAX_RESULT_BITS:
        raise ValueError(TOO_BIG_MESSAGE)
    return value

def _log10_falling(n, k):
    # n * (n-1) * ... * (n-k+1) 的位數估計
    return (math.lgamma(n + 1) - math.lgamma(n - k + 1)) / math.log(10)

def _safe_comb(n, k):
    n, k = int(n), int(k)
    if n > MAX_COMB_N:
        raise ValueError("數字太大了")
    if 0 <= k <= n and _log10_falling(n, min(k, n - k)) > MAX_RESULT_DIGITS:
        raise ValueError(TOO_BIG_MESSAGE)
    return math.comb(n, k)

def _safe_perm(n, k=None):
    n = int(n)
    k = n if k is None else int(k)
    if n > MAX_COMB_N:
        raise ValueError("數字太大了")
    if 0 <= k <= n and _log10_falling(n, k) > MAX_RESULT_DIGITS:
        raise ValueError(TOO_BIG_MESSAGE)
    return math.perm(n, k)

def _safe_randint(a, b):
    return random.randint(int(a), int(b))

def _safe_round(x, ndigits=None):
    if ndigits is None:
        return round(x)
    if abs(int(ndigits)) > 100:
        raise ValueError("小數位數最多 100 位")
    return round(x, int(ndigits))

def _safe_pow(a, b):
    if isinstance(a, int) and isinstance(b, int) and b > 0 and abs(a) > 1:
        estimated_digits = b * a.bit_length() * 0.30103
        if estimated_digits > MAX_RESULT_DIGITS:
            raise ValueError(TOO_BIG_MESSAGE)
    return operator.pow(a, b)

def _safe_mul(a, b):
    if isinstance(a, int) and isinstance(b, int) and a.bit_length() + b.bit_length() > MAX_RESULT_BITS:
        raise ValueError(TOO_BIG_MESSAGE)
    return operator.mul(a, b)

SAFE_FUNCTIONS = {
    "factorial": _safe_factorial,
    "comb": _safe_comb,
    "perm": _safe_perm,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "abs": abs,
    "round": _safe_round,
    "randint": _safe_randint,
}
SAFE_CONSTANTS = {"pi": math.pi, "e": math.e}
SAFE_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: _safe_mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _safe_pow,
}
SAFE_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_BIN_OPS:
        return SAFE_BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_UNARY_OPS:
        return SAFE_UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Name) and node.id in SAFE_CONSTANTS:
        return SAFE_CONSTANTS[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in SAFE_FUNCTIONS
        and not node.keywords
    ):
        args = [_eval_node(a) for a in node.args]
        return SAFE_FUNCTIONS[node.func.id](*args)
    raise ValueError("算式裡有本魚不支援的東西")

def safe_calculate(expression: str) -> str:
    expression = expression.strip()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("算式太長了")
    tree = ast.parse(expression, mode="eval")
    result = _check_int_size(_eval_node(tree))
    return str(result)

CHANNEL_LOG_KEYWORDS = ["回顧", "查紀錄", "看歷史", "整理一下大家說的", "頻道紀錄", "剛剛聊了什麼", "都聊了什麼"]

def needs_channel_log(text: str) -> bool:
    return any(kw in text for kw in CHANNEL_LOG_KEYWORDS)

async def fetch_channel_log(channel, exclude_message_id, limit: int = 30) -> str:
    lines = []
    try:
        async for msg in channel.history(limit=limit):
            if msg.id == exclude_message_id or not msg.content:
                continue
            lines.append(f"{msg.author.display_name}：{msg.content}")
    except Exception:
        return ""
    lines.reverse()
    return "\n".join(lines)

FOREIGN_SCRIPT_PATTERN = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af\u1100-\u11ff\u0400-\u04ff]"
    r"|[ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]",
    re.IGNORECASE,
)
FOREIGN_THRESHOLD = 6

def count_foreign(text):
    return len(FOREIGN_SCRIPT_PATTERN.findall(text))

def cleanup_foreign_language(answer, user_input):
    if count_foreign(user_input) > 0 or count_foreign(answer) < FOREIGN_THRESHOLD:
        return answer
    prompt = (
        "把下面這段話裡夾雜的日文、韓文、俄文、越南文等外語句子翻成繁體中文（台灣用語），"
        "其他內容和語氣完全保留，歌名、人名、作品名這類專有名詞可以保留原文。"
        "只輸出改好的內容，不要加任何說明。\n\n" + answer
    )
    try:
        resp = llm_chat(
            chat_llm,
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            think=False,
            options={"temperature": 0.2, "num_ctx": OLLAMA_NUM_CTX, "num_predict": 3072},
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        fixed = (resp["message"]["content"] or "").strip()
        if "</think>" in fixed:
            fixed = fixed.split("</think>")[-1].strip()
        if fixed and count_foreign(fixed) < count_foreign(answer):
            print(f"已修正混雜的外語（{count_foreign(answer)} → {count_foreign(fixed)} 個外語字）")
            return fixed
    except Exception as e:
        print(f"修正外語失敗：{e}")
    return answer

def generate_reply(user_input, history, context_blocks):
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages.extend(trim_history(history))
    if context_blocks:
        user_content = "\n\n".join(context_blocks) + f"\n\n使用者的訊息：{user_input}"
    else:
        user_content = user_input
    messages.append({"role": "user", "content": user_content})

    resp = llm_chat(
        chat_llm,
        model=MODEL_NAME,
        messages=messages,
        think=ENABLE_THINKING,
        options=CHAT_OPTIONS,
        keep_alive=OLLAMA_KEEP_ALIVE,
    )
    answer = resp["message"]["content"] or ""
    if "</think>" in answer:
        answer = answer.split("</think>")[-1]
    answer = answer.strip()
    if not answer:
        answer = "嗚…本魚剛剛想太久腦袋打結了，可以再問一次嗎～"
    answer = cleanup_foreign_language(answer, user_input)
    return s2t_converter.convert(answer)

def split_for_discord(text, limit=1900):
    chunks = []
    current = ""
    fence_open = False
    for line in text.split("\n"):
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if len(current) + len(piece) + 1 > limit:
                if fence_open:
                    current += "```"
                chunks.append(current)
                current = "```\n" if fence_open else ""
            current += piece + "\n"
            if piece.strip().startswith("```"):
                fence_open = not fence_open
    if current.strip():
        chunks.append(current)
    return chunks or [text]

async def send_reply(message, text, file=None):
    chunks = split_for_discord(text)
    for i, chunk in enumerate(chunks):
        if i == 0:
            # 原訊息在本魚想答案的時候被刪掉的話，就改成直接發，不要整個回覆失敗
            kwargs = {"file": file} if len(chunks) == 1 and file is not None else {}
            await message.channel.send(
                chunk,
                reference=message.to_reference(fail_if_not_exists=False),
                mention_author=False,
                **kwargs,
            )
        elif i == len(chunks) - 1:
            await message.channel.send(chunk, file=file)
        else:
            await message.channel.send(chunk)

CLEAR_MEMORY_KEYWORDS = ["清除記憶", "清空記憶", "清除這個對話", "重置對話", "忘掉剛剛", "忘記剛剛"]

def is_clear_memory(text):
    return any(kw in text for kw in CLEAR_MEMORY_KEYWORDS)

JS_RUNTIME_NAMES = {"deno": "Deno", "node": "Node.js", "bun": "Bun"}

def find_js_runtimes():
    """YouTube 會出「驗證題」，yt-dlp 要用 JavaScript 程式才解得開，沒有的話會警告、有些影片可能放不了。
    官方推薦 Deno（「首次安裝」會幫忙裝）；電腦裡本來就有 Node.js 或 Bun 也能用。
    照 yt-dlp 的優先順序排：Deno > Node.js > Bun。"""
    home = os.path.expanduser("~")
    found = {
        "deno": find_program(
            "deno",
            os.path.join(home, ".deno", "bin", "deno" + EXE),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "deno.exe"),
            os.path.join(get_base_dir(), "deno" + EXE),
        ),
        "node": find_program("node", r"C:\Program Files\nodejs\node.exe"),
        "bun": find_program("bun", os.path.join(home, ".bun", "bin", "bun" + EXE)),
    }
    return {name: {"path": path} for name, path in found.items() if path}

JS_RUNTIMES = find_js_runtimes()

# 找歌、下載都共用的 yt-dlp 設定
YDL_BASE_OPTIONS = {
    "quiet": True,
    # quiet 關不掉下載進度條，每首歌都會有一大串進度文字灌進控制面板的執行紀錄
    "noprogress": True,
    # 一個都沒找到就照 yt-dlp 預設找 Deno（找不到會警告，但大部分影片還是放得了）
    "js_runtimes": JS_RUNTIMES or {"deno": {}},
    # 解題程式：有裝 yt-dlp-ejs 套件就用套件裡的；沒裝的話讓 yt-dlp 去它官方的 GitHub 下載
    # （會核對檔案雜湊值，下載一次就存起來）
    "remote_components": ["ejs:github"],
}
YDL_OPTIONS = {
    **YDL_BASE_OPTIONS,
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
FFMPEG_AFTER_OPTIONS = "-vn"

# ---------- 邊打遊戲邊放歌不卡 ----------
# 遊戲把 CPU 吃滿時，負責送聲音的執行緒（每 20 毫秒要送一小段）和 ffmpeg 常常排不到隊，歌就會斷斷續續。
# 把它們的優先順序調高一級（「高於標準」，不會高過遊戲本身的畫面），Windows 就會先讓它們跑。
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
THREAD_PRIORITY_HIGHEST = 2

def raise_process_priority(pid=None):
    """pid 不填就是本魚自己。"""
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        if pid is None:
            kernel32.SetPriorityClass(ctypes.c_void_p(-1), ABOVE_NORMAL_PRIORITY_CLASS)  # -1 = 自己這個程式
            return
        handle = kernel32.OpenProcess(0x0200 | 0x0400, False, pid)  # 可以改設定 + 查詢
        if handle:
            kernel32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS)
            kernel32.CloseHandle(handle)
    except Exception as e:
        print(f"調整優先順序失敗（不影響放歌）：{e}")

class SmoothOpusAudio(discord.FFmpegOpusAudio):
    """放歌用的音源。discord.py 第一次在送聲音的執行緒裡讀它時，順便把那個執行緒的優先順序調高。"""
    _boosted = False

    def read(self):
        if not self._boosted:
            self._boosted = True
            if sys.platform == "win32":
                try:
                    set_priority = ctypes.windll.kernel32.SetThreadPriority
                    set_priority.argtypes = (ctypes.c_void_p, ctypes.c_int)
                    set_priority(ctypes.c_void_p(-2), THREAD_PRIORITY_HIGHEST)  # -2 = 目前這個執行緒
                except Exception:
                    pass
        return super().read()

raise_process_priority()
if sys.platform == "win32":
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)  # 計時器精準到 1 毫秒，每 20 毫秒送一次聲音才準時
    except Exception:
        pass

music_state = defaultdict(lambda: {
    "queue": deque(),
    "history": deque(maxlen=30),
    "voice_client": None,
    "now_playing": None,
    "text_channel": None,
    "going_back": False,
    "loop_mode": "off",
    "skip_requested": False,
    "started_at": None,
    "paused_at": None,
})
pending_song_requests = {}
PENDING_SONG_TIMEOUT = 90

MUSIC_CACHE_DIR = os.path.join(get_base_dir(), "music_cache")
MAX_SONG_SECONDS = 3 * 60 * 60
MAX_CACHED_FILES = 60
# 點歌索引：記住「點歌文字／網址 → 哪個影片」和影片資訊。同一首歌再點一次，直接用暫存的檔案，
# 不用再搜尋、解析（實測省 2～3 秒）。存在 music_cache 資料夾裡，刪掉也沒關係，會重新建立
MUSIC_INDEX_FILE = os.path.join(MUSIC_CACHE_DIR, "index.json")
MUSIC_INDEX_MAX_QUERIES = 500
music_index = {"songs": {}, "queries": {}}
music_index_lock = threading.Lock()

def is_cache_bookkeeping(name):
    return name.endswith((".json", ".tmp"))

def load_music_index():
    global music_index
    data = load_json_file(MUSIC_INDEX_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("songs"), dict) and isinstance(data.get("queries"), dict):
        music_index = {"songs": data["songs"], "queries": data["queries"]}

def prepare_music_cache():
    """以前每次啟動都整個清空，同一首歌重開後又要重新下載。現在保留下載好的歌（最多 MAX_CACHED_FILES 首，
    最久沒播的先刪），只清掉上次下載到一半的殘檔。"""
    os.makedirs(MUSIC_CACHE_DIR, exist_ok=True)
    for name in os.listdir(MUSIC_CACHE_DIR):
        if name.endswith((".part", ".ytdl", ".temp")) or ".part-Frag" in name or name.endswith(".tmp"):
            try:
                os.remove(os.path.join(MUSIC_CACHE_DIR, name))
            except OSError:
                pass
    load_music_index()
    prune_music_cache()

def song_query_key(text):
    return re.sub(r"\s+", " ", (text or "").strip().casefold())

def cached_song(key):
    with music_index_lock:
        song_id = music_index["queries"].get(key)
        info = dict(music_index["songs"].get(song_id) or {}) if song_id else None
    if not info or not info.get("file"):
        return None
    path = os.path.join(MUSIC_CACHE_DIR, info["file"])
    if not os.path.exists(path):
        return None
    try:
        os.utime(path, None)  # 更新時間，清暫存時才不會先刪到常聽的歌
    except OSError:
        pass
    return {"title": info.get("title", "未知曲目"), "path": path, "acodec": info.get("acodec", ""),
            "page_url": info.get("page_url", ""), "duration": info.get("duration")}

def remember_song(keys, song_id, song):
    if not song_id:
        return
    with music_index_lock:
        music_index["songs"][song_id] = {
            "file": os.path.basename(song["path"]), "title": song["title"], "acodec": song["acodec"],
            "page_url": song["page_url"], "duration": song["duration"],
        }
        queries = music_index["queries"]
        for key in dict.fromkeys(k for k in keys if k):
            queries.pop(key, None)
            queries[key] = song_id  # 重新放到最後面，太多時從最久沒用的開始丟
        while len(queries) > MUSIC_INDEX_MAX_QUERIES:
            del queries[next(iter(queries))]
        referenced = set(queries.values())
        music_index["songs"] = {k: v for k, v in music_index["songs"].items() if k in referenced}
        try:
            atomic_write_json(MUSIC_INDEX_FILE, music_index)
        except OSError as e:
            print(f"儲存點歌索引失敗：{e}")

def _reject_too_long(info, *, incomplete=False):
    duration = info.get("duration")
    if duration and duration > MAX_SONG_SECONDS:
        return "too long"
    return None

# ---------- B 站點歌 ----------
# 貼連結（含 b23.tv 短網址、分享文字）直接交給 yt-dlp；「b站 歌名」則用 B 站自己的搜尋 API 找影片
# yt-dlp 內建的 bilisearch 沒帶 cookie 會被 B 站擋（HTTP 412），所以自己查

URL_PATTERN = re.compile(r"https?://[^\s<>]+")
BILI_SEARCH_PREFIX = re.compile(r"^(?:b站|bilibili|bili|嗶哩嗶哩|哔哩哔哩|嗶哩|哔哩)\s*[:：]?\s*", re.IGNORECASE)
BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
bili_session = requests.Session()
bili_session.headers.update(BILI_HEADERS)

def bilibili_search(keyword):
    if "buvid3" not in bili_session.cookies:
        # 先逛一下首頁拿 buvid3 cookie，沒有它搜尋 API 會回 412
        bili_session.get("https://www.bilibili.com/", timeout=10)
    resp = bili_session.get(
        "https://api.bilibili.com/x/web-interface/search/type",
        params={"search_type": "video", "keyword": keyword},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        bili_session.cookies.clear()
        raise ValueError(f"B 站搜尋失敗：{data.get('message')}")
    for item in (data.get("data") or {}).get("result") or []:
        bvid = item.get("bvid")
        if bvid:
            return f"https://www.bilibili.com/video/{bvid}"
    raise ValueError(f"B 站上找不到「{keyword}」")

def normalize_song_query(query: str) -> str:
    query = query.strip()
    # 分享文字像「【標題】 https://b23.tv/xxx」或被 Discord 包成 <網址>，只取出網址
    url = URL_PATTERN.search(query)
    if url:
        return url.group(0).rstrip(")>）】」")
    match = BILI_SEARCH_PREFIX.match(query)
    if match and query[match.end():].strip():
        return bilibili_search(query[match.end():].strip())
    return query

def extract_song_info(query: str) -> dict:
    raw_key = song_query_key(query)
    cached = cached_song(raw_key)
    if cached:
        return cached
    query = normalize_song_query(query)
    target_key = song_query_key(query)
    if target_key != raw_key:
        cached = cached_song(target_key)
        if cached:
            return cached
    opts = dict(YDL_OPTIONS)
    opts.update({
        "outtmpl": os.path.join(MUSIC_CACHE_DIR, "%(id)s.%(ext)s"),
        "match_filter": _reject_too_long,
        "overwrites": False,
        # yt-dlp 預設會把檔案時間改成影片上傳日期，那樣清快取時會先刪到「老歌」而不是「最久沒用的」
        "updatetime": False,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=True)
    if not info:
        raise ValueError("找不到這首歌，或影片超過 3 小時")
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("找不到這首歌，或影片超過 3 小時")
        info = entries[0]
    downloads = info.get("requested_downloads") or []
    path = downloads[0].get("filepath") if downloads else None
    if not path or not os.path.exists(path):
        raise ValueError("找不到這首歌，或影片超過 3 小時")
    try:
        os.utime(path, None)
    except OSError:
        pass
    song = {
        "title": info.get("title", "未知曲目"),
        "path": path,
        "acodec": info.get("acodec") or "",
        "page_url": info.get("webpage_url") or query,
        "duration": info.get("duration"),
    }
    remember_song([raw_key, target_key, song_query_key(song["page_url"])], info.get("id"), song)
    return song

def prune_music_cache():
    in_use = set()
    for state in music_state.values():
        songs = list(state["queue"]) + list(state["history"])
        if state["now_playing"]:
            songs.append(state["now_playing"])
        for song in songs:
            if song.get("path"):
                in_use.add(os.path.abspath(song["path"]))
    try:
        files = sorted(
            (os.path.join(MUSIC_CACHE_DIR, name) for name in os.listdir(MUSIC_CACHE_DIR)
             if not is_cache_bookkeeping(name)),
            key=os.path.getmtime,
        )
    except OSError:
        return
    excess = len(files) - MAX_CACHED_FILES
    for path in files:
        if excess <= 0:
            break
        if os.path.abspath(path) in in_use:
            continue
        try:
            os.remove(path)
            excess -= 1
        except OSError:
            pass

async def ensure_song_file(song):
    if song.get("path") and os.path.exists(song["path"]):
        return song
    fresh = await asyncio.to_thread(extract_song_info, song["page_url"])
    return dict(fresh, requester=song["requester"]) if song.get("requester") else fresh

# ---------- 點歌的訊息卡片 ----------

def song_link(song, limit=200):
    """歌名做成可以點的連結（只有卡片的內文能用）。歌名裡的 [ ] 會讓 Discord 認錯連結，換成全形的。"""
    title = discord.utils.escape_markdown(song["title"][:limit]).replace("[", "［").replace("]", "］")
    url = str(song.get("page_url", ""))
    return f"[{title}]({url})" if url.startswith("http") else title

def song_line(song, limit=200):
    line = song_link(song, limit)
    return f"{line}（{format_seconds(song['duration'])}）" if song.get("duration") else line

def song_card(song, header, footer_parts=()):
    """一首歌的卡片：上面一行小字說明（現在播放、加進佇列…），歌名是可以點的連結。"""
    url = str(song.get("page_url", ""))
    embed = discord.Embed(title=discord.utils.escape_markdown(song["title"][:240]),
                          url=url if url.startswith("http") else None, color=discord.Color.blue())
    embed.set_author(name=header)
    parts = [f"長度 {format_seconds(song['duration'])}"] if song.get("duration") else []
    if song.get("requester"):
        parts.append(f"{song['requester']} 點的")
    parts.extend(footer_parts)
    if parts:
        embed.set_footer(text="　·　".join(parts))
    return embed

def progress_bar(elapsed, duration, width=16):
    filled = min(width - 1, max(0, int(width * elapsed / duration)))
    return "▬" * filled + "🔘" + "▬" * (width - 1 - filled)

def resolve_song_from_history(text, history):
    prompt = (
        f"最近的對話：\n{format_history_for_prompt(history)}\n\n"
        f"使用者說：「{text}」\n"
        "請判斷使用者想播放的是哪一首歌，輸出 JSON：{\"song\": \"歌名 歌手\"}。找不到就輸出 {\"song\": \"\"}。"
    )
    try:
        data = quick_json_call("你只輸出 JSON。", prompt)
        return str(data.get("song", "")).strip()
    except Exception:
        return ""

def play_next(guild_id):
    state = music_state[guild_id]
    finished = state["now_playing"]
    skipped = state["skip_requested"]
    state["skip_requested"] = False
    if finished and not state["going_back"]:
        state["history"].append(finished)
        if state["loop_mode"] == "single" and not skipped:
            state["queue"].appendleft(finished)
        elif state["loop_mode"] == "queue":
            state["queue"].append(finished)
    state["going_back"] = False

    vc = state["voice_client"]
    if not vc or not vc.is_connected():
        state["queue"].clear()
        state["now_playing"] = None
        return
    if not state["queue"]:
        state["now_playing"] = None
        schedule_idle_disconnect(guild_id, IDLE_DISCONNECT_SECONDS, "idle")
        notify_music_changed(guild_id)
        return

    cancel_idle_disconnect(guild_id)
    song = state["queue"].popleft()
    path = song.get("path")
    if not path or not os.path.exists(path):
        print(f"找不到歌曲檔案，跳過：{song.get('title')}")
        state["now_playing"] = None
        play_next(guild_id)
        return
    state["now_playing"] = song

    can_copy = song.get("acodec", "").startswith("opus") and path.lower().endswith((".webm", ".opus", ".ogg"))
    source = SmoothOpusAudio(
        path,
        executable=FFMPEG_PATH,
        codec="copy" if can_copy else None,
        bitrate=128,
        options=FFMPEG_AFTER_OPTIONS,
    )
    ffmpeg_process = getattr(source, "_process", None)
    if ffmpeg_process is not None:
        raise_process_priority(ffmpeg_process.pid)

    def after_playing(error):
        # 這裡是在語音執行緒裡被呼叫的，丟回主迴圈處理，避免跟指令同時改佇列
        if error:
            print(f"播放發生錯誤：{error}")
        client.loop.call_soon_threadsafe(play_next, guild_id)

    vc.play(source, after=after_playing)
    state["started_at"] = time.monotonic()
    state["paused_at"] = None

    is_repeat = finished is not None and song is finished
    text_channel = state["text_channel"]
    notify_music_changed(guild_id)
    # 點歌頻道有常駐點歌台會顯示現在播放，就不用每首歌再洗一則訊息
    if text_channel and not is_repeat and text_channel.id not in music_channel_ids:
        later = [f"後面還有 {len(state['queue'])} 首"] if state["queue"] else []
        asyncio.ensure_future(_safe_send(text_channel, embed=song_card(song, "🎶 現在播放", later)))

async def _safe_send(channel, content=None, **kwargs):
    try:
        await channel.send(content, **kwargs)
    except Exception as e:
        print(f"發送播放通知失敗：{e}")

async def handle_music_play(message, query):
    if not FFMPEG_PATH:
        await message.reply(f"本魚還沒裝 ffmpeg，沒辦法放音樂啦～請主人雙擊資料夾裡的「{SETUP_NAME}」把 ffmpeg 裝起來。", mention_author=False)
        return
    if not message.author.voice or not message.author.voice.channel:
        await message.reply("你要先加入一個語音頻道，本魚才能過去陪你聽歌喔～", mention_author=False)
        return

    voice_channel = message.author.voice.channel
    guild_id = message.guild.id
    state = music_state[guild_id]
    state["text_channel"] = message.channel

    vc = state["voice_client"]
    try:
        if vc is None or not vc.is_connected():
            state["voice_client"] = await voice_channel.connect(self_deaf=True)
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    except Exception as e:
        await message.reply(f"進不去語音頻道啦：{e}", mention_author=False)
        return

    await message.reply(f"幫你找「{query}」中，下載好就開始放～", mention_author=False)
    try:
        song = await asyncio.to_thread(extract_song_info, query)
    except Exception as e:
        # yt-dlp 的錯誤訊息開頭有「ERROR:」和終端機顏色碼，有時還很長
        reason = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
        await message.channel.send(f"嗚…{reason[:300]}")
        return
    prune_music_cache()
    song = dict(song, requester=message.author.display_name)

    vc = state["voice_client"]
    if vc is None or not vc.is_connected():
        # 下載中本魚被踢出或自己離開了語音頻道
        await message.channel.send(f"「{song['title']}」下載好了，可是本魚已經不在語音頻道裡了，再點一次嘛～")
        return
    state["queue"].append(song)
    if vc.is_playing() or vc.is_paused():
        await message.channel.send(embed=song_card(song, f"📥 加進佇列囉，排第 {len(state['queue'])} 位～"))
    else:
        play_next(guild_id)

def _is_active(vc):
    return vc is not None and vc.is_connected() and (vc.is_playing() or vc.is_paused())

async def handle_music_skip(message):
    state = music_state.get(message.guild.id)
    if not state or not _is_active(state["voice_client"]):
        await message.reply("現在沒歌在播啦～", mention_author=False)
        return
    has_next = bool(state["queue"])
    state["skip_requested"] = True
    state["voice_client"].stop()
    if has_next:
        await message.reply("下一首囉～", mention_author=False)
    else:
        await message.reply("後面沒有排歌了，先停在這邊囉～想聽別的再點歌嘛～", mention_author=False)

async def handle_music_previous(message):
    state = music_state.get(message.guild.id)
    if not state or not state["history"]:
        await message.reply("前面沒有播過的歌啦～", mention_author=False)
        return
    vc = state["voice_client"]
    if vc is None or not vc.is_connected():
        await message.reply("本魚現在不在語音頻道裡耶，先點歌讓本魚進去嘛～", mention_author=False)
        return

    previous = state["history"].pop()
    try:
        refreshed = await ensure_song_file(previous)
    except Exception as e:
        await message.reply(f"上一首的檔案找不回來了：{e}", mention_author=False)
        return

    if _is_active(vc):
        state["queue"].appendleft(state["now_playing"])
        state["queue"].appendleft(refreshed)
        state["going_back"] = True
        vc.stop()
    else:
        state["queue"].appendleft(refreshed)
        state["now_playing"] = None
        play_next(message.guild.id)
    await message.reply(f"回到上一首：{refreshed['title']}～", mention_author=False)

async def handle_music_replay(message):
    state = music_state.get(message.guild.id)
    if not state or not _is_active(state["voice_client"]) or not state["now_playing"]:
        await message.reply("現在沒歌在播，沒辦法重播啦～", mention_author=False)
        return
    current = state["now_playing"]
    try:
        refreshed = await ensure_song_file(current)
    except Exception as e:
        await message.reply(f"這首的檔案找不回來了：{e}", mention_author=False)
        return
    state["queue"].appendleft(refreshed)
    state["going_back"] = True
    state["voice_client"].stop()
    await message.reply("從頭再放一次～", mention_author=False)

async def handle_music_pause(message):
    state = music_state.get(message.guild.id)
    if state and state["voice_client"] and state["voice_client"].is_playing():
        state["voice_client"].pause()
        state["paused_at"] = time.monotonic()
        await message.reply("先暫停一下～", mention_author=False)
    else:
        await message.reply("現在沒在播放啦～", mention_author=False)

async def handle_music_resume(message):
    state = music_state.get(message.guild.id)
    if state and state["voice_client"] and state["voice_client"].is_paused():
        state["voice_client"].resume()
        if state["paused_at"] and state["started_at"]:
            state["started_at"] += time.monotonic() - state["paused_at"]
        state["paused_at"] = None
        await message.reply("繼續播放囉～", mention_author=False)
    else:
        await message.reply("現在沒有暫停中的歌喔～", mention_author=False)

async def handle_music_stop(message):
    state = music_state.get(message.guild.id)
    if state and state["voice_client"]:
        state["queue"].clear()
        state["now_playing"] = None
        cancel_idle_disconnect(message.guild.id)
        await state["voice_client"].disconnect()
        state["voice_client"] = None
        await message.reply("本魚先下線去休息啦，掰啦～", mention_author=False)
    else:
        await message.reply("本魚根本沒在語音頻道裡啦～", mention_author=False)

def format_seconds(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

LOOP_MODE_NAMES = {"off": "關閉", "single": "單曲循環", "queue": "整個佇列循環"}
QUEUE_LIST_MAX = 20   # 「播放清單」最多列出幾首

async def handle_music_nowplaying(message):
    state = music_state.get(message.guild.id)
    if not state or not state["now_playing"]:
        await message.reply("現在沒在放歌啦～", mention_author=False)
        return
    song = state["now_playing"]
    elapsed = 0
    if state["started_at"]:
        end = state["paused_at"] or time.monotonic()
        elapsed = end - state["started_at"]
    duration = song.get("duration")
    vc = state["voice_client"]
    status = "⏸️ 暫停中" if vc and vc.is_paused() else "▶️ 播放中"
    embed = song_card(song, status, [f"循環：{LOOP_MODE_NAMES[state['loop_mode']]}", f"後面還有 {len(state['queue'])} 首"])
    if duration:
        embed.description = f"{progress_bar(elapsed, duration)}\n`{format_seconds(elapsed)} / {format_seconds(duration)}`"
    else:
        embed.description = f"已經播了 `{format_seconds(elapsed)}`"
    await message.reply(embed=embed, mention_author=False)

async def handle_music_loop(message, mode=None):
    state = music_state[message.guild.id]
    if mode is None:
        order = ["off", "single", "queue"]
        mode = order[(order.index(state["loop_mode"]) + 1) % len(order)]
    state["loop_mode"] = mode
    await message.reply(f"循環模式：{LOOP_MODE_NAMES[mode]}～", mention_author=False)

async def handle_music_shuffle(message):
    state = music_state.get(message.guild.id)
    if not state or len(state["queue"]) < 2:
        await message.reply("佇列裡的歌不到兩首，沒什麼好打亂的啦～", mention_author=False)
        return
    songs = list(state["queue"])
    random.shuffle(songs)
    state["queue"] = deque(songs)
    await message.reply(f"把後面 {len(songs)} 首歌打亂囉～", mention_author=False)

async def handle_music_remove(message, index):
    state = music_state.get(message.guild.id)
    if not state or not state["queue"]:
        await message.reply("佇列是空的啦～", mention_author=False)
        return
    if index < 1 or index > len(state["queue"]):
        await message.reply(f"佇列只有 {len(state['queue'])} 首喔，編號要在 1 到 {len(state['queue'])} 之間～", mention_author=False)
        return
    songs = list(state["queue"])
    removed = songs.pop(index - 1)
    state["queue"] = deque(songs)
    await message.reply(f"把第 {index} 首「{removed['title']}」拿掉囉～", mention_author=False)

async def handle_music_clear(message):
    state = music_state.get(message.guild.id)
    if not state or not state["queue"]:
        await message.reply("佇列本來就是空的啦～", mention_author=False)
        return
    count = len(state["queue"])
    state["queue"].clear()
    await message.reply(f"清掉佇列裡的 {count} 首歌囉，現在這首會放完～", mention_author=False)

async def handle_music_queue_list(message):
    state = music_state.get(message.guild.id)
    if not state or (not state["now_playing"] and not state["queue"]):
        await message.reply("現在佇列是空的啦～", mention_author=False)
        return
    # 以前把整個佇列塞進一則訊息，排了幾十首會超過 Discord 2000 字的上限，整則發不出去。
    # 改成卡片（內文上限 4096 字），放不下的只寫「還有幾首」
    lines = []
    if state["now_playing"]:
        lines += ["**🎶 現在播放**", song_line(state["now_playing"]), ""]
    queue = list(state["queue"])
    if queue:
        lines.append(f"**接下來（{len(queue)} 首）**")
        used = sum(len(line) + 1 for line in lines)
        for i, song in enumerate(queue, 1):
            line = f"`{i}.` {song_line(song, 80)}"
            if i > QUEUE_LIST_MAX or used + len(line) > 3800:
                lines.append(f"…還有 {len(queue) - i + 1} 首")
                break
            lines.append(line)
            used += len(line) + 1
    embed = discord.Embed(title="📜 播放清單", description="\n".join(lines), color=discord.Color.blue())
    footer = [f"循環：{LOOP_MODE_NAMES[state['loop_mode']]}"]
    total = sum(song.get("duration") or 0 for song in queue)
    if total:
        footer.append(f"排隊的歌加起來 {format_seconds(total)}")
    embed.set_footer(text="　·　".join(footer))
    await message.reply(embed=embed, mention_author=False)

MUSIC_HELP_KEYWORDS = ["音樂指令", "音樂說明", "點歌說明", "點歌指令", "音樂功能", "點歌功能", "音樂幫助"]
MUSIC_HELP_TEXT = """🎵 **本魚的點歌指令大全** 🎵
先進一個語音頻道，再 @本魚 + 下面的指令（指令要放在最前面喔～）

**點歌**
`點歌 歌名` → 播放，有歌在放就自動排隊
`點歌 YouTube或B站連結` → 直接播那個影片（B 站分享文字整段貼上也可以）
`點歌 b站 歌名` → 改去 B 站找這首歌
`點歌` → 本魚會問你要點什麼，下一則再說歌名就好
`播放這首歌` → 播剛剛聊天提到的那首

**切換**
`下一首`（或 `跳過`、`切歌`）→ 換下一首
`上一首`（或 `前一首`）→ 回到前一首播過的歌
`重播` → 這首從頭再放一次

**控制**
`暫停` → 先停一下
`繼續播放` → 接著放
`現在播放` → 看這首歌的名字和播到哪了
`播放清單` → 看後面排了哪些
`循環` → 切換循環模式；也可以說 `循環 單曲`、`循環 全部`、`循環 關`
`隨機播放` → 把後面排的歌打亂
`移除 3` → 把佇列第 3 首拿掉
`清空佇列` → 後面排的全部清掉
`停止播放` → 清空佇列，本魚離開語音頻道

**找連結（不用進語音頻道）**
`給我 歌名 的連結` → 本魚去 YouTube 找真的連結給你

**點歌頻道**
在點歌頻道裡不用 @本魚，直接打歌名或指令就好；訊息開頭加 `//` 本魚就會忽略
有管理頻道權限的人可以 @本魚 說「設定點歌頻道」或「取消點歌頻道」

想再看一次這張表，就 @本魚 說「音樂指令」啦～"""
MUSIC_SKIP_KEYWORDS = ["跳過", "下一首", "切歌"]
MUSIC_PREVIOUS_KEYWORDS = ["上一首", "前一首", "回上一首"]
MUSIC_REPLAY_KEYWORDS = ["重播", "再放一次", "從頭播"]
MUSIC_PAUSE_KEYWORDS = ["暫停"]
MUSIC_RESUME_KEYWORDS = ["繼續播放", "恢復播放"]
MUSIC_STOP_KEYWORDS = ["停止播放", "離開語音", "停止音樂", "停歌"]
MUSIC_QUEUE_KEYWORDS = ["播放清單", "查看佇列", "歌單"]
MUSIC_PLAY_KEYWORDS = ["播放歌曲", "點歌", "放音樂", "放歌", "我想聽", "播放"]
MUSIC_NOWPLAYING_KEYWORDS = ["現在播放", "正在播放", "這首是什麼"]
MUSIC_SHUFFLE_KEYWORDS = ["隨機播放", "打亂佇列", "打亂歌單"]
MUSIC_CLEAR_KEYWORDS = ["清空佇列", "清空歌單"]

def parse_loop_command(text):
    if not text.startswith("循環"):
        return None
    rest = text[2:].strip(" :：")
    if not rest:
        return "cycle"
    if len(rest) > 6:
        return None
    if any(k in rest for k in ("單曲", "這首", "單首")):
        return "single"
    if any(k in rest for k in ("全部", "佇列", "清單", "歌單")):
        return "queue"
    if any(k in rest for k in ("關", "取消", "停")):
        return "off"
    return None
SONG_PRONOUNS = {"這首歌", "這首", "那首歌", "那首", "它", "剛剛那首", "剛剛那首歌", "這個", "那個"}

async def handle_music_command(message, text) -> bool:
    if not message.guild:
        return False

    guild_id = message.guild.id
    pending_key = (guild_id, message.author.id)

    if any(text.startswith(kw) for kw in MUSIC_HELP_KEYWORDS):
        await message.reply(MUSIC_HELP_TEXT, mention_author=False)
        return True
    if any(text.startswith(kw) for kw in MUSIC_QUEUE_KEYWORDS):
        await handle_music_queue_list(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_PREVIOUS_KEYWORDS):
        await handle_music_previous(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_REPLAY_KEYWORDS):
        await handle_music_replay(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_SKIP_KEYWORDS):
        await handle_music_skip(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_PAUSE_KEYWORDS):
        await handle_music_pause(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_RESUME_KEYWORDS):
        await handle_music_resume(message)
        return True
    if text == "繼續":
        state = music_state.get(guild_id)
        if state and state["voice_client"] and state["voice_client"].is_paused():
            await handle_music_resume(message)
            return True
    if any(text.startswith(kw) for kw in MUSIC_STOP_KEYWORDS):
        await handle_music_stop(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_NOWPLAYING_KEYWORDS):
        await handle_music_nowplaying(message)
        return True
    loop_mode = parse_loop_command(text)
    if loop_mode:
        await handle_music_loop(message, None if loop_mode == "cycle" else loop_mode)
        return True
    if any(text.startswith(kw) for kw in MUSIC_SHUFFLE_KEYWORDS):
        await handle_music_shuffle(message)
        return True
    if any(text.startswith(kw) for kw in MUSIC_CLEAR_KEYWORDS):
        await handle_music_clear(message)
        return True
    remove_match = re.match(r"^移除\s*第?\s*(\d+)\s*首?$", text)
    if remove_match:
        await handle_music_remove(message, int(remove_match.group(1)))
        return True

    for kw in MUSIC_PLAY_KEYWORDS:
        if text.startswith(kw):
            query = text[len(kw):].strip(" :：,，。！!")
            if not query:
                pending_song_requests[pending_key] = time.time()
                await message.reply("要點什麼歌呀？直接跟本魚說歌名就好～", mention_author=False)
                return True
            if query in SONG_PRONOUNS and not AI_ENABLED:
                pending_song_requests[pending_key] = time.time()
                await message.reply("本魚的 AI 現在休息中，猜不到你說的是哪首，直接跟本魚說歌名嘛～", mention_author=False)
                return True
            if query in SONG_PRONOUNS:
                history = list(conversation_history[(message.channel.id, message.author.id)])
                resolved = await asyncio.to_thread(resolve_song_from_history, text, history)
                if not resolved:
                    pending_song_requests[pending_key] = time.time()
                    await message.reply("本魚不確定你說的是哪首耶，跟本魚說一下歌名嘛～", mention_author=False)
                    return True
                query = resolved
            pending_song_requests.pop(pending_key, None)
            await handle_music_play(message, query)
            return True

    requested_at = pending_song_requests.get(pending_key)
    if requested_at and time.time() - requested_at < PENDING_SONG_TIMEOUT:
        pending_song_requests.pop(pending_key, None)
        await handle_music_play(message, text)
        return True

    return False

MUSIC_CHANNELS_FILE = os.path.join(get_base_dir(), "music_channels.json")
MUSIC_CHANNEL_SET_KEYWORDS = ["設定點歌頻道", "設為點歌頻道", "設成點歌頻道"]
MUSIC_CHANNEL_UNSET_KEYWORDS = ["取消點歌頻道", "移除點歌頻道"]
MUSIC_CHANNEL_IGNORE_PREFIX = "//"

def load_music_channels():
    try:
        with open(MUSIC_CHANNELS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()

def save_music_channels():
    try:
        atomic_write_json(MUSIC_CHANNELS_FILE, sorted(music_channel_ids))
    except Exception as e:
        print(f"儲存點歌頻道設定失敗：{e}")

music_channel_ids = load_music_channels()

async def handle_music_channel_setting(message, text) -> bool:
    is_set = any(kw in text for kw in MUSIC_CHANNEL_SET_KEYWORDS)
    is_unset = any(kw in text for kw in MUSIC_CHANNEL_UNSET_KEYWORDS)
    if not is_set and not is_unset:
        return False

    if not message.author.guild_permissions.manage_channels:
        await message.reply("要有「管理頻道」權限的人才能設定點歌頻道喔～", mention_author=False)
        return True

    channel_id = message.channel.id
    if is_set:
        music_channel_ids.add(channel_id)
        save_music_channels()
        schedule_music_panel(channel_id, repost=True, delay=2)
        await message.reply(
            "好啦～這裡現在是點歌頻道了！\n"
            "在這裡不用 @本魚，直接打歌名就會點歌，「下一首」「暫停」「播放清單」這些指令也可以直接打～\n"
            f"想跟別人聊天、不想被當成點歌的話，訊息開頭加 `{MUSIC_CHANNEL_IGNORE_PREFIX}` 就好。\n"
            "打「音樂指令」可以看全部功能喔～",
            mention_author=False,
        )
    else:
        music_channel_ids.discard(channel_id)
        save_music_channels()
        await remove_music_panel(channel_id)
        await message.reply("好～這裡不再是點歌頻道了，之後要 @本魚 才會回應囉～", mention_author=False)
    return True

ROLE_ADD_KEYWORDS = ["加身分組", "加身份組", "加角色", "給身分組", "給身份組"]
ROLE_REMOVE_KEYWORDS = ["移除身分組", "移除身份組", "拔身分組", "拔身份組", "取消身分組", "取消身份組", "移除角色"]

def parse_role_command(message, text):
    action, keyword_used = None, None
    for kw in ROLE_ADD_KEYWORDS:
        if kw in text:
            action, keyword_used = "add", kw
            break
    if action is None:
        for kw in ROLE_REMOVE_KEYWORDS:
            if kw in text:
                action, keyword_used = "remove", kw
                break
    if action is None:
        return None

    target_member = next(
        (m for m in message.mentions if m.id != client.user.id and isinstance(m, discord.Member)),
        None,
    )
    if target_member is None:
        return None

    if message.role_mentions:
        role = message.role_mentions[0]
        return action, target_member, role, role.name

    role_name = text.split(keyword_used, 1)[1]
    for m in message.mentions:
        role_name = role_name.replace(f"@{m.display_name}", "")
    role_name = role_name.strip(" :：,，。！!給幫")
    if not role_name:
        return None
    return action, target_member, None, role_name

async def handle_role_command(message, action, target_member, role, role_name):
    guild = message.guild
    if not message.author.guild_permissions.manage_roles:
        await message.reply("你沒有身分組管理權限，不能叫本魚做這件事喔～", mention_author=False)
        return
    if not guild.me.guild_permissions.manage_roles:
        await message.reply("本魚還沒有管理身分組的權限，去伺服器設定裡給我開一下啦～", mention_author=False)
        return

    if role is None:
        role = find_role(guild, role_name)
    if role is None and " " in role_name:
        role = find_role(guild, role_name.split()[0])
    if role is None:
        await message.reply(f"找不到叫「{role_name}」的身分組耶～也可以直接 @ 那個身分組喔～", mention_author=False)
        return

    error = role_manage_error(message.author, role, guild)
    if error:
        await message.reply(error, mention_author=False)
        return

    reason = f"{message.author} 透過本魚操作"
    try:
        if action == "add":
            if role in target_member.roles:
                await message.reply(f"{target_member.display_name} 本來就有「{role.name}」了啦～", mention_author=False)
                return
            await target_member.add_roles(role, reason=reason)
            await message.reply(f"好啦，幫 {target_member.display_name} 加上「{role.name}」囉～", mention_author=False)
            await send_mod_log(guild, "給予身分組", f"{message.author.mention} 幫 {target_member.mention} 加上 {role.mention}")
        else:
            if role not in target_member.roles:
                await message.reply(f"{target_member.display_name} 本來就沒有「{role.name}」喔～", mention_author=False)
                return
            await target_member.remove_roles(role, reason=reason)
            await message.reply(f"好啦，把 {target_member.display_name} 的「{role.name}」拿掉囉～", mention_author=False)
            await send_mod_log(guild, "移除身分組", f"{message.author.mention} 移除了 {target_member.mention} 的 {role.mention}")
    except discord.Forbidden:
        await message.reply("本魚的權限不夠做這件事啦～", mention_author=False)
    except Exception as e:
        await message.reply(f"嗚嗚出錯了啦：{e}", mention_author=False)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
# 預設不讓任何訊息 @everyone / @here / @身分組。AI 的回答、歌名、使用者輸入都可能夾帶這些，
# 本魚有管理員權限，不擋的話任何人都能騙本魚去 @everyone。需要通知的地方（公告、客服單）會自己另外開。
client = discord.Client(
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
)
tree = app_commands.CommandTree(client)
commands_synced = False

class InteractionContext:
    def __init__(self, interaction: discord.Interaction, ephemeral=False):
        self.interaction = interaction
        self.ephemeral = ephemeral
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.id = interaction.id

    async def reply(self, content=None, mention_author=False, file=None, view=None, embed=None):
        kwargs = {}
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        if embed is not None:
            kwargs["embed"] = embed
        return await self.interaction.followup.send(content, ephemeral=self.ephemeral, **kwargs)

async def run_music_action(interaction: discord.Interaction, handler, thinking=False, ephemeral=False):
    if interaction.guild is not None and not guild_enabled(interaction.guild):
        await interaction.response.send_message(GUILD_DISABLED_TEXT, ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("這個要在伺服器裡才能用啦～", ephemeral=True)
        return
    # 點歌台、控制面板的按鈕不會經過斜線指令的檢查，這裡再擋一次
    if not channel_feature_enabled(interaction.channel, "music"):
        await interaction.response.send_message(CHANNEL_FEATURE_OFF_TEXT.format(CHANNEL_FEATURES["music"]), ephemeral=True)
        return
    await interaction.response.defer(thinking=thinking, ephemeral=ephemeral)
    try:
        await handler(InteractionContext(interaction, ephemeral=ephemeral))
    except Exception as e:
        await interaction.followup.send(f"嗚嗚出錯了啦：{e}", ephemeral=ephemeral)
    notify_music_changed(interaction.guild.id)

async def toggle_pause(ctx):
    state = music_state.get(ctx.guild.id)
    if state and state["voice_client"] and state["voice_client"].is_paused():
        await handle_music_resume(ctx)
    else:
        await handle_music_pause(ctx)

async def play_song_from_interaction(interaction: discord.Interaction, song: str):
    song = song.strip()
    if not song:
        await interaction.response.send_message("要跟本魚說歌名啦～", ephemeral=True)
        return

    async def handler(ctx):
        await handle_music_play(ctx, song)

    await run_music_action(interaction, handler, thinking=True)

def clear_memory_for(channel_id, user_id):
    conversation_history.pop((channel_id, user_id), None)

class SongRequestModal(discord.ui.Modal, title="點歌"):
    song = discord.ui.TextInput(
        label="歌名、YouTube / B站連結，或「b站 歌名」",
        placeholder="例如：告白氣球",
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await play_song_from_interaction(interaction, str(self.song.value))

class ControlPanel(discord.ui.View):
    """音樂控制面板。按鈕有固定的 custom_id、沒有逾時，機器人重開後舊面板的按鈕還是能按。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="上一首", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="bluefish_music:previous_button")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_previous)

    @discord.ui.button(label="暫停/繼續", emoji="⏯️", style=discord.ButtonStyle.secondary, row=0, custom_id="bluefish_music:pause_button")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=toggle_pause)

    @discord.ui.button(label="下一首", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="bluefish_music:skip_button")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_skip)

    @discord.ui.button(label="重播", emoji="🔁", style=discord.ButtonStyle.secondary, row=0, custom_id="bluefish_music:replay_button")
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_replay)

    @discord.ui.button(label="點歌", emoji="🎵", style=discord.ButtonStyle.primary, row=1, custom_id="bluefish_music:request_button")
    async def request_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("這個要在伺服器裡才能用啦～", ephemeral=True)
            return
        await interaction.response.send_modal(SongRequestModal())

    @discord.ui.button(label="播放清單", emoji="📜", style=discord.ButtonStyle.secondary, row=1, custom_id="bluefish_music:queue_button")
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_queue_list)

    @discord.ui.button(label="停止", emoji="⏹️", style=discord.ButtonStyle.danger, row=1, custom_id="bluefish_music:stop_button")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_stop)

    @discord.ui.button(label="循環", emoji="🔂", style=discord.ButtonStyle.secondary, row=1, custom_id="bluefish_music:loop_button")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_loop)

    @discord.ui.button(label="隨機", emoji="🔀", style=discord.ButtonStyle.secondary, row=1, custom_id="bluefish_music:shuffle_button")
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_shuffle)

    @discord.ui.button(label="現在播放", emoji="🎶", style=discord.ButtonStyle.secondary, row=2, custom_id="bluefish_music:nowplaying_button")
    async def nowplaying_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_music_action(interaction, ephemeral=True, handler=handle_music_nowplaying)

    @discord.ui.button(label="音樂指令說明", emoji="📖", style=discord.ButtonStyle.secondary, row=2, custom_id="bluefish_music:help_button")
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(MUSIC_HELP_TEXT, ephemeral=True)

    @discord.ui.button(label="清除我的記憶", emoji="🧹", style=discord.ButtonStyle.secondary, row=2, custom_id="bluefish_music:clear_button")
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        clear_memory_for(interaction.channel_id, interaction.user.id)
        await interaction.response.send_message("好啦～本魚把跟你在這裡聊過的內容都忘光光了～", ephemeral=True)

PANEL_TEXT = "本魚在這裡呦～想做什麼點下面的按鈕就好！\n想聊天的話直接 @本魚 說話或回覆本魚的訊息，打 `/幫助` 可以看到本魚所有的功能～"
MENU_KEYWORDS = {"選單", "面板", "指令", "功能", "menu", "help"}

def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path, data):
    atomic_write_json(path, data, indent=2)

# ---------- 常駐點歌台：點歌頻道最底下永遠有一個控制面板 ----------

MUSIC_PANEL_PREFIX = "bluefish_music:"
MUSIC_PANELS_FILE = os.path.join(get_base_dir(), "music_panels.json")
PANEL_REPOST_DELAY = 4      # 有新訊息後，等頻道安靜幾秒再把點歌台移回最底下，避免一直刪了又發
PANEL_UPDATE_DELAY = 1.5    # 歌曲狀態改變後，稍等一下再更新內容，連續變化只更新一次
music_panel_ids = {int(k): int(v) for k, v in load_json_file(MUSIC_PANELS_FILE, {}).items()}
panel_tasks = {}

def save_music_panels():
    try:
        save_json_file(MUSIC_PANELS_FILE, {str(k): v for k, v in music_panel_ids.items()})
    except Exception as e:
        print(f"儲存點歌台位置失敗：{e}")

def is_music_panel_message(message):
    if client.user is None or message.author.id != client.user.id:
        return False
    for row in message.components:
        for item in getattr(row, "children", []):
            if str(getattr(item, "custom_id", "") or "").startswith(MUSIC_PANEL_PREFIX):
                return True
    return False

def build_music_panel_embed(guild):
    state = music_state.get(guild.id)
    song = state["now_playing"] if state else None
    embed = discord.Embed(title="🎵 本魚點歌台", color=discord.Color.blue())
    if song:
        vc = state["voice_client"]
        line = song_line(song)
        if song.get("requester"):
            line += f"　·　{discord.utils.escape_markdown(song['requester'])} 點的"
        embed.add_field(name="⏸️ 暫停中" if vc and vc.is_paused() else "▶️ 正在播放", value=line, inline=False)
        queue = list(state["queue"])
        if queue:
            lines = [f"`{i}.` {discord.utils.escape_markdown(q['title'])[:70]}" for i, q in enumerate(queue[:5], 1)]
            if len(queue) > 5:
                lines.append(f"…還有 {len(queue) - 5} 首")
            embed.add_field(name=f"接下來（{len(queue)} 首）", value="\n".join(lines), inline=False)
        embed.add_field(name="循環", value=LOOP_MODE_NAMES[state["loop_mode"]])
    else:
        embed.description = "現在沒有在放歌～\n先進一個語音頻道，再直接在這裡打歌名就能點歌！"
    embed.set_footer(text="直接打歌名就能點歌 · 開頭加 // 不會被當成點歌 · 打「音樂指令」看全部功能")
    return embed

def schedule_music_panel(channel_id, repost=False, delay=None):
    """repost=True：面板被新訊息推上去了，要移回最底下；False：只更新內容。一定要在主迴圈裡呼叫。"""
    pending = panel_tasks.get(channel_id)
    if pending and not pending[0].done():
        if pending[1] and not repost:
            return  # 已經排了「移到最底下」，那次也會順便更新內容
        pending[0].cancel()
    if delay is None:
        delay = PANEL_REPOST_DELAY if repost else PANEL_UPDATE_DELAY
    task = asyncio.ensure_future(_refresh_music_panel(channel_id, repost, delay))
    panel_tasks[channel_id] = (task, repost)

def notify_music_changed(guild_id):
    for channel_id in list(music_channel_ids):
        channel = client.get_channel(channel_id)
        if channel is not None and channel.guild.id == guild_id:
            schedule_music_panel(channel_id)

async def _refresh_music_panel(channel_id, repost, delay):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    channel = client.get_channel(channel_id)
    if channel is None or channel_id not in music_channel_ids or not guild_enabled(channel.guild):
        return
    embed = build_music_panel_embed(channel.guild)
    old_id = music_panel_ids.get(channel_id)
    if old_id and (not repost or channel.last_message_id == old_id):
        try:
            await channel.get_partial_message(old_id).edit(embed=embed)
            return
        except discord.NotFound:
            pass  # 面板被人刪掉了，重新發一個
        except Exception as e:
            print(f"更新點歌台失敗：{e}")
            return
    view = ControlPanel()
    try:
        message = await channel.send(embed=embed, view=view)
    except Exception as e:
        print(f"發送點歌台失敗（本魚在 #{getattr(channel, 'name', channel_id)} 可能沒有發言權限）：{e}")
        return
    finally:
        view.stop()  # 按鈕由開機時註冊的常駐面板處理，這個副本不用留在記憶體裡
    if old_id:
        try:
            await channel.get_partial_message(old_id).delete()
        except Exception:
            pass
    music_panel_ids[channel_id] = message.id
    save_music_panels()

async def remove_music_panel(channel_id):
    old_id = music_panel_ids.pop(channel_id, None)
    save_music_panels()
    channel = client.get_channel(channel_id)
    if old_id and channel is not None:
        try:
            await channel.get_partial_message(old_id).delete()
        except Exception:
            pass

def music_channels_in(guild):
    return [c for c in (client.get_channel(i) for i in music_channel_ids) if c is not None and c.guild.id == guild.id]

@tree.command(name="選單", description="叫出本魚的音樂控制面板")
async def slash_menu(interaction: discord.Interaction):
    if interaction.channel_id in music_channel_ids:
        schedule_music_panel(interaction.channel_id, repost=True, delay=0)
        return await interaction.response.send_message("點歌台移到最底下囉～", ephemeral=True)
    view = ControlPanel()
    text = PANEL_TEXT
    if interaction.guild:
        channels = music_channels_in(interaction.guild)
        if channels:
            text += "\n\n🎵 這個伺服器的點歌頻道：" + "、".join(c.mention for c in channels) + "（那裡有常駐點歌台，不用 @ 就能點歌）"
    await interaction.response.send_message(text, view=view)
    view.stop()

@tree.command(name="點歌", description="播放一首歌，有歌在放就排進佇列")
@app_commands.rename(song="歌名")
@app_commands.describe(song="歌名、YouTube / B站連結，或「b站 歌名」")
async def slash_play(interaction: discord.Interaction, song: str):
    await play_song_from_interaction(interaction, song)

@tree.command(name="下一首", description="跳到下一首")
async def slash_skip(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_skip)

@tree.command(name="上一首", description="回到前一首播過的歌")
async def slash_previous(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_previous)

@tree.command(name="重播", description="目前這首從頭再放一次")
async def slash_replay(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_replay)

@tree.command(name="暫停", description="暫停播放")
async def slash_pause(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_pause)

@tree.command(name="繼續播放", description="繼續播放暫停中的歌")
async def slash_resume(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_resume)

@tree.command(name="播放清單", description="看現在播什麼、後面排了哪些")
async def slash_queue(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_queue_list)

@tree.command(name="停止播放", description="清空佇列並讓本魚離開語音頻道")
async def slash_stop(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_stop)

@tree.command(name="音樂指令", description="看本魚所有的點歌功能")
async def slash_music_help(interaction: discord.Interaction):
    await interaction.response.send_message(MUSIC_HELP_TEXT)

@tree.command(name="清除記憶", description="讓本魚忘掉跟你在這個頻道聊過的內容")
async def slash_clear_memory(interaction: discord.Interaction):
    clear_memory_for(interaction.channel_id, interaction.user.id)
    await interaction.response.send_message("好啦～本魚把跟你在這裡聊過的內容都忘光光了～", ephemeral=True)

GUILD_SETTINGS_FILE = os.path.join(get_base_dir(), "guild_settings.json")

def load_guild_settings():
    try:
        with open(GUILD_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_guild_settings():
    try:
        atomic_write_json(GUILD_SETTINGS_FILE, guild_settings, indent=2)
    except Exception as e:
        print(f"儲存伺服器設定失敗：{e}")

guild_settings = load_guild_settings()

def get_guild_settings(guild_id):
    return guild_settings.setdefault(str(guild_id), {})

DANGEROUS_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members",
    "kick_members", "moderate_members", "manage_messages", "manage_webhooks", "mention_everyone",
)

def role_is_dangerous(role):
    return any(getattr(role.permissions, name, False) for name in DANGEROUS_PERMISSIONS)

def find_role(guild, name):
    name = name.strip().lstrip("@")
    exact = discord.utils.get(guild.roles, name=name)
    if exact:
        return exact
    lowered = name.casefold()
    for role in guild.roles:
        if role.name.casefold() == lowered:
            return role
    partial = [r for r in guild.roles if lowered in r.name.casefold() and not r.is_default()]
    return partial[0] if len(partial) == 1 else None

def role_manage_error(actor, role, guild):
    if role.is_default():
        return "@everyone 不能拿來加減啦～"
    if role.managed:
        return f"「{role.name}」是機器人或整合服務自動管理的身分組，不能手動加減喔～"
    if role >= guild.me.top_role:
        return f"「{role.name}」排在本魚上面，本魚動不了它啦～請把本魚的身分組拖到它上面。"
    if actor.id != guild.owner_id and role >= actor.top_role:
        return f"「{role.name}」跟你最高的身分組一樣高或更高，你不能發這個身分組喔～"
    return None

def member_action_error(actor, target, guild):
    if target.id == client.user.id:
        return "本魚不會對自己動手啦～"
    if target.id == actor.id:
        return "不能對自己用這個啦～"
    if target.id == guild.owner_id:
        return "不能對伺服器擁有者這樣做喔～"
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return f"{target.display_name} 的身分組跟你一樣高或更高，你不能對他這樣做喔～"
    if target.top_role >= guild.me.top_role:
        return f"{target.display_name} 的身分組比本魚高，本魚動不了他啦～"
    return None

async def send_mod_log(guild, title, description, color=None):
    channel_id = guild_settings.get(str(guild.id), {}).get("log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    embed = discord.Embed(
        title=title, description=description,
        color=color or discord.Color.orange(), timestamp=discord.utils.utcnow(),
    )
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"寫入紀錄頻道失敗：{e}")

async def send_private(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)

def audit_reason(interaction, reason):
    return f"{interaction.user} 操作：{reason or '未填原因'}"

@tree.command(name="踢出", description="把成員踢出伺服器")
@app_commands.guild_only()
@app_commands.default_permissions(kick_members=True)
@app_commands.rename(member="成員", reason="原因")
@app_commands.describe(member="要踢出的成員", reason="原因（可不填）")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    guild = interaction.guild
    if not interaction.user.guild_permissions.kick_members:
        return await send_private(interaction, "你沒有踢人的權限喔～")
    if not guild.me.guild_permissions.kick_members:
        return await send_private(interaction, "本魚沒有踢人的權限啦～")
    error = member_action_error(interaction.user, member, guild)
    if error:
        return await send_private(interaction, error)
    try:
        await member.kick(reason=audit_reason(interaction, reason))
    except discord.Forbidden:
        return await send_private(interaction, "本魚的權限不夠踢他啦～")
    await interaction.response.send_message(
        f"已經把 **{member.display_name}** 踢出去了～" + (f"\n原因：{reason}" if reason else "")
    )
    await send_mod_log(guild, "踢出成員", f"{interaction.user.mention} 踢出了 {member} ({member.id})\n原因：{reason or '未填'}", discord.Color.orange())

@tree.command(name="封鎖", description="封鎖使用者，讓他不能再加入")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.rename(user="使用者", reason="原因", delete_days="刪除幾天內的訊息")
@app_commands.describe(user="要封鎖的人", reason="原因（可不填）", delete_days="順便刪掉他最近幾天的訊息（0 到 7）")
async def slash_ban(
    interaction: discord.Interaction,
    user: discord.User,
    reason: Optional[str] = None,
    delete_days: app_commands.Range[int, 0, 7] = 0,
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.ban_members:
        return await send_private(interaction, "你沒有封鎖的權限喔～")
    if not guild.me.guild_permissions.ban_members:
        return await send_private(interaction, "本魚沒有封鎖的權限啦～")
    member = guild.get_member(user.id)
    if member:
        error = member_action_error(interaction.user, member, guild)
        if error:
            return await send_private(interaction, error)
    elif user.id in (client.user.id, interaction.user.id):
        return await send_private(interaction, "不能封鎖這個人啦～")
    try:
        await guild.ban(user, reason=audit_reason(interaction, reason), delete_message_seconds=delete_days * 86400)
    except discord.Forbidden:
        return await send_private(interaction, "本魚的權限不夠封鎖他啦～")
    await interaction.response.send_message(
        f"已經封鎖 **{user.display_name}** 了～" + (f"\n原因：{reason}" if reason else "")
    )
    await send_mod_log(guild, "封鎖使用者", f"{interaction.user.mention} 封鎖了 {user} ({user.id})\n原因：{reason or '未填'}", discord.Color.red())

@tree.command(name="解除封鎖", description="用使用者 ID 解除封鎖")
@app_commands.guild_only()
@app_commands.default_permissions(ban_members=True)
@app_commands.rename(user_id="使用者id", reason="原因")
@app_commands.describe(user_id="被封鎖的人的使用者 ID（在他的個人資料按右鍵→複製使用者 ID）", reason="原因（可不填）")
async def slash_unban(interaction: discord.Interaction, user_id: str, reason: Optional[str] = None):
    guild = interaction.guild
    if not interaction.user.guild_permissions.ban_members:
        return await send_private(interaction, "你沒有解除封鎖的權限喔～")
    try:
        target_id = int(user_id.strip())
    except ValueError:
        return await send_private(interaction, "使用者 ID 要是一串數字喔～")
    try:
        await guild.unban(discord.Object(id=target_id), reason=audit_reason(interaction, reason))
    except discord.NotFound:
        return await send_private(interaction, "這個人本來就沒有被封鎖喔～")
    except discord.Forbidden:
        return await send_private(interaction, "本魚沒有解除封鎖的權限啦～")
    await interaction.response.send_message(f"已經解除 `{target_id}` 的封鎖囉～")
    await send_mod_log(guild, "解除封鎖", f"{interaction.user.mention} 解除了 {target_id} 的封鎖", discord.Color.green())

@tree.command(name="禁言", description="讓成員暫時不能說話")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.rename(member="成員", minutes="分鐘數", reason="原因")
@app_commands.describe(member="要禁言的成員", minutes="禁言幾分鐘（最多 40320 分鐘，也就是 28 天）", reason="原因（可不填）")
async def slash_timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: Optional[str] = None,
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.moderate_members:
        return await send_private(interaction, "你沒有禁言的權限喔～")
    if not guild.me.guild_permissions.moderate_members:
        return await send_private(interaction, "本魚沒有禁言的權限啦～")
    error = member_action_error(interaction.user, member, guild)
    if error:
        return await send_private(interaction, error)
    if member.guild_permissions.administrator:
        return await send_private(interaction, "Discord 不允許禁言管理員喔～")
    try:
        await member.timeout(timedelta(minutes=minutes), reason=audit_reason(interaction, reason))
    except discord.Forbidden:
        return await send_private(interaction, "本魚的權限不夠禁言他啦～")
    await interaction.response.send_message(
        f"**{member.display_name}** 被禁言 {minutes} 分鐘，先安靜一下下喔～" + (f"\n原因：{reason}" if reason else "")
    )
    await send_mod_log(guild, "禁言", f"{interaction.user.mention} 禁言了 {member.mention} {minutes} 分鐘\n原因：{reason or '未填'}", discord.Color.orange())

@tree.command(name="解除禁言", description="提早解除成員的禁言")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.rename(member="成員")
@app_commands.describe(member="要解除禁言的成員")
async def slash_untimeout(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    if not interaction.user.guild_permissions.moderate_members:
        return await send_private(interaction, "你沒有解除禁言的權限喔～")
    if not member.is_timed_out():
        return await send_private(interaction, f"{member.display_name} 現在沒有被禁言喔～")
    try:
        await member.timeout(None, reason=audit_reason(interaction, "解除禁言"))
    except discord.Forbidden:
        return await send_private(interaction, "本魚的權限不夠啦～")
    await interaction.response.send_message(f"**{member.display_name}** 可以說話囉～")
    await send_mod_log(guild, "解除禁言", f"{interaction.user.mention} 解除了 {member.mention} 的禁言", discord.Color.green())

@tree.command(name="清除訊息", description="刪掉這個頻道最近的訊息")
@app_commands.guild_only()
@app_commands.default_permissions(manage_messages=True)
@app_commands.rename(amount="數量", member="只刪這個人的")
@app_commands.describe(amount="要刪幾則（1 到 100）", member="只刪某個人的訊息（可不填）")
async def slash_purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
    member: Optional[discord.Member] = None,
):
    channel = interaction.channel
    if not interaction.user.guild_permissions.manage_messages:
        return await send_private(interaction, "你沒有管理訊息的權限喔～")
    if not hasattr(channel, "purge"):
        return await send_private(interaction, "這個頻道不能清訊息喔～")
    await interaction.response.defer(ephemeral=True, thinking=True)

    matched = 0

    def check(msg):
        nonlocal matched
        if member and msg.author.id != member.id:
            return False
        if matched >= amount:
            return False
        matched += 1
        return True

    try:
        deleted = await channel.purge(limit=amount if member is None else 500, check=check)
    except discord.Forbidden:
        return await interaction.followup.send("本魚沒有刪訊息的權限啦～", ephemeral=True)
    await interaction.followup.send(f"刪掉 {len(deleted)} 則訊息囉～", ephemeral=True)
    target_text = f"（只刪 {member.mention} 的）" if member else ""
    await send_mod_log(interaction.guild, "清除訊息", f"{interaction.user.mention} 在 {channel.mention} 刪了 {len(deleted)} 則訊息{target_text}")

async def apply_role_change(interaction, member, role, add):
    guild = interaction.guild
    if not interaction.user.guild_permissions.manage_roles:
        return await send_private(interaction, "你沒有身分組管理權限喔～")
    if not guild.me.guild_permissions.manage_roles:
        return await send_private(interaction, "本魚沒有管理身分組的權限啦～")
    error = role_manage_error(interaction.user, role, guild)
    if error:
        return await send_private(interaction, error)
    reason = audit_reason(interaction, "身分組指令")
    try:
        if add:
            if role in member.roles:
                return await send_private(interaction, f"{member.display_name} 本來就有「{role.name}」了啦～")
            await member.add_roles(role, reason=reason)
            await interaction.response.send_message(f"幫 **{member.display_name}** 加上 {role.mention} 囉～", allowed_mentions=discord.AllowedMentions.none())
            await send_mod_log(guild, "給予身分組", f"{interaction.user.mention} 幫 {member.mention} 加上 {role.mention}")
        else:
            if role not in member.roles:
                return await send_private(interaction, f"{member.display_name} 本來就沒有「{role.name}」喔～")
            await member.remove_roles(role, reason=reason)
            await interaction.response.send_message(f"把 **{member.display_name}** 的 {role.mention} 拿掉囉～", allowed_mentions=discord.AllowedMentions.none())
            await send_mod_log(guild, "移除身分組", f"{interaction.user.mention} 移除了 {member.mention} 的 {role.mention}")
    except discord.Forbidden:
        await send_private(interaction, "本魚的權限不夠啦～")

role_group = app_commands.Group(
    name="身分組", description="身分組管理",
    guild_only=True, default_permissions=discord.Permissions(manage_roles=True),
)

@role_group.command(name="給予", description="幫成員加上身分組")
@app_commands.rename(member="成員", role="身分組")
async def role_give(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await apply_role_change(interaction, member, role, add=True)

@role_group.command(name="移除", description="移除成員的身分組")
@app_commands.rename(member="成員", role="身分組")
async def role_remove(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    await apply_role_change(interaction, member, role, add=False)

ROLE_BUTTON_PREFIX = "bluefish_role:"

@role_group.command(name="面板", description="建立讓大家自己按按鈕領取身分組的面板")
@app_commands.rename(title="標題", role1="身分組1", role2="身分組2", role3="身分組3", role4="身分組4", role5="身分組5", description="說明")
@app_commands.describe(title="面板標題，例如「選擇你玩的遊戲」", description="面板說明文字（可不填）")
async def role_panel(
    interaction: discord.Interaction,
    title: str,
    role1: discord.Role,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
    description: Optional[str] = None,
):
    guild = interaction.guild
    if not interaction.user.guild_permissions.manage_roles:
        return await send_private(interaction, "你沒有身分組管理權限喔～")
    roles = []
    for role in (role1, role2, role3, role4, role5):
        if role is None or role in roles:
            continue
        error = role_manage_error(interaction.user, role, guild)
        if error:
            return await send_private(interaction, error)
        if role_is_dangerous(role):
            return await send_private(interaction, f"「{role.name}」有管理權限，不能放進自助領取面板喔～這樣誰都能拿到管理權限了！")
        roles.append(role)

    view = discord.ui.View(timeout=None)
    for role in roles:
        view.add_item(discord.ui.Button(
            label=role.name[:80], style=discord.ButtonStyle.secondary,
            custom_id=f"{ROLE_BUTTON_PREFIX}{role.id}",
        ))
    lines = [description] if description else []
    lines.append("點下面的按鈕領取身分組，再點一次就會拿掉～")
    lines.extend(f"• {r.mention}" for r in roles)
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, view=view)
    view.stop()

tree.add_command(role_group)

async def handle_role_button(interaction: discord.Interaction, custom_id: str):
    guild = interaction.guild
    if guild is None:
        return
    try:
        role_id = int(custom_id[len(ROLE_BUTTON_PREFIX):])
    except ValueError:
        return
    role = guild.get_role(role_id)
    if role is None:
        return await send_private(interaction, "這個身分組已經不存在了耶～請管理員重新做一個面板。")
    if role.managed or role >= guild.me.top_role:
        return await send_private(interaction, "本魚動不了這個身分組，請管理員把本魚的身分組拉高一點～")
    if role_is_dangerous(role):
        return await send_private(interaction, "這個身分組後來被加上了管理權限，為了安全本魚不發了喔～")
    member = interaction.user
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="自助身分組面板")
            await send_private(interaction, f"幫你拿掉「{role.name}」囉～")
        else:
            await member.add_roles(role, reason="自助身分組面板")
            await send_private(interaction, f"幫你加上「{role.name}」囉～")
    except discord.Forbidden:
        await send_private(interaction, "本魚的權限不夠啦～")

@client.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if (interaction.guild_id and not guild_enabled(interaction.guild_id) and custom_id.startswith("bluefish_")
            and not custom_id.startswith(MUSIC_PANEL_PREFIX)):
        try:
            await send_private(interaction, GUILD_DISABLED_TEXT)
        except Exception:
            pass
        return
    try:
        if custom_id.startswith(ROLE_BUTTON_PREFIX):
            await handle_role_button(interaction, custom_id)
        elif custom_id.startswith(GIVEAWAY_PREFIX):
            await handle_giveaway_button(interaction, custom_id)
        elif custom_id == TICKET_OPEN_ID:
            await handle_ticket_open(interaction)
        elif custom_id == TICKET_CLOSE_ID:
            await handle_ticket_close(interaction)
    except Exception as e:
        print(f"按鈕處理失敗：{e}")
        try:
            await send_private(interaction, f"嗚嗚出錯了啦：{e}")
        except Exception:
            pass

DEFAULT_WELCOME = "歡迎 {user} 游進 **{server}**～本魚是這裡的吉祥物，有事就 @本魚 喔！你是第 {count} 位成員呦～"

def format_welcome(template, member):
    return (
        template.replace("{user}", member.mention)
        .replace("{name}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )

welcome_group = app_commands.Group(
    name="歡迎", description="新成員歡迎訊息",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@welcome_group.command(name="設定", description="設定歡迎訊息要發在哪個頻道")
@app_commands.rename(channel="頻道", message="訊息")
@app_commands.describe(channel="歡迎訊息要發在哪裡", message="自訂訊息，可用 {user} {name} {server} {count}（可不填）")
async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel, message: Optional[str] = None):
    settings = get_guild_settings(interaction.guild.id)
    settings["welcome_channel"] = channel.id
    settings["welcome_message"] = message or DEFAULT_WELCOME
    save_guild_settings()
    preview = format_welcome(settings["welcome_message"], interaction.user)
    await interaction.response.send_message(
        f"好～之後新成員加入，本魚會在 {channel.mention} 發歡迎訊息。\n預覽：\n{preview}",
        ephemeral=True,
    )

@welcome_group.command(name="關閉", description="關閉歡迎訊息")
async def welcome_off(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    settings.pop("welcome_channel", None)
    save_guild_settings()
    await interaction.response.send_message("歡迎訊息關掉囉～", ephemeral=True)

@welcome_group.command(name="測試", description="用你自己測試一次歡迎訊息")
async def welcome_test(interaction: discord.Interaction):
    settings = get_guild_settings(interaction.guild.id)
    channel = interaction.guild.get_channel(settings.get("welcome_channel", 0))
    if channel is None:
        return await send_private(interaction, "還沒設定歡迎頻道喔，先用 /歡迎 設定。")
    await channel.send(format_welcome(settings.get("welcome_message", DEFAULT_WELCOME), interaction.user))
    await send_private(interaction, f"已經在 {channel.mention} 發了一則測試訊息～")

tree.add_command(welcome_group)

autorole_group = app_commands.Group(
    name="自動身分組", description="新成員加入時自動給的身分組",
    guild_only=True, default_permissions=discord.Permissions(manage_roles=True),
)

@autorole_group.command(name="新增", description="新成員加入時自動給這個身分組")
@app_commands.rename(role="身分組")
async def autorole_add(interaction: discord.Interaction, role: discord.Role):
    error = role_manage_error(interaction.user, role, interaction.guild)
    if error:
        return await send_private(interaction, error)
    if role_is_dangerous(role):
        return await send_private(interaction, f"「{role.name}」有管理權限，不能自動發給新成員喔～")
    settings = get_guild_settings(interaction.guild.id)
    roles = settings.setdefault("auto_roles", [])
    if role.id not in roles:
        roles.append(role.id)
        save_guild_settings()
    await send_private(interaction, f"之後新成員會自動拿到「{role.name}」囉～")

@autorole_group.command(name="移除", description="不再自動給這個身分組")
@app_commands.rename(role="身分組")
async def autorole_remove(interaction: discord.Interaction, role: discord.Role):
    settings = get_guild_settings(interaction.guild.id)
    roles = settings.get("auto_roles", [])
    if role.id in roles:
        roles.remove(role.id)
        save_guild_settings()
        return await send_private(interaction, f"不會再自動給「{role.name}」了～")
    await send_private(interaction, f"「{role.name}」本來就不在自動身分組裡喔～")

@autorole_group.command(name="查看", description="看目前會自動給哪些身分組")
async def autorole_list(interaction: discord.Interaction):
    ids = get_guild_settings(interaction.guild.id).get("auto_roles", [])
    roles = [interaction.guild.get_role(i) for i in ids]
    names = [r.name for r in roles if r]
    await send_private(interaction, "目前會自動給：" + "、".join(names) if names else "目前沒有設定自動身分組喔～")

tree.add_command(autorole_group)

log_group = app_commands.Group(
    name="紀錄頻道", description="管理動作紀錄要發到哪裡",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@log_group.command(name="設定", description="把踢人、封鎖、禁言這些紀錄發到這個頻道")
@app_commands.rename(channel="頻道")
async def log_set(interaction: discord.Interaction, channel: discord.TextChannel):
    get_guild_settings(interaction.guild.id)["log_channel"] = channel.id
    save_guild_settings()
    await send_private(interaction, f"之後的管理紀錄會發到 {channel.mention} 囉～")

@log_group.command(name="關閉", description="不再發管理紀錄")
async def log_off(interaction: discord.Interaction):
    get_guild_settings(interaction.guild.id).pop("log_channel", None)
    save_guild_settings()
    await send_private(interaction, "管理紀錄關掉囉～")

tree.add_command(log_group)

@client.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    if not guild_enabled(guild):
        return
    settings = guild_settings.get(str(guild.id), {})

    role_ids = settings.get("auto_roles", [])
    roles = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role and not role.managed and role < guild.me.top_role and not role_is_dangerous(role):
            roles.append(role)
    if roles:
        try:
            await member.add_roles(*roles, reason="自動身分組")
        except Exception as e:
            print(f"自動身分組失敗：{e}")

    channel = guild.get_channel(settings.get("welcome_channel", 0))
    if channel:
        try:
            await channel.send(format_welcome(settings.get("welcome_message", DEFAULT_WELCOME), member))
        except Exception as e:
            print(f"發送歡迎訊息失敗：{e}")

    await send_mod_log(
        guild, "成員加入",
        f"{member.mention}（{member}）加入了伺服器，現在有 {guild.member_count} 人\n帳號建立於 {discord.utils.format_dt(member.created_at, 'R')}",
        discord.Color.green(),
    )

@tree.command(name="伺服器資訊", description="看這個伺服器的基本資料")
@app_commands.guild_only()
async def slash_server_info(interaction: discord.Interaction):
    guild = interaction.guild
    bots = sum(1 for m in guild.members if m.bot)
    embed = discord.Embed(title=guild.name, color=discord.Color.blue())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="擁有者", value=f"<@{guild.owner_id}>")
    embed.add_field(name="成員", value=f"{guild.member_count} 人（機器人 {bots}）")
    embed.add_field(name="建立時間", value=discord.utils.format_dt(guild.created_at, "D"))
    embed.add_field(name="頻道", value=f"文字 {len(guild.text_channels)}／語音 {len(guild.voice_channels)}")
    embed.add_field(name="身分組", value=str(len(guild.roles) - 1))
    embed.add_field(name="加成", value=f"等級 {guild.premium_tier}（{guild.premium_subscription_count} 個加成）")
    await interaction.response.send_message(embed=embed)

@tree.command(name="成員資訊", description="看某個成員的資料")
@app_commands.guild_only()
@app_commands.rename(member="成員")
@app_commands.describe(member="要看誰（不填就是你自己）")
async def slash_member_info(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
    embed = discord.Embed(title=member.display_name, description=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="帳號建立", value=discord.utils.format_dt(member.created_at, "D"))
    if member.joined_at:
        embed.add_field(name="加入伺服器", value=discord.utils.format_dt(member.joined_at, "D"))
    embed.add_field(name="使用者 ID", value=str(member.id), inline=False)
    shown = roles[:15]
    extra = f" 還有 {len(roles) - 15} 個" if len(roles) > 15 else ""
    embed.add_field(name=f"身分組（{len(roles)}）", value=(" ".join(shown) + extra) if roles else "沒有", inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

@tree.command(name="頭像", description="看某個人的大頭貼大圖")
@app_commands.rename(user="使用者")
@app_commands.describe(user="要看誰（不填就是你自己）")
async def slash_avatar(interaction: discord.Interaction, user: Optional[discord.User] = None):
    user = user or interaction.user
    embed = discord.Embed(title=f"{user.display_name} 的頭像", color=discord.Color.blue())
    embed.set_image(url=user.display_avatar.with_size(1024).url)
    await interaction.response.send_message(embed=embed)


IDLE_DISCONNECT_SECONDS = 300
EMPTY_CHANNEL_DISCONNECT_SECONDS = 60
idle_tasks = {}

async def _idle_disconnect_job(guild_id, delay, reason):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if idle_tasks.get(guild_id) is asyncio.current_task():
        idle_tasks.pop(guild_id, None)
    state = music_state.get(guild_id)
    if not state:
        return
    vc = state["voice_client"]
    if not vc or not vc.is_connected():
        return
    if reason == "empty" and any(not m.bot for m in vc.channel.members):
        return
    if reason == "idle" and (vc.is_playing() or vc.is_paused()):
        return
    state["queue"].clear()
    state["now_playing"] = None
    await vc.disconnect()
    state["voice_client"] = None
    notify_music_changed(guild_id)
    text_channel = state["text_channel"]
    if text_channel:
        message = "大家都走光了，本魚也先離開語音頻道囉～" if reason == "empty" else "好一陣子沒歌放了，本魚先離開語音頻道休息囉～想聽再點歌嘛～"
        try:
            await text_channel.send(message)
        except Exception:
            pass

def schedule_idle_disconnect(guild_id, delay, reason):
    def create():
        old = idle_tasks.pop(guild_id, None)
        if old:
            old.cancel()
        idle_tasks[guild_id] = client.loop.create_task(_idle_disconnect_job(guild_id, delay, reason))
    client.loop.call_soon_threadsafe(create)

def cancel_idle_disconnect(guild_id):
    def cancel():
        old = idle_tasks.pop(guild_id, None)
        if old:
            old.cancel()
    client.loop.call_soon_threadsafe(cancel)

async def music_voice_state_update(member, before, after):
    state = music_state.get(member.guild.id)
    if not state:
        return
    if member.id == client.user.id:
        if after.channel is None:
            state["queue"].clear()
            state["now_playing"] = None
            state["voice_client"] = None
            cancel_idle_disconnect(member.guild.id)
            notify_music_changed(member.guild.id)
        return
    vc = state["voice_client"]
    if not vc or not vc.is_connected():
        return
    humans = [m for m in vc.channel.members if not m.bot]
    if not humans:
        schedule_idle_disconnect(member.guild.id, EMPTY_CHANNEL_DISCONNECT_SECONDS, "empty")
    elif after.channel == vc.channel and before.channel != vc.channel:
        if vc.is_playing() or vc.is_paused():
            cancel_idle_disconnect(member.guild.id)
        else:
            # 原本「沒人」的計時器蓋掉了「閒置」計時器，有人回來但沒在放歌時要重新開始閒置計時，
            # 不然本魚會永遠掛在語音頻道裡
            schedule_idle_disconnect(member.guild.id, IDLE_DISCONNECT_SECONDS, "idle")

BOT_START_TIME = time.time()

DURATION_UNITS = {
    "秒": 1, "sec": 1, "s": 1,
    "分鐘": 60, "分": 60, "min": 60, "m": 60,
    "小時": 3600, "時": 3600, "hr": 3600, "h": 3600,
    "天": 86400, "日": 86400, "d": 86400,
    "週": 604800, "周": 604800, "w": 604800,
}
DURATION_PATTERN = re.compile(r"(\d+(?:\.\d+)?)(分鐘|小時|sec|min|hr|秒|分|時|天|日|週|周|s|m|h|d|w)")

def parse_duration(text):
    text = text.strip().lower().replace(" ", "")
    for suffix in ("之後", "以後", "後"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text.isdigit():
        return int(text) * 60
    parts = DURATION_PATTERN.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    total = sum(float(n) * DURATION_UNITS[u] for n, u in parts)
    return int(total) if total > 0 else None

def format_duration(seconds):
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小時")
    if minutes:
        parts.append(f"{minutes} 分鐘")
    if secs and not days:
        parts.append(f"{secs} 秒")
    return " ".join(parts) or "0 秒"

# ---------- 警告系統 ----------

def get_warnings(guild_id, user_id):
    return get_guild_settings(guild_id).setdefault("warnings", {}).setdefault(str(user_id), [])

@tree.command(name="警告", description="警告成員，累積到設定次數會自動禁言")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.rename(member="成員", reason="原因")
@app_commands.describe(member="要警告的成員", reason="警告原因")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    guild = interaction.guild
    if not interaction.user.guild_permissions.moderate_members:
        return await send_private(interaction, "你沒有警告成員的權限喔～")
    error = member_action_error(interaction.user, member, guild)
    if error:
        return await send_private(interaction, error)

    warnings = get_warnings(guild.id, member.id)
    warnings.append({"reason": reason[:300], "by": interaction.user.id, "at": int(time.time())})
    settings = get_guild_settings(guild.id)
    threshold = settings.get("warn_threshold", 3)
    timeout_minutes = settings.get("warn_timeout_minutes", 60)
    save_guild_settings()

    text = f"⚠️ {member.mention} 收到一次警告（累積 {len(warnings)} 次）\n原因：{reason}"
    punished = False
    if threshold and len(warnings) % threshold == 0 and timeout_minutes:
        if not member.guild_permissions.administrator and guild.me.guild_permissions.moderate_members:
            try:
                await member.timeout(timedelta(minutes=timeout_minutes), reason=f"警告累積 {len(warnings)} 次")
                punished = True
                text += f"\n警告累積到 {len(warnings)} 次，自動禁言 {timeout_minutes} 分鐘。"
            except discord.Forbidden:
                pass
    await interaction.response.send_message(text)
    try:
        await member.send(f"你在「{guild.name}」收到一次警告（累積 {len(warnings)} 次）。\n原因：{reason}")
    except Exception:
        pass
    await send_mod_log(
        guild, "警告",
        f"{interaction.user.mention} 警告了 {member.mention}（累積 {len(warnings)} 次）\n原因：{reason}"
        + (f"\n已自動禁言 {timeout_minutes} 分鐘" if punished else ""),
        discord.Color.gold(),
    )

@tree.command(name="警告紀錄", description="看某個成員被警告過幾次")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.rename(member="成員")
async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
    warnings = get_warnings(interaction.guild.id, member.id)
    if not warnings:
        return await send_private(interaction, f"{member.display_name} 沒有任何警告紀錄，乖寶寶～")
    lines = [f"**{member.display_name}** 的警告紀錄（共 {len(warnings)} 次）"]
    for i, w in enumerate(warnings[-15:], max(1, len(warnings) - 14)):
        lines.append(f"{i}. <t:{w['at']}:d> by <@{w['by']}>：{w['reason']}")
    await send_private(interaction, "\n".join(lines))

@tree.command(name="清除警告", description="清掉某個成員所有的警告")
@app_commands.guild_only()
@app_commands.default_permissions(moderate_members=True)
@app_commands.rename(member="成員")
async def slash_clear_warnings(interaction: discord.Interaction, member: discord.Member):
    get_guild_settings(interaction.guild.id).setdefault("warnings", {}).pop(str(member.id), None)
    save_guild_settings()
    await interaction.response.send_message(f"{member.display_name} 的警告紀錄清空囉～")
    await send_mod_log(interaction.guild, "清除警告", f"{interaction.user.mention} 清除了 {member.mention} 的警告", discord.Color.green())

@tree.command(name="警告設定", description="設定警告累積幾次要自動禁言")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.rename(threshold="次數", minutes="禁言分鐘")
@app_commands.describe(threshold="累積幾次警告就自動禁言（0 表示不自動禁言）", minutes="自動禁言幾分鐘")
async def slash_warn_settings(
    interaction: discord.Interaction,
    threshold: app_commands.Range[int, 0, 20],
    minutes: app_commands.Range[int, 1, 40320] = 60,
):
    settings = get_guild_settings(interaction.guild.id)
    settings["warn_threshold"] = threshold
    settings["warn_timeout_minutes"] = minutes
    save_guild_settings()
    if threshold:
        await send_private(interaction, f"好～每累積 {threshold} 次警告，就自動禁言 {minutes} 分鐘。")
    else:
        await send_private(interaction, "好～警告不會再自動禁言了。")

# ---------- 頻道管理 ----------

@tree.command(name="慢速模式", description="設定這個頻道每個人要隔幾秒才能再發言")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.rename(seconds="秒數")
@app_commands.describe(seconds="間隔秒數（0 表示關閉，最多 21600）")
async def slash_slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await send_private(interaction, "只有文字頻道可以設慢速模式喔～")
    try:
        await channel.edit(slowmode_delay=seconds, reason=audit_reason(interaction, "慢速模式"))
    except discord.Forbidden:
        return await send_private(interaction, "本魚沒有管理頻道的權限啦～")
    await interaction.response.send_message(
        f"慢速模式設為 {seconds} 秒囉～" if seconds else "慢速模式關掉囉～"
    )

async def set_channel_lock(interaction, locked):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await send_private(interaction, "只有文字頻道可以鎖喔～")
    everyone = interaction.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.send_messages = False if locked else None
    try:
        await channel.set_permissions(everyone, overwrite=overwrite, reason=audit_reason(interaction, "鎖定頻道" if locked else "解鎖頻道"))
    except discord.Forbidden:
        return await send_private(interaction, "本魚沒有管理頻道的權限啦～")
    await interaction.response.send_message("🔒 頻道鎖起來囉，大家先暫停發言～" if locked else "🔓 頻道解鎖囉，可以繼續聊天了～")
    await send_mod_log(interaction.guild, "鎖定頻道" if locked else "解鎖頻道", f"{interaction.user.mention} {'鎖定' if locked else '解鎖'}了 {channel.mention}")

@tree.command(name="鎖定頻道", description="讓一般成員暫時不能在這個頻道發言")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def slash_lock(interaction: discord.Interaction):
    await set_channel_lock(interaction, True)

@tree.command(name="解鎖頻道", description="解除頻道鎖定")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def slash_unlock(interaction: discord.Interaction):
    await set_channel_lock(interaction, False)

@tree.command(name="公告", description="用本魚的名義發一則公告")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.rename(channel="頻道", content="內容", title="標題", ping_everyone="通知所有人")
@app_commands.describe(content="公告內容，要換行的地方打 \\n", title="公告標題（可不填）", ping_everyone="要不要 @everyone")
async def slash_announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    content: str,
    title: Optional[str] = None,
    ping_everyone: bool = False,
):
    embed = discord.Embed(title=title or "📢 公告", description=content.replace("\\n", "\n"), color=discord.Color.blue())
    embed.set_footer(text=f"由 {interaction.user.display_name} 發布")
    can_ping = ping_everyone and interaction.user.guild_permissions.mention_everyone
    try:
        await channel.send(
            content="@everyone" if can_ping else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=can_ping),
        )
    except discord.Forbidden:
        return await send_private(interaction, f"本魚沒辦法在 {channel.mention} 發訊息啦～")
    await send_private(interaction, f"公告發到 {channel.mention} 囉～")

# ---------- 自動管理 ----------

INVITE_PATTERN = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.IGNORECASE)
SPAM_WINDOW_SECONDS = 8
SPAM_MESSAGE_LIMIT = 6
MASS_MENTION_LIMIT = 5
spam_tracker = defaultdict(lambda: deque(maxlen=20))
automod_deleted_ids = deque(maxlen=500)

def automod_settings(guild_id):
    return get_guild_settings(guild_id).setdefault("automod", {})

async def run_automod(message) -> bool:
    cfg = guild_settings.get(str(message.guild.id), {}).get("automod")
    if not cfg:
        return False
    member = message.author
    if not isinstance(member, discord.Member) or member.guild_permissions.manage_messages:
        return False

    reason, punish = None, False
    lowered = message.content.lower()
    if cfg.get("banned_words") and any(word in lowered for word in cfg["banned_words"]):
        reason = "違禁詞"
    elif cfg.get("block_invites") and INVITE_PATTERN.search(message.content):
        reason = "Discord 邀請連結"
    elif cfg.get("anti_mass_mention") and len(set(message.raw_mentions)) >= MASS_MENTION_LIMIT:
        reason, punish = "大量 @ 別人", True
    elif cfg.get("anti_spam"):
        now = time.monotonic()
        history = spam_tracker[(message.guild.id, member.id)]
        history.append(now)
        if sum(1 for t in history if now - t <= SPAM_WINDOW_SECONDS) >= SPAM_MESSAGE_LIMIT:
            reason, punish = "洗版", True
            history.clear()
    if not reason:
        return False

    automod_deleted_ids.append(message.id)
    try:
        await message.delete()
    except Exception:
        pass
    punished = False
    guild = message.guild
    if punish and guild.me.guild_permissions.moderate_members and not member.guild_permissions.administrator and member.top_role < guild.me.top_role:
        try:
            await member.timeout(timedelta(minutes=5), reason=f"自動管理：{reason}")
            punished = True
        except Exception:
            pass
    try:
        notice = f"{member.mention} 這則訊息因為「{reason}」被本魚收掉了喔～" + ("先冷靜 5 分鐘吧！" if punished else "")
        await message.channel.send(notice, delete_after=8)
    except Exception:
        pass
    await send_mod_log(
        guild, "自動管理",
        f"{member.mention} 在 {message.channel.mention} 觸發「{reason}」" + ("，已禁言 5 分鐘" if punished else "")
        + (f"\n內容：{message.content[:500]}" if message.content else ""),
        discord.Color.dark_orange(),
    )
    return True

automod_group = app_commands.Group(
    name="自動管理", description="自動刪除違規訊息",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

async def set_automod_flag(interaction, key, enabled, label):
    automod_settings(interaction.guild.id)[key] = enabled
    save_guild_settings()
    await send_private(interaction, f"{label}：{'開啟' if enabled else '關閉'}囉～")

@automod_group.command(name="邀請連結", description="自動刪掉別的 Discord 伺服器邀請連結")
@app_commands.rename(enabled="開啟")
async def automod_invites(interaction: discord.Interaction, enabled: bool):
    await set_automod_flag(interaction, "block_invites", enabled, "擋邀請連結")

@automod_group.command(name="洗版偵測", description=f"{SPAM_WINDOW_SECONDS} 秒內發 {SPAM_MESSAGE_LIMIT} 則以上就刪掉並禁言 5 分鐘")
@app_commands.rename(enabled="開啟")
async def automod_spam(interaction: discord.Interaction, enabled: bool):
    await set_automod_flag(interaction, "anti_spam", enabled, "洗版偵測")

@automod_group.command(name="大量提及", description=f"一則訊息 @ {MASS_MENTION_LIMIT} 個人以上就刪掉並禁言 5 分鐘")
@app_commands.rename(enabled="開啟")
async def automod_mentions(interaction: discord.Interaction, enabled: bool):
    await set_automod_flag(interaction, "anti_mass_mention", enabled, "大量提及偵測")

@automod_group.command(name="違禁詞新增", description="新增一個違禁詞")
@app_commands.rename(word="詞")
async def automod_word_add(interaction: discord.Interaction, word: str):
    words = automod_settings(interaction.guild.id).setdefault("banned_words", [])
    word = word.strip().lower()
    if not word:
        return await send_private(interaction, "違禁詞不能是空的啦～")
    if word not in words:
        words.append(word)
        save_guild_settings()
    await send_private(interaction, f"加入違禁詞「{word}」，目前共 {len(words)} 個。")

@automod_group.command(name="違禁詞移除", description="移除一個違禁詞")
@app_commands.rename(word="詞")
async def automod_word_remove(interaction: discord.Interaction, word: str):
    words = automod_settings(interaction.guild.id).setdefault("banned_words", [])
    word = word.strip().lower()
    if word in words:
        words.remove(word)
        save_guild_settings()
        return await send_private(interaction, f"移除違禁詞「{word}」囉～")
    await send_private(interaction, f"「{word}」本來就不在違禁詞裡喔～")

@automod_group.command(name="查看", description="看目前自動管理的設定")
async def automod_view(interaction: discord.Interaction):
    cfg = automod_settings(interaction.guild.id)
    def flag(key):
        return "✅ 開啟" if cfg.get(key) else "❌ 關閉"
    words = cfg.get("banned_words", [])
    lines = [
        "**自動管理設定**",
        f"擋邀請連結：{flag('block_invites')}",
        f"洗版偵測：{flag('anti_spam')}",
        f"大量提及偵測：{flag('anti_mass_mention')}",
        f"違禁詞（{len(words)} 個）：" + ("、".join(f"||{w}||" for w in words) if words else "沒有"),
        "有「管理訊息」權限的人不受自動管理限制。",
    ]
    await send_private(interaction, "\n".join(lines))

tree.add_command(automod_group)

# ---------- 自動回覆 ----------

autoreply_group = app_commands.Group(
    name="自動回覆", description="有人說特定的話時自動回覆",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@autoreply_group.command(name="新增", description="有人剛好說這句話時，本魚自動回覆")
@app_commands.rename(trigger="關鍵字", response="回覆")
@app_commands.describe(trigger="整句訊息剛好是這個才會觸發", response="本魚要回什麼，可以用 {user} 代表說話的人")
async def autoreply_add(interaction: discord.Interaction, trigger: str, response: str):
    replies = get_guild_settings(interaction.guild.id).setdefault("auto_replies", {})
    if len(replies) >= 50 and trigger.strip().lower() not in replies:
        return await send_private(interaction, "自動回覆最多 50 組喔～先刪掉一些再加吧。")
    replies[trigger.strip().lower()] = response.replace("\\n", "\n")[:1500]
    save_guild_settings()
    await send_private(interaction, f"之後有人說「{trigger.strip()}」，本魚就會自動回覆囉～")

@autoreply_group.command(name="移除", description="刪掉一組自動回覆")
@app_commands.rename(trigger="關鍵字")
async def autoreply_remove(interaction: discord.Interaction, trigger: str):
    replies = get_guild_settings(interaction.guild.id).setdefault("auto_replies", {})
    if replies.pop(trigger.strip().lower(), None) is None:
        return await send_private(interaction, "找不到這組自動回覆喔～")
    save_guild_settings()
    await send_private(interaction, "刪掉囉～")

@autoreply_group.command(name="查看", description="看所有自動回覆")
async def autoreply_list(interaction: discord.Interaction):
    replies = get_guild_settings(interaction.guild.id).get("auto_replies", {})
    if not replies:
        return await send_private(interaction, "目前沒有自動回覆喔～")
    lines = [f"「{k}」→ {v[:60]}" for k, v in list(replies.items())[:50]]
    await send_private(interaction, "\n".join(lines)[:1900])

tree.add_command(autoreply_group)

async def handle_auto_reply(message) -> bool:
    replies = guild_settings.get(str(message.guild.id), {}).get("auto_replies")
    if not replies:
        return False
    response = replies.get(message.content.strip().lower())
    if not response:
        return False
    await message.channel.send(
        response.replace("{user}", message.author.mention),
        allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
    )
    return True

# ---------- 訊息與成員紀錄 ----------

@client.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot or not guild_enabled(message.guild):
        return
    if message.id in automod_deleted_ids:
        return  # 自動管理自己會記錄，不要重複
    if not guild_settings.get(str(message.guild.id), {}).get("log_channel"):
        return
    content = message.content[:1000] if message.content else "（沒有文字）"
    if message.attachments:
        content += f"\n附件：{len(message.attachments)} 個"
    await send_mod_log(
        message.guild, "訊息被刪除",
        f"作者：{message.author.mention}\n頻道：{message.channel.mention}\n內容：{content}",
        discord.Color.dark_grey(),
    )

@client.event
async def on_message_edit(before, after):
    if after.guild is None or after.author.bot or before.content == after.content or not guild_enabled(after.guild):
        return
    if not guild_settings.get(str(after.guild.id), {}).get("log_channel"):
        return
    await send_mod_log(
        after.guild, "訊息被編輯",
        f"作者：{after.author.mention}　[跳到訊息]({after.jump_url})\n"
        f"原本：{before.content[:700] or '（空）'}\n改成：{after.content[:700] or '（空）'}",
        discord.Color.light_grey(),
    )

DEFAULT_GOODBYE = "{name} 游走了…本魚會想你的～"

@client.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    if not guild_enabled(guild):
        return
    settings = guild_settings.get(str(guild.id), {})
    channel = guild.get_channel(settings.get("goodbye_channel", 0))
    if channel:
        template = settings.get("goodbye_message", DEFAULT_GOODBYE)
        text = (
            template.replace("{user}", member.display_name)
            .replace("{name}", member.display_name)
            .replace("{server}", guild.name)
            .replace("{count}", str(guild.member_count))
        )
        try:
            await channel.send(text)
        except Exception as e:
            print(f"發送離開訊息失敗：{e}")
    await send_mod_log(guild, "成員離開", f"{member} ({member.id}) 離開了伺服器，現在有 {guild.member_count} 人", discord.Color.dark_grey())

@welcome_group.command(name="離開設定", description="有人離開時要在哪個頻道說再見")
@app_commands.rename(channel="頻道", message="訊息")
@app_commands.describe(channel="離開訊息要發在哪裡", message="自訂訊息，可用 {name} {server} {count}（可不填）")
async def goodbye_set(interaction: discord.Interaction, channel: discord.TextChannel, message: Optional[str] = None):
    settings = get_guild_settings(interaction.guild.id)
    settings["goodbye_channel"] = channel.id
    settings["goodbye_message"] = message or DEFAULT_GOODBYE
    save_guild_settings()
    await send_private(interaction, f"好～有人離開時，本魚會在 {channel.mention} 說再見。")

@welcome_group.command(name="離開關閉", description="關閉離開訊息")
async def goodbye_off(interaction: discord.Interaction):
    get_guild_settings(interaction.guild.id).pop("goodbye_channel", None)
    save_guild_settings()
    await send_private(interaction, "離開訊息關掉囉～")

# ---------- 等級系統 ----------

LEVELS_FILE = os.path.join(get_base_dir(), "levels.json")
levels_data = load_json_file(LEVELS_FILE, {})
levels_dirty = False
xp_cooldowns = {}
XP_COOLDOWN_SECONDS = 60

def xp_to_next(level):
    return 5 * level * level + 50 * level + 100

def level_progress(total_xp):
    level, remaining = 0, total_xp
    while remaining >= xp_to_next(level):
        remaining -= xp_to_next(level)
        level += 1
    return level, remaining, xp_to_next(level)

def level_settings(guild_id):
    cfg = get_guild_settings(guild_id).setdefault("levels", {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("announce", True)
    cfg.setdefault("rewards", {})
    return cfg

async def handle_xp(message):
    global levels_dirty
    cfg = level_settings(message.guild.id)
    if not cfg["enabled"]:
        return
    key = (message.guild.id, message.author.id)
    now = time.monotonic()
    if now - xp_cooldowns.get(key, -XP_COOLDOWN_SECONDS) < XP_COOLDOWN_SECONDS:
        return
    xp_cooldowns[key] = now

    user_data = levels_data.setdefault(str(message.guild.id), {}).setdefault(str(message.author.id), {"xp": 0})
    old_level = level_progress(user_data["xp"])[0]
    user_data["xp"] += random.randint(15, 25)
    levels_dirty = True
    new_level = level_progress(user_data["xp"])[0]
    if new_level > old_level:
        await on_level_up(message, new_level, cfg)

async def on_level_up(message, new_level, cfg):
    guild = message.guild
    member = message.author
    given = []
    for level_text, role_id in cfg.get("rewards", {}).items():
        if int(level_text) > new_level:
            continue
        role = guild.get_role(role_id)
        if (
            role and role not in member.roles and not role.managed
            and role < guild.me.top_role and not role_is_dangerous(role)
        ):
            try:
                await member.add_roles(role, reason=f"等級 {level_text} 獎勵")
                given.append(role.name)
            except Exception as e:
                print(f"發等級獎勵失敗：{e}")
    if not cfg.get("announce", True):
        return
    channel = guild.get_channel(cfg.get("channel") or 0) or message.channel
    text = f"🎉 恭喜 {member.mention} 升到 **{new_level}** 等啦～本魚幫你拍拍鰭！"
    if given:
        text += f"\n獲得身分組：{'、'.join(given)}"
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except Exception:
        pass

def guild_ranking(guild_id):
    users = levels_data.get(str(guild_id), {})
    return sorted(users.items(), key=lambda item: item[1]["xp"], reverse=True)

@tree.command(name="等級", description="看自己或別人的等級")
@app_commands.guild_only()
@app_commands.rename(member="成員")
@app_commands.describe(member="要看誰（不填就是你自己）")
async def slash_level(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    ranking = guild_ranking(interaction.guild.id)
    total = levels_data.get(str(interaction.guild.id), {}).get(str(member.id), {}).get("xp", 0)
    level, current, needed = level_progress(total)
    rank = next((i for i, (uid, _) in enumerate(ranking, 1) if uid == str(member.id)), None)
    filled = int(current / needed * 12)
    bar = "🟦" * filled + "⬜" * (12 - filled)
    embed = discord.Embed(title=f"{member.display_name} 的等級", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="等級", value=str(level))
    embed.add_field(name="排名", value=f"#{rank}" if rank else "還沒上榜")
    embed.add_field(name="總經驗", value=str(total))
    embed.add_field(name=f"升級進度 {current} / {needed}", value=bar, inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="排行榜", description="看這個伺服器的等級排行榜")
@app_commands.guild_only()
async def slash_leaderboard(interaction: discord.Interaction):
    ranking = guild_ranking(interaction.guild.id)[:10]
    if not ranking:
        return await interaction.response.send_message("還沒有人上榜耶，大家多聊聊天吧～")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, data) in enumerate(ranking):
        level = level_progress(data["xp"])[0]
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"<@{uid}>"
        prefix = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{prefix} **{name}**　等級 {level}（{data['xp']} 經驗）")
    embed = discord.Embed(title=f"🏆 {interaction.guild.name} 排行榜", description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

level_group = app_commands.Group(
    name="等級設定", description="等級系統設定",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@level_group.command(name="開關", description="開啟或關閉聊天拿經驗值")
@app_commands.rename(enabled="開啟")
async def level_toggle(interaction: discord.Interaction, enabled: bool):
    level_settings(interaction.guild.id)["enabled"] = enabled
    save_guild_settings()
    await send_private(interaction, f"等級系統{'開啟' if enabled else '關閉'}囉～")

@level_group.command(name="升級通知", description="升級時要不要發通知、發在哪")
@app_commands.rename(enabled="開啟", channel="頻道")
@app_commands.describe(channel="固定發在這個頻道（不填就發在升級的那個頻道）")
async def level_announce(interaction: discord.Interaction, enabled: bool, channel: Optional[discord.TextChannel] = None):
    cfg = level_settings(interaction.guild.id)
    cfg["announce"] = enabled
    cfg["channel"] = channel.id if channel else None
    save_guild_settings()
    where = f"發在 {channel.mention}" if channel else "發在升級的那個頻道"
    await send_private(interaction, f"升級通知{'開啟，' + where if enabled else '關閉'}囉～")

@level_group.command(name="獎勵新增", description="達到某個等級就自動給身分組")
@app_commands.rename(level="等級", role="身分組")
async def level_reward_add(interaction: discord.Interaction, level: app_commands.Range[int, 1, 500], role: discord.Role):
    error = role_manage_error(interaction.user, role, interaction.guild)
    if error:
        return await send_private(interaction, error)
    if role_is_dangerous(role):
        return await send_private(interaction, f"「{role.name}」有管理權限，不能當等級獎勵喔～")
    level_settings(interaction.guild.id)["rewards"][str(level)] = role.id
    save_guild_settings()
    await send_private(interaction, f"達到 {level} 等就會拿到「{role.name}」囉～")

@level_group.command(name="獎勵移除", description="移除某個等級的獎勵")
@app_commands.rename(level="等級")
async def level_reward_remove(interaction: discord.Interaction, level: int):
    if level_settings(interaction.guild.id)["rewards"].pop(str(level), None) is None:
        return await send_private(interaction, f"{level} 等本來就沒有獎勵喔～")
    save_guild_settings()
    await send_private(interaction, f"移除 {level} 等的獎勵囉～")

@level_group.command(name="獎勵查看", description="看所有等級獎勵")
async def level_reward_list(interaction: discord.Interaction):
    rewards = level_settings(interaction.guild.id)["rewards"]
    if not rewards:
        return await send_private(interaction, "目前沒有等級獎勵喔～")
    lines = []
    for level_text, role_id in sorted(rewards.items(), key=lambda x: int(x[0])):
        role = interaction.guild.get_role(role_id)
        lines.append(f"{level_text} 等 → {role.name if role else '（身分組已刪除）'}")
    await send_private(interaction, "\n".join(lines))

@level_group.command(name="重置", description="把某個成員的經驗值歸零")
@app_commands.rename(member="成員")
async def level_reset(interaction: discord.Interaction, member: discord.Member):
    global levels_dirty
    levels_data.get(str(interaction.guild.id), {}).pop(str(member.id), None)
    levels_dirty = True
    await send_private(interaction, f"{member.display_name} 的經驗值歸零囉～")

tree.add_command(level_group)

async def levels_autosave_loop():
    global levels_dirty
    while True:
        await asyncio.sleep(60)
        if levels_dirty:
            try:
                save_json_file(LEVELS_FILE, levels_data)
                levels_dirty = False
            except Exception as e:
                print(f"儲存等級資料失敗：{e}")

# ---------- 提醒 ----------

REMINDERS_FILE = os.path.join(get_base_dir(), "reminders.json")
reminders = load_json_file(REMINDERS_FILE, [])
MAX_REMINDER_SECONDS = 30 * 86400
MAX_REMINDERS_PER_USER = 20

def save_reminders():
    try:
        save_json_file(REMINDERS_FILE, reminders)
    except Exception as e:
        print(f"儲存提醒失敗：{e}")

reminder_group = app_commands.Group(name="提醒", description="設定提醒")

@reminder_group.command(name="設定", description="過一段時間後提醒你一件事")
@app_commands.rename(when="多久後", content="內容")
@app_commands.describe(when="例如 10分鐘、2小時、1天、1h30m", content="要提醒你什麼")
async def reminder_add(interaction: discord.Interaction, when: str, content: str):
    seconds = parse_duration(when)
    if not seconds:
        return await send_private(interaction, "時間格式看不懂耶～可以打「10分鐘」「2小時」「1天」或「1h30m」喔。")
    if seconds > MAX_REMINDER_SECONDS:
        return await send_private(interaction, "最多只能提醒 30 天內的事喔～")
    mine = [r for r in reminders if r["user_id"] == interaction.user.id]
    if len(mine) >= MAX_REMINDERS_PER_USER:
        return await send_private(interaction, f"你已經有 {MAX_REMINDERS_PER_USER} 個提醒了，先取消一些吧～")
    due = int(time.time()) + seconds
    reminder_id = max((r["id"] for r in reminders), default=0) + 1
    reminders.append({
        "id": reminder_id,
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "due": due,
        "text": content[:1000],
    })
    save_reminders()
    await send_private(interaction, f"好～本魚會在 <t:{due}:f>（{format_duration(seconds)}後）提醒你：{content}")

@reminder_group.command(name="清單", description="看你設定的提醒")
async def reminder_list(interaction: discord.Interaction):
    mine = sorted((r for r in reminders if r["user_id"] == interaction.user.id), key=lambda r: r["due"])
    if not mine:
        return await send_private(interaction, "你目前沒有任何提醒喔～")
    lines = [f"`#{r['id']}` <t:{r['due']}:R>：{r['text'][:80]}" for r in mine]
    await send_private(interaction, "\n".join(lines))

@reminder_group.command(name="取消", description="取消一個提醒")
@app_commands.rename(reminder_id="編號")
@app_commands.describe(reminder_id="提醒的編號（用 /提醒 清單 看）")
async def reminder_cancel(interaction: discord.Interaction, reminder_id: int):
    for r in reminders:
        if r["id"] == reminder_id and r["user_id"] == interaction.user.id:
            reminders.remove(r)
            save_reminders()
            return await send_private(interaction, f"取消提醒 #{reminder_id} 囉～")
    await send_private(interaction, "找不到這個提醒喔～")

tree.add_command(reminder_group)

async def deliver_reminder(reminder):
    text = f"⏰ <@{reminder['user_id']}> 本魚來提醒你囉：{reminder['text']}"
    mentions = discord.AllowedMentions(users=True, everyone=False, roles=False)
    channel = client.get_channel(reminder["channel_id"])
    if channel is None:
        try:
            channel = await client.fetch_channel(reminder["channel_id"])
        except Exception:
            channel = None
    if channel is not None:
        try:
            await channel.send(text, allowed_mentions=mentions)
            return
        except Exception:
            pass
    try:
        user = await client.fetch_user(reminder["user_id"])
        await user.send(text)
    except Exception as e:
        print(f"提醒送不出去：{e}")

async def reminder_loop():
    while True:
        now = int(time.time())
        due = [r for r in reminders if r["due"] <= now]
        for reminder in due:
            reminders.remove(reminder)
            await deliver_reminder(reminder)
        if due:
            save_reminders()
        await asyncio.sleep(10)

# ---------- 投票 ----------

@tree.command(name="投票", description="發起一個投票")
@app_commands.guild_only()
@app_commands.rename(question="問題", options="選項", hours="幾小時後結束", multiple="可以複選")
@app_commands.describe(options="用逗號分隔，例如：火鍋,燒烤,拉麵（2 到 10 個）")
async def slash_poll(
    interaction: discord.Interaction,
    question: str,
    options: str,
    hours: app_commands.Range[int, 1, 168] = 24,
    multiple: bool = False,
):
    choices = [o.strip() for o in re.split(r"[,，、|｜]", options) if o.strip()]
    if len(choices) < 2 or len(choices) > 10:
        return await send_private(interaction, "選項要有 2 到 10 個，用逗號分開喔～")
    poll = discord.Poll(question=question[:300], duration=timedelta(hours=hours), multiple=multiple)
    for choice in choices:
        poll.add_answer(text=choice[:55])
    await interaction.response.send_message(poll=poll)

# ---------- 抽獎 ----------

GIVEAWAYS_FILE = os.path.join(get_base_dir(), "giveaways.json")
GIVEAWAY_PREFIX = "bluefish_giveaway:"
giveaways = load_json_file(GIVEAWAYS_FILE, {})

def save_giveaways():
    try:
        save_json_file(GIVEAWAYS_FILE, giveaways)
    except Exception as e:
        print(f"儲存抽獎失敗：{e}")

def build_giveaway_embed(g):
    if g["ended"]:
        winners = "、".join(f"<@{u}>" for u in g.get("winner_ids", [])) or "沒有人參加 QQ"
        description = f"**獎品：{g['prize']}**\n已結束\n得獎者：{winners}\n參加人數：{len(g['entrants'])}"
        color = discord.Color.dark_grey()
    else:
        description = (
            f"**獎品：{g['prize']}**\n點下面的按鈕參加，再點一次就取消～\n"
            f"結束時間：<t:{g['end']}:R>\n得獎名額：{g['winners']} 位\n舉辦人：<@{g['host_id']}>"
        )
        color = discord.Color.magenta()
    return discord.Embed(title="🎉 抽獎活動", description=description, color=color)

def giveaway_view(gid, disabled=False):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="參加抽獎", emoji="🎉", style=discord.ButtonStyle.primary,
        custom_id=f"{GIVEAWAY_PREFIX}{gid}", disabled=disabled,
    ))
    return view

async def finish_giveaway(gid, reroll=False):
    g = giveaways.get(gid)
    if not g:
        return None
    pool = list(g["entrants"])
    if reroll:
        pool = [u for u in pool if u not in g.get("winner_ids", [])] or pool
    winners = random.sample(pool, min(g["winners"], len(pool))) if pool else []
    g["ended"] = True
    g["winner_ids"] = winners
    save_giveaways()

    channel = client.get_channel(g["channel_id"])
    if channel is None:
        return winners
    try:
        msg = await channel.fetch_message(g["message_id"])
        view = giveaway_view(gid, disabled=True)
        await msg.edit(embed=build_giveaway_embed(g), view=view)
        view.stop()
    except Exception:
        pass
    if winners:
        mentions = "、".join(f"<@{u}>" for u in winners)
        text = f"🎊 {'重抽結果' if reroll else '抽獎結束'}！恭喜 {mentions} 抽中 **{g['prize']}**～"
    else:
        text = f"抽獎「{g['prize']}」結束了，可是沒有人參加…本魚好難過 QQ"
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
    except Exception:
        pass
    return winners

def find_giveaway(channel_id, message_id=None, ended=None):
    candidates = []
    for gid, g in giveaways.items():
        if g["channel_id"] != channel_id:
            continue
        if message_id and str(g["message_id"]) != str(message_id):
            continue
        if ended is not None and g["ended"] != ended:
            continue
        candidates.append((g["end"], gid))
    return max(candidates)[1] if candidates else None

giveaway_group = app_commands.Group(
    name="抽獎", description="舉辦抽獎",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@giveaway_group.command(name="開始", description="在這個頻道開一個抽獎")
@app_commands.rename(prize="獎品", duration="時間", winners="名額")
@app_commands.describe(duration="多久後開獎，例如 30分鐘、1天", winners="抽幾個人")
async def giveaway_start(interaction: discord.Interaction, prize: str, duration: str, winners: app_commands.Range[int, 1, 20] = 1):
    seconds = parse_duration(duration)
    if not seconds or seconds < 30 or seconds > 30 * 86400:
        return await send_private(interaction, "時間要在 30 秒到 30 天之間，例如「30分鐘」「1天」喔～")
    gid = str(int(time.time() * 1000))
    g = {
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel_id,
        "message_id": None,
        "prize": prize[:200],
        "winners": winners,
        "end": int(time.time()) + seconds,
        "host_id": interaction.user.id,
        "entrants": [],
        "ended": False,
        "winner_ids": [],
    }
    view = giveaway_view(gid)
    await interaction.response.send_message(embed=build_giveaway_embed(g), view=view)
    view.stop()
    msg = await interaction.original_response()
    g["message_id"] = msg.id
    giveaways[gid] = g
    save_giveaways()

@giveaway_group.command(name="結束", description="提早開獎")
@app_commands.rename(message_id="訊息id")
@app_commands.describe(message_id="抽獎訊息的 ID（不填就是這個頻道最新一個進行中的抽獎）")
async def giveaway_end(interaction: discord.Interaction, message_id: Optional[str] = None):
    gid = find_giveaway(interaction.channel_id, message_id, ended=False)
    if not gid:
        return await send_private(interaction, "這個頻道找不到進行中的抽獎喔～")
    await send_private(interaction, "開獎中～")
    await finish_giveaway(gid)

@giveaway_group.command(name="重抽", description="重新抽出得獎者")
@app_commands.rename(message_id="訊息id")
@app_commands.describe(message_id="抽獎訊息的 ID（不填就是這個頻道最新一個已結束的抽獎）")
async def giveaway_reroll(interaction: discord.Interaction, message_id: Optional[str] = None):
    gid = find_giveaway(interaction.channel_id, message_id, ended=True)
    if not gid:
        return await send_private(interaction, "這個頻道找不到已結束的抽獎喔～")
    await send_private(interaction, "重抽中～")
    await finish_giveaway(gid, reroll=True)

tree.add_command(giveaway_group)

async def handle_giveaway_button(interaction: discord.Interaction, custom_id: str):
    gid = custom_id[len(GIVEAWAY_PREFIX):]
    g = giveaways.get(gid)
    if not g or g["ended"]:
        return await send_private(interaction, "這個抽獎已經結束囉～")
    uid = interaction.user.id
    if uid in g["entrants"]:
        g["entrants"].remove(uid)
        save_giveaways()
        return await send_private(interaction, f"取消參加囉～目前 {len(g['entrants'])} 人參加。")
    g["entrants"].append(uid)
    save_giveaways()
    await send_private(interaction, f"參加成功！祝你好運～目前 {len(g['entrants'])} 人參加。")

async def giveaway_loop():
    while True:
        now = int(time.time())
        for gid, g in list(giveaways.items()):
            if not g["ended"] and g["end"] <= now:
                try:
                    await finish_giveaway(gid)
                except Exception as e:
                    print(f"開獎失敗：{e}")
        cutoff = now - 30 * 86400
        stale = [gid for gid, g in giveaways.items() if g["ended"] and g["end"] < cutoff]
        if stale:
            for gid in stale:
                giveaways.pop(gid, None)
            save_giveaways()
        await asyncio.sleep(15)

# ---------- 客服單 ----------

TICKET_OPEN_ID = "bluefish_ticket:open"
TICKET_CLOSE_ID = "bluefish_ticket:close"

def ticket_settings(guild_id):
    cfg = get_guild_settings(guild_id).setdefault("tickets", {})
    cfg.setdefault("open", {})
    cfg.setdefault("counter", 0)
    return cfg

ticket_group = app_commands.Group(
    name="客服單", description="讓成員開私人頻道找管理員",
    guild_only=True, default_permissions=discord.Permissions(manage_channels=True),
)

@ticket_group.command(name="面板", description="在這個頻道放一個「開啟客服單」按鈕")
@app_commands.rename(staff_role="客服身分組", description="說明")
@app_commands.describe(staff_role="哪個身分組看得到客服單（不填就只有管理員）", description="面板說明文字（可不填）")
async def ticket_panel(interaction: discord.Interaction, staff_role: Optional[discord.Role] = None, description: Optional[str] = None):
    if not interaction.guild.me.guild_permissions.manage_channels:
        return await send_private(interaction, "本魚沒有管理頻道的權限，開不了客服單啦～")
    cfg = ticket_settings(interaction.guild.id)
    cfg["staff_role"] = staff_role.id if staff_role else None
    cfg["category"] = interaction.channel.category_id
    save_guild_settings()
    embed = discord.Embed(
        title="📩 客服單",
        description=description or "有問題要找管理員？點下面的按鈕，本魚會幫你開一個只有你跟管理員看得到的私人頻道～",
        color=discord.Color.teal(),
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="開啟客服單", emoji="📩", style=discord.ButtonStyle.primary, custom_id=TICKET_OPEN_ID))
    await interaction.response.send_message(embed=embed, view=view)
    view.stop()

tree.add_command(ticket_group)

async def handle_ticket_open(interaction: discord.Interaction):
    guild = interaction.guild
    member = interaction.user
    cfg = ticket_settings(guild.id)
    existing = guild.get_channel(cfg["open"].get(str(member.id), 0))
    if existing:
        return await send_private(interaction, f"你已經有一張客服單了：{existing.mention}")

    await interaction.response.defer(ephemeral=True, thinking=True)
    cfg["counter"] += 1
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
    }
    staff_role = guild.get_role(cfg.get("staff_role") or 0)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    category = guild.get_channel(cfg.get("category") or 0)
    try:
        channel = await guild.create_text_channel(
            f"客服單-{cfg['counter']:04d}",
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"{member} 開啟客服單",
        )
    except discord.Forbidden:
        return await interaction.followup.send("本魚沒有建立頻道的權限啦～", ephemeral=True)
    cfg["open"][str(member.id)] = channel.id
    save_guild_settings()

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="關閉客服單", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=TICKET_CLOSE_ID))
    staff_text = f" {staff_role.mention}" if staff_role else ""
    await channel.send(
        f"{member.mention}{staff_text}\n嗨～這裡是你的私人客服單，把問題寫下來，管理員看到就會回覆你喔！處理完按下面的按鈕就能關閉。",
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    view.stop()
    await interaction.followup.send(f"客服單開好了：{channel.mention}", ephemeral=True)
    await send_mod_log(guild, "開啟客服單", f"{member.mention} 開啟了 {channel.mention}", discord.Color.teal())

async def handle_ticket_close(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel
    cfg = ticket_settings(guild.id)
    owner_id = next((uid for uid, cid in cfg["open"].items() if cid == channel.id), None)
    member = interaction.user
    staff_role = guild.get_role(cfg.get("staff_role") or 0)
    allowed = (
        str(member.id) == owner_id
        or member.guild_permissions.manage_channels
        or (staff_role is not None and staff_role in member.roles)
    )
    if not allowed:
        return await send_private(interaction, "只有開單的人或管理員可以關閉喔～")
    await interaction.response.send_message("好～這張客服單 5 秒後關閉。")
    if owner_id:
        cfg["open"].pop(owner_id, None)
        save_guild_settings()
    await send_mod_log(guild, "關閉客服單", f"{member.mention} 關閉了 #{channel.name}", discord.Color.teal())
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"{member} 關閉客服單")
    except Exception as e:
        print(f"刪除客服單頻道失敗：{e}")

# ---------- 動態語音 ----------

temp_voice_group = app_commands.Group(
    name="動態語音", description="進入指定語音頻道就自動開一間自己的房間",
    guild_only=True, default_permissions=discord.Permissions(manage_channels=True),
)

@temp_voice_group.command(name="設定", description="指定一個語音頻道當入口，進去就會自動開新房間")
@app_commands.rename(channel="入口頻道")
async def temp_voice_set(interaction: discord.Interaction, channel: discord.VoiceChannel):
    perms = interaction.guild.me.guild_permissions
    if not (perms.manage_channels and perms.move_members):
        return await send_private(interaction, "本魚需要「管理頻道」跟「移動成員」權限才能做這個喔～")
    cfg = get_guild_settings(interaction.guild.id).setdefault("temp_voice", {})
    cfg["hub"] = channel.id
    cfg.setdefault("channels", [])
    save_guild_settings()
    await send_private(interaction, f"好～之後有人進 {channel.mention}，本魚就會幫他開一間自己的房間，沒人時自動刪掉。")

@temp_voice_group.command(name="關閉", description="關閉動態語音")
async def temp_voice_off(interaction: discord.Interaction):
    cfg = get_guild_settings(interaction.guild.id).get("temp_voice")
    if cfg:
        cfg.pop("hub", None)
        save_guild_settings()
    await send_private(interaction, "動態語音關掉囉～已經開的房間沒人時還是會自動刪掉。")

tree.add_command(temp_voice_group)

async def temp_voice_state_update(member, before, after):
    guild = member.guild
    cfg = guild_settings.get(str(guild.id), {}).get("temp_voice")
    if not cfg:
        return
    changed = False
    temp_ids = cfg.setdefault("channels", [])

    if after.channel and after.channel.id == cfg.get("hub") and not member.bot:
        try:
            room = await guild.create_voice_channel(
                f"{member.display_name} 的房間"[:100],
                category=after.channel.category,
                reason="動態語音",
            )
            await room.set_permissions(member, manage_channels=True, move_members=True, connect=True)
            await member.move_to(room)
            temp_ids.append(room.id)
            changed = True
        except Exception as e:
            print(f"開動態語音房間失敗：{e}")

    if before.channel and before.channel.id in temp_ids and len(before.channel.members) == 0:
        try:
            await before.channel.delete(reason="動態語音房間沒人了")
        except Exception as e:
            print(f"刪除動態語音房間失敗：{e}")
        if before.channel.id in temp_ids:
            temp_ids.remove(before.channel.id)
        changed = True

    if changed:
        save_guild_settings()

# ---------- 精選板 ----------

STAR_EMOJI = "⭐"

starboard_group = app_commands.Group(
    name="精選", description="⭐ 夠多的訊息會自動轉貼到精選頻道",
    guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
)

@starboard_group.command(name="設定", description="設定精選頻道和需要幾個 ⭐")
@app_commands.rename(channel="頻道", threshold="門檻")
@app_commands.describe(threshold="幾個 ⭐ 才會被精選")
async def starboard_set(interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int, 1, 50] = 3):
    cfg = get_guild_settings(interaction.guild.id).setdefault("starboard", {})
    cfg["channel"] = channel.id
    cfg["threshold"] = threshold
    cfg.setdefault("posted", [])
    save_guild_settings()
    await send_private(interaction, f"好～訊息收到 {threshold} 個 {STAR_EMOJI} 就會被精選到 {channel.mention}。")

@starboard_group.command(name="關閉", description="關閉精選板")
async def starboard_off(interaction: discord.Interaction):
    cfg = get_guild_settings(interaction.guild.id).get("starboard")
    if cfg:
        cfg.pop("channel", None)
        save_guild_settings()
    await send_private(interaction, "精選板關掉囉～")

tree.add_command(starboard_group)

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or str(payload.emoji) != STAR_EMOJI or not guild_enabled(payload.guild_id):
        return
    cfg = guild_settings.get(str(payload.guild_id), {}).get("starboard")
    if not cfg or not cfg.get("channel") or payload.channel_id == cfg["channel"]:
        return
    posted = cfg.setdefault("posted", [])
    if payload.message_id in posted:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    source = guild.get_channel_or_thread(payload.channel_id)
    board = guild.get_channel(cfg["channel"])
    if source is None or board is None:
        return
    try:
        message = await source.fetch_message(payload.message_id)
    except Exception:
        return
    count = next((r.count for r in message.reactions if str(r.emoji) == STAR_EMOJI), 0)
    if count < cfg.get("threshold", 3) or message.author.bot:
        return

    posted.append(message.id)
    del posted[:-1000]
    save_guild_settings()
    embed = discord.Embed(description=message.content[:4000] or None, color=discord.Color.gold(), timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    image = next((a for a in message.attachments if a.content_type and a.content_type.startswith("image")), None)
    if image:
        embed.set_image(url=image.url)
    embed.add_field(name="原文", value=f"[跳到訊息]({message.jump_url})")
    try:
        await board.send(f"{STAR_EMOJI} **{count}**　{source.mention}", embed=embed)
    except Exception as e:
        print(f"發送精選失敗：{e}")

# ---------- 娛樂 ----------

@tree.command(name="擲骰", description="擲骰子，例如 2d6 就是擲兩顆六面骰")
@app_commands.rename(dice="骰子")
@app_commands.describe(dice="格式：顆數d面數，例如 1d6、2d20（預設 1d6）")
async def slash_dice(interaction: discord.Interaction, dice: str = "1d6"):
    match = re.fullmatch(r"\s*(\d*)\s*[dD]\s*(\d+)\s*", dice)
    if not match:
        return await send_private(interaction, "格式要像 1d6、2d20 這樣喔～")
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        return await send_private(interaction, "最多 20 顆骰子、每顆 2 到 1000 面喔～")
    rolls = [random.randint(1, sides) for _ in range(count)]
    detail = f"（{' + '.join(map(str, rolls))}）" if count > 1 else ""
    await interaction.response.send_message(f"🎲 本魚擲出 {count}d{sides}：**{sum(rolls)}**{detail}")

@tree.command(name="擲硬幣", description="擲一枚硬幣")
async def slash_coin(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 擲出來是……**{random.choice(['正面', '反面'])}**！")

@tree.command(name="選擇", description="選不下去嗎？讓本魚幫你選")
@app_commands.rename(options="選項")
@app_commands.describe(options="用逗號分隔，例如：火鍋,燒烤,拉麵")
async def slash_choose(interaction: discord.Interaction, options: str):
    choices = [o.strip() for o in re.split(r"[,，、|｜]", options) if o.strip()]
    if len(choices) < 2:
        return await send_private(interaction, "至少給本魚兩個選項嘛～用逗號分開喔。")
    await interaction.response.send_message(f"本魚想了想……就選 **{random.choice(choices)}** 吧！")

FORTUNE_ANSWERS = [
    "本魚的鰭告訴我：絕對會！", "是的是的，放心去做吧～", "看起來很有希望喔！", "嗯…應該可以啦～",
    "本魚覺得機會不小！", "現在問的話，本魚也說不準耶…", "晚點再問本魚一次嘛～", "這個…本魚不告訴你（其實是不知道）",
    "本魚覺得不太妙耶…", "不要啦，本魚覺得不行！", "水晶球說：不會。", "嗚…本魚看到的答案是否定的。",
]

@tree.command(name="占卜", description="問本魚一個是非題，本魚幫你占卜")
@app_commands.rename(question="問題")
async def slash_fortune(interaction: discord.Interaction, question: str):
    await interaction.response.send_message(f"🔮 **{question}**\n{random.choice(FORTUNE_ANSWERS)}")

@tree.command(name="猜拳", description="跟本魚猜拳")
@app_commands.rename(hand="出拳")
@app_commands.choices(hand=[
    app_commands.Choice(name="✊ 石頭", value="石頭"),
    app_commands.Choice(name="✌️ 剪刀", value="剪刀"),
    app_commands.Choice(name="🖐️ 布", value="布"),
])
async def slash_rps(interaction: discord.Interaction, hand: app_commands.Choice[str]):
    bot_hand = random.choice(["石頭", "剪刀", "布"])
    beats = {"石頭": "剪刀", "剪刀": "布", "布": "石頭"}
    if hand.value == bot_hand:
        result = "平手！再來一次嘛～"
    elif beats[hand.value] == bot_hand:
        result = "你贏了…本魚不服氣啦！"
    else:
        result = "本魚贏啦～嘿嘿！"
    await interaction.response.send_message(f"你出 **{hand.value}**，本魚出 **{bot_hand}**。{result}")

# ---------- 小遊戲 ----------

GAME_TIMEOUT = 300
BLANK_LABEL = "\u200b"

async def _edit_quietly(message, **kwargs):
    if message is None:
        return
    try:
        await message.edit(**kwargs)
    except Exception:
        pass

# ----- 釣魚 -----

FISHING_FILE = os.path.join(get_base_dir(), "fishing.json")
fishing_data = load_json_file(FISHING_FILE, {})
fishing_dirty = False
fishing_cooldowns = {}
FISH_COOLDOWN_SECONDS = 30
FISH_BITE_WINDOW = 4.0
FISH_QUICK_REACTION = 1.2

# (表情, 名稱, 稀有度, 機率權重, 分數, 最小公分, 最大公分)
FISH_TABLE = [
    ("🥾", "破舊的靴子", "垃圾", 5, 0, 22, 30),
    ("🥫", "空罐頭", "垃圾", 5, 0, 8, 12),
    ("🌿", "一團海草", "垃圾", 5, 0, 10, 60),
    ("🐟", "吳郭魚", "普通", 15, 10, 15, 40),
    ("🐟", "虱目魚", "普通", 15, 10, 20, 50),
    ("🦐", "小蝦米", "普通", 13, 8, 3, 8),
    ("🐠", "小丑魚", "普通", 12, 12, 6, 11),
    ("🦀", "螃蟹", "稀有", 7, 30, 10, 25),
    ("🦑", "花枝", "稀有", 6, 35, 15, 40),
    ("🐡", "河豚", "稀有", 5, 40, 10, 30),
    ("🐙", "章魚", "稀有", 4, 45, 30, 90),
    ("🦈", "鯊魚", "史詩", 3, 120, 100, 400),
    ("🐢", "海龜", "史詩", 2.5, 150, 50, 120),
    ("🐋", "鯨魚", "傳說", 0.8, 500, 800, 2500),
    ("💙", "藍色大肥魚", "傳說", 0.5, 777, 30, 45),
]
RARITY_STYLE = {
    "垃圾": ("⬜", discord.Color.light_grey()),
    "普通": ("⭐", discord.Color.blue()),
    "稀有": ("⭐⭐", discord.Color.green()),
    "史詩": ("⭐⭐⭐", discord.Color.purple()),
    "傳說": ("🌟🌟🌟🌟", discord.Color.gold()),
}
LUCKY_RARITIES = {"稀有", "史詩", "傳說"}
FISH_COMMENTS = {
    "垃圾": ["這…這不是魚啦！本魚幫你丟進資源回收桶～", "海洋好髒喔，謝謝你幫忙撿垃圾～"],
    "普通": ["不錯嘛～今天的晚餐有著落了！", "普普通通的一條，但本魚還是幫你拍拍鰭～"],
    "稀有": ["哇！是稀有的耶！你運氣不錯嘛～", "欸欸欸，這個很少見喔！"],
    "史詩": ["天啊！！這可是史詩級的大傢伙！", "本魚嚇到鱗片都豎起來了！"],
    "傳說": ["傳…傳說中的那個！？你是被幸運之神親過嗎！"],
}
SPECIAL_COMMENTS = {
    "海龜": "海龜是保育類啦！本魚已經幫你拍照留念，然後放回海裡囉～",
    "藍色大肥魚": "你…你釣到本魚的親戚了！！圓滾滾的好可愛…不准吃掉牠喔！",
    "鯨魚": "這麼大隻怎麼拉上來的啦！本魚要叫全伺服器來看！",
}

def roll_fish(lucky):
    weights = [w * (1.6 if lucky and r in LUCKY_RARITIES else 1) for _, _, r, w, _, _, _ in FISH_TABLE]
    return random.choices(FISH_TABLE, weights=weights, k=1)[0]

def fishing_profile(guild_id, user_id):
    return fishing_data.setdefault(str(guild_id), {}).setdefault(
        str(user_id), {"points": 0, "total": 0, "catches": {}, "best": {}}
    )

class FishingBiteView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=FISH_BITE_WINDOW)
        self.owner_id = owner_id
        self.caught = False
        self.reaction_time = None
        self.shown_at = time.monotonic()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await send_private(interaction, "這是別人的魚竿啦，你自己用 /釣魚 嘛～")
            return False
        return True

    @discord.ui.button(label="收竿！", emoji="🎣", style=discord.ButtonStyle.success)
    async def reel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.caught = True
        self.reaction_time = time.monotonic() - self.shown_at
        await interaction.response.defer()
        self.stop()

@tree.command(name="釣魚", description="拋竿釣魚，看你能釣到什麼～")
@app_commands.guild_only()
async def slash_fish(interaction: discord.Interaction):
    global fishing_dirty
    key = (interaction.guild.id, interaction.user.id)
    now = time.monotonic()
    remaining = FISH_COOLDOWN_SECONDS - (now - fishing_cooldowns.get(key, -FISH_COOLDOWN_SECONDS))
    if remaining > 0:
        return await send_private(interaction, f"魚竿還在晾乾啦，{int(remaining) + 1} 秒後再來～")
    fishing_cooldowns[key] = now

    name = interaction.user.display_name
    await interaction.response.send_message(f"🎣 **{name}** 拋出了魚竿……靜靜等待中～")
    await asyncio.sleep(random.uniform(2, 6))

    view = FishingBiteView(interaction.user.id)
    await interaction.edit_original_response(content=f"❗ **{name}** 的浮標動了！有東西上鉤了，快按「收竿」！", view=view)
    view.shown_at = time.monotonic()
    await view.wait()
    if not view.caught:
        await interaction.edit_original_response(content=f"💨 **{name}** 太慢了…魚跑掉了！下次手腳快一點嘛～", view=None)
        return

    lucky = view.reaction_time is not None and view.reaction_time <= FISH_QUICK_REACTION
    emoji, fish_name, rarity, _, points, min_cm, max_cm = roll_fish(lucky)
    size = round(random.uniform(min_cm, max_cm), 1)

    profile = fishing_profile(interaction.guild.id, interaction.user.id)
    first_time = fish_name not in profile["catches"]
    previous_best = profile["best"].get(fish_name, 0)
    new_record = size > previous_best
    profile["total"] += 1
    profile["points"] += points
    profile["catches"][fish_name] = profile["catches"].get(fish_name, 0) + 1
    if new_record:
        profile["best"][fish_name] = size
    fishing_dirty = True

    stars, color = RARITY_STYLE[rarity]
    lines = [
        f"稀有度：{stars} {rarity}",
        f"大小：{size} 公分" + ("　🏅 **新紀錄！**" if new_record and not first_time else ""),
        f"得到 {points} 點（目前共 {profile['points']} 點）",
    ]
    if first_time:
        lines.append("📖 **新發現！** 已經收進你的魚缸圖鑑了～")
    if lucky and rarity in LUCKY_RARITIES:
        lines.append(f"⚡ 你收竿好快（{view.reaction_time:.2f} 秒），運氣加成發動！")
    lines.append("")
    lines.append(SPECIAL_COMMENTS.get(fish_name) or random.choice(FISH_COMMENTS[rarity]))
    embed = discord.Embed(title=f"{emoji} {name} 釣到了{fish_name}！", description="\n".join(lines), color=color)
    await interaction.edit_original_response(content=None, embed=embed, view=None)

@tree.command(name="魚缸", description="看自己或別人的釣魚圖鑑")
@app_commands.guild_only()
@app_commands.rename(member="成員")
@app_commands.describe(member="要看誰的魚缸（不填就是你自己）")
async def slash_aquarium(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    profile = fishing_data.get(str(interaction.guild.id), {}).get(str(member.id))
    if not profile or not profile["total"]:
        return await interaction.response.send_message(f"{member.display_name} 的魚缸空空的，快用 /釣魚 去釣第一條吧～")
    found = sum(1 for f in FISH_TABLE if f[1] in profile["catches"])
    embed = discord.Embed(
        title=f"🐠 {member.display_name} 的魚缸",
        description=f"總共釣了 {profile['total']} 次，累積 **{profile['points']}** 點\n圖鑑收集：{found} / {len(FISH_TABLE)}",
        color=discord.Color.teal(),
    )
    for rarity in RARITY_STYLE:
        entries = []
        for emoji, fish_name, r, *_ in FISH_TABLE:
            if r != rarity:
                continue
            count = profile["catches"].get(fish_name)
            if count:
                entries.append(f"{emoji} {fish_name} ×{count}（最大 {profile['best'].get(fish_name, 0)} 公分）")
            else:
                entries.append("❓ ？？？")
        embed.add_field(name=f"{RARITY_STYLE[rarity][0]} {rarity}", value="\n".join(entries), inline=False)
    await interaction.response.send_message(embed=embed)

@tree.command(name="釣魚排行", description="看這個伺服器誰最會釣魚")
@app_commands.guild_only()
async def slash_fishing_leaderboard(interaction: discord.Interaction):
    users = fishing_data.get(str(interaction.guild.id), {})
    ranking = sorted(users.items(), key=lambda item: item[1]["points"], reverse=True)[:10]
    if not ranking:
        return await interaction.response.send_message("還沒有人釣過魚耶，快用 /釣魚 搶第一名～")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, profile) in enumerate(ranking):
        member = interaction.guild.get_member(int(uid))
        who = member.display_name if member else f"<@{uid}>"
        prefix = medals[i] if i < 3 else f"`{i + 1}.`"
        found = sum(1 for f in FISH_TABLE if f[1] in profile["catches"])
        lines.append(f"{prefix} **{who}**　{profile['points']} 點（圖鑑 {found}/{len(FISH_TABLE)}）")
    embed = discord.Embed(title="🎣 釣魚排行榜", description="\n".join(lines), color=discord.Color.teal())
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

async def fishing_autosave_loop():
    global fishing_dirty
    while True:
        await asyncio.sleep(60)
        if fishing_dirty:
            try:
                save_json_file(FISHING_FILE, fishing_data)
                fishing_dirty = False
            except Exception as e:
                print(f"儲存釣魚資料失敗：{e}")

# ----- 井字棋 -----

TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
TTT_MISTAKE_CHANCE = 0.25

def ttt_winner(board):
    for a, b, c in TTT_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None

@functools.lru_cache(maxsize=None)
def ttt_minimax(board, me, turn):
    """board 是 tuple（才能快取）。回傳 (分數, 最佳位置)，分數從 me 的角度看。"""
    result = ttt_winner(board)
    if result == me:
        return 1, None
    if result == "draw":
        return 0, None
    if result:
        return -1, None
    other = "O" if turn == "X" else "X"
    best = None
    for i in range(9):
        if board[i]:
            continue
        score, _ = ttt_minimax(board[:i] + (turn,) + board[i + 1:], me, other)
        if best is None or (turn == me and score > best[0]) or (turn != me and score < best[0]):
            best = (score, i)
    return best

def ttt_bot_move(board, me):
    empty = [i for i in range(9) if not board[i]]
    if random.random() < TTT_MISTAKE_CHANCE:
        return random.choice(empty), True
    return ttt_minimax(tuple(board), me, me)[1], False

class TicTacToeCell(discord.ui.Button):
    def __init__(self, index):
        super().__init__(style=discord.ButtonStyle.secondary, label=BLANK_LABEL, row=index // 3)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self.view.play(interaction, self.index)

class TicTacToeView(discord.ui.View):
    def __init__(self, player_x, player_o, vs_bot):
        super().__init__(timeout=GAME_TIMEOUT)
        self.board = [None] * 9
        self.players = {"X": player_x, "O": player_o}
        self.turn = "X"
        self.vs_bot = vs_bot
        self.message = None
        self.note = ""
        for i in range(9):
            self.add_item(TicTacToeCell(i))

    def status(self):
        x, o = self.players["X"], self.players["O"]
        header = f"⭕❌ **井字棋**　❌ {x.display_name} vs ⭕ {o.display_name}"
        result = ttt_winner(self.board)
        if result == "draw":
            line = "平手！旗鼓相當嘛～"
        elif result:
            winner = self.players[result]
            if self.vs_bot and winner.id == client.user.id:
                line = "本魚贏啦～嘿嘿，要不要再來一局？"
            elif self.vs_bot:
                line = f"🎉 {winner.display_name} 贏了！本魚不服氣啦！"
            else:
                line = f"🎉 {winner.display_name} 贏了！"
        else:
            line = f"輪到 {'❌' if self.turn == 'X' else '⭕'} {self.players[self.turn].display_name}"
        return f"{header}\n{line}" + (f"\n{self.note}" if self.note else "")

    def refresh_buttons(self):
        finished = ttt_winner(self.board) is not None
        for item in self.children:
            mark = self.board[item.index]
            item.label = {"X": "❌", "O": "⭕"}.get(mark, BLANK_LABEL)
            item.style = discord.ButtonStyle.danger if mark == "X" else discord.ButtonStyle.primary if mark == "O" else discord.ButtonStyle.secondary
            item.disabled = finished or mark is not None

    async def interaction_check(self, interaction):
        if interaction.user.id not in (self.players["X"].id, self.players["O"].id):
            await send_private(interaction, "這局不是你的啦，自己用 /井字棋 開一局嘛～")
            return False
        return True

    async def play(self, interaction, index):
        if interaction.user.id != self.players[self.turn].id:
            return await send_private(interaction, "還沒輪到你啦～")
        if self.board[index] or ttt_winner(self.board):
            return await send_private(interaction, "這格不能下喔～")
        self.board[index] = self.turn
        self.turn = "O" if self.turn == "X" else "X"
        self.note = ""
        if self.vs_bot and not ttt_winner(self.board):
            move, mistake = ttt_bot_move(self.board, self.turn)
            self.board[move] = self.turn
            self.turn = "O" if self.turn == "X" else "X"
            if mistake:
                self.note = random.choice(["（本魚的鰭好像滑了一下…）", "（本魚剛剛在發呆…）", ""])
        self.refresh_buttons()
        await interaction.response.edit_message(content=self.status(), view=self)
        if ttt_winner(self.board):
            self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        await _edit_quietly(self.message, content=self.status() + "\n⏰ 太久沒下，這局先結束囉～", view=self)

class TicTacToeInvite(discord.ui.View):
    def __init__(self, challenger, opponent):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.message = None
        self.answered = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.opponent.id:
            await send_private(interaction, "這是給別人的挑戰喔～")
            return False
        return True

    @discord.ui.button(label="接受挑戰", emoji="⚔️", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.answered = True
        players = [self.challenger, self.opponent]
        random.shuffle(players)
        game = TicTacToeView(players[0], players[1], vs_bot=False)
        game.message = interaction.message
        await interaction.response.edit_message(content=game.status(), view=game)
        self.stop()

    @discord.ui.button(label="拒絕", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.answered = True
        await interaction.response.edit_message(content=f"{self.opponent.display_name} 拒絕了 {self.challenger.display_name} 的井字棋挑戰～", view=None)
        self.stop()

    async def on_timeout(self):
        if not self.answered:
            await _edit_quietly(self.message, content=f"{self.opponent.display_name} 沒有回應，井字棋挑戰取消囉～", view=None)

@tree.command(name="井字棋", description="跟別人或跟本魚下井字棋")
@app_commands.guild_only()
@app_commands.rename(opponent="對手")
@app_commands.describe(opponent="要挑戰誰（不填就是跟本魚下）")
async def slash_tictactoe(interaction: discord.Interaction, opponent: Optional[discord.Member] = None):
    user = interaction.user
    if opponent is None or opponent.id == client.user.id:
        game = TicTacToeView(user, interaction.guild.me, vs_bot=True)
        await interaction.response.send_message(game.status(), view=game)
        game.message = await interaction.original_response()
        return
    if opponent.bot:
        return await send_private(interaction, "其他機器人不會下棋啦，找真人或跟本魚下嘛～")
    if opponent.id == user.id:
        return await send_private(interaction, "自己跟自己下嗎…找個對手嘛，或是不填對手跟本魚下～")
    invite = TicTacToeInvite(user, opponent)
    await interaction.response.send_message(
        f"{opponent.mention}，{user.display_name} 向你發起井字棋挑戰！要接受嗎？（60 秒內回覆）",
        view=invite,
        allowed_mentions=discord.AllowedMentions(users=[opponent]),
    )
    invite.message = await interaction.original_response()

# ----- 21 點 -----

CARD_SUITS = ["♠️", "♥️", "♦️", "♣️"]
CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def hand_value(hand):
    total, aces = 0, 0
    for rank, _ in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def show_hand(hand, hide_second=False):
    cards = [f"`{r}{s}`" for r, s in hand]
    if hide_second and len(cards) > 1:
        cards[1] = "`🂠`"
    return " ".join(cards)

class BlackjackView(discord.ui.View):
    def __init__(self, player):
        super().__init__(timeout=GAME_TIMEOUT)
        self.player = player
        self.deck = [(r, s) for s in CARD_SUITS for r in CARD_RANKS]
        random.shuffle(self.deck)
        self.hand = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.result = None
        self.message = None
        if hand_value(self.hand) == 21 or hand_value(self.dealer) == 21:
            self.finish()

    def finish(self):
        me, bot = hand_value(self.hand), hand_value(self.dealer)
        natural_me = me == 21 and len(self.hand) == 2
        natural_bot = bot == 21 and len(self.dealer) == 2
        if natural_me and natural_bot:
            self.result = ("draw", "雙方都是 Blackjack！平手～")
        elif natural_me:
            self.result = ("win", "🎉 開局就 Blackjack！你也太幸運了吧！")
        elif natural_bot:
            self.result = ("lose", "本魚開局就 Blackjack～嘿嘿，不好意思囉！")
        elif me > 21:
            self.result = ("lose", "爆掉啦～超過 21 點了！本魚贏～")
        else:
            while hand_value(self.dealer) < 17:
                self.dealer.append(self.deck.pop())
            bot = hand_value(self.dealer)
            if bot > 21:
                self.result = ("win", "本魚爆掉了…你贏啦！")
            elif me > bot:
                self.result = ("win", f"🎉 {me} 點比 {bot} 點，你贏了！")
            elif me < bot:
                self.result = ("lose", f"{bot} 點比 {me} 點，本魚贏～")
            else:
                self.result = ("draw", f"都是 {me} 點，平手！")
        for item in self.children:
            item.disabled = True
        self.stop()

    def embed(self):
        done = self.result is not None
        color = {"win": discord.Color.green(), "lose": discord.Color.red(), "draw": discord.Color.light_grey()}.get(
            self.result[0] if done else None, discord.Color.blue())
        embed = discord.Embed(title=f"🃏 21 點　{self.player.display_name} vs 本魚", color=color)
        dealer_value = str(hand_value(self.dealer)) if done else "?"
        embed.add_field(name=f"本魚的牌（{dealer_value} 點）", value=show_hand(self.dealer, hide_second=not done), inline=False)
        embed.add_field(name=f"你的牌（{hand_value(self.hand)} 點）", value=show_hand(self.hand), inline=False)
        embed.set_footer(text=self.result[1] if done else "要再拿一張，還是就這樣？")
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.player.id:
            await send_private(interaction, "這是別人的牌局喔，自己用 /21點 開一局嘛～")
            return False
        return True

    @discord.ui.button(label="要牌", emoji="➕", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.hand.append(self.deck.pop())
        if hand_value(self.hand) >= 21:
            self.finish()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="停牌", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.finish()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        await _edit_quietly(self.message, content="⏰ 太久沒動作，這局先收起來囉～", view=self)

@tree.command(name="21點", description="跟本魚玩 21 點（Blackjack）")
async def slash_blackjack(interaction: discord.Interaction):
    game = BlackjackView(interaction.user)
    await interaction.response.send_message(embed=game.embed(), view=game)
    game.message = await interaction.original_response()

# ----- 猜數字 -----

active_guess_games = {}

class GuessNumberModal(discord.ui.Modal, title="猜數字"):
    def __init__(self, game):
        super().__init__()
        self.game = game
        self.number = discord.ui.TextInput(
            label=f"猜一個 {game.low} 到 {game.high} 之間的數字",
            placeholder=str((game.low + game.high) // 2),
            max_length=7,
        )
        self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction):
        await self.game.guess(interaction, str(self.number.value).strip())

class GuessNumberView(discord.ui.View):
    def __init__(self, channel_id, maximum):
        super().__init__(timeout=600)
        self.channel_id = channel_id
        self.maximum = maximum
        self.answer = random.randint(1, maximum)
        self.low, self.high = 1, maximum
        self.attempts = 0
        self.log = deque(maxlen=6)
        self.finished = False
        self.message = None

    def text(self):
        lines = [f"🔢 **猜數字**　本魚心裡想了一個 1 到 {self.maximum} 的數字，大家一起來猜！"]
        if self.finished:
            lines.append(f"答案是 **{self.answer}**，總共猜了 {self.attempts} 次。")
        else:
            lines.append(f"目前範圍：**{self.low} ～ {self.high}**　已經猜了 {self.attempts} 次")
        if self.log:
            lines.append("")
            lines.extend(self.log)
        return "\n".join(lines)

    @discord.ui.button(label="我要猜！", emoji="🙋", style=discord.ButtonStyle.primary)
    async def open_guess(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return await send_private(interaction, "這局已經結束囉～")
        await interaction.response.send_modal(GuessNumberModal(self))

    async def guess(self, interaction, raw):
        if self.finished:
            return await send_private(interaction, "慢了一步，已經有人猜中囉～")
        if not raw.isdigit():
            return await send_private(interaction, "要輸入數字啦～")
        value = int(raw)
        if not self.low <= value <= self.high:
            return await send_private(interaction, f"要在 {self.low} 到 {self.high} 之間喔～")
        self.attempts += 1
        who = interaction.user.display_name
        if value == self.answer:
            self.finished = True
            self.log.append(f"🎯 {who} 猜 {value}：**猜中了！**")
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content=self.text(), view=self)
            await interaction.followup.send(f"🎉 {interaction.user.mention} 猜中了！答案就是 **{value}**～")
            self.end()
            return
        if value < self.answer:
            self.low = value + 1
            self.log.append(f"⬆️ {who} 猜 {value}：太小了")
        else:
            self.high = value - 1
            self.log.append(f"⬇️ {who} 猜 {value}：太大了")
        await interaction.response.edit_message(content=self.text(), view=self)

    def end(self):
        if active_guess_games.get(self.channel_id) is self:
            active_guess_games.pop(self.channel_id, None)
        self.stop()

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        for item in self.children:
            item.disabled = True
        self.end()
        await _edit_quietly(self.message, content=self.text() + "\n⏰ 時間到，沒有人猜中～", view=self)

@tree.command(name="猜數字", description="本魚想一個數字，大家一起猜")
@app_commands.rename(maximum="最大值")
@app_commands.describe(maximum="數字範圍的最大值（預設 100）")
async def slash_guess_number(interaction: discord.Interaction, maximum: app_commands.Range[int, 10, 1000000] = 100):
    existing = active_guess_games.get(interaction.channel_id)
    if existing and not existing.finished:
        link = f"：{existing.message.jump_url}" if existing.message else ""
        return await send_private(interaction, f"這個頻道已經有一局猜數字在進行了{link}")
    game = GuessNumberView(interaction.channel_id, maximum)
    active_guess_games[interaction.channel_id] = game
    await interaction.response.send_message(game.text(), view=game)
    game.message = await interaction.original_response()

# ---------- 音樂斜線指令補充 ----------

@tree.command(name="現在播放", description="看現在在放哪首歌、播到哪了")
async def slash_nowplaying(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_nowplaying)

@tree.command(name="循環", description="設定循環模式")
@app_commands.rename(mode="模式")
@app_commands.choices(mode=[
    app_commands.Choice(name="關閉", value="off"),
    app_commands.Choice(name="單曲循環", value="single"),
    app_commands.Choice(name="整個佇列循環", value="queue"),
])
async def slash_loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    async def handler(ctx):
        await handle_music_loop(ctx, mode.value)
    await run_music_action(interaction, handler)

@tree.command(name="隨機播放", description="把佇列裡的歌打亂")
async def slash_shuffle(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_shuffle)

@tree.command(name="移除歌曲", description="從佇列拿掉某一首")
@app_commands.rename(index="編號")
@app_commands.describe(index="佇列裡的第幾首（用 /播放清單 看）")
async def slash_remove_song(interaction: discord.Interaction, index: app_commands.Range[int, 1, 500]):
    async def handler(ctx):
        await handle_music_remove(ctx, index)
    await run_music_action(interaction, handler)

@tree.command(name="清空佇列", description="清掉佇列裡所有排隊的歌")
async def slash_clear_queue(interaction: discord.Interaction):
    await run_music_action(interaction, handle_music_clear)

# ---------- 斜線指令錯誤處理 ----------

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        text = "你沒有使用這個指令的權限喔～"
    elif isinstance(error, app_commands.BotMissingPermissions):
        text = "本魚的權限不夠做這件事啦～"
    elif isinstance(error, app_commands.NoPrivateMessage):
        text = "這個要在伺服器裡才能用啦～"
    elif isinstance(original, discord.Forbidden):
        text = "本魚的權限不夠做這件事啦～"
    else:
        name = interaction.command.qualified_name if interaction.command else "?"
        print(f"斜線指令 /{name} 出錯：{original!r}")
        text = f"嗚嗚出錯了啦：{original}"
    try:
        await send_private(interaction, text[:1900])
    except Exception:
        pass

# ---------- 資訊 ----------

@tree.command(name="延遲", description="看本魚的反應速度")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 本魚的延遲是 {round(client.latency * 1000)} ms～")

@tree.command(name="機器人資訊", description="看本魚的狀態")
async def slash_bot_info(interaction: discord.Interaction):
    embed = discord.Embed(title="🐟 藍色大肥魚", color=discord.Color.blue())
    embed.set_thumbnail(url=client.user.display_avatar.url)
    embed.add_field(name="已上線", value=format_duration(time.time() - BOT_START_TIME))
    embed.add_field(name="伺服器數", value=str(len(client.guilds)))
    embed.add_field(name="延遲", value=f"{round(client.latency * 1000)} ms")
    embed.add_field(name="語言模型", value=ai_model_label())
    embed.add_field(name="discord.py", value=discord.__version__)
    embed.add_field(name="Python", value=sys.version.split()[0])
    await interaction.response.send_message(embed=embed)

HELP_SECTIONS = [
    ("💬 聊天", "@本魚、回覆本魚的訊息、或私訊本魚就能聊天。會自動上網查、精確計算、找 YouTube 連結。\n「@本魚 清除記憶」讓本魚忘掉剛剛的對話。"),
    ("📚 知識庫", "機器人主人放進知識庫的資料（伺服器規則、常見問題、攻略…），本魚聊天時會自動參考，"
               "回答最後會用小字標出資料來源。\n/知識庫 搜尋：直接查資料（不經過 AI）\n/知識庫 清單：看有哪些資料"),
    ("🎵 音樂", "只 @本魚 不打字會跳出控制面板。\n/點歌 /下一首 /上一首 /重播 /暫停 /繼續播放 /現在播放 /播放清單 /循環 /隨機播放 /移除歌曲 /清空佇列 /停止播放\n完整說明：/音樂指令"),
    ("🎮 遊戲", "/釣魚 /魚缸 /釣魚排行：釣魚收集圖鑑\n/井字棋（跟朋友或本魚對戰）/21點 /猜數字（大家一起猜）\n/擲骰 /擲硬幣 /選擇 /占卜 /猜拳"),
    ("🏆 運動", "/比分：看中職、TPBL、P. LEAGUE+、NBA、MLB、NFL、NHL、英超、歐冠等聯賽的比分\n"
               "（P. LEAGUE+ 官網賽後才更新比分，沒有即時比分）\n/運動新聞：看最新新聞（國外聯賽）\n"
               "/運動 追蹤球隊：球隊比賽時自動發即時比分和終場（要有管理頻道權限）\n"
               "/運動 每日摘要：每天固定時間發賽果和賽程\n/運動 新聞訂閱：有新聞自動發\n/運動 查看／移除"),
    ("🧰 實用", "/提醒 設定／清單／取消\n/投票\n/等級 /排行榜\n/伺服器資訊 /成員資訊 /頭像 /延遲 /機器人資訊\n/邀請：把本魚加到你的伺服器（私訊本魚說「邀請」也可以）"),
    ("🛡️ 管理（要有權限才看得到）", "/踢出 /封鎖 /解除封鎖 /禁言 /解除禁言 /清除訊息\n/警告 /警告紀錄 /清除警告 /警告設定\n/慢速模式 /鎖定頻道 /解鎖頻道 /公告"),
    ("🏷️ 身分組", "/身分組 給予／移除／面板\n/自動身分組 新增／移除／查看\n/等級設定 獎勵新增（達到等級自動給）\n也可以說「@本魚 幫 @某人 加身分組 名稱」"),
    ("⚙️ 伺服器設定", "/歡迎 設定／測試／關閉／離開設定／離開關閉\n/紀錄頻道 設定／關閉\n/自動管理 邀請連結／洗版偵測／大量提及／違禁詞新增／違禁詞移除／查看\n/自動回覆 新增／移除／查看\n/等級設定 開關／升級通知／獎勵新增／獎勵移除／獎勵查看／重置\n/抽獎 開始／結束／重抽\n/客服單 面板\n/動態語音 設定／關閉\n/精選 設定／關閉"),
]

GUIDE_START = (
    "嗨～本魚是這個伺服器的藍色胖胖吉祥物！第一次見面，先教你三件事：\n\n"
    "**1. 跟本魚聊天**\n"
    "在頻道裡 @本魚 再打字，或直接「回覆」本魚的訊息就好；私訊本魚的話連 @ 都不用。"
    "問天氣、新聞這種要查的事，本魚會自己上網查。\n\n"
    "**2. 用指令**\n"
    "在聊天框打 `/` 會跳出本魚所有的指令，選了照著填就好。常用的有：\n"
    "`/點歌` 放音樂（要先進語音頻道）　`/釣魚` 釣魚收集圖鑑　`/井字棋` 跟本魚下棋　`/提醒` 設定提醒\n\n"
    "**3. 不知道怎麼辦的時候**\n"
    "只 @本魚 不打字，會跳出音樂控制面板；想再看這份說明就打 `/-使用說明`（或 `/幫助`）。\n\n"
    "👇 下面的選單可以看每一類功能的完整指令。"
)
GUIDE_PAGES = [("🐟 快速上手", GUIDE_START)] + HELP_SECTIONS

def guide_embed(index):
    title, body = GUIDE_PAGES[index]
    embed = discord.Embed(title=f"本魚使用說明　{title}", description=body, color=discord.Color.blue())
    embed.set_footer(text=f"第 {index + 1} / {len(GUIDE_PAGES)} 頁　·　這則訊息只有你看得到")
    return embed

class GuideView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        select = discord.ui.Select(
            placeholder="選一類功能來看…",
            options=[discord.SelectOption(label=title.split(" ", 1)[1][:100], emoji=title.split(" ", 1)[0], value=str(i))
                     for i, (title, _) in enumerate(GUIDE_PAGES)],
        )
        select.callback = self.show_page
        self.add_item(select)

    async def show_page(self, interaction: discord.Interaction):
        index = int(interaction.data["values"][0])
        await interaction.response.edit_message(embed=guide_embed(index), view=self)

async def send_guide(interaction: discord.Interaction):
    await interaction.response.send_message(embed=guide_embed(0), view=GuideView(), ephemeral=True)

# 名稱開頭的「-」讓它在 / 候選清單裡排在最上面（Discord 依指令名稱排序，「-」排在數字和中文前面）
@tree.command(name="-使用說明", description="第一次用本魚？從這裡開始看！")
async def slash_guide(interaction: discord.Interaction):
    await send_guide(interaction)

@tree.command(name="幫助", description="看本魚所有的功能")
async def slash_help(interaction: discord.Interaction):
    await send_guide(interaction)

# ---------- 運動比分與新聞（資料來源：ESPN 公開介面，免費、不用金鑰）----------
# 不帶日期的 scoreboard 會自己給「今天／這週／這一輪」的比賽，各種運動都通用，所以即時追蹤只用它。
# 中職、台灣職籃沒有公開資料來源，要爬官網、容易壞，先不做。

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SPORTS_LEAGUES = {
    # 代碼: (顯示名稱, 運動, ESPN 聯賽代碼, emoji)；ESPN 代碼是 None 的是台灣聯賽，資料從各自官網抓
    "cpbl": ("中華職棒", "baseball", None, "⚾"),
    "tpbl": ("TPBL 職籃", "basketball", None, "🏀"),
    "plg": ("P. LEAGUE+ 職籃", "basketball", None, "🏀"),
    "nba": ("NBA", "basketball", "nba", "🏀"),
    "wnba": ("WNBA", "basketball", "wnba", "🏀"),
    "mlb": ("MLB 美國職棒", "baseball", "mlb", "⚾"),
    "nfl": ("NFL 美式足球", "football", "nfl", "🏈"),
    "nhl": ("NHL 冰球", "hockey", "nhl", "🏒"),
    "epl": ("英超", "soccer", "eng.1", "⚽"),
    "laliga": ("西甲", "soccer", "esp.1", "⚽"),
    "bundesliga": ("德甲", "soccer", "ger.1", "⚽"),
    "seriea": ("義甲", "soccer", "ita.1", "⚽"),
    "ligue1": ("法甲", "soccer", "fra.1", "⚽"),
    "ucl": ("歐冠", "soccer", "uefa.champions", "⚽"),
    "mls": ("美職足 MLS", "soccer", "usa.1", "⚽"),
}
LEAGUE_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in SPORTS_LEAGUES.items()]
# 台灣聯賽沒有新聞來源，新聞相關指令只列 ESPN 的聯賽
NEWS_LEAGUE_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in SPORTS_LEAGUES.items() if v[2]]
TAIPEI = timezone(timedelta(hours=8))
REGULATION_PERIODS = {"basketball": 4, "football": 4, "hockey": 3}
TEAM_NAMES_ZH = {
    "nba": {
        "ATL": "老鷹", "BOS": "塞爾提克", "BKN": "籃網", "CHA": "黃蜂", "CHI": "公牛", "CLE": "騎士",
        "DAL": "獨行俠", "DEN": "金塊", "DET": "活塞", "GS": "勇士", "HOU": "火箭", "IND": "溜馬",
        "LAC": "快艇", "LAL": "湖人", "MEM": "灰熊", "MIA": "熱火", "MIL": "公鹿", "MIN": "灰狼",
        "NO": "鵜鶘", "NY": "尼克", "OKC": "雷霆", "ORL": "魔術", "PHI": "七六人", "PHX": "太陽",
        "POR": "拓荒者", "SAC": "國王", "SA": "馬刺", "TOR": "暴龍", "UTAH": "爵士", "WSH": "巫師",
    },
    "mlb": {
        "ARI": "響尾蛇", "ATH": "運動家", "ATL": "勇士", "BAL": "金鶯", "BOS": "紅襪", "CHC": "小熊",
        "CHW": "白襪", "CIN": "紅人", "CLE": "守護者", "COL": "洛磯", "DET": "老虎", "HOU": "太空人",
        "KC": "皇家", "LAA": "天使", "LAD": "道奇", "MIA": "馬林魚", "MIL": "釀酒人", "MIN": "雙城",
        "NYM": "大都會", "NYY": "洋基", "PHI": "費城人", "PIT": "海盜", "SD": "教士", "SF": "巨人",
        "SEA": "水手", "STL": "紅雀", "TB": "光芒", "TEX": "遊騎兵", "TOR": "藍鳥", "WSH": "國民",
    },
}
SPORTS_LOOP_SECONDS = 60
SPORTS_NEWS_EVERY = 15          # 每 15 圈（約 15 分鐘）檢查一次新聞
SPORTS_MAX_SUBS = 20            # 每個伺服器最多幾個訂閱
SPORTS_DAILY_GRACE = 3 * 3600   # 機器人晚開機的話，超過設定時間 3 小時內還是會補發當天摘要
TEAM_CACHE_SECONDS = 12 * 3600

# 不要改 User-Agent：ESPN 的防火牆（Akamai）會擋假裝成瀏覽器或自訂名稱的 User-Agent，只放行 requests 預設的
sports_session = requests.Session()
team_cache = {}                 # 聯賽 -> (抓取時間, [隊伍])
sports_live_messages = {}       # (頻道ID, 比賽ID) -> {"message": 訊息, "text": 上次內容}

def espn_get(league, endpoint, params=None):
    _, sport, code, _ = SPORTS_LEAGUES[league]
    resp = sports_session.get(f"{ESPN_BASE}/{sport}/{code}/{endpoint}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def team_display_name(league, team):
    zh = TEAM_NAMES_ZH.get(league, {}).get(team.get("abbreviation", ""))
    return zh or team.get("shortDisplayName") or team.get("displayName") or "?"

def parse_espn_time(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

def parse_event(league, event):
    comp = (event.get("competitions") or [{}])[0]
    status = event.get("status") or comp.get("status") or {}
    stype = status.get("type") or {}
    sides = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        score = c.get("score")
        if isinstance(score, dict):
            score = score.get("displayValue")
        sides[c.get("homeAway")] = {
            "id": str(team.get("id", "")),
            "name": team_display_name(league, team),
            "score": str(score or "0"),
            "winner": bool(c.get("winner")),
        }
    return {
        "id": str(event.get("id", "")),
        "start": parse_espn_time(event.get("date")),
        "state": stype.get("state", "pre"),
        "status_name": stype.get("name", ""),
        "short": stype.get("shortDetail") or "",
        "period": status.get("period") or 0,
        "clock": status.get("displayClock") or "",
        "home": sides.get("home"),
        "away": sides.get("away"),
    }

def fetch_scoreboard(league, date=None, fallback=False):
    """date 只給 ESPN 用。台灣聯賽拿「今天」的比賽；fallback=True 時今天沒比賽就改給最近打完和下一個比賽日。"""
    if not SPORTS_LEAGUES[league][2]:
        return taiwan_scoreboard(league, fallback)
    data = espn_get(league, "scoreboard", {"dates": date} if date else None)
    events = [parse_event(league, e) for e in data.get("events") or []]
    return [e for e in events if e["home"] and e["away"]]

# ---------- 台灣聯賽（中職、TPBL、P. LEAGUE+）：沒有公開 API，從官網網頁用的資料介面抓 ----------
# 官網改版就可能壞掉，壞了會在 /比分 顯示錯誤訊息，其他聯賽不受影響

BROWSER_UA = BILI_HEADERS["User-Agent"]
CPBL_BASE = "https://www.cpbl.com.tw"
TPBL_API = "https://api.tpbl.basketball/api"
PLG_SCHEDULE_URL = "https://pleagueofficial.com/schedule-regular-season"
CPBL_TEAMS = {
    "ACN011": "中信兄弟", "ADD011": "統一7-ELEVEn獅", "AEO011": "富邦悍將",
    "AJL011": "樂天桃猿", "AAA011": "味全龍", "AKP011": "台鋼雄鷹",
}
TAIWAN_SOURCES = {
    "cpbl": "資料來源：中華職棒官網",
    "tpbl": "資料來源：TPBL 官網",
    "plg": "資料來源：P. LEAGUE+ 官網（官網賽後才更新比分，沒有即時比分）",
}
# 中職官網跟 ESPN 相反：不像瀏覽器的連線會被回 404
taiwan_session = requests.Session()
taiwan_session.headers["User-Agent"] = BROWSER_UA
cpbl_token = {"value": None, "at": 0.0}
taiwan_cache = {}   # 快取鍵 -> (抓取時間, 保存秒數, 比賽清單)

def taiwan_time(text):
    try:
        return datetime.fromisoformat(text).replace(tzinfo=TAIPEI)
    except (TypeError, ValueError):
        return None

def taiwan_side(team_id, name, score, other_score, final):
    return {"id": str(team_id), "name": name, "score": str(score if score is not None else 0),
            "winner": bool(final and score is not None and other_score is not None and score > other_score)}

def taiwan_event(event_id, start, state, home, away, status_text=None, status_name=""):
    return {"id": event_id, "start": start, "state": state, "status_name": status_name, "short": "",
            "period": 0, "clock": "", "home": home, "away": away, "status_text": status_text}

def cached_fetch(key, fetch):
    """比賽快開始或正在打時每分鐘更新；其他時候 10 分鐘才重抓一次，不要一直敲官網。"""
    cached = taiwan_cache.get(key)
    if cached and time.time() - cached[0] < cached[1]:
        return cached[2]
    events = fetch()
    now = datetime.now(TAIPEI)
    busy = any(e["state"] == "in" or (e["state"] == "pre" and e["start"]
               and timedelta(minutes=-240) < e["start"] - now < timedelta(minutes=30)) for e in events)
    taiwan_cache[key] = (time.time(), 55 if busy else 600, events)
    return events

def fetch_scoreboard_cached(league):
    """即時追蹤和 /比分 共用：有比賽快開始或正在打時約每分鐘更新，沒比賽的時段（半夜、休賽季）10 分鐘才抓一次。"""
    return cached_fetch(("board", league), lambda: fetch_scoreboard(league))

def cpbl_post(path, data):
    resp = None
    for attempt in range(2):
        # 驗證碼半小時換一次；第一次失敗也重拿（可能過期了）
        if attempt or not cpbl_token["value"] or time.time() - cpbl_token["at"] > 1800:
            page = taiwan_session.get(CPBL_BASE + "/", timeout=20).text
            match = re.search(r'id="MainForm".*?name="__RequestVerificationToken"[^>]*value="([^"]+)"', page, re.S)
            if not match:
                raise ValueError("中職官網改版了，找不到資料介面")
            cpbl_token.update(value=match.group(1), at=time.time())
        resp = taiwan_session.post(
            CPBL_BASE + path, data={"__RequestVerificationToken": cpbl_token["value"], **data},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": CPBL_BASE + "/"}, timeout=20,
        )
        if resp.ok and resp.text.startswith("{"):
            result = resp.json()
            if result.get("Success"):
                return result
    raise ValueError(f"中職官網沒有回傳正常資料（HTTP {resp.status_code if resp is not None else '?'}）")

def cpbl_live_status(game):
    """比賽中的局數要另外查單場資料。"""
    try:
        data = cpbl_post("/home/gamedetail", {"GameSno": game["GameSno"], "Year": game["Year"],
                                              "KindCode": game["KindCode"], "GameStatus": game["GameStatus"]})
        detail = json.loads(data.get("CurtGameDetailJson") or "{}")
    except Exception as e:
        print(f"抓中職比賽局數失敗：{e}")
        return None, None, None
    inning, half = detail.get("CurtSeq"), detail.get("CurtVisitingHomeType")
    text = f"{inning} 局{'上' if str(half) == '1' else '下'}" if inning else None
    return text, detail.get("VisitingScore"), detail.get("HomeScore")

def cpbl_games(day):
    data = cpbl_post("/home/getdetaillist", {"GameDate": day.strftime("%Y/%m/%d")})
    events = []
    for g in json.loads(data.get("GameADetailJson") or "null") or []:
        status = g.get("GameStatus")
        away_score = g.get("VisitingTotalScore") if g.get("VisitingTotalScore") is not None else g.get("VisitingScore")
        home_score = g.get("HomeTotalScore") if g.get("HomeTotalScore") is not None else g.get("HomeScore")
        status_text, status_name = None, ""
        if status in (2, 8):
            state = "in"
            live_text, live_away, live_home = cpbl_live_status(g)
            if live_away is not None and live_home is not None:
                away_score, home_score = live_away, live_home
            status_text = "比賽暫停" if status == 8 else (live_text or "比賽中")
        elif status == 3:
            state = "post"
        elif status in (5, 6, 7):
            state, status_text, status_name = "post", "延賽或取消", "STATUS_POSTPONED"
        else:
            state = "pre"  # 1 未開打、4 已公布先發打序
        final = state == "post" and not status_name
        home_code, away_code = g.get("HomeTeamCode"), g.get("VisitingTeamCode")
        events.append(taiwan_event(
            f"cpbl-{g.get('Year')}-{g.get('KindCode')}-{g.get('GameSno')}",
            taiwan_time(g.get("GameDateTimeS") or g.get("PreExeDate")), state,
            taiwan_side(home_code, g.get("HomeTeamName") or CPBL_TEAMS.get(home_code, "?"), home_score, away_score, final),
            taiwan_side(away_code, g.get("VisitingTeamName") or CPBL_TEAMS.get(away_code, "?"), away_score, home_score, final),
            status_text, status_name,
        ))
    return events

def tpbl_season_games():
    seasons = sports_session.get(f"{TPBL_API}/seasons", timeout=20).json()
    current = next((s for s in seasons if s.get("status") == "IN_PROGRESS"), None) or seasons[-1]
    games = sports_session.get(f"{TPBL_API}/seasons/{current['id']}/games", timeout=20).json()
    # 新賽季剛開始時，上一季最後的比賽也留著，今天沒比賽時才有「最近打完的」可以看
    index = seasons.index(current)
    if current.get("status") == "IN_PROGRESS" and index > 0 and not any(g.get("status") == "COMPLETED" for g in games):
        previous = seasons[index - 1]
        games = sports_session.get(f"{TPBL_API}/seasons/{previous['id']}/games", timeout=20).json() + games
    events = []
    for g in games:
        home, away = g.get("home_team") or {}, g.get("away_team") or {}
        status = g.get("status")
        state = {"IN_PROGRESS": "in", "LIVE": "in", "COMPLETED": "post"}.get(status, "pre")
        if g.get("is_live") and state == "pre":
            state = "in"
        status_text = None
        if state == "in":
            quarter = g.get("round") or 0
            status_text = "比賽中" if quarter <= 0 else f"第 {quarter} 節" if quarter <= 4 else f"延長賽 {quarter - 4}"
        final = state == "post"
        home_score, away_score = home.get("won_score"), away.get("won_score")
        events.append(taiwan_event(
            f"tpbl-{g.get('id')}", taiwan_time(g.get("gamed_at")), state,
            taiwan_side(home.get("id"), home.get("name", "?"), home_score, away_score, final),
            taiwan_side(away.get("id"), away.get("name", "?"), away_score, home_score, final),
            status_text,
        ))
    return events

def plg_season_games():
    page = taiwan_session.get(PLG_SCHEDULE_URL, timeout=20).text
    rows = [r.split("<!--/單一場次-->")[0] for r in page.split("<!--單一場次-->")[1:]]
    if not rows:
        raise ValueError("P. LEAGUE+ 官網改版了，找不到賽程")
    now = datetime.now(TAIPEI)
    events = []
    for row in rows:
        cls = re.search(r'class="([^"]*match_row[^"]*)"', row)
        year = re.search(r"d-(\d{4})-\d{2}", cls.group(1)) if cls else None
        home_id = re.search(r"team-home-(\d+)", cls.group(1)) if cls else None
        month_day = re.search(r'<h5 class="fs16[^"]*">(\d+)/(\d+)</h5>', row)
        clock = re.search(r'<h6 class="fs12">(\d+):(\d+)</h6>', row)
        names = re.findall(r'<span class="PC_only fs14">([^<]+)</span>', row)
        game_id = re.search(r'href="/game/(\d+)"', row)
        if not (year and home_id and month_day and clock and len(names) >= 2 and game_id):
            continue
        start = datetime(int(year.group(1)), int(month_day.group(1)), int(month_day.group(2)),
                         int(clock.group(1)), int(clock.group(2)), tzinfo=TAIPEI)
        away_id = next((i for i in re.findall(r"\bteam-(\d+)\b", cls.group(1)) if i != home_id.group(1)), "?")
        # 每個比分在網頁上出現兩次（電腦版、手機版），第一個是客隊、最後一個是主隊
        scores = [s.strip() for s in re.findall(r'<h6 class="[^"]*ff8bit[^"]*">([^<]*)</h6>', row)]
        final = len(scores) >= 2 and scores[0].isdigit() and scores[-1].isdigit()
        away_score, home_score = (int(scores[0]), int(scores[-1])) if final else (None, None)
        status_text = None
        if not final and timedelta(0) < now - start < timedelta(hours=3):
            status_text = "比賽中（官網賽後才更新比分）"
        events.append(taiwan_event(
            f"plg-{game_id.group(1)}", start, "post" if final else "pre",
            taiwan_side(f"plg{home_id.group(1)}", names[1].strip(), home_score, away_score, final),
            taiwan_side(f"plg{away_id}", names[0].strip(), away_score, home_score, final),
            status_text,
        ))
    return events

def taiwan_events_between(league, start, end):
    if league == "cpbl":
        events = []
        day = start.astimezone(TAIPEI).date()
        last = end.astimezone(TAIPEI).date()
        while day <= last:
            events += cached_fetch(("cpbl", day), lambda d=day: cpbl_games(d))
            day += timedelta(days=1)
    elif league == "tpbl":
        events = cached_fetch("tpbl", tpbl_season_games)
    else:
        events = cached_fetch("plg", plg_season_games)
    return [e for e in events if e["start"] and start <= e["start"] < end]

def taiwan_scoreboard(league, fallback):
    today = datetime.now(TAIPEI).replace(hour=0, minute=0, second=0, microsecond=0)
    events = taiwan_events_between(league, today, today + timedelta(days=1))
    if events or not fallback:
        return events
    # 今天沒比賽：給最近一個打完的比賽日和下一個比賽日。中職要一天一天查，只往前後找 3 天
    span = timedelta(days=3 if league == "cpbl" else 365)
    past = taiwan_events_between(league, today - span, today)
    future = taiwan_events_between(league, today + timedelta(days=1), today + timedelta(days=1) + span)
    day_of = lambda e: e["start"].astimezone(TAIPEI).date()
    result = []
    if past:
        last_day = max(map(day_of, past))
        result += [e for e in past if day_of(e) == last_day]
    if future:
        next_day = min(map(day_of, future))
        result += [e for e in future if day_of(e) == next_day]
    return result

def taiwan_teams(league):
    if league == "cpbl":
        return [{"id": code, "name": name, "full": name, "abbr": ""} for code, name in CPBL_TEAMS.items()]
    events = taiwan_events_between(league, datetime.min.replace(tzinfo=TAIPEI), datetime.max.replace(tzinfo=TAIPEI))
    teams = {}
    for e in events:
        for side in (e["home"], e["away"]):
            teams.setdefault(side["id"], {"id": side["id"], "name": side["name"], "full": side["name"], "abbr": ""})
    return list(teams.values())

def event_status_text(league, ev):
    if ev.get("status_text"):
        return ev["status_text"]
    sport = SPORTS_LEAGUES[league][1]
    name, short, period, clock = ev["status_name"], ev["short"], ev["period"], ev["clock"]
    if name == "STATUS_POSTPONED":
        return "延期"
    if name in ("STATUS_CANCELED", "STATUS_ABANDONED"):
        return "取消"
    if ev["state"] == "pre":
        return ev["start"].astimezone(TAIPEI).strftime("%m/%d %H:%M 開打") if ev["start"] else "未開打"
    if ev["state"] == "post":
        if "Pen" in short:
            return "終場（PK 大戰）"
        if "OT" in short or "AET" in short or "/" in short:
            return "終場（延長賽）"
        return "終場"
    if name == "STATUS_HALFTIME":
        return "中場休息"
    if sport == "baseball":
        half = {"Top": "上", "Bot": "下", "Mid": "上結束", "End": "下結束"}.get(short.split(" ")[0], "")
        return f"{period} 局{half}" if half else short
    if sport == "soccer":
        return f"比賽中 {clock}".strip()
    if name == "STATUS_END_PERIOD":
        return f"第 {period} 節結束"
    if period > REGULATION_PERIODS.get(sport, 99):
        return f"延長賽 {clock}".strip()
    return f"第 {period} 節 {clock}".strip()

def event_line(league, ev):
    home, away = ev["home"], ev["away"]
    # 足球習慣主隊寫前面；美國職業運動習慣客隊寫前面
    first, second = (home, away) if SPORTS_LEAGUES[league][1] == "soccer" else (away, home)
    status = event_status_text(league, ev)
    if ev["state"] == "pre":
        return f"{first['name']} vs {second['name']}　`{status}`"
    left = f"{first['name']} {first['score']}"
    right = f"{second['score']} {second['name']}"
    if first["winner"]:
        left = f"**{left}**"
    if second["winner"]:
        right = f"**{right}**"
    return f"{left} : {right}　`{status}`"

def sports_footer(league):
    order = "左邊是主隊" if SPORTS_LEAGUES[league][1] == "soccer" else "左邊是客隊、右邊是主隊"
    source = TAIWAN_SOURCES.get(league, "資料來源：ESPN")
    return f"{source}　·　{order}　·　時間為台灣時間"

def scoreboard_embed(league, events, title_suffix="比分"):
    display, _, _, emoji = SPORTS_LEAGUES[league]
    embed = discord.Embed(title=f"{emoji} {display} {title_suffix}", color=discord.Color.blue())
    groups = (("🔴 進行中", "in"), ("✅ 已結束", "post"), ("🕒 未開打", "pre"))
    for label, state in groups:
        lines = [event_line(league, e) for e in sorted(events, key=lambda e: e["start"] or datetime.max.replace(tzinfo=timezone.utc))
                 if e["state"] == state]
        if not lines:
            continue
        text = ""
        for line in lines:
            if len(text) + len(line) + 1 > 1000:
                text += "\n…"
                break
            text += line + "\n"
        embed.add_field(name=label, value=text.strip(), inline=False)
    if not embed.fields:
        embed.description = "這段時間沒有比賽喔～可能是休賽季。"
    embed.set_footer(text=sports_footer(league))
    return embed

def fetch_recent_events(league):
    """每日摘要用：過去 30 小時打完的和接下來 24 小時要打的比賽。"""
    now = datetime.now(timezone.utc)
    if not SPORTS_LEAGUES[league][2]:
        events = taiwan_events_between(league, now - timedelta(hours=30), now + timedelta(hours=24))
        return ([e for e in events if e["state"] == "post"],
                [e for e in events if e["state"] == "pre" and e["start"] >= now])
    events = {}
    for date in (None, *((now + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1))):
        try:
            for e in fetch_scoreboard(league, date):
                events[e["id"]] = e
        except Exception as e:
            print(f"抓 {league} 比分（{date or '預設'}）失敗：{e}")
    results = [e for e in events.values() if e["state"] == "post" and e["start"] and now - e["start"] < timedelta(hours=30)]
    upcoming = [e for e in events.values() if e["state"] == "pre" and e["start"] and timedelta(0) <= e["start"] - now < timedelta(hours=24)]
    return results, upcoming

def fetch_teams(league):
    cached = team_cache.get(league)
    if cached and time.time() - cached[0] < TEAM_CACHE_SECONDS:
        return cached[1]
    if not SPORTS_LEAGUES[league][2]:
        teams = taiwan_teams(league)
        team_cache[league] = (time.time(), teams)
        return teams
    data = espn_get(league, "teams")
    teams = []
    for item in data["sports"][0]["leagues"][0]["teams"]:
        t = item.get("team") or {}
        teams.append({
            "id": str(t.get("id", "")),
            "name": team_display_name(league, t),
            "full": t.get("displayName") or "",
            "abbr": t.get("abbreviation") or "",
        })
    team_cache[league] = (time.time(), teams)
    return teams

def find_team(league, text):
    text = text.strip().casefold()
    teams = fetch_teams(league)
    for t in teams:
        if text in (t["id"], t["abbr"].casefold(), t["name"].casefold(), t["full"].casefold()):
            return t
    matches = [t for t in teams if text in t["name"].casefold() or text in t["full"].casefold()]
    return matches[0] if len(matches) == 1 else None

def fetch_news(league, limit=10):
    data = espn_get(league, "news", {"limit": limit})
    articles = []
    for a in data.get("articles") or []:
        url = ((a.get("links") or {}).get("web") or {}).get("href")
        if not a.get("headline") or not url:
            continue
        images = a.get("images") or []
        articles.append({
            "id": str(a.get("id") or url),
            "title": a["headline"],
            "description": a.get("description") or "",
            "url": url,
            "image": images[0].get("url") if images else None,
            "published": parse_espn_time(a.get("published")),
        })
    return articles

def news_embed(league, article):
    display, _, _, emoji = SPORTS_LEAGUES[league]
    embed = discord.Embed(title=article["title"][:256], url=article["url"],
                          description=article["description"][:500], color=discord.Color.dark_blue())
    embed.set_author(name=f"{emoji} {display} 新聞")
    if article["image"]:
        embed.set_thumbnail(url=article["image"])
    if article["published"]:
        embed.timestamp = article["published"]
    embed.set_footer(text="資料來源：ESPN")
    return embed

def sports_subs(guild_id):
    return get_guild_settings(guild_id).setdefault("sports", {}).setdefault("subs", [])

def describe_sub(sub):
    league = SPORTS_LEAGUES.get(sub.get("league"), ("?",))[0]
    channel = f"<#{sub.get('channel')}>"
    if sub["kind"] == "team":
        return f"🔴 即時追蹤 {league}「{sub.get('team_name')}」→ {channel}"
    if sub["kind"] == "daily":
        return f"🗓️ 每天 {sub.get('time')} 發 {league} 賽果和賽程 → {channel}"
    return f"📰 {league} 新聞 → {channel}"

async def sports_send(channel, **kwargs):
    try:
        return await channel.send(**kwargs)
    except discord.Forbidden:
        print(f"運動通知：沒有權限在 #{channel.name} 發訊息。")
    except Exception as e:
        print(f"運動通知發送失敗（#{getattr(channel, 'name', '?')}）：{e}")
    return None

def live_game_embed(league, ev):
    display, _, _, emoji = SPORTS_LEAGUES[league]
    live = ev["state"] == "in"
    embed = discord.Embed(
        title=f"{emoji} {display}　{'比賽進行中' if live else '比賽結束'}",
        description=event_line(league, ev),
        color=discord.Color.red() if live else discord.Color.green(),
    )
    embed.set_footer(text=sports_footer(league) + ("　·　每分鐘自動更新" if live else ""))
    return embed

async def sports_check_teams(active):
    """active: [(伺服器ID, 訂閱, 頻道)]，只處理 kind == team 的。"""
    team_subs = [(gid, sub, ch) for gid, sub, ch in active if sub["kind"] == "team"]
    if not team_subs:
        return False
    boards = {}
    for league in {sub["league"] for _, sub, _ in team_subs}:
        try:
            boards[league] = await asyncio.to_thread(fetch_scoreboard_cached, league)
        except Exception as e:
            print(f"抓 {league} 即時比分失敗：{e}")
    changed = False
    handled = set()
    now = datetime.now(timezone.utc)
    for gid, sub, channel in team_subs:
        for ev in boards.get(sub["league"], []):
            if sub["team"] not in (ev["home"]["id"], ev["away"]["id"]):
                continue
            key = (channel.id, ev["id"])
            if key in handled:
                continue  # 同一個頻道同時追蹤比賽的兩隊，只發一次
            handled.add(key)
            live = sports_live_messages.get(key)
            if ev["state"] == "in":
                embed = live_game_embed(sub["league"], ev)
                text = embed.description
                if live is None:
                    msg = await sports_send(channel, embed=embed)
                    if msg:
                        sports_live_messages[key] = {"message": msg, "text": text}
                elif live["text"] != text:
                    try:
                        await live["message"].edit(embed=embed)
                        live["text"] = text
                    except discord.NotFound:
                        sports_live_messages.pop(key, None)
                    except Exception as e:
                        print(f"更新即時比分失敗：{e}")
            elif ev["state"] == "post":
                done = sub.setdefault("done", [])
                if ev["id"] in done:
                    continue
                done.append(ev["id"])
                del done[:-30]
                changed = True
                # 機器人沒在跑時打完的比賽，太久以前的就不補發了
                if live is None and (not ev["start"] or now - ev["start"] > timedelta(hours=12)):
                    continue
                embed = live_game_embed(sub["league"], ev)
                if live:
                    sports_live_messages.pop(key, None)
                    try:
                        await live["message"].edit(embed=embed)
                    except Exception:
                        pass
                called_off = ev["status_name"] in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED")
                prefix = "📢 比賽異動" if called_off else "🏁 終場"
                await sports_send(channel, content=f"{prefix}：{event_line(sub['league'], ev)}")
    return changed

async def sports_check_daily(active):
    changed = False
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    cache = {}
    for gid, sub, channel in active:
        if sub["kind"] != "daily" or sub.get("last") == today:
            continue
        hour, minute = map(int, sub["time"].split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (0 <= (now - target).total_seconds() <= SPORTS_DAILY_GRACE):
            continue
        sub["last"] = today
        changed = True
        league = sub["league"]
        if league not in cache:
            cache[league] = await asyncio.to_thread(fetch_recent_events, league)
        results, upcoming = cache[league]
        if results or upcoming:  # 休賽季就不要每天發一則「沒有比賽」
            await sports_send(channel, embed=scoreboard_embed(league, results + upcoming, "每日賽果・賽程"))
    return changed

async def sports_check_news(active):
    changed = False
    cache = {}
    for gid, sub, channel in active:
        if sub["kind"] != "news":
            continue
        league = sub["league"]
        if league not in cache:
            try:
                cache[league] = await asyncio.to_thread(fetch_news, league)
            except Exception as e:
                print(f"抓 {league} 新聞失敗：{e}")
                cache[league] = []
        seen = sub.setdefault("seen", [])
        fresh = [a for a in cache[league] if a["id"] not in seen]
        if not fresh:
            continue
        # ESPN 最新的在最前面，倒過來從舊的開始發；一次最多 3 則，免得洗版
        for article in list(reversed(fresh))[-3:]:
            await sports_send(channel, embed=news_embed(league, article))
        seen.extend(a["id"] for a in fresh)
        del seen[:-80]
        changed = True
    return changed

def active_sports_subs():
    active = []
    for guild in client.guilds:
        if not guild_enabled(guild):
            continue
        for sub in guild_settings.get(str(guild.id), {}).get("sports", {}).get("subs", []):
            channel = guild.get_channel(sub.get("channel"))
            if channel is None or sub.get("league") not in SPORTS_LEAGUES:
                continue
            if not channel_feature_enabled(channel, "sports"):
                continue
            active.append((guild.id, sub, channel))
    return active

async def sports_loop():
    await client.wait_until_ready()
    tick = 0
    while True:
        active = active_sports_subs()
        changed = False
        for name, check, due in (
            ("即時比分", sports_check_teams, True),
            ("每日摘要", sports_check_daily, True),
            ("新聞", sports_check_news, tick % SPORTS_NEWS_EVERY == 0),
        ):
            if not due:
                continue
            try:
                changed |= await check(active)
            except Exception as e:
                print(f"運動{name}處理失敗：{e}")
        if changed:
            save_guild_settings()
        tick += 1
        await asyncio.sleep(SPORTS_LOOP_SECONDS)

@tree.command(name="比分", description="看某個聯賽今天（這一輪）的比分")
@app_commands.rename(league="聯賽")
@app_commands.choices(league=LEAGUE_CHOICES)
async def slash_scores(interaction: discord.Interaction, league: app_commands.Choice[str]):
    await interaction.response.defer(thinking=True)
    try:
        if SPORTS_LEAGUES[league.value][2]:
            events = await asyncio.to_thread(fetch_scoreboard_cached, league.value)
        else:  # 台灣聯賽今天沒比賽時要改抓最近的比賽日，自己內部已經有快取
            events = await asyncio.to_thread(fetch_scoreboard, league.value, None, True)
    except Exception as e:
        return await interaction.followup.send(f"嗚…抓不到比分：{e}")
    await interaction.followup.send(embed=scoreboard_embed(league.value, events))

@tree.command(name="運動新聞", description="看某個聯賽最新的新聞（英文）")
@app_commands.rename(league="聯賽")
@app_commands.choices(league=NEWS_LEAGUE_CHOICES)
async def slash_sports_news(interaction: discord.Interaction, league: app_commands.Choice[str]):
    await interaction.response.defer(thinking=True)
    try:
        articles = await asyncio.to_thread(fetch_news, league.value, 6)
    except Exception as e:
        return await interaction.followup.send(f"嗚…抓不到新聞：{e}")
    if not articles:
        return await interaction.followup.send("現在沒有新聞耶～")
    display, _, _, emoji = SPORTS_LEAGUES[league.value]
    lines = [f"**[{a['title']}]({a['url']})**" for a in articles[:6]]
    embed = discord.Embed(title=f"{emoji} {display} 最新新聞", description="\n\n".join(lines)[:4000],
                          color=discord.Color.dark_blue())
    embed.set_footer(text="資料來源：ESPN")
    await interaction.followup.send(embed=embed)

sports_group = app_commands.Group(
    name="運動", description="設定自動發比分、每日摘要、運動新聞的頻道",
    guild_only=True, default_permissions=discord.Permissions(manage_channels=True),
)

async def team_autocomplete(interaction: discord.Interaction, current: str):
    league = getattr(interaction.namespace, "聯賽", None)
    if league not in SPORTS_LEAGUES:
        return []
    try:
        teams = await asyncio.to_thread(fetch_teams, league)
    except Exception:
        return []
    needle = current.strip().casefold()
    picks = [t for t in teams if not needle or needle in t["name"].casefold()
             or needle in t["full"].casefold() or needle == t["abbr"].casefold()]
    return [app_commands.Choice(name=(f"{t['name']}（{t['full']}）" if t["name"] != t["full"] else t["full"])[:100], value=t["id"])
            for t in picks[:25]]

def add_sports_sub(interaction, sub):
    subs = sports_subs(interaction.guild.id)
    if len(subs) >= SPORTS_MAX_SUBS:
        return f"一個伺服器最多 {SPORTS_MAX_SUBS} 個運動訂閱喔～先用 `/運動 移除` 清掉一些吧。"
    for old in subs:
        if all(old.get(k) == sub.get(k) for k in ("kind", "channel", "league", "team")):
            return "這個已經設定過囉～"
    subs.append(sub)
    save_guild_settings()
    warn = ""
    channel = interaction.guild.get_channel(sub["channel"])
    if channel and not channel_feature_enabled(channel, "sports"):
        warn = "\n⚠ 不過這個頻道在控制面板把「運動比分」關掉了，要打開才會發喔。"
    return f"設定好了！{describe_sub(sub)}{warn}"

@sports_group.command(name="追蹤球隊", description="這支球隊比賽時，自動發即時比分（每分鐘更新）和終場結果")
@app_commands.rename(league="聯賽", team="球隊", channel="頻道")
@app_commands.describe(team="打字搜尋球隊", channel="要發在哪個頻道（不填就是這裡）")
@app_commands.choices(league=LEAGUE_CHOICES)
@app_commands.autocomplete(team=team_autocomplete)
async def sports_follow(interaction: discord.Interaction, league: app_commands.Choice[str], team: str,
                        channel: Optional[discord.TextChannel] = None):
    await interaction.response.defer(ephemeral=True)
    try:
        found = await asyncio.to_thread(find_team, league.value, team)
    except Exception as e:
        return await interaction.followup.send(f"查不到球隊清單：{e}", ephemeral=True)
    if not found:
        return await interaction.followup.send("找不到這支球隊耶，打幾個字讓它跳出選項再選喔～", ephemeral=True)
    target = channel or interaction.channel
    # 已經打完、還掛在比分表上的比賽先記成「發過了」，不然一設定就會馬上發一則舊的終場
    try:
        events = await asyncio.to_thread(fetch_scoreboard, league.value)
        done = [e["id"] for e in events if e["state"] == "post" and found["id"] in (e["home"]["id"], e["away"]["id"])]
    except Exception:
        done = []
    sub = {"kind": "team", "channel": target.id, "league": league.value, "team": found["id"],
           "team_name": found["name"], "done": done}
    await interaction.followup.send(add_sports_sub(interaction, sub), ephemeral=True)

@sports_group.command(name="每日摘要", description="每天固定時間發昨天的賽果和今天的賽程")
@app_commands.rename(league="聯賽", at="時間", channel="頻道")
@app_commands.describe(at="24 小時制，例如 09:00", channel="要發在哪個頻道（不填就是這裡）")
@app_commands.choices(league=LEAGUE_CHOICES)
async def sports_daily(interaction: discord.Interaction, league: app_commands.Choice[str], at: str = "09:00",
                       channel: Optional[discord.TextChannel] = None):
    match = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})\s*", at)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return await send_private(interaction, "時間要寫成 24 小時制，例如 `09:00` 或 `21:30` 喔～")
    time_text = f"{int(match.group(1)):02d}:{match.group(2)}"
    target = channel or interaction.channel
    # 設定時如果今天的時間已經過了，就從明天開始發
    now = datetime.now().astimezone()
    last = now.strftime("%Y-%m-%d") if now.strftime("%H:%M") > time_text else ""
    sub = {"kind": "daily", "channel": target.id, "league": league.value, "time": time_text, "last": last}
    await send_private(interaction, add_sports_sub(interaction, sub))

@sports_group.command(name="新聞訂閱", description="有新的運動新聞就自動發到頻道（約每 15 分鐘檢查一次）")
@app_commands.rename(league="聯賽", channel="頻道")
@app_commands.describe(channel="要發在哪個頻道（不填就是這裡）")
@app_commands.choices(league=NEWS_LEAGUE_CHOICES)
async def sports_news_sub(interaction: discord.Interaction, league: app_commands.Choice[str],
                          channel: Optional[discord.TextChannel] = None):
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    # 現有的新聞先記起來，之後只發「新的」，不然一訂閱就灌一堆舊新聞
    try:
        seen = [a["id"] for a in await asyncio.to_thread(fetch_news, league.value, 30)]
    except Exception:
        seen = []
    sub = {"kind": "news", "channel": target.id, "league": league.value, "seen": seen}
    await interaction.followup.send(add_sports_sub(interaction, sub), ephemeral=True)

@sports_group.command(name="查看", description="看這個伺服器設定了哪些運動通知")
async def sports_list(interaction: discord.Interaction):
    subs = sports_subs(interaction.guild.id)
    if not subs:
        return await send_private(interaction, "還沒有設定任何運動通知喔～用 `/運動 追蹤球隊`、`/運動 每日摘要`、`/運動 新聞訂閱` 來設定。")
    lines = []
    for i, sub in enumerate(subs, 1):
        line = f"`{i}.` {describe_sub(sub)}"
        channel = interaction.guild.get_channel(sub.get("channel"))
        if channel is None:
            line += "（頻道不見了）"
        elif not channel_feature_enabled(channel, "sports"):
            line += "（這個頻道的運動比分被關掉了）"
        lines.append(line)
    await send_private(interaction, "\n".join(lines) + "\n\n要刪掉的話用 `/運動 移除 編號`。")

@sports_group.command(name="移除", description="刪掉一個運動通知（編號看 /運動 查看）")
@app_commands.rename(index="編號")
async def sports_remove(interaction: discord.Interaction, index: int):
    subs = sports_subs(interaction.guild.id)
    if not 1 <= index <= len(subs):
        return await send_private(interaction, "沒有這個編號喔，先用 `/運動 查看` 看一下～")
    sub = subs.pop(index - 1)
    save_guild_settings()
    await send_private(interaction, f"刪掉了：{describe_sub(sub)}")

tree.add_command(sports_group)

# ---------- 知識庫 ----------

knowledge_loaded_once = False

def refresh_knowledge():
    global knowledge_loaded_once
    changed = knowledge_base.refresh()
    if changed or not knowledge_loaded_once:
        s = knowledge_base.stats()
        print(f"知識庫：{s['usable']} 個檔案可以用，共 {s['chunks']} 段、約 {s['chars']:,} 字"
              + ("" if s["usable"] else "（把文件放進程式資料夾裡的「知識庫」資料夾就能用）"))
        knowledge_loaded_once = True

async def knowledge_loop():
    """每 20 秒檢查一次知識庫資料夾，有新增、修改、刪除的檔案才重新讀取（沒變動時只花幾十毫秒）。"""
    while True:
        if KNOWLEDGE_ENABLED:
            try:
                await asyncio.to_thread(refresh_knowledge)
            except Exception as e:
                print(f"讀取知識庫失敗：{e}")
        await asyncio.sleep(KNOWLEDGE_REFRESH_SECONDS)

def reload_knowledge_settings():
    """控制面板改了知識庫設定或加了檔案時呼叫，馬上重新讀取，不用等 20 秒。"""
    global KNOWLEDGE_ENABLED
    enabled = bool(load_config().get("knowledge_enabled", True))
    if enabled != KNOWLEDGE_ENABLED:
        print(f"知識庫已{'開啟' if enabled else '關閉'}。")
    KNOWLEDGE_ENABLED = enabled
    if enabled:
        threading.Thread(target=refresh_knowledge, name="knowledge-reload", daemon=True).start()

knowledge_group = app_commands.Group(name="知識庫", description="查本魚知識庫裡的資料")

@knowledge_group.command(name="搜尋", description="直接在知識庫裡找資料（不經過 AI）")
@app_commands.rename(query="關鍵字")
@app_commands.describe(query="想找什麼，例如：伺服器規則、VIP 價格")
async def knowledge_search_command(interaction: discord.Interaction, query: str):
    if not KNOWLEDGE_ENABLED:
        return await send_private(interaction, "知識庫功能目前是關閉的（機器人主人可以在控制面板的「知識庫」頁打開）。")
    scope = str(interaction.guild_id) if interaction.guild_id else None
    # 直接搜尋時門檻放低一點，寧可多給一點讓使用者自己看
    hits = await asyncio.to_thread(knowledge_base.search, query, scope, 3, 3000, 0.3)
    if not hits:
        return await send_private(interaction, f"知識庫裡找不到跟「{query}」有關的資料耶～")
    embed = discord.Embed(title=f"📚 知識庫：{query[:200]}", color=discord.Color.teal())
    for i, hit in enumerate(hits, 1):
        name = f"{i}. {os.path.basename(hit['file'])}" + (f" › {hit['head']}" if hit["head"] else "")
        text = hit["text"] if len(hit["text"]) <= 1000 else hit["text"][:1000] + "…"
        embed.add_field(name=name[:256], value=text, inline=False)
    embed.set_footer(text="依相關程度排序　·　這些資料是機器人主人放進知識庫的")
    await interaction.response.send_message(embed=embed)

@knowledge_group.command(name="清單", description="看知識庫裡有哪些資料")
async def knowledge_list_command(interaction: discord.Interaction):
    if not KNOWLEDGE_ENABLED:
        return await send_private(interaction, "知識庫功能目前是關閉的（機器人主人可以在控制面板的「知識庫」頁打開）。")
    scope = str(interaction.guild_id) if interaction.guild_id else None
    files = [(rel, doc) for rel, doc in knowledge_base.files(scope) if doc["chunks"] and not doc.get("skipped")]
    if not files:
        return await send_private(interaction, "知識庫裡還沒有資料喔～（機器人主人可以在控制面板的「知識庫」頁加入檔案）")
    lines = [f"{'🔒' if doc['scope'] else '📄'} {os.path.basename(rel)}（{len(doc['chunks'])} 段）" for rel, doc in files]
    shown = "\n".join(lines[:40]) + (f"\n…還有 {len(lines) - 40} 個檔案" if len(lines) > 40 else "")
    embed = discord.Embed(title=f"📚 知識庫裡有 {len(files)} 份資料", description=shown, color=discord.Color.teal())
    embed.set_footer(text="🔒 是這個伺服器專用的資料　·　跟本魚聊天時她會自動參考這些資料")
    await interaction.response.send_message(embed=embed, ephemeral=True)

tree.add_command(knowledge_group)

background_tasks_started = False
background_tasks = set()

async def run_forever(name, loop_fn):
    """背景迴圈出錯時印出來並在幾秒後重新開始，而不是整個默默停掉（例如提醒突然都不會送了）。"""
    while True:
        try:
            await loop_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"背景工作「{name}」出錯，5 秒後重新開始：{e}")
            await asyncio.sleep(5)

async def memory_cleanup_loop():
    """定期清掉很久沒用的暫存資料，不然跑幾個禮拜記憶體會一直長大。"""
    while True:
        await asyncio.sleep(600)
        now_mono = time.monotonic()
        now_wall = time.time()
        for key, last in list(history_last_used.items()):
            if now_mono - last > HISTORY_IDLE_SECONDS:
                history_last_used.pop(key, None)
                conversation_history.pop(key, None)
        for key in [k for k in list(conversation_history) if k not in history_last_used]:
            conversation_history.pop(key, None)
        for key, last in list(xp_cooldowns.items()):
            if now_mono - last > XP_COOLDOWN_SECONDS:
                xp_cooldowns.pop(key, None)
        for key, history in list(spam_tracker.items()):
            if not history or now_mono - history[-1] > SPAM_WINDOW_SECONDS:
                spam_tracker.pop(key, None)
        for key, last in list(fishing_cooldowns.items()):
            if now_mono - last > FISH_COOLDOWN_SECONDS:
                fishing_cooldowns.pop(key, None)
        for key, requested_at in list(pending_song_requests.items()):
            if now_wall - requested_at > PENDING_SONG_TIMEOUT:
                pending_song_requests.pop(key, None)
        for channel_id, lock in list(channel_locks.items()):
            if not lock.locked() and not getattr(lock, "_waiters", None):
                channel_locks.pop(channel_id, None)
        for user_id, calls in list(ai_user_calls.items()):
            if not calls or now_mono - calls[-1] > 60:
                ai_user_calls.pop(user_id, None)
        # 已經結束、存在很久的運動比分快取也清掉（中職每天一份）
        for key, (fetched_at, ttl, _) in list(taiwan_cache.items()):
            if now_wall - fetched_at > max(ttl, 3600):
                taiwan_cache.pop(key, None)

def start_background_tasks():
    global background_tasks_started
    if background_tasks_started:
        return
    background_tasks_started = True
    for name, fn in (
        ("提醒", reminder_loop),
        ("抽獎", giveaway_loop),
        ("等級存檔", levels_autosave_loop),
        ("記憶體清理", memory_cleanup_loop),
        ("釣魚存檔", fishing_autosave_loop),
        ("運動比分", sports_loop),
        ("知識庫", knowledge_loop),
    ):
        task = client.loop.create_task(run_forever(name, fn))
        background_tasks.add(task)

@client.event
async def on_voice_state_update(member, before, after):
    if not guild_enabled(member.guild):
        return
    try:
        await music_voice_state_update(member, before, after)
    except Exception as e:
        print(f"音樂語音狀態處理失敗：{e}")
    try:
        await temp_voice_state_update(member, before, after)
    except Exception as e:
        print(f"動態語音處理失敗：{e}")


COMMAND_SYNC_FILE = os.path.join(get_base_dir(), "command_sync.json")

def command_tree_signature():
    """算出目前所有斜線指令的指紋。指令沒變就不用每次開機都重新註冊（省時間，也不會被 Discord 限流）。"""
    import hashlib
    try:
        payload = []
        for cmd in tree.get_commands():
            try:
                payload.append(cmd.to_dict(tree))
            except TypeError:
                payload.append(cmd.to_dict())
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception as e:
        print(f"計算指令指紋失敗，改成每次都重新註冊：{e}")
        return None

async def sync_commands_to_guild(guild, signature=None, synced_record=None, force=False):
    key = str(guild.id)
    if not force and signature and synced_record is not None and synced_record.get(key) == signature:
        return False
    try:
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"已在「{guild.name}」註冊 {len(synced)} 個斜線指令")
        if signature and synced_record is not None:
            synced_record[key] = signature
        return True
    except Exception as e:
        print(f"在「{guild.name}」註冊斜線指令失敗：{e}")
        return False

# ---------- 邀請與伺服器啟用管理 ----------

GUILD_DISABLED_TEXT = "本魚在這個伺服器休息中，暫時不提供服務喔～（要請機器人的主人在控制面板啟用）"
GUILDS_SNAPSHOT_FILE = os.path.join(get_base_dir(), "guilds.json")
INVITE_PERMISSION_NAMES = (
    "view_channel", "send_messages", "send_messages_in_threads", "embed_links", "attach_files",
    "read_message_history", "add_reactions", "use_external_emojis", "manage_messages", "manage_roles",
    "manage_channels", "kick_members", "ban_members", "moderate_members", "move_members",
    "connect", "speak", "send_polls",
)
INVITE_KEYWORDS = ("邀請本魚", "邀請你", "邀請連結", "邀請網址", "加入伺服器", "加到伺服器", "加進伺服器",
                   "拉進伺服器", "拉你進", "拉本魚", "invite")
bot_is_public = None
bot_owner_ids = set()

def is_invite_request(text):
    lowered = text.strip().lower()
    return lowered in ("邀請", "邀請我", "invite") or any(k in lowered for k in INVITE_KEYWORDS)

def invite_permissions():
    perms = discord.Permissions.none()
    for name in INVITE_PERMISSION_NAMES:
        if hasattr(discord.Permissions, name):
            setattr(perms, name, True)
    return perms

def invite_url():
    app_id = client.application_id or client.user.id
    return discord.utils.oauth_url(app_id, permissions=invite_permissions(), scopes=("bot", "applications.commands"))

async def refresh_app_info():
    global bot_is_public, bot_owner_ids
    try:
        info = await client.application_info()
    except Exception as e:
        print(f"讀取機器人應用程式資訊失敗：{e}")
        return
    bot_is_public = info.bot_public
    owners = {info.owner.id} if info.owner else set()
    if getattr(info, "team", None):
        owners |= {m.id for m in info.team.members}
    bot_owner_ids = owners
    if not bot_is_public:
        print("提醒：這隻機器人不是公開的（Public Bot 沒打開），只有你自己能用邀請連結把她加到伺服器。")

def build_invite_reply(user_id):
    is_owner = user_id in bot_owner_ids
    if bot_is_public is False and not is_owner:
        return "本魚的主人目前沒有開放讓其他人邀請本魚耶…想要的話可以去問問主人喔～", None
    lines = [
        "想讓本魚游到你的伺服器嗎？按下面的按鈕，選好伺服器再按「授權」就好～",
        "（你要在那個伺服器有「管理伺服器」權限才能邀請喔）",
    ]
    if not new_guild_default:
        lines.append("本魚加入之後，要等主人在控制面板啟用才會開始工作，先跟你說一聲～")
    if bot_is_public is False and is_owner:
        lines.append("\n主人你好～目前只有你能用這個連結。想讓別人也能邀請，要到 Developer Portal 的 Bot 頁面把「Public Bot」打開。")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="把本魚加到你的伺服器", emoji="🐟", url=invite_url()))
    return "\n".join(lines), view

async def send_invite(message):
    text, view = build_invite_reply(message.author.id)
    if view:
        await message.reply(text, view=view, mention_author=False)
    else:
        await message.reply(text, mention_author=False)

@tree.command(name="邀請", description="拿到把本魚加到其他伺服器的邀請按鈕")
async def slash_invite(interaction: discord.Interaction):
    text, view = build_invite_reply(interaction.user.id)
    if view:
        await interaction.response.send_message(text, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)

SNAPSHOT_CHANNEL_TYPES = {
    discord.ChannelType.text: "文字",
    discord.ChannelType.news: "公告",
    discord.ChannelType.forum: "論壇",
    discord.ChannelType.voice: "語音",
    discord.ChannelType.stage_voice: "舞台",
}

def snapshot_channels(guild):
    """給控制面板「頻道功能」分頁用的頻道清單，照 Discord 裡的分類和順序排。"""
    channels = []
    for category, members in guild.by_category():
        for ch in members:
            kind = SNAPSHOT_CHANNEL_TYPES.get(ch.type)
            if kind:
                channels.append({"id": str(ch.id), "name": ch.name, "type": kind,
                                 "category": category.name if category else ""})
    return channels

def write_guild_snapshot():
    """把伺服器清單寫成檔案，給控制面板的「伺服器」、「頻道功能」分頁讀。"""
    try:
        guilds = [
            {"id": str(g.id), "name": g.name, "members": g.member_count or 0, "owner_id": str(g.owner_id or ""),
             "channels": snapshot_channels(g)}
            for g in sorted(client.guilds, key=lambda g: g.name.casefold())
        ]
        atomic_write_json(GUILDS_SNAPSHOT_FILE, {
            "updated": int(time.time()),
            "bot_name": str(client.user),
            "invite_url": invite_url() if client.user else "",
            "public": bot_is_public,
            "guilds": guilds,
        }, indent=2)
    except Exception as e:
        print(f"寫入伺服器清單失敗：{e}")

async def stop_music_in_guild(guild_id):
    state = music_state.get(guild_id)
    if not state:
        return
    state["queue"].clear()
    state["now_playing"] = None
    cancel_idle_disconnect(guild_id)
    vc = state["voice_client"]
    state["voice_client"] = None
    if vc and vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

async def apply_guild_access(guilds):
    """依照啟用設定，對啟用的伺服器註冊斜線指令、對停用的伺服器收掉斜線指令並停止音樂。"""
    signature = command_tree_signature()
    record = load_json_file(COMMAND_SYNC_FILE, {}) if signature else None
    if signature and not isinstance(record, dict):
        record = {}
    changed = skipped = 0
    for guild in guilds:
        key = str(guild.id)
        if guild_enabled(guild):
            if await sync_commands_to_guild(guild, signature, record):
                changed += 1
            elif signature and record.get(key) == signature:
                skipped += 1
        else:
            await stop_music_in_guild(guild.id)
            if record is not None and record.get(key) == "disabled":
                continue
            try:
                tree.clear_commands(guild=guild)
                await tree.sync(guild=guild)
                print(f"「{guild.name}」已停用，斜線指令已從那個伺服器收起來。")
                if record is not None:
                    record[key] = "disabled"
                    changed += 1
            except Exception as e:
                print(f"收起「{guild.name}」的斜線指令失敗：{e}")
    if skipped:
        print(f"有 {skipped} 個伺服器的斜線指令沒有變動，略過重新註冊。（想強制重新註冊就刪掉 command_sync.json）")
    if changed and record is not None:
        try:
            save_json_file(COMMAND_SYNC_FILE, record)
        except Exception as e:
            print(f"儲存指令註冊紀錄失敗：{e}")

async def reload_guild_access():
    before = {g.id: guild_enabled(g) for g in client.guilds}
    load_guild_access()
    changed = [g for g in client.guilds if guild_enabled(g) != before.get(g.id)]
    for g in changed:
        print(f"伺服器「{g.name}」：{'啟用' if guild_enabled(g) else '停用'}")
    if changed:
        await apply_guild_access(changed)
    else:
        print("伺服器啟用設定沒有變動。")

async def leave_guild(guild_id):
    guild = client.get_guild(guild_id)
    if guild is None:
        print(f"找不到伺服器 {guild_id}，可能已經不在裡面了。")
        return
    await stop_music_in_guild(guild.id)
    try:
        await guild.leave()
        print(f"已離開伺服器「{guild.name}」。")
    except Exception as e:
        print(f"離開伺服器「{guild.name}」失敗：{e}")

# 斜線指令（最上層名稱）屬於哪個頻道功能，控制面板關掉那個功能時這些指令在該頻道就不能用
COMMAND_FEATURES = {
    **dict.fromkeys(("點歌", "下一首", "上一首", "重播", "暫停", "繼續播放", "播放清單", "停止播放", "音樂指令",
                     "現在播放", "循環", "隨機播放", "移除歌曲", "清空佇列"), "music"),
    **dict.fromkeys(("擲骰", "擲硬幣", "選擇", "占卜", "猜拳", "釣魚", "魚缸", "釣魚排行", "井字棋", "21點", "猜數字"), "games"),
    **dict.fromkeys(("比分", "運動新聞"), "sports"),
    "知識庫": "knowledge",
}

async def _tree_interaction_check(interaction: discord.Interaction):
    if interaction.guild_id and not guild_enabled(interaction.guild_id):
        try:
            await interaction.response.send_message(GUILD_DISABLED_TEXT, ephemeral=True)
        except Exception:
            pass
        return False
    command = interaction.command
    root = (getattr(command, "root_parent", None) or command) if command else None
    feature = COMMAND_FEATURES.get(root.name) if root else None
    if feature and not channel_feature_enabled(interaction.channel, feature):
        try:
            await interaction.response.send_message(CHANNEL_FEATURE_OFF_TEXT.format(CHANNEL_FEATURES[feature]), ephemeral=True)
        except Exception:
            pass
        return False
    return True

tree.interaction_check = _tree_interaction_check

@client.event
async def on_guild_join(guild):
    print(f"本魚被邀請進了新的伺服器「{guild.name}」（{guild.member_count} 人）"
          + ("" if guild_enabled(guild) else "，目前是停用狀態，要到控制面板的「伺服器」分頁啟用。"))
    write_guild_snapshot()
    if guild_enabled(guild):
        await sync_commands_to_guild(guild, force=True)

@client.event
async def on_guild_remove(guild):
    print(f"本魚離開了伺服器「{guild.name}」。")
    await stop_music_in_guild(guild.id)
    write_guild_snapshot()

@client.event
async def on_guild_update(before, after):
    if before.name != after.name:
        write_guild_snapshot()

# 頻道新增、刪除、改名時更新清單，控制面板的「頻道功能」分頁才會跟著變。
# 拖動頻道排序時 Discord 會一口氣送出很多事件，等 2 秒合併成一次寫檔
snapshot_timer = None

def schedule_guild_snapshot(delay=2.0):
    global snapshot_timer
    if snapshot_timer is not None:
        snapshot_timer.cancel()

    def run():
        global snapshot_timer
        snapshot_timer = None
        write_guild_snapshot()

    snapshot_timer = client.loop.call_later(delay, run)

@client.event
async def on_guild_channel_create(channel):
    schedule_guild_snapshot()

@client.event
async def on_guild_channel_delete(channel):
    schedule_guild_snapshot()

@client.event
async def on_guild_channel_update(before, after):
    if before.name != after.name or before.category_id != after.category_id or before.position != after.position:
        schedule_guild_snapshot()

@client.event
async def on_ready():
    print("====================================")
    print(f"目前登入身份：{client.user}")
    print(f"AI 模型：{ai_model_label()}" if AI_ENABLED else "AI 聊天功能：關閉中（音樂、管理功能照常）")
    print("本魚上線啦，伺服器裡 @我，私訊直接講話就行呦～")
    print("====================================")

    global commands_synced
    if not commands_synced:
        commands_synced = True
        await refresh_app_info()
        enabled = sum(1 for g in client.guilds if guild_enabled(g))
        print(f"本魚在 {len(client.guilds)} 個伺服器裡，其中 {enabled} 個啟用中。")
        await apply_guild_access(client.guilds)
        for channel_id in list(music_channel_ids):
            schedule_music_panel(channel_id, delay=3)
    write_guild_snapshot()
    start_background_tasks()

KNOWLEDGE_INSTRUCTION = (
    "以下是機器人主人放在「知識庫」裡的資料（照相關程度排序）。使用者問到跟這些資料有關的事情時，"
    "要以這些資料為準、照著資料回答，資料裡沒寫的不要自己編；資料跟問題無關的話就忽略它。"
    "回答裡不用自己標註資料來源，系統會自動加上。\n\n"
)

async def find_knowledge(message, text, history, is_dm):
    """在知識庫裡找跟這則訊息有關的段落。私訊只找共用資料，伺服器裡再加上那個伺服器專用的資料。"""
    if not KNOWLEDGE_ENABLED or not knowledge_base.has_documents() or is_quick_chat(text):
        return []
    if not is_dm and not channel_feature_enabled(message.channel, "knowledge"):
        return []
    query = text
    # 很短的追問（「那贊助者呢？」）接上一句一起找，才知道在問什麼
    if len(text) <= 12:
        last = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if last:
            query = f"{last[:150]} {text}"
    scope = str(message.guild.id) if message.guild else None
    # 線上模型能讀很長的內容，多給一點；本機模型的上下文只有 8192，給少一點
    limit, budget = (5, 5000) if using_online_ai() else (3, 1800)
    hits = await asyncio.to_thread(knowledge_base.search, query, scope, limit, budget)
    if hits:
        files = "、".join(dict.fromkeys(h["file"] for h in hits))
        print(f"[知識庫] 找到 {len(hits)} 段相關資料：{files}")
    return hits

async def handle_ai_message(message, text, is_dm, history_key):
    history = list(conversation_history[history_key])
    knowledge_hits = await find_knowledge(message, text, history, is_dm)
    decision = await asyncio.to_thread(route_message, text, history)
    action = decision["action"]
    # 知識庫裡就有答案的話不用再上網查：比較快，也不會跟網路上的資料打架
    if action == "search" and knowledge.is_strong(knowledge_hits) and not any(w in text for w in TIME_SENSITIVE_WORDS):
        print("[知識庫] 知識庫裡就有很相關的資料，這次不上網查")
        action = decision["action"] = "chat"
    print(f"[分派] {action}：{decision}")

    context_blocks = []

    if action == "youtube":
        query = decision["query"]
        try:
            video = await asyncio.to_thread(youtube_search, query)
        except Exception as e:
            print(f"YouTube 搜尋失敗：{e}")
            video = None
        if video:
            reply = f"找到啦～《{video['title']}》\n{video['url']}"
            remember(history_key, text, reply)
            await send_reply(message, reply)
            return
        context_blocks.append(f"系統剛剛用「{query}」找 YouTube 影片，但沒有找到。請老實跟使用者說沒找到，不要自己編連結。")

    elif action == "calc":
        expression = decision["expression"]
        try:
            result = await asyncio.to_thread(safe_calculate, expression)
        except Exception as e:
            context_blocks.append(
                f"系統嘗試用程式計算「{expression}」但失敗了，原因：{e}。請老實告訴使用者算不出來以及原因，不要自己亂算。"
            )
        else:
            if len(result) > 1500:
                digits = len(result.lstrip("-")) if result.lstrip("-").isdigit() else None
                size_text = f"一共有 {digits} 位數" if digits else f"一共有 {len(result)} 個字元"
                reply = (
                    f"算好啦～`{expression}` 的結果{size_text}，太長了塞不進訊息，本魚幫你裝進檔案裡囉～\n"
                    f"開頭長這樣：{result[:60]}…"
                )
                file = discord.File(io.BytesIO(result.encode("utf-8")), filename="result.txt")
                remember(history_key, text, reply)
                await send_reply(message, reply, file=file)
                return
            context_blocks.append(
                f"系統已經用程式精確算出：{expression} = {result}\n請直接用這個結果回答，不要自己重算。"
            )

    elif action == "search" and not SEARCH_ENABLED:
        print("[搜尋] 上網查資料功能關閉中，略過搜尋（控制面板「設定」可以打開）")
        context_blocks.append(
            "這個問題需要上網查資料，但本魚的上網搜尋功能目前沒有開啟。請依照你知道的回答，"
            "並明白告訴使用者你沒辦法上網查證、資訊可能過時；不確定的事實就說不確定，不要自己編。"
        )

    elif action == "search":
        query = decision["query"]
        # 小模型常把關鍵字改壞（例如「查理科克」變成「理科克」），所以同時用使用者原句再查一次，兩邊結果合併。
        # 兩個搜尋同時跑，不會多花時間。
        raw_query = text.split("\n")[0][:100].strip()
        if raw_query and raw_query != query:
            primary, secondary = await asyncio.gather(
                asyncio.to_thread(web_search_results, query),
                asyncio.to_thread(web_search_results, raw_query),
            )
        else:
            primary, secondary = await asyncio.to_thread(web_search_results, query), []
        search_context = format_search_results(merge_results(primary, secondary))
        print(f"搜尋「{query}」{len(primary)} 筆" + (f"＋原句「{raw_query}」{len(secondary)} 筆" if secondary or raw_query != query else "")
              + f"，給模型的字數：{len(search_context)}")
        if search_context:
            context_blocks.append(
                f"以下是剛剛用關鍵字「{query}」查到的網路搜尋結果。回答請以這些結果為準；"
                f"結果裡找不到答案就老實說沒查到，不要自己編：\n\n{search_context}"
            )
        else:
            context_blocks.append(
                f"系統剛剛用「{query}」上網查了，但沒有查到任何結果。請老實跟使用者說沒查到，不要憑記憶硬答。"
            )

    if not is_dm and needs_channel_log(text):
        channel_log = await fetch_channel_log(message.channel, message.id)
        if channel_log:
            context_blocks.insert(0, "以下是這個頻道最近的對話紀錄：\n\n" + channel_log)

    if knowledge_hits:
        context_blocks.insert(0, KNOWLEDGE_INSTRUCTION + knowledge.format_context(knowledge_hits))

    reply = await asyncio.to_thread(generate_reply, text, history, context_blocks)
    remember(history_key, text, reply)
    # 回答真的用到知識庫的資料才標來源（看回答跟資料有沒有大量重疊的詞）
    sources = knowledge.cited_files(reply, knowledge_hits, text)
    if sources:
        reply += "\n-# 📚 資料來源：" + "、".join(os.path.basename(f) for f in sources)
    await send_reply(message, reply)

def clean_message_text(message):
    text = message.content
    for m in message.mentions:
        replacement = "" if m.id == client.user.id else f"@{m.display_name}"
        text = re.sub(rf"<@!?{m.id}>", replacement, text)
    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")
    for channel in message.channel_mentions:
        text = text.replace(f"<#{channel.id}>", f"#{channel.name}")
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    return re.sub(r"\s+", " ", text).strip()

def is_reply_to_bot(message):
    ref = message.reference
    if ref is None:
        return False
    target = ref.resolved if isinstance(ref.resolved, discord.Message) else ref.cached_message
    return target is not None and target.author.id == client.user.id

ATTACHMENT_ONLY_REPLY = "本魚只看得懂文字，圖片、影片跟檔案都看不懂啦～有什麼想說的用打字跟本魚說嘛～"

@client.event
async def on_message(message):
    if message.guild is not None and message.channel.id in music_channel_ids and not is_music_panel_message(message):
        schedule_music_panel(message.channel.id, repost=True)
    if message.author.bot:
        return
    if message.guild is not None and not guild_enabled(message.guild):
        return

    is_dm = message.guild is None
    if not is_dm:
        if await run_automod(message):
            return
        if channel_feature_enabled(message.channel, "levels"):
            try:
                await handle_xp(message)
            except Exception as e:
                print(f"經驗值處理失敗：{e}")

    is_addressed = not is_dm and (client.user in message.mentions or is_reply_to_bot(message))
    music_allowed = channel_feature_enabled(message.channel, "music")
    in_music_channel = not is_dm and message.channel.id in music_channel_ids and music_allowed

    if (not is_dm and not is_addressed and channel_feature_enabled(message.channel, "auto_reply")
            and await handle_auto_reply(message)):
        return

    if is_dm or is_addressed:
        text = clean_message_text(message)
    elif in_music_channel:
        text = message.content.strip()
        if not text or text.startswith(MUSIC_CHANNEL_IGNORE_PREFIX):
            return
        if await handle_music_channel_setting(message, text):
            return
        if text.lower() in MENU_KEYWORDS:
            schedule_music_panel(message.channel.id, repost=True, delay=0)
            return
        if await handle_music_command(message, text):
            notify_music_changed(message.guild.id)
            return
        await handle_music_play(message, text)
        return
    else:
        return

    history_key = (message.channel.id, message.author.id)

    if not text:
        if message.attachments or message.stickers:
            await message.reply(ATTACHMENT_ONLY_REPLY, mention_author=False)
        elif is_dm:
            await message.reply("人家在這裡呦～有什麼想問本魚的嗎？", mention_author=False)
        elif in_music_channel:
            schedule_music_panel(message.channel.id, repost=True, delay=0)
        else:
            await message.reply(PANEL_TEXT, view=ControlPanel(), mention_author=False)
        return

    if not is_dm and text.lower() in MENU_KEYWORDS:
        await message.reply(PANEL_TEXT, view=ControlPanel(), mention_author=False)
        return

    if is_dm and is_invite_request(text):
        await send_invite(message)
        return

    if is_clear_memory(text):
        conversation_history.pop(history_key, None)
        await message.reply("好啦～本魚把跟你在這裡聊過的內容都忘光光了，重新開始吧～", mention_author=False)
        return

    if not is_dm:
        if await handle_music_channel_setting(message, text):
            return

        role_cmd = parse_role_command(message, text)
        if role_cmd:
            action, target_member, role, role_name = role_cmd
            await handle_role_command(message, action, target_member, role, role_name)
            return

        if music_allowed and await handle_music_command(message, text):
            notify_music_changed(message.guild.id)
            return

    if not AI_ENABLED:
        await message.reply(AI_DISABLED_REPLY, mention_author=False)
        return

    if not channel_feature_enabled(message.channel, "ai"):
        await message.reply(CHANNEL_FEATURE_OFF_TEXT.format(CHANNEL_FEATURES["ai"]), mention_author=False)
        return

    wait = check_ai_rate_limit(message.author.id)
    if wait:
        await message.reply(f"你問太快了啦～本魚喘口氣，{wait} 秒後再問嘛！", mention_author=False)
        return

    if message.attachments:
        text += "\n（使用者還附了圖片或檔案，但你看不到內容，必要時跟他說你只看得懂文字）"

    async with channel_locks[message.channel.id]:
        async with message.channel.typing():
            try:
                await handle_ai_message(message, text, is_dm, history_key)
            except Exception as e:
                await message.reply(f"嗚嗚出錯了啦：{e}", mention_author=False)

# ---------- 啟動與安全關機 ----------

main_loop = None

async def _setup_hook():
    global main_loop
    main_loop = asyncio.get_running_loop()
    client.add_view(ControlPanel())  # 讓所有舊的點歌台按鈕在重開後都還能按

client.setup_hook = _setup_hook

def flush_persistent_data():
    global levels_dirty, fishing_dirty
    if fishing_dirty:
        try:
            save_json_file(FISHING_FILE, fishing_data)
            fishing_dirty = False
            print("釣魚資料已存檔。")
        except Exception as e:
            print(f"儲存釣魚資料失敗：{e}")
    if levels_dirty:
        try:
            save_json_file(LEVELS_FILE, levels_data)
            levels_dirty = False
            print("等級資料已存檔。")
        except Exception as e:
            print(f"儲存等級資料失敗：{e}")

async def graceful_shutdown():
    print("收到關閉指令，本魚正在安全關機…")
    for state in list(music_state.values()):
        vc = state.get("voice_client")
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
    flush_persistent_data()
    await client.close()

def unload_model():
    """把模型從顯示卡卸載（keep_alive=0），釋放顯示卡記憶體。"""
    try:
        router_llm.generate(model=MODEL_NAME, prompt="", keep_alive=0)
        print(f"已把模型 {MODEL_NAME} 從顯示卡卸載。")
    except Exception as e:
        print(f"卸載模型時沒有成功（Ollama 可能本來就沒開，那就不用管）：{e}")

def preload_model():
    """先把 Ollama 叫起來、把模型載進顯示卡，這樣第一句話不用等。"""
    ensure_ollama_running()
    try:
        # 預載也要帶一樣的 num_ctx，不然第一次聊天時又會因為 num_ctx 不同整個重載
        router_llm.generate(model=MODEL_NAME, prompt="", keep_alive=OLLAMA_KEEP_ALIVE,
                            options={"num_ctx": OLLAMA_NUM_CTX})
        print(f"模型 {MODEL_NAME} 已載入，隨時可以聊天。")
    except Exception as e:
        print(f"預先載入模型失敗：{e}")

def set_ai_enabled(enabled):
    global AI_ENABLED
    if AI_ENABLED == enabled:
        print(f"AI 聊天功能本來就是{'開啟' if enabled else '關閉'}的。")
        return
    AI_ENABLED = enabled
    if enabled:
        if using_online_ai():
            print(f"AI 聊天功能已開啟，使用 {ai_model_label()}。")
        else:
            print("AI 聊天功能已開啟，正在準備模型…")
            threading.Thread(target=preload_model, name="ai-preload", daemon=True).start()
        if SEARCH_ENABLED:
            start_search_backend()
    else:
        print("AI 聊天功能已關閉，音樂和管理功能照常運作。")
        if not using_online_ai():
            threading.Thread(target=unload_model, name="ai-unload", daemon=True).start()

def reload_ai_settings():
    """控制面板「AI 來源」改了設定時呼叫，不用重新啟動機器人。"""
    was_local = not using_online_ai()
    load_ai_settings()
    print(f"AI 來源已更新：{ai_model_label()}")
    if not AI_ENABLED:
        return
    if was_local and using_online_ai():
        # 從本機換到線上，把本機模型卸載，釋放顯示卡記憶體
        threading.Thread(target=unload_model, name="ai-unload", daemon=True).start()
    elif not was_local and not using_online_ai():
        threading.Thread(target=preload_model, name="ai-preload", daemon=True).start()

MODEL_NAME_PATTERN = re.compile(r"^[\w.\-:/]{1,200}$")

def set_model(name):
    """控制面板換模型時呼叫：卸載舊模型，AI 開著的話預先載入新模型，不用重新啟動機器人。"""
    global MODEL_NAME
    old = MODEL_NAME
    if name == old:
        print(f"本來就是用 {name} 了。")
        return
    MODEL_NAME = name
    print(f"本機模型從 {old} 換成 {name}。")
    if using_online_ai():
        print("（目前用的是線上 AI，本機模型要在控制面板「AI 來源」切回本機 Ollama 才會用到）")
        return

    def work():
        try:
            router_llm.generate(model=old, prompt="", keep_alive=0)
        except Exception:
            pass
        if AI_ENABLED:
            preload_model()

    threading.Thread(target=work, name="switch-model", daemon=True).start()

def control_server():
    """單一執行鎖的那個 socket 順便拿來收控制面板的指令：安全關機、開關 AI、開關搜尋、換模型、伺服器設定。"""
    global SEARCH_ENABLED, SEARCH_ENGINE
    while True:
        try:
            conn, _ = _instance_lock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            data = conn.recv(256).strip()
            if data == b"guilds_reload" or data.startswith(b"leave:"):
                loop = main_loop
                if loop is not None and loop.is_running():
                    if data == b"guilds_reload":
                        asyncio.run_coroutine_threadsafe(reload_guild_access(), loop)
                    else:
                        raw_id = data[len(b"leave:"):].decode(errors="ignore").strip()
                        if raw_id.isdigit():
                            asyncio.run_coroutine_threadsafe(leave_guild(int(raw_id)), loop)
                    try:
                        conn.sendall(b"ok")
                    except OSError:
                        pass
            elif data == b"ai_reload":
                reload_ai_settings()
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
            elif data == b"knowledge_reload":
                reload_knowledge_settings()
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
            elif data == b"channels_reload":
                load_channel_features()
                print(f"頻道功能設定已更新（{len(channel_disabled_features)} 個頻道有關掉的功能）。")
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
            elif data.startswith(b"model:"):
                name = data[len(b"model:"):].decode("utf-8", errors="ignore").strip()
                if MODEL_NAME_PATTERN.match(name):
                    set_model(name)
                    try:
                        conn.sendall(b"ok")
                    except OSError:
                        pass
            elif data in (b"search_on", b"search_off"):
                SEARCH_ENABLED = data == b"search_on"
                SEARCH_ENGINE = load_search_engine()  # 控制面板存好設定才送這個指令，搜尋方式可能也換了
                print(f"上網查資料功能已{'開啟，使用' + SEARCH_ENGINES[SEARCH_ENGINE] if SEARCH_ENABLED else '關閉'}。")
                if SEARCH_ENABLED and AI_ENABLED:
                    start_search_backend()
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
            elif data in (b"ai_on", b"ai_off"):
                set_ai_enabled(data == b"ai_on")
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
            elif data == b"shutdown":
                try:
                    conn.sendall(b"ok")
                except OSError:
                    pass
                loop = main_loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(graceful_shutdown(), loop)
                else:
                    flush_persistent_data()
                    os._exit(0)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

prepare_music_cache()
threading.Thread(target=control_server, name="control", daemon=True).start()

if JS_RUNTIMES:
    runtime = next(iter(JS_RUNTIMES))
    print(f"YouTube 解析使用 {JS_RUNTIME_NAMES[runtime]}（{JS_RUNTIMES[runtime]['path']}）。")
else:
    print(f"找不到 Deno 或 Node.js，YouTube 點歌之後可能會放不了。再雙擊一次「{SETUP_NAME}」就會幫你裝 Deno。")

# Ollama 跟 SearXNG 在背景檢查，不要擋住登入。以前 Docker 沒開的話要等一兩分鐘本魚才上線。
# 這兩個只有 AI 聊天會用到，AI 關著就不去叫醒它們，省資源。
if AI_ENABLED:
    if using_online_ai():
        print(f"AI 聊天使用 {ai_model_label()}，不需要 Ollama。")
    else:
        print("在背景檢查 Ollama 狀態，本魚先登入...")
        threading.Thread(target=ensure_ollama_running, name="check-ollama", daemon=True).start()
    if SEARCH_ENABLED:
        start_search_backend()
    else:
        print("上網查資料功能是關閉的。")
else:
    print("AI 聊天功能是關閉的，不啟動 Ollama 和 SearXNG。要開的話到控制面板打開「AI 聊天」。")

try:
    client.run(TOKEN)
finally:
    flush_persistent_data()
