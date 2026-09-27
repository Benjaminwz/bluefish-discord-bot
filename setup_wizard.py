"""藍色大肥魚 安裝精靈。
安裝程式（Windows 的 .exe、Mac 的 .app）準備好本魚專用的 Python 之後，會打開這個視窗一步步設定：
選 AI 聊天要用哪一種、貼上 Discord Token，再下載放歌用的 ffmpeg、YouTube 用的 Deno，
選了本機 AI 的話也會裝好 Ollama 和模型。
加上 --if-needed 打開時，已經設定過的話就直接打開控制面板。"""
import importlib.machinery
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))


def load_panel():
    """共用控制面板的設定檔、配色和工具（讀寫設定、Token、金鑰、開機啟動、偵測顯示卡）。"""
    loader = importlib.machinery.SourceFileLoader("bluefish_panel", os.path.join(HERE, "panel.pyw"))
    spec = importlib.util.spec_from_loader("bluefish_panel", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


panel = load_panel()
IS_WINDOWS, IS_MAC = panel.IS_WINDOWS, panel.IS_MAC
px = panel.px
FONT = panel.FONT
GEMINI_MODEL = panel.AI_PROVIDERS["gemini"][3]

# 選項代碼：(存進設定檔的 ai_provider, 標題, 說明)
AI_CHOICES = (
    ("free", "免費線上 AI", "什麼都不用申請，最簡單。大家共用，有時要等 10～50 秒。"),
    ("gemini", "Google Gemini（推薦）", "用 Google 帳號拿一把免費金鑰，又快又穩，大約多花 1 分鐘。"),
    ("ollama", "本機 AI（Ollama）", "免費、對話不會送出去，但要好的顯示卡，會下載 3～10 GB 的模型。"),
    ("off", "先不要 AI 聊天", "管理、點歌、遊戲這些功能照樣能用，之後在控制面板隨時可以打開。"),
)


def open_panel():
    python = panel.windowed_python() if IS_WINDOWS else sys.executable
    subprocess.Popen([python, panel.PANEL_SCRIPT], cwd=HERE, creationflags=panel.CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cpu_arch():
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def ssl_context():
    """Mac 上下載用 certifi 的憑證清單，免得遇到「憑證驗證失敗」。"""
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def extract_one(archive, wanted, dest):
    """從 zip 裡找出一個檔案（wanted 判斷檔名）放到 dest。"""
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if not info.is_dir() and wanted(info.filename):
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                if not IS_WINDOWS:
                    os.chmod(dest, 0o755)
                return True
    return False


def recommended_model(vram):
    """跟首次安裝一樣，依顯示卡記憶體挑模型（Mac 是可以拿來跑模型的記憶體）。"""
    if vram and vram >= 11:
        return "qwen3:14b"
    if vram and vram >= 7:
        return "qwen3:8b"
    return "qwen3:4b"


def find_ollama():
    exe = panel.ollama_exe()
    return exe if os.path.exists(exe) else shutil.which(exe)


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        if IS_MAC:
            self.tk.call("tk", "scaling", 96 / 72)  # 跟控制面板一樣，Mac 上字不要太小
        panel.UI_SCALE = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title("藍色大肥魚 安裝精靈")
        try:
            if IS_WINDOWS and os.path.exists(panel.ICON_PATH):
                self.iconbitmap(default=panel.ICON_PATH)
            elif os.path.exists(panel.DOCK_ICON_PATH):
                self._dock_icon = tk.PhotoImage(file=panel.DOCK_ICON_PATH)
                self.iconphoto(True, self._dock_icon)
        except tk.TclError:
            pass
        w, h = px(720), px(560)
        self.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+{max(0, (self.winfo_screenheight() - h) // 2 - px(20))}")
        self.minsize(w, h)
        self.configure(bg=panel.BG)
        panel.App._setup_theme(self)  # 按鈕、卡片的樣式跟控制面板一樣
        style = ttk.Style(self)
        style.configure("Card.TRadiobutton", background=panel.CARD, font=(FONT, 10, "bold"))
        style.map("Card.TRadiobutton", background=[("active", panel.CARD)])

        self.cfg = panel.load_config()
        self.keys = panel.load_ai_keys()
        # 第一次設定時預設「免費線上 AI + 上網查資料」；設定過的（例如重新安裝）就照原本的選擇
        configured = bool(self.cfg.get("setup_done"))
        provider = self.cfg.get("ai_provider")
        if configured and self.cfg.get("ai_enabled") is False:
            provider = "off"
        self.var_ai = tk.StringVar(value=provider if provider in {c[0] for c in AI_CHOICES} else "free")
        self.var_key = tk.StringVar(value=self.keys.get("gemini", ""))
        self.var_search = tk.BooleanVar(value=self.cfg.get("search_enabled") is not False if configured else True)
        self.var_token = tk.StringVar(value=panel.read_token())
        self.var_boot = tk.BooleanVar(value=panel.boot_enabled())
        self.gpu = (None, None)
        self.busy = False
        self.install_started = False

        self._build_frame()
        self.pages = [self._page_welcome(), self._page_ai(), self._page_discord(), self._page_install(), self._page_done()]
        self.index = 0
        self._show(0)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        threading.Thread(target=self._detect_gpu, daemon=True).start()

    # ---------- 外框：上面深藍色標題列，下面按鈕列 ----------
    def _build_frame(self):
        head = tk.Frame(self, bg=panel.SIDEBAR_BG)
        head.pack(fill="x")
        self.avatar = None
        if os.path.exists(panel.AVATAR_PATH):
            try:
                self.avatar = tk.PhotoImage(file=panel.AVATAR_PATH)
                if panel.UI_SCALE < 1.4:
                    self.avatar = self.avatar.subsample(2)
            except tk.TclError:
                self.avatar = None
        if self.avatar:
            tk.Label(head, image=self.avatar, bg=panel.SIDEBAR_BG).pack(side="left", padx=px(18, 10), pady=px(12))
        titles = tk.Frame(head, bg=panel.SIDEBAR_BG)
        titles.pack(side="left", pady=px(12), padx=px(0 if self.avatar else 18, 0))
        tk.Label(titles, text="藍色大肥魚 安裝精靈", bg=panel.SIDEBAR_BG, fg="#ffffff",
                 font=(FONT, 14, "bold")).pack(anchor="w")
        self.step_label = tk.Label(titles, text="", bg=panel.SIDEBAR_BG, fg=panel.SIDEBAR_TEXT, font=(FONT, 9))
        self.step_label.pack(anchor="w")

        foot = ttk.Frame(self)
        foot.pack(side="bottom", fill="x", padx=px(20), pady=px(14))
        self.btn_cancel = ttk.Button(foot, text="取消", command=self.cancel)
        self.btn_cancel.pack(side="left")
        self.btn_next = ttk.Button(foot, text="下一步", style="Accent.TButton", command=self.next)
        self.btn_next.pack(side="right")
        self.btn_back = ttk.Button(foot, text="上一步", command=self.back)
        self.btn_back.pack(side="right", padx=px(8))

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=px(20), pady=px(16, 0))

    def _card(self, title=None):
        card = tk.Frame(self.body, bg=panel.CARD, highlightthickness=1, highlightbackground=panel.BORDER)
        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=px(18), pady=px(14))
        if title:
            ttk.Label(inner, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=px(0, 8))
        return card, inner

    def _text(self, parent, text, style="Card.TLabel", **pack):
        label = ttk.Label(parent, text=text, style=style, justify="left", wraplength=px(620))
        label.pack(anchor="w", **pack)
        return label

    # ---------- 各頁 ----------
    def _page_welcome(self):
        card, inner = self._card("歡迎！接下來幫你把本魚設定好")
        self._text(inner, "只要幾個步驟：\n"
                          "  ① 選 AI 聊天要用哪一種\n"
                          "  ② 貼上 Discord 機器人的 Token\n"
                          "  ③ 自動下載放歌需要的工具\n\n"
                          "大約 3～5 分鐘。每一項之後都能在控制面板改，不用擔心選錯。")
        return card

    def _page_ai(self):
        card, inner = self._card("① AI 聊天要用哪一種？")
        for code, title, note in AI_CHOICES:
            ttk.Radiobutton(inner, text=title, value=code, variable=self.var_ai, style="Card.TRadiobutton",
                            command=self._update_ai_page).pack(anchor="w", pady=px(4, 0))
            label = self._text(inner, note, style="CardMuted.TLabel", padx=px(26, 0))
            if code == "ollama":
                self.gpu_label = label
        self.gemini_box = ttk.Frame(inner, style="Card.TFrame")
        row = ttk.Frame(self.gemini_box, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Gemini 金鑰：", style="Card.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.var_key, show="•").pack(side="left", fill="x", expand=True, padx=px(6))
        ttk.Button(row, text="取得金鑰", style="Compact.TButton",
                   command=lambda: webbrowser.open(panel.AI_PROVIDERS["gemini"][4])).pack(side="left")
        self._text(self.gemini_box, "按「取得金鑰」用 Google 帳號登入 → 按「Create API key」→ 複製，貼在上面。",
                   style="CardMuted.TLabel", pady=px(4, 0))
        self.search_check = ttk.Checkbutton(inner, text="讓 AI 能上網查資料（問到新聞、天氣會先上網查，不用另外裝東西）",
                                            variable=self.var_search, style="Card.TCheckbutton")
        self.search_check.pack(anchor="w", pady=px(12, 0))
        self._update_ai_page()
        return card

    def _update_ai_page(self):
        choice = self.var_ai.get()
        if choice == "gemini":
            self.gemini_box.pack(fill="x", pady=px(10, 0), before=self.search_check)
        else:
            self.gemini_box.pack_forget()
        self.search_check.configure(state="disabled" if choice == "off" else "normal")

    def _detect_gpu(self):
        name, vram = panel.detect_gpu()
        self.gpu = (name, vram)
        if name:
            text = f"免費、對話不會送出去。你的電腦：{name}（約 {vram:.0f} GB），會下載 {recommended_model(vram)}。"
            try:
                self.after(0, lambda: self.gpu_label.configure(text=text))
            except (RuntimeError, tk.TclError):
                pass  # 視窗已經關了

    def _page_discord(self):
        card, inner = self._card("② 貼上 Discord 機器人的 Token")
        self._text(inner, "還沒有機器人的話，照這樣做（大約 3 分鐘）：\n"
                          "  ① 按下面的按鈕打開 Discord Developer Portal，按「New Application」取個名字\n"
                          "  ② 左邊選「Bot」，按「Reset Token」再按複製\n"
                          "  ③ 同一頁往下，把「Message Content Intent」和「Server Members Intent」打開，按「Save」\n"
                          "      （沒打開的話，本魚一啟動就會關掉！）")
        ttk.Button(inner, text="打開 Discord Developer Portal", style="Compact.TButton",
                   command=lambda: webbrowser.open("https://discord.com/developers/applications")
                   ).pack(anchor="w", pady=px(10))
        row = ttk.Frame(inner, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Token：", style="Card.TLabel").pack(side="left")
        self.token_entry = ttk.Entry(row, textvariable=self.var_token, show="•")
        self.token_entry.pack(side="left", fill="x", expand=True, padx=px(6))
        self.btn_show_token = ttk.Button(row, text="顯示", width=6, command=self._toggle_token)
        self.btn_show_token.pack(side="left")
        self._text(inner, "Token 只會存在這台電腦，不會傳給別人。還沒有的話可以先跳過，之後在控制面板的「設定」填。",
                   style="CardMuted.TLabel", pady=px(8, 0))
        return card

    def _toggle_token(self):
        hidden = self.token_entry.cget("show") == "•"
        self.token_entry.configure(show="" if hidden else "•")
        self.btn_show_token.configure(text="隱藏" if hidden else "顯示")

    def _page_install(self):
        card, inner = self._card("③ 下載需要的工具")
        self.task_box = ttk.Frame(inner, style="Card.TFrame")
        self.task_box.pack(fill="x")
        self.progress = ttk.Progressbar(inner, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=px(16, 6))
        self.status = self._text(inner, "", style="CardMuted.TLabel")
        return card

    def _page_done(self):
        card, inner = self._card("🎉 設定好了！")
        self.done_note = self._text(inner, "")
        self.invite_box = ttk.Frame(inner, style="Card.TFrame")
        self._text(self.invite_box, "最後一步：把本魚加進你的 Discord 伺服器。", pady=px(12, 6))
        row = ttk.Frame(self.invite_box, style="Card.TFrame")
        row.pack(anchor="w")
        ttk.Button(row, text="用瀏覽器打開邀請連結", style="Compact.Accent.TButton",
                   command=lambda: webbrowser.open(panel.invite_url_from_token())).pack(side="left")
        ttk.Button(row, text="複製連結", style="Compact.TButton", command=self._copy_invite).pack(side="left", padx=px(8))
        self.boot_check = ttk.Checkbutton(inner, text="電腦開機時自動讓本魚上線", variable=self.var_boot,
                                          style="Card.TCheckbutton")
        self.boot_check.pack(anchor="w", pady=px(16, 0))
        self._text(inner, "在 Discord 打 /-使用說明 可以看新手教學。", style="CardMuted.TLabel", pady=px(12, 0))
        return card

    def _copy_invite(self):
        self.clipboard_clear()
        self.clipboard_append(panel.invite_url_from_token())
        messagebox.showinfo("已複製", "邀請連結複製好了，可以貼給別人。", parent=self)

    # ---------- 換頁 ----------
    def _show(self, index):
        for page in self.pages:
            page.pack_forget()
        self.index = index
        self.pages[index].pack(fill="both", expand=True)
        names = ("歡迎", "選 AI", "Discord 機器人", "下載工具", "完成")
        self.step_label.configure(text=f"第 {index + 1} 步，共 {len(names)} 步：{names[index]}")
        self.btn_back.configure(state="normal" if 0 < index < 3 else "disabled")
        self.btn_next.configure(text="完成並打開控制面板" if index == 4 else "下一步", state="normal")
        if index == 3 and not self.install_started:
            self.install_started = True
            self.btn_next.configure(state="disabled")
            self.btn_cancel.configure(state="disabled")
            threading.Thread(target=self._install, daemon=True).start()
        if index == 4:
            has_token = bool(panel.invite_url_from_token())
            if has_token:
                self.invite_box.pack(anchor="w", fill="x", before=self.boot_check)
            self.done_note.configure(text="接下來在控制面板按「▶ 啟動」，本魚就會上線～" if has_token else
                                     "還沒填 Token：打開控制面板後，到「設定」貼上 Token，再按「▶ 啟動」。")

    def back(self):
        if self.index > 0 and not self.busy:
            self._show(self.index - 1)

    def next(self):
        if self.busy:
            return
        if self.index == 1 and not self._check_ai():
            return
        if self.index == 2 and not self._check_token():
            return
        if self.index == 4:
            self.finish()
            return
        self._show(self.index + 1)

    def cancel(self):
        if self.busy:
            return
        if self.index == 4 or messagebox.askyesno(
                "離開安裝精靈", "還沒設定完，確定要離開嗎？\n之後打開「藍色大肥魚控制面板」一樣能設定。", parent=self):
            self.destroy()

    def _check_ai(self):
        if self.var_ai.get() != "gemini":
            return True
        key = self.var_key.get().strip()
        if not key:
            if messagebox.askyesno("還沒貼金鑰", "還沒貼 Gemini 金鑰。要先改用免費線上 AI 嗎？\n"
                                   "（之後在控制面板的「AI 來源」可以再換成 Gemini）", parent=self):
                self.var_ai.set("free")
                self._update_ai_page()
                self._show(2)
            return False
        # 測試金鑰要連網，放到背景做，視窗才不會卡住
        self.busy = True
        self.btn_next.configure(text="測試金鑰中…", state="disabled")

        def worker():
            try:
                panel.test_online_ai("gemini", panel.AI_PROVIDERS["gemini"][2], key, GEMINI_MODEL)
                error = None
            except Exception as e:
                error = str(e)
            self.after(0, lambda: self._key_tested(error))

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _key_tested(self, error):
        self.busy = False
        self.btn_next.configure(text="下一步", state="normal")
        if not error:
            self._show(2)
            return
        if messagebox.askyesno("金鑰測試失敗", f"{error}\n\n要先改用免費線上 AI 嗎？（按「否」可以回去改金鑰）", parent=self):
            self.var_ai.set("free")
            self._update_ai_page()
            self._show(2)

    def _check_token(self):
        token = self.var_token.get().strip()
        if not token:
            return messagebox.askyesno("還沒貼 Token", "還沒貼 Token，要先跳過嗎？\n之後在控制面板的「設定」可以填。", parent=self)
        if token.count(".") != 2 or len(token) < 50:
            return messagebox.askyesno("Token 看起來不太對", "Token 通常是很長一串、中間有兩個「.」。\n確定是複製「Reset Token」後出現的那串嗎？\n\n按「是」照樣使用。", parent=self)
        return True

    # ---------- 安裝 ----------
    def _install(self):
        tasks = [("儲存設定", self._save_settings),
                 ("ffmpeg（放歌用）", self._install_ffmpeg),
                 ("Deno（YouTube 點歌用）", self._install_deno)]
        if self.var_ai.get() == "ollama":
            tasks += [("Ollama（本機 AI）", self._install_ollama), ("AI 模型", self._pull_model)]
        rows = {}
        done = threading.Event()

        def make_rows():
            for name, _ in tasks:
                row = ttk.Frame(self.task_box, style="Card.TFrame")
                row.pack(fill="x", pady=px(3))
                icon = ttk.Label(row, text="⏳", style="Card.TLabel", width=3)
                icon.pack(side="left")
                ttk.Label(row, text=name, style="Card.TLabel").pack(side="left")
                detail = ttk.Label(row, text="", style="CardMuted.TLabel")
                detail.pack(side="left", padx=px(10))
                rows[name] = (icon, detail)
            done.set()

        self.after(0, make_rows)
        done.wait()
        failed = False
        for name, action in tasks:
            icon, detail = rows[name]
            self._ui(lambda d=detail: d.configure(text="進行中…"))
            try:
                result = action() or "好了"
                self._ui(lambda i=icon, d=detail, r=result: (i.configure(text="✔"), d.configure(text=r)))
            except Exception as e:
                failed = True
                message = str(e)[:80]
                self._ui(lambda i=icon, d=detail, m=message: (i.configure(text="✖"), d.configure(text=m)))
            self._ui(lambda: self.progress.configure(value=0))
        final = ("有些東西沒裝好，本魚還是能用；之後再執行一次安裝程式就會補裝。" if failed
                 else "全部裝好了！按「下一步」。")
        self._ui(lambda: (self.status.configure(text=final), self.btn_next.configure(state="normal"),
                          self.btn_cancel.configure(state="normal")))

    def _ui(self, func):
        self.after(0, func)

    def _report(self, what, done, total):
        if total:
            text = f"下載{what}：{done / 1048576:.0f} / {total / 1048576:.0f} MB"
            self._ui(lambda: (self.progress.configure(value=min(done / total, 1.0)), self.status.configure(text=text)))
        else:
            self._ui(lambda: self.status.configure(text=f"下載{what}：{done / 1048576:.0f} MB"))

    def _download(self, url, dest, what):
        request = urllib.request.Request(url, headers={"User-Agent": "BlueFish-Setup"})
        with urllib.request.urlopen(request, timeout=60, context=ssl_context()) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            total = total if total > 100000 else 0  # 有些網站給的大小不對，就不顯示百分比
            done, last = 0, 0.0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    self._report(what, done, total)

    def _save_settings(self):
        choice = self.var_ai.get()
        cfg = panel.load_config()
        cfg["ai_enabled"] = choice != "off"
        if choice != "off":
            cfg["ai_provider"] = choice
        if choice == "gemini":
            cfg.setdefault("online_models", {})["gemini"] = GEMINI_MODEL
            keys = panel.load_ai_keys()
            keys["gemini"] = self.var_key.get().strip()
            panel.save_ai_keys(keys)
        cfg["search_enabled"] = choice != "off" and self.var_search.get()
        cfg.setdefault("search_engine", "builtin")
        # 安裝程式已經建好捷徑了，控制面板第一次打開時不用再建
        cfg["shortcuts_created"] = True
        panel.save_config(cfg)
        token = self.var_token.get().strip()
        if token:
            panel.write_token(token)
        return "存好了"

    def _install_ffmpeg(self):
        if panel.ffmpeg_available():
            return "已經有了"
        dest = os.path.join(HERE, "ffmpeg" + panel.EXE)
        if IS_WINDOWS:
            url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
            wanted = lambda name: name.endswith("/bin/ffmpeg.exe")
        else:
            arch = "arm64" if cpu_arch() == "arm64" else "amd64"
            url = f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release/ffmpeg.zip"
            wanted = lambda name: name.rsplit("/", 1)[-1] == "ffmpeg"
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "ffmpeg.zip")
            self._download(url, archive, " ffmpeg")
            if not extract_one(archive, wanted, dest):
                raise RuntimeError("下載的檔案裡找不到 ffmpeg")
        return "裝好了"

    def _install_deno(self):
        if panel.js_runtime_name():
            return f"已經有 {panel.js_runtime_name()} 了"
        if IS_WINDOWS:
            target = "x86_64-pc-windows-msvc"  # ARM 的 Windows 也能跑這個
        else:
            target = "aarch64-apple-darwin" if cpu_arch() == "arm64" else "x86_64-apple-darwin"
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "deno.zip")
            self._download(f"https://github.com/denoland/deno/releases/latest/download/deno-{target}.zip", archive, " Deno")
            name = "deno" + panel.EXE
            if not extract_one(archive, lambda n: n.rsplit("/", 1)[-1] == name, os.path.join(HERE, name)):
                raise RuntimeError("下載的檔案裡找不到 Deno")
        return "裝好了"

    def _install_ollama(self):
        if find_ollama():
            return "已經有了"
        with tempfile.TemporaryDirectory() as tmp:
            if IS_WINDOWS:
                setup = os.path.join(tmp, "OllamaSetup.exe")
                self._download("https://ollama.com/download/OllamaSetup.exe", setup, " Ollama（檔案很大，要等一陣子）")
                self._ui(lambda: self.status.configure(text="安裝 Ollama 中…"))
                subprocess.run([setup, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
            else:
                archive = os.path.join(tmp, "Ollama.zip")
                self._download("https://ollama.com/download/Ollama-darwin.zip", archive, " Ollama")
                apps = os.path.join(os.path.expanduser("~"), "Applications")
                os.makedirs(apps, exist_ok=True)
                # ditto 會保留 App 裡的捷徑和執行權限，Python 的 zipfile 做不到
                subprocess.run(["ditto", "-x", "-k", archive, apps], check=True)
        if not find_ollama():
            raise RuntimeError("裝好了但找不到 Ollama，重新開機後再試試")
        return "裝好了"

    def _pull_model(self):
        exe = find_ollama()
        if not exe:
            raise RuntimeError("沒有 Ollama")
        if not panel.http_alive(panel.OLLAMA_URL):
            subprocess.Popen([exe, "serve"], creationflags=panel.CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(30):
                if panel.http_alive(panel.OLLAMA_URL):
                    break
                time.sleep(1)
        model = recommended_model(self.gpu[1])
        body = json.dumps({"model": model, "stream": True}).encode("utf-8")
        request = urllib.request.Request(f"{panel.OLLAMA_URL}/api/pull", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as resp:
            for line in resp:
                data = json.loads(line or b"{}")
                if data.get("error"):
                    raise RuntimeError(data["error"])
                if data.get("total"):
                    self._report(f"模型 {model}", data.get("completed") or 0, data["total"])
        cfg = panel.load_config()
        cfg["model_name"] = model
        panel.save_config(cfg)
        return f"{model} 裝好了"

    # ---------- 完成 ----------
    def finish(self):
        cfg = panel.load_config()
        cfg["setup_done"] = True
        if self.var_boot.get():
            cfg["autostart_bot"] = True  # 開機打開控制面板後，順便讓本魚上線
        panel.save_config(cfg)
        try:
            panel.set_boot(self.var_boot.get())
        except Exception as e:
            messagebox.showwarning("開機自動啟動", f"設定開機自動啟動失敗：{e}\n可以在控制面板的「設定」再試一次。", parent=self)
        open_panel()
        self.destroy()


if __name__ == "__main__":
    if "--if-needed" in sys.argv and panel.load_config().get("setup_done"):
        open_panel()
        sys.exit(0)
    Wizard().mainloop()
