"""本魚的知識庫：把文件放進「知識庫」資料夾，AI 回答時會自動找出相關的段落參考。

機器人和控制面板共用這個檔案。搜尋用 BM25 關鍵字檢索（中文用兩個字一組切詞），
不需要額外的 AI 模型，本機 Ollama 和各種線上 AI 都能用，完全離線、速度很快。

資料夾規則：
- 檔名或資料夾名稱開頭是「_」或「.」的會被略過（可以拿來暫時停用某個檔案）。
- 資料夾名稱裡有 [伺服器ID] 的，裡面的檔案只給那個伺服器用；其他檔案所有伺服器和私訊都能用。
"""
import os
import re
import csv
import json
import math
import time
import html
import zipfile
import threading
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

GUIDE_NAME = "_使用說明.txt"
CACHE_DIR_NAME = "_快取"
CACHE_FILE_NAME = "索引快取.json"
CACHE_VERSION = 1

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".json", ".jsonl", ".yaml", ".yml", ".ini", ".cfg", ".toml",
    ".xml", ".srt", ".vtt", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".php", ".rb", ".sql",
    ".bat", ".ps1", ".sh", ".css", ".lua",
}
TABLE_EXTS = {".csv", ".tsv"}
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
SUPPORTED_EXTS = TEXT_EXTS | TABLE_EXTS | OFFICE_EXTS | {".pdf"}
SUPPORTED_TEXT = "txt、md、PDF、Word（docx）、Excel（xlsx）、PowerPoint（pptx）、csv、json、網頁、程式碼"

MAX_FILE_BYTES = 40 * 1024 * 1024       # 太大的檔案先略過，免得讀很久
MAX_FILE_CHARS = 3_000_000              # 單一檔案最多讀這麼多字
MAX_TOTAL_CHARS = 8_000_000             # 整個知識庫最多這麼多字（大約 500 MB 記憶體以內）
TOO_MUCH_TEXT = "知識庫的資料超過 800 萬字，這個檔案先略過"
CHUNK_TARGET = 520                      # 每一段大約多少字
CHUNK_MAX = 900
CHUNK_OVERLAP = 90                      # 前一段的結尾接到下一段開頭，句子被切開時也找得到
MISSING_PYPDF = "要讀 PDF 需要 pypdf 套件：到控制面板主控台按「安裝／更新套件」"

GUIDE_TEXT = """把文件放進這個資料夾，本魚回答問題時就會自動參考裡面的資料。

支援：{formats}
改了檔案不用重開機器人，大約 20 秒內會自動重新讀取。

小技巧：
- 伺服器規則、常見問題、活動公告、遊戲攻略、商品價目表……都很適合放進來。
- 用清楚的標題和段落，本魚比較容易找到正確的那一段（Markdown 的 # 標題會被當成章節）。
- 檔名或資料夾名稱開頭是「_」的會被略過，想暫時停用某個檔案，在檔名前面加「_」就好。
- 只想給某個伺服器用的資料，在控制面板「知識庫」頁按「建立伺服器專用資料夾」，
  把檔案放進那個資料夾就好（資料夾名稱裡的 [數字] 是伺服器 ID，不要改掉）。
- 在 Discord 打 /知識庫 搜尋 可以直接查資料，/知識庫 清單 可以看有哪些檔案。

注意：放在這裡的資料，本魚可能會在聊天時說出來，不要放密碼、個資這類不能公開的東西。
（這個說明檔的檔名開頭是「_」，所以不會被當成資料。）
"""

# ---------- 切詞 ----------

CJK_CLASS = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
TOKEN_RE = re.compile(rf"[{CJK_CLASS}]+|[a-z0-9]+")
CJK_RUN_RE = re.compile(rf"[{CJK_CLASS}]{{3,}}")
STOPWORDS = {
    # 問句裡常見、對找資料沒幫助的詞（兩個字一組切出來的樣子）
    "請問", "問一", "一下", "什麼", "甚麼", "怎麼", "怎樣", "如何", "為什", "為何", "哪裡", "哪個", "哪些",
    "多少", "可以", "能不", "不能", "是不", "不是", "有沒", "沒有", "這個", "那個", "一個", "就是", "還是",
    "的話", "知道", "告訴", "跟我", "給我", "幫我", "我們", "你們", "他們", "本魚", "大家", "現在", "然後",
    "因為", "所以", "但是", "而且", "或是", "或者", "如果", "的是", "是什", "麼是", "要怎", "該怎", "會不",
    "不會", "有什", "麼好", "介紹", "說明", "一些", "東西", "問題", "謝謝",
    "the", "a", "an", "is", "are", "was", "what", "how", "why", "who", "which", "of", "to", "in", "on",
    "for", "and", "or", "do", "does", "can", "you", "me", "i", "it", "this", "that", "please",
}
# 虛字：含有這些字的兩字組多半是跨詞亂切出來的（例如「器的」「則是」），權重打折
STOPCHARS = set("的了是在和與及嗎呢吧啊呀喔哦也都就還很把被給讓從對向之其而或並我你他她它們這那個麼什怎哪誰")
WEAK_WEIGHT = 0.35
MISSING_WEIGHT = 1.2   # 資料裡完全沒有的詞，也要算進分母，不然只要對到一個詞就會被當成很相關
_converters = None


def tokenize(text):
    """英文、數字用整個單字；中日韓文字用兩個字一組（只有一個字時就用那個字）。"""
    tokens = []
    for match in TOKEN_RE.finditer(text.lower()):
        piece = match.group()
        if piece[0].isascii():
            tokens.append(piece)
        elif len(piece) == 1:
            tokens.append(piece)
        else:
            tokens.extend(piece[i:i + 2] for i in range(len(piece) - 1))
    return tokens


def script_variants(text):
    """同一句話的繁體版和簡體版，資料是簡體、問題是繁體時也找得到。"""
    global _converters
    if _converters is None:
        try:
            from opencc import OpenCC
            _converters = (OpenCC("t2s"), OpenCC("s2t"))
        except Exception:
            _converters = ()
    variants = [text]
    for converter in _converters:
        try:
            converted = converter.convert(text)
        except Exception:
            continue
        if converted not in variants:
            variants.append(converted)
    return variants


# 常見的同義詞（兩個字的詞），問題用「規定」、資料寫「規則」也找得到
SYNONYM_SETS = [
    {"規則", "規定", "守則", "規範", "條款"},
    {"價格", "價錢", "費用", "售價", "收費", "定價"},
    {"獎勵", "獎品", "獎賞", "獲得", "得到", "贈品"},
    {"時間", "時候", "幾點", "日期"},
    {"成立", "創立", "建立", "創建"},
    {"開始", "開放", "舉行", "舉辦"},
    {"禁止", "不准", "不可"},
    {"申請", "報名", "登記"},
    {"聯絡", "聯繫", "連絡"},
    {"攻略", "打法", "技巧", "教學"},
    {"名字", "名稱", "叫做"},
    {"生日", "出生"},
    {"地點", "地方", "位置", "在哪"},
    {"方法", "方式", "步驟", "做法"},
]
SYNONYMS = {word: group - {word} for group in SYNONYM_SETS for word in group}


def _is_cjk_pair(token):
    return len(token) == 2 and not token[0].isascii()


def query_groups(query):
    """把問題切成「詞組」，回傳 [(詞的集合, 是否弱化)]。
    同一個位置的繁體詞、簡體詞和同義詞放在同一組，任何一個對到都算對到。
    虛字組成的詞（的、是…）和緊鄰「什麼、多少」這類問句詞的碎片（「月多」「少錢」）權重打折。"""
    variants = [tokenize(v) for v in script_variants(query)]
    base = variants[0]
    stop = [t in STOPWORDS for t in base]
    groups = []
    for i, token in enumerate(base):
        if stop[i]:
            continue
        group = {token}
        for other in variants[1:]:
            if len(other) == len(base):
                group.add(other[i])
        for word in list(group):
            for synonym in SYNONYMS.get(word, ()):
                group.update(tokenize(v)[0] for v in script_variants(synonym) if tokenize(v))
        weak = any(ch in STOPCHARS for ch in token)
        if _is_cjk_pair(token):
            if i + 1 < len(base) and stop[i + 1] and base[i + 1][0] == token[1]:
                weak = True
            if i > 0 and stop[i - 1] and base[i - 1][1:] == token[:1]:
                weak = True
        groups.append((group, weak))
    for other in variants[1:]:
        if len(other) != len(base):  # 長度對不上（很少見）就當成額外的詞
            groups.extend(({t}, False) for t in other if t not in STOPWORDS)
    if not groups:  # 整句都是問句詞，只好照原樣找
        groups = [({t}, False) for t in base]
    unique, seen = [], set()
    for group, weak in groups:
        key = frozenset(group)
        if key not in seen:
            seen.add(key)
            unique.append((group, weak))
    return unique


def query_phrases(query):
    """問題裡 3～8 個字的連續片語（例如「伺服器的規則」），大部分是有意義的詞才算。"""
    phrases = set()
    for variant in script_variants(query.lower()):
        for run in CJK_RUN_RE.findall(variant):
            for n in range(min(len(run), 8), 2, -1):
                for i in range(len(run) - n + 1):
                    sub = run[i:i + n]
                    grams = {sub[j:j + 2] for j in range(len(sub) - 1)}
                    if len(grams - STOPWORDS) * 2 > len(grams):
                        phrases.add(sub)
        phrases |= {w for w in re.findall(r"[a-z0-9]{4,}", variant) if w not in STOPWORDS}
    return phrases


# ---------- 讀檔 ----------

def decode_bytes(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "cp950", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(text):
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def read_text_file(path, ext):
    with open(path, "rb") as f:
        text = decode_bytes(f.read(MAX_FILE_CHARS * 4))
    if ext in (".html", ".htm", ".xml"):
        return html_to_text(text)
    if ext == ".json":
        try:  # 展開 \u 跳脫字元、排版，中文才讀得到
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            return text
    return text


def read_table_file(path, ext):
    with open(path, "rb") as f:
        text = decode_bytes(f.read(MAX_FILE_CHARS * 4))
    delimiter = "\t" if ext == ".tsv" else ","
    rows = [" | ".join(cell.strip() for cell in row if cell.strip())
            for row in csv.reader(text.splitlines(), delimiter=delimiter)]
    return [r for r in rows if r]


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(f"{W_NS}p"):
        parts = []
        for node in para.iter():
            if node.tag == f"{W_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{W_NS}tab":
                parts.append("\t")
            elif node.tag in (f"{W_NS}br", f"{W_NS}cr"):
                parts.append("\n")
        text = "".join(parts).strip()
        if not text:
            lines.append("")
            continue
        style = para.find(f"{W_NS}pPr/{W_NS}pStyle")
        style_name = (style.get(f"{W_NS}val") or "") if style is not None else ""
        level = re.search(r"(?:heading|標題)\s*(\d)", style_name, re.IGNORECASE)
        if level or style_name.lower() == "title":
            text = "#" * int(level.group(1) if level else 1) + " " + text
        lines.append(text)
    return "\n".join(lines)


def _slide_number(name):
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def read_pptx(path):
    parts = []
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=_slide_number)
        for name in slides:
            root = ET.fromstring(z.read(name))
            texts = []
            for para in root.iter(f"{A_NS}p"):
                line = "".join(t.text or "" for t in para.iter(f"{A_NS}t")).strip()
                if line:
                    texts.append(line)
            if texts:
                parts.append(f"# 第 {_slide_number(name)} 張投影片\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _column_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    value = 0
    for ch in letters.group() if letters else "":
        value = value * 26 + ord(ch) - 64
    return value


def read_xlsx(path):
    """回傳 [(工作表名稱, [每一列的文字])]。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S_NS}t")))
        # 工作表名稱 → 檔案位置
        sheets = []
        rel_target = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rel_target[rel.get("Id")] = "xl/" + rel.get("Target", "").lstrip("/").replace("xl/", "", 1)
        if "xl/workbook.xml" in names:
            for sheet in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{S_NS}sheet"):
                target = rel_target.get(sheet.get(f"{R_NS}id"))
                if target in names:
                    sheets.append((sheet.get("name") or target, target))
        if not sheets:
            sheets = [(n, n) for n in sorted(names) if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        result = []
        for sheet_name, target in sheets:
            rows = []
            for row in ET.fromstring(z.read(target)).iter(f"{S_NS}row"):
                cells = []
                for cell in row.iter(f"{S_NS}c"):
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(f"{S_NS}t"))
                    else:
                        v = cell.find(f"{S_NS}v")
                        value = v.text if v is not None and v.text is not None else ""
                        if kind == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        elif kind == "b":
                            value = "是" if value == "1" else "否"
                        elif re.fullmatch(r"-?\d+\.0", value):
                            value = value[:-2]
                    if value.strip():
                        cells.append((_column_index(cell.get("r")), value.strip()))
                if cells:
                    rows.append(" | ".join(v for _, v in sorted(cells)))
            if rows:
                result.append((sheet_name, rows))
        return result


def read_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DependencyMissing(MISSING_PYPDF)
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("PDF 有設密碼，讀不到")
    pages = []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            pages.append(f"# 第 {number} 頁\n{text}")
            total += len(text)
            if total > MAX_FILE_CHARS:
                break
    if not pages:
        raise ValueError("PDF 裡沒有文字（可能是掃描的圖片）")
    return "\n\n".join(pages)


class DependencyMissing(Exception):
    pass


# ---------- 切段 ----------

HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
SENTENCE_END_RE = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s")


def _split_long(text):
    """太長的段落先照句子切，還是太長就硬切。"""
    pieces, current = [], ""
    for sentence in SENTENCE_END_RE.split(text):
        if not sentence:
            continue
        if len(current) + len(sentence) > CHUNK_TARGET and current:
            pieces.append(current)
            current = ""
        current += sentence
        while len(current) > CHUNK_MAX:
            pieces.append(current[:CHUNK_TARGET])
            current = current[CHUNK_TARGET:]
    if current.strip():
        pieces.append(current)
    return pieces


def chunk_text(text):
    """回傳 [(章節標題, 內容)]。照段落累積到大約 CHUNK_TARGET 字，記住最近的 # 標題當作章節。
    同一個章節裡，上一段的結尾會接到下一段開頭，句子剛好被切開時也找得到。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")[:MAX_FILE_CHARS]
    chunks = []
    state = {"heading": "", "parts": [], "size": 0, "carry": ""}

    def flush():
        if state["parts"]:
            body = "\n\n".join(state["parts"])
            prefix = state["carry"] + "…\n" if state["carry"] else ""
            chunks.append((state["heading"], (prefix + body).strip()))
            state["carry"] = body[-CHUNK_OVERLAP:].lstrip() if len(body) > CHUNK_OVERLAP * 2 else ""
            state["parts"], state["size"] = [], 0

    def add(piece):
        piece = piece.strip()
        if not piece:
            return
        for part in (_split_long(piece) if len(piece) > CHUNK_MAX else [piece]):
            if state["size"] + len(part) > CHUNK_TARGET and state["parts"]:
                flush()
            state["parts"].append(part)
            state["size"] += len(part)

    for block in re.split(r"\n\s*\n", text):
        lines = []
        for line in block.split("\n"):
            match = HEADING_RE.match(line)
            if match:
                add("\n".join(lines))
                lines = []
                flush()
                state["carry"] = ""  # 換章節了，不接上一章的結尾
                state["heading"] = match.group(2).strip()[:80]
            else:
                lines.append(line)
        add("\n".join(lines))
    flush()
    return chunks


def chunk_rows(rows, heading=""):
    """表格：每一段都帶著第一列（欄位名稱），單獨一段也看得懂。"""
    if not rows:
        return []
    header, body = rows[0], rows[1:] or []
    chunks, current = [], []
    size = 0
    for row in body:
        if size + len(row) > CHUNK_TARGET and current:
            chunks.append((heading, f"欄位：{header}\n" + "\n".join(current)))
            current, size = [], 0
        current.append(row)
        size += len(row) + 1
    if current or not chunks:
        chunks.append((heading, f"欄位：{header}\n" + "\n".join(current)))
    return chunks


def extract_chunks(path, ext):
    if ext in TABLE_EXTS:
        return chunk_rows(read_table_file(path, ext))
    if ext == ".xlsx":
        chunks = []
        for sheet_name, rows in read_xlsx(path):
            chunks.extend(chunk_rows(rows, heading=f"工作表：{sheet_name}"))
        return chunks
    if ext == ".docx":
        return chunk_text(read_docx(path))
    if ext == ".pptx":
        return chunk_text(read_pptx(path))
    if ext == ".pdf":
        return chunk_text(read_pdf(path))
    return chunk_text(read_text_file(path, ext))


# ---------- 知識庫 ----------

SCOPE_RE = re.compile(r"\[(\d{17,20})\]")
K1, B = 1.4, 0.75


def ensure_folder(folder):
    os.makedirs(folder, exist_ok=True)
    guide = os.path.join(folder, GUIDE_NAME)
    if not os.path.exists(guide):
        with open(guide, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(GUIDE_TEXT.format(formats=SUPPORTED_TEXT))


def scope_of(rel_path):
    for part in rel_path.replace("\\", "/").split("/")[:-1]:
        match = SCOPE_RE.search(part)
        if match:
            return match.group(1)
    return None


class _Index:
    def __init__(self, chunks):
        self.chunks = chunks            # [{"file", "head", "text", "scope"}]
        self.postings = defaultdict(list)
        self.lengths = []
        for cid, chunk in enumerate(chunks):
            tokens = tokenize(f"{chunk['title']} {chunk['head']} {chunk['text']}")
            self.lengths.append(len(tokens) or 1)
            for token, count in Counter(tokens).items():
                self.postings[token].append((cid, count))
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

    def idf(self, token):
        df = len(self.postings.get(token, ()))
        if not df:
            return 0.0
        n = len(self.chunks)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))


class KnowledgeBase:
    def __init__(self, folder):
        self.folder = folder
        self.docs = {}                  # 相對路徑 → 檔案資訊與段落
        self.index = _Index([])
        self.last_refresh = 0.0
        self._lock = threading.Lock()
        self._cache = None

    # 讀取與索引

    def _cache_path(self):
        return os.path.join(self.folder, CACHE_DIR_NAME, CACHE_FILE_NAME)

    def _load_cache(self):
        if self._cache is not None:
            return self._cache
        try:
            with open(self._cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache = data.get("docs", {}) if data.get("version") == CACHE_VERSION else {}
        except Exception:
            self._cache = {}
        return self._cache

    def _save_cache(self):
        data = {"version": CACHE_VERSION, "docs": {
            rel: {k: doc[k] for k in ("size", "mtime", "chunks")}
            for rel, doc in self.docs.items() if not doc.get("error")
        }}
        path = self._cache_path()
        temp = f"{path}.{os.getpid()}.tmp"  # 機器人和控制面板可能同時寫，各用各的暫存檔
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(temp, path)
            self._cache = data["docs"]
        except OSError:
            try:
                os.remove(temp)
            except OSError:
                pass

    def _scan(self):
        found = []
        if not os.path.isdir(self.folder):
            return found
        for root, dirs, files in os.walk(self.folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")))
            for name in sorted(files):
                if name.startswith(("_", ".", "~$")):
                    continue
                ext = os.path.splitext(name)[1].lower()
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                rel = os.path.relpath(path, self.folder).replace("\\", "/")
                found.append((rel, path, ext, stat.st_size, stat.st_mtime_ns))
        return found

    def _load_doc(self, rel, path, ext, size, mtime):
        doc = {"size": size, "mtime": mtime, "ext": ext, "scope": scope_of(rel), "chunks": [], "error": None}
        if ext not in SUPPORTED_EXTS:
            doc["error"] = "不支援的格式"
            return doc
        if size > MAX_FILE_BYTES:
            doc["error"] = f"檔案太大（超過 {MAX_FILE_BYTES // 1024 // 1024} MB）"
            return doc
        cached = self._load_cache().get(rel)
        if cached and cached.get("size") == size and cached.get("mtime") == mtime:
            doc["chunks"] = [tuple(c) for c in cached.get("chunks", [])]
            return doc
        try:
            doc["chunks"] = extract_chunks(path, ext)
            if not doc["chunks"]:
                doc["error"] = "檔案是空的"
        except DependencyMissing as e:
            doc["error"] = str(e)
            doc["retry"] = True
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            doc["error"] = "檔案壞掉或格式不對，讀不到"
        except Exception as e:
            doc["error"] = f"讀不到：{e}"[:120]
        return doc

    def refresh(self):
        """重新檢查資料夾，有新增、修改、刪除的檔案才重建索引。回傳有沒有變動。"""
        with self._lock:
            found = self._scan()
            new_docs = {}
            changed = False
            for rel, path, ext, size, mtime in found:
                old = self.docs.get(rel)
                if old and old["size"] == size and old["mtime"] == mtime and not old.get("retry"):
                    new_docs[rel] = old
                    continue
                new_docs[rel] = self._load_doc(rel, path, ext, size, mtime)
                changed = True
            if set(new_docs) != set(self.docs):
                changed = True
            if changed:
                chunks = []
                total = 0
                for rel, doc in sorted(new_docs.items()):
                    title = os.path.splitext(os.path.basename(rel))[0]
                    size = sum(len(text) for _, text in doc["chunks"])
                    if total + size > MAX_TOTAL_CHARS:
                        # 資料太多會吃掉很多記憶體，超過上限的檔案先略過（照檔名順序）
                        doc["skipped"] = True
                        continue
                    doc.pop("skipped", None)
                    total += size
                    for head, text in doc["chunks"]:
                        chunks.append({"file": rel, "title": title, "head": head, "text": text, "scope": doc["scope"]})
                index = _Index(chunks)
                self.docs = new_docs
                self.index = index
                self._save_cache()
            self.last_refresh = time.time()
            return changed

    def refresh_if_stale(self, max_age):
        if time.time() - self.last_refresh > max_age:
            return self.refresh()
        return False

    # 查詢

    def has_documents(self):
        return bool(self.index.chunks)

    def files(self, scope=None, include_all_scopes=False):
        """[(相對路徑, 檔案資訊)]，給控制面板和 /知識庫 清單用。"""
        return [(rel, doc) for rel, doc in sorted(self.docs.items())
                if include_all_scopes or doc["scope"] is None or doc["scope"] == scope]

    def stats(self):
        chars = sum(len(c["text"]) for c in self.index.chunks)
        usable = sum(1 for d in self.docs.values() if d["chunks"])
        return {"files": len(self.docs), "usable": usable, "chunks": len(self.index.chunks), "chars": chars}

    def search(self, query, scope=None, limit=4, max_chars=2400, min_coverage=0.45):
        """找出跟問題最相關的段落。回傳 [{"file", "head", "text", "score", "coverage"}]，照相關程度排序。

        coverage 是問題裡的關鍵詞有多少比例出現在這一段（照詞的稀有程度加權），
        太低代表只是剛好有幾個字一樣，不算相關。"""
        index = self.index
        if not index.chunks or not query.strip():
            return []
        parsed = query_groups(query)
        groups = [group for group, _ in parsed]
        weak_flags = [weak for _, weak in parsed]
        weights, multipliers, present = [], [], []
        for group, weak in zip(groups, weak_flags):
            multiplier = WEAK_WEIGHT if weak else 1.0
            idf = max((index.idf(t) for t in group), default=0.0)
            present.append(idf > 0)
            weights.append((idf if idf > 0 else MISSING_WEIGHT) * multiplier)
            multipliers.append(multiplier)
        total_weight = sum(weights)
        if not any(present):
            return []
        scores = defaultdict(float)
        matched = defaultdict(float)
        for group, weight, multiplier, exists in zip(groups, weights, multipliers, present):
            if not exists:
                continue
            best = {}
            for token in group:
                idf = index.idf(token)
                for cid, tf in index.postings.get(token, ()):
                    norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * index.lengths[cid] / index.avg_length))
                    value = idf * norm * multiplier
                    if value > best.get(cid, 0):
                        best[cid] = value
            for cid, value in best.items():
                chunk = index.chunks[cid]
                if chunk["scope"] is not None and chunk["scope"] != scope:
                    continue
                scores[cid] += value
                matched[cid] += weight
        if not scores:
            return []
        # 問題裡連續三個字以上的片語整段出現的話，加分（「新手任務」比分開的「新手」「任務」更準）
        phrases = query_phrases(query)
        ranked = sorted(scores, key=scores.get, reverse=True)[:60]
        results = []
        for cid in ranked:
            chunk = index.chunks[cid]
            coverage = matched[cid] / total_weight
            haystack = f"{chunk['title']} {chunk['head']} {chunk['text']}".lower()
            hit_phrase = max((len(p) for p in phrases if p in haystack), default=0)
            score = scores[cid]
            if hit_phrase:
                score *= 1.0 + min(hit_phrase, 8) * 0.06
                if hit_phrase >= 4:
                    coverage = max(coverage, 0.75)
            if coverage < min_coverage:
                continue
            results.append({"file": chunk["file"], "head": chunk["head"], "text": chunk["text"],
                            "score": round(score * (0.5 + coverage), 3), "coverage": round(coverage, 2)})
        results.sort(key=lambda r: r["score"], reverse=True)
        picked, per_file, used = [], Counter(), 0
        for hit in results:
            if per_file[hit["file"]] >= 2:
                continue
            if picked and used + len(hit["text"]) > max_chars:
                continue
            picked.append(hit)
            per_file[hit["file"]] += 1
            used += len(hit["text"])
            if len(picked) >= limit:
                break
        return picked


def is_strong(hits):
    """最相關的那段幾乎涵蓋了問題的所有關鍵詞，代表知識庫裡就有答案。"""
    return bool(hits) and hits[0]["coverage"] >= 0.75


def format_context(hits):
    """整理成要給 AI 看的參考資料。"""
    blocks = []
    for i, hit in enumerate(hits, 1):
        where = hit["file"] + (f" › {hit['head']}" if hit["head"] else "")
        blocks.append(f"【資料 {i}：{where}】\n{hit['text']}")
    return "\n\n".join(blocks)


def cited_files(answer, hits, question=""):
    """看 AI 的回答有沒有真的用到這些資料，用到的才列成來源。
    只算「資料裡有、問題裡沒有」的詞，才知道是從資料學來的；資料裡的數字（價格、日期）出現在回答裡也算。"""
    answer_tokens = set(tokenize(answer)) - STOPWORDS
    question_tokens = {t for v in script_variants(question) for t in tokenize(v)}
    files = []
    for hit in hits:
        hit_tokens = {t for v in script_variants(hit["text"]) for t in tokenize(v)}
        learned = (answer_tokens & hit_tokens) - question_tokens
        has_number = any(t.isdigit() and len(t) >= 2 for t in learned)
        ratio = len(learned) / max(len(answer_tokens), 1)
        if (len(learned) >= 3 or has_number or ratio >= 0.2) and hit["file"] not in files:
            files.append(hit["file"])
    return files
