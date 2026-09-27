"""做出兩個安裝檔，放在 installer/dist：
  BlueFish-Setup-Windows-<版本>.exe  （Inno Setup 安裝精靈）
  BlueFish-Mac-<版本>.zip             （裡面是「藍色大肥魚.app」和安裝說明）
用法：python installer/build.py v1.2.3 [--test] [--skip-windows] [--skip-mac]
--test 做測試用的 Windows 安裝檔（不同的 AppId、不建捷徑），在開發的電腦上試裝不會蓋掉原本的捷徑。
要先裝 Inno Setup 6（winget install JRSoftware.InnoSetup）。只用 Python 內建的東西，不用另外裝套件。"""
import io
import os
import plistlib
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WIN = os.path.join(HERE, "windows")
DIST = os.path.join(HERE, "dist")
NAVY = (0x1A, 0x26, 0x56)
WHITE = (0xFF, 0xFF, 0xFF)
MAC_APP = "藍色大肥魚.app"
MAC_FILES = ("deepseek_discord_bot.py", "panel.pyw", "knowledge.py", "setup_wizard.py", "requirements.txt",
             "README.md", "CHANGELOG.md", "cover.png", "avatar.png", "bluefish.png", "bluefish.icns")


# ---------- 圖片（不用 Pillow：自己讀 PNG、寫 BMP） ----------
def read_png(path):
    data = open(path, "rb").read()
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    assert depth == 8 and ctype in (2, 6) and interlace == 0, path
    bpp = 3 if ctype == 2 else 4
    pos, idat = 8, b""
    while pos < len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw, stride, rows, prev, i = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp), 0
    for _ in range(h):
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    return w, h, [[tuple(r[x * bpp:x * bpp + 3]) + ((r[x * bpp + 3],) if bpp == 4 else (255,)) for x in range(w)] for r in rows]


def resize(img, nw, nh):
    """雙線性縮放（先把顏色乘上透明度，邊緣才不會有黑邊）。"""
    w, h, px = img
    pre = [[(r * a / 255, g * a / 255, b * a / 255, a) for r, g, b, a in row] for row in px]
    out = []
    for y in range(nh):
        sy = min(max((y + 0.5) * h / nh - 0.5, 0), h - 1)
        y0 = int(sy)
        y1, fy = min(y0 + 1, h - 1), sy - y0
        row = []
        for x in range(nw):
            sx = min(max((x + 0.5) * w / nw - 0.5, 0), w - 1)
            x0 = int(sx)
            x1, fx = min(x0 + 1, w - 1), sx - x0
            p = [pre[y0][x0][k] * (1 - fx) * (1 - fy) + pre[y0][x1][k] * fx * (1 - fy)
                 + pre[y1][x0][k] * (1 - fx) * fy + pre[y1][x1][k] * fx * fy for k in range(4)]
            a = p[3]
            row.append((p[0] * 255 / a, p[1] * 255 / a, p[2] * 255 / a, a) if a > 0 else (0, 0, 0, 0))
        out.append(row)
    return nw, nh, out


def canvas(w, h, bg, img, left, top):
    """把圖片貼到純色底上，回傳 RGB。"""
    rows = [[bg] * w for _ in range(h)]
    iw, ih, px = img
    for y in range(ih):
        for x in range(iw):
            cx, cy = left + x, top + y
            if 0 <= cx < w and 0 <= cy < h:
                r, g, b, a = px[y][x]
                t = a / 255
                rows[cy][cx] = tuple(round(c * t + bg[k] * (1 - t)) for k, c in enumerate((r, g, b)))
    return rows


def write_bmp(path, rows):
    h, w = len(rows), len(rows[0])
    pad = (4 - w * 3 % 4) % 4
    body = b"".join(bytes(v for r, g, b in row for v in (b, g, r)) + b"\0" * pad for row in reversed(rows))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + info + body)


def make_wizard_images():
    """安裝精靈左邊的大圖（深藍底 + 本魚封面）和右上角的小圖（大頭貼），各做一般和高解析度兩種。"""
    out = os.path.join(WIN, "build")
    os.makedirs(out, exist_ok=True)
    cover = read_png(os.path.join(ROOT, "cover.png"))
    icon = read_png(os.path.join(ROOT, "bluefish.png"))
    for scale, suffix in ((1, ""), (2, "_2x")):
        w, h = 164 * scale, 314 * scale
        cw = int(w * 0.92)
        ch = round(cover[1] * cw / cover[0])
        img = resize(cover, cw, ch)
        write_bmp(os.path.join(out, f"wizard{suffix}.bmp"), canvas(w, h, NAVY, img, (w - cw) // 2, h - ch - 6 * scale))
        s = 55 * scale
        write_bmp(os.path.join(out, f"wizard_small{suffix}.bmp"), canvas(s, s, WHITE, resize(icon, s, s), 0, 0))


# ---------- Windows ----------
def ensure_uv():
    exe = os.path.join(WIN, "uv", "uv.exe")
    if os.path.exists(exe) and time.time() - os.path.getmtime(exe) < 7 * 86400:
        return exe
    print("下載 uv…")
    url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with open(exe, "wb") as f:
            f.write(z.read(next(n for n in z.namelist() if n.endswith("uv.exe") and "uvx" not in n)))
    return exe


def find_iscc():
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        for sub in (os.path.join("Programs", "Inno Setup 6"), "Inno Setup 6"):
            path = os.path.join(base, sub, "ISCC.exe")
            if os.path.exists(path):
                return path
    sys.exit("找不到 Inno Setup 6，請先執行：winget install JRSoftware.InnoSetup")


def build_windows(version, test):
    ensure_uv()
    make_wizard_images()
    args = [find_iscc(), "/Q", f"/DAppVersion={version}"] + (["/DTESTBUILD"] if test else []) + [os.path.join(WIN, "bluefish.iss")]
    subprocess.run(args, check=True)
    return os.path.join(DIST, f"BlueFish-Setup-Windows-{version}.exe")


# ---------- Mac ----------
def zip_add(z, name, data, mode):
    info = zipfile.ZipInfo(name, time.localtime()[:6])
    info.create_system = 3  # Unix：Mac 解壓縮時才會照這裡的權限（啟動程式要能執行）
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def build_mac(version):
    plain = version.lstrip("v")
    info = {
        "CFBundleName": "藍色大肥魚",
        "CFBundleDisplayName": "藍色大肥魚",
        "CFBundleIdentifier": "com.bluefish.app",
        "CFBundleExecutable": "bluefish",
        "CFBundleIconFile": "bluefish",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": plain,
        "CFBundleVersion": plain,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    launcher = open(os.path.join(HERE, "mac", "launcher.sh"), "rb").read().replace(b"\r\n", b"\n")
    path = os.path.join(DIST, f"BlueFish-Mac-{version}.zip")
    os.makedirs(DIST, exist_ok=True)
    app = MAC_APP + "/Contents"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in (MAC_APP, app, app + "/MacOS", app + "/Resources", app + "/Resources/app"):
            zip_add(z, folder + "/", b"", 0o40755)
        zip_add(z, app + "/Info.plist", plistlib.dumps(info), 0o100644)
        zip_add(z, app + "/MacOS/bluefish", launcher, 0o100755)
        zip_add(z, app + "/Resources/bluefish.icns", open(os.path.join(ROOT, "bluefish.icns"), "rb").read(), 0o100644)
        for name in MAC_FILES:
            zip_add(z, f"{app}/Resources/app/{name}", open(os.path.join(ROOT, name), "rb").read(), 0o100644)
        zip_add(z, app + "/Resources/app/default_config.json", open(os.path.join(HERE, "default_config.json"), "rb").read(), 0o100644)
        zip_add(z, app + "/Resources/app/VERSION", plain.encode(), 0o100644)
        zip_add(z, "安裝說明.txt", open(os.path.join(HERE, "mac", "安裝說明.txt"), "rb").read(), 0o100644)
    return path


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("v"):
        sys.exit(__doc__)
    version = sys.argv[1]
    made = []
    if "--skip-windows" not in sys.argv:
        made.append(build_windows(version, "--test" in sys.argv))
    if "--skip-mac" not in sys.argv:
        made.append(build_mac(version))
    for path in made:
        print(f"{os.path.getsize(path) / 1048576:6.1f} MB  {path}")
