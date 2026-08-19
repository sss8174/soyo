#!python3.12
# -*- coding: utf-8 -*-
# ↑ダブルクリック起動時にPython3.12を使う指定。既定の3.14はTcl/Tkが壊れて
#   いるため、これが無いとGUIが無言で起動失敗する(2026-08-12調査)。
r"""
パイプライン管理.pyw ― video100パイプラインの操作GUI(素材投入〜完成まで全部)
────────────────────────────────────────────────────────
「どのフォルダに何を置き、どのファイルを消せばやり直せるか」を知らなくても、
この画面だけで最初から最後まで運用できる管理画面。

流れ:
  ①「➕ 新しい動画を作る」または番号を選んで「Aに追加」→ 素材動画を選ぶ(コピー)
  ② ▶で処理開始(別窓コンソール。一覧は自動更新)
  ③ 完成/要確認 はダブルクリックで再生。ズームがズレていたら「🎯ズーム先指定」
  ④ やり直したい工程のボタンを押す → ▶で再実行

ダブルクリックで起動(pywなのでコンソールは出ない)。
"""

import csv
import json
import queue
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk


# ドラッグ&ドロップ対応(tkinterdnd2。未導入でもボタン操作で使える)
def _ensure_tkdnd():
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        return TkinterDnD, DND_FILES
    except Exception:
        return None, None


TkinterDnD, DND_FILES = _ensure_tkdnd()
_TkBase = TkinterDnD.Tk if TkinterDnD else tk.Tk

BASE = Path(__file__).parent
STORE = Path(r"E:\video100")
WORK = STORE / "work"
FINAL = STORE / "final"
FLAGGED = FINAL / "要確認"
SAMPLE = FINAL / "sample"
REPORT = STORE / "演出チェック.html"
GALLERY = STORE / "完成ギャラリー.html"
LEDGER = FINAL / "台帳.csv"
VOICE_LONG = BASE / "VOICE_LONG"   # 連続ボイス素材(「声を選ぶ」の選択肢)
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
CREATE_NEW_CONSOLE = 0x00000010
NO_WINDOW = 0x08000000

# 工程の並び(進み具合の判定に使う)。数字は概ねの進捗%(処理時間の配分ベース)
# ※旧順(04_sound/05_upslide)の名残も判定できるよう残してある
STAGES = [("01_joined.mp4", "結合", 20),
          ("02_mosaic.mp4", "モザイク", 45),
          ("03_loop.mp4", "ループ延長", 50),
          ("04_sound.mp4", "音当て(旧)", 65),
          ("05_upslide.mp4", "カメラ演出(旧)", 90),
          ("04_camera.mp4", "カメラ演出", 80),
          ("05_sound.mp4", "音当て", 92),
          ("08_full.mp4", "B/C連結", 95)]

# やり直しレベル → 消す中間ファイル(旧名も含めて消す)
REDO = {
    "最初から": ["01_joined.mp4", "01_sec*.mp4", "02_mosaic.mp4",
                 "03_loop.mp4", "04_camera.mp4", "05_sound.mp4",
                 "05_plain.mp4", "04_sound.mp4", "05_upslide.mp4",
                 "06_bmosaic.mp4", "06_cmosaic.mp4", "07_bpart.mp4",
                 "08_full.mp4"],
    "モザイクから": ["02_mosaic.mp4", "03_loop.mp4", "04_camera.mp4",
                     "05_sound.mp4", "05_plain.mp4",
                     "04_sound.mp4", "05_upslide.mp4",
                     "06_bmosaic.mp4", "06_cmosaic.mp4",
                     "07_bpart.mp4", "08_full.mp4"],
    "カメラ演出から": ["04_camera.mp4", "05_sound.mp4", "05_plain.mp4",
                       "05_upslide.mp4", "08_full.mp4"],
    "音当てから(音だけ数十秒)": ["05_sound.mp4", "05_plain.mp4",
                                 "04_sound.mp4", "08_full.mp4"],
    "B/Cパートから": ["06_bmosaic.mp4", "06_cmosaic.mp4", "07_bpart.mp4",
                      "08_full.mp4"],
}


# ── 状態スキャン ─────────────────────────────────────────
def count_clips(d, depth=1):
    """フォルダ内の動画数(セクション用にサブフォルダ1段まで)。"""
    if not d.is_dir():
        return 0
    n = 0
    for f in d.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            n += 1
        elif f.is_dir() and depth > 0:
            n += count_clips(f, depth - 1)
    return n


def a_count(folder):
    for name in ("A", "clips"):
        if (folder / name).is_dir():
            return count_clips(folder / name)
    return len([f for f in folder.iterdir() if f.is_file()
                and f.suffix.lower() in VIDEO_EXTS
                and not f.name[0:1].isdigit()])


def final_video(name):
    """完成品(またはNG品)のパスを返す。無ければNone。"""
    for p in (FINAL / f"{name}.mp4", FLAGGED / f"{name}.mp4"):
        if p.is_file():
            return p
    return None


def scan_one(folder):
    name = folder.name
    a = a_count(folder)
    b = count_clips(folder / "B")
    c = count_clips(folder / "C")
    pct, prog = 0, "未処理"
    for fname, label, p in STAGES:
        if (folder / fname).is_file():
            pct, prog = p, f"{label}済み"
    state = ""
    if (FINAL / f"{name}.mp4").is_file():
        state = "✅ 完成"
        pct, prog = 100, "完了"
    elif (FLAGGED / f"{name}.mp4").is_file():
        state = "⚠ 要確認"
        pct, prog = 100, "完了"
    elif (folder / "camera_preview" / "plan.json").is_file() \
            and not (folder / "04_camera.mp4").is_file():
        state = "🕐 演出待ち"
        pct = max(pct, 70)
    note = []
    if (folder / "camera.txt").is_file():
        note.append("camera.txt")
    try:
        v = (folder / "voice.txt").read_text(encoding="utf-8").strip()
        if v and v.lower() not in ("random", "ランダム"):
            note.append(f"声:{Path(v).stem[:14]}")
    except Exception:
        pass
    try:
        plan = json.loads((folder / "camera_preview" / "plan.json")
                          .read_text(encoding="utf-8"))
        if plan.get("low_confidence"):
            note.append("ズーム低信頼")
    except Exception:
        pass
    memo = ""
    try:
        memo = (folder / "メモ.txt").read_text(encoding="utf-8").strip()
        memo = memo.splitlines()[0] if memo else ""
    except Exception:
        pass
    return {"name": name, "a": a, "b": b, "c": c, "pct": pct,
            "prog": prog, "state": state, "note": "・".join(note),
            "memo": memo}


def redo_targets(folder, level):
    """やり直しで消すファイル一覧(実在する物だけ)を返す。"""
    name = folder.name
    files = []
    for pat in REDO[level]:
        files += sorted(folder.glob(pat))
    for p in (FINAL / f"{name}.mp4", FLAGGED / f"{name}.mp4",
              FINAL / f"{name}_演出前.mp4", FLAGGED / f"{name}_演出前.mp4",
              SAMPLE / f"{name}_sample.mp4", SAMPLE / f"{name}_thumb.jpg"):
        if p.is_file():
            files.append(p)
    dirs = [folder / "check"]
    if level == "最初から":
        dirs.append(folder / "camera_preview")
    return files, [d for d in dirs if d.is_dir()]


def read_ledger():
    """台帳.csvを読んで {番号: 最新行dict} を返す(要確認の理由表示・見積り用)。"""
    info = {}
    if not LEDGER.is_file():
        return info
    try:
        with open(LEDGER, encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                name = row.get("番号")
                if not name:
                    continue
                prev = info.get(name)
                if prev:   # 新しい行の空欄は過去の行から補完(合格追記行など)
                    for k, v in row.items():
                        if v in (None, ""):
                            row[k] = prev.get(k, "")
                info[name] = row
    except Exception:
        pass
    return info


def flag_reason(row):
    """台帳の1行から要確認の理由文字列を作る。"""
    if not row:
        return ""
    parts = []
    try:
        if int(row.get("要確認枚数") or 0) > 0:
            parts.append(f"モザイク疑い{row['要確認枚数']}枚")
    except ValueError:
        pass
    if row.get("品質警告"):
        parts.append(row["品質警告"])
    return "・".join(parts)


def append_ledger_row(name, verdict, note=""):
    """GUI操作(合格など)を台帳に1行追記する。"""
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        new = not LEDGER.is_file()
        with open(LEDGER, "a", encoding="utf-8-sig" if new else "utf-8",
                  newline="") as fp:
            w = csv.writer(fp)
            if new:
                w.writerow(["番号", "処理日時", "尺(秒)", "判定", "要確認枚数",
                            "モザイク検出", "打点数", "SE", "声", "B_SE",
                            "B_声", "B_呼吸", "品質警告", "処理分"])
            w.writerow([name, datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "", verdict, "", "", "", "", "", "", "", "",
                        note, ""])
    except Exception:
        pass


def approve(name):
    r"""要確認の動画を目視合格として final\ 直下へ昇格する。"""
    moved = []
    for suffix in (".mp4", "_演出前.mp4"):
        src = FLAGGED / f"{name}{suffix}"
        if src.is_file():
            dst = FINAL / f"{name}{suffix}"
            dst.unlink(missing_ok=True)
            shutil.move(src, dst)
            moved.append(dst.name)
    if moved:
        append_ledger_row(name, "合格(目視)", "要確認から手動で合格判定")
    return moved


def estimate_minutes():
    """台帳の実績(直近20本の処理分)から1本あたりの平均分を出す。"""
    if not LEDGER.is_file():
        return 0.0
    mins = []
    try:
        with open(LEDGER, encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                try:
                    v = float(row.get("処理分") or 0)
                    if v > 0:
                        mins.append(v)
                except ValueError:
                    pass
    except Exception:
        return 0.0
    # 途中で止まっていた実行の記録(数時間)は見積りを壊すため除外し、
    # 残りの中央値を使う(平均だと外れ値に引きずられる)
    mins = mins[-20:]
    valid = sorted(v for v in mins if v <= 120) or sorted(mins)
    return valid[len(valid) // 2] if valid else 0.0


def build_gallery():
    """final全体のサムネ一覧HTMLを生成してパスを返す(無ければNone)。"""
    ledger = read_ledger()
    items = []
    for d, badge, cls in ((FINAL, "完成", "ok"), (FLAGGED, "要確認", "ng")):
        if not d.is_dir():
            continue
        for v in sorted(d.glob("*.mp4")):
            if v.name.endswith("_演出前.mp4"):
                continue
            name = v.stem
            thumb = SAMPLE / f"{name}_thumb.jpg"
            rel_v = v.relative_to(STORE).as_posix()
            rel_t = thumb.relative_to(STORE).as_posix() if thumb.is_file() \
                else ""
            row = ledger.get(name) or {}
            meta = " / ".join(x for x in (
                f"{row.get('尺(秒)', '')}秒" if row.get("尺(秒)") else "",
                row.get("SE", ""), flag_reason(row)) if x)
            img = (f'<img src="{rel_t}" loading="lazy">' if rel_t
                   else '<div class="noimg">サムネなし</div>')
            items.append(
                f'<a class="card" href="{rel_v}"><div class="th">{img}</div>'
                f'<div class="t">{name} <span class="b {cls}">{badge}</span>'
                f'</div><div class="m">{meta}</div></a>')
    if not items:
        return None
    GALLERY.write_text(f"""<!doctype html><html lang="ja"><head>
<meta charset="utf-8"><title>完成ギャラリー</title><style>
body{{background:#1e1e2e;color:#cdd6f4;font-family:'Yu Gothic UI',sans-serif;
     margin:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
      gap:14px}}
.card{{background:#28283c;border-radius:10px;padding:10px;color:#cdd6f4;
      text-decoration:none;display:block}}
.card:hover{{background:#32324a}}
.th img{{width:100%;border-radius:6px}} .noimg{{padding:40px 0;
      text-align:center;color:#6c7086;background:#1e1e2e;border-radius:6px}}
.t{{margin-top:6px;font-weight:bold}} .m{{color:#a6adc8;font-size:12px;
      margin-top:2px}}
.b{{font-size:11px;padding:1px 8px;border-radius:9px;font-weight:normal}}
.b.ok{{background:#2e4d3a}} .b.ng{{background:#5d3040}}
</style></head><body>
<h1>完成ギャラリー <small style="font-size:14px">({len(items)}本
 — カードクリックで再生)</small></h1>
<div class="grid">{"".join(items)}</div></body></html>""",
                       encoding="utf-8")
    return GALLERY


def next_empty_number():
    """素材も成果物も無い最初の番号フォルダ。無ければ新規に採番して作る。"""
    nums = []
    for f in sorted(WORK.iterdir()) if WORK.is_dir() else []:
        if f.is_dir() and f.name.isdigit():
            nums.append(f.name)
            s = scan_one(f)
            if not (s["a"] or s["b"] or s["c"]) and s["prog"] == "未処理" \
                    and not s["state"]:
                return f
    new = WORK / f"{(max(map(int, nums)) + 1) if nums else 1:03d}"
    new.mkdir(parents=True, exist_ok=True)
    return new


# パイプラインの中間生成物(直下置き素材のクリア時に消さないための除外リスト)
INTERMEDIATE = {"01_joined.mp4", "02_mosaic.mp4", "03_loop.mp4",
                "04_sound.mp4", "05_upslide.mp4", "06_bmosaic.mp4",
                "06_cmosaic.mp4", "07_bpart.mp4", "08_full.mp4"}


def part_files(folder, part):
    r"""指定パート(A/B/C)の素材ファイル一覧。Aは A\→clips\→直下 の順で探す。"""
    if part == "A":
        for name in ("A", "clips"):
            d = folder / name
            if d.is_dir():
                return sorted([f for f in d.rglob("*") if f.is_file()
                               and f.suffix.lower() in VIDEO_EXTS])
        return sorted([f for f in folder.iterdir() if f.is_file()
                       and f.suffix.lower() in VIDEO_EXTS
                       and f.name not in INTERMEDIATE
                       and not f.name.startswith("01_sec")
                       and not f.name.endswith(".part.mp4")])
    d = folder / part
    if not d.is_dir():
        return []
    return sorted([f for f in d.rglob("*") if f.is_file()
                   and f.suffix.lower() in VIDEO_EXTS])


def clear_parts(folder, parts):
    """指定パートの素材を削除して削除数を返す(フォルダ自体は残す)。"""
    n = 0
    for part in parts:
        for f in part_files(folder, part):
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
    return n


def add_clips(folder, part, paths, progress=None, cancel=None):
    r"""動画を work\{番号}\{A|B|C}\ にコピーして追加。追加した数を返す。
    progress(ファイル名, 済み本数) を渡すと1本コピーするごとに呼ぶ。
    cancel(threading.Event) がセットされたらそこで打ち切る。
    ※GUIからは別スレッドで呼ぶため、この中では tkinter に触らないこと。"""
    dst = folder / part
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in paths:
        if cancel is not None and cancel.is_set():
            break
        p = Path(p)
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        target = dst / p.name
        i = 2
        while target.exists():
            target = dst / f"{p.stem}_{i}{p.suffix}"
            i += 1
        shutil.copy(p, target)
        n += 1
        if progress is not None:
            progress(p.name, n)
    return n


# ── エラーの見える化 ────────────────────────────────
# pythonw.exe で起動しているためコンソールが無く、例外が起きても何も出ずに
# 終了してしまう。原因が分かるようにログとダイアログの両方に出す。
ERROR_LOG = BASE / "起動エラー.txt"


def report_error(text, title="パイプライン管理のエラー"):
    """例外の内容をログに書き、ダイアログで知らせる(失敗しても落ちない)。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    where = ""
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as fp:
            fp.write(f"===== {stamp} =====\n{text}\n")
        where = f"\n\n詳しい内容: {ERROR_LOG}"
    except Exception:
        pass
    try:    # Windowsの素のダイアログ(tkinterが壊れていても出せる)
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, text[-1500:] + where, title, 0x10)
    except Exception:
        pass


# ── モーダル表示中の目印(自動更新を止める判定に使う) ──────
# ダイアログ表示中は tkinter が入れ子のイベントループに入るため、その裏で
# 一覧の作り直しが走ると操作が固まったように見える。表示中はこの数が増える。
_MODAL = 0


def _modal(func, *a, **kw):
    global _MODAL
    _MODAL += 1
    try:
        return func(*a, **kw)
    finally:
        _MODAL -= 1


def show_info(parent, title, msg):
    return _modal(messagebox.showinfo, title, msg, parent=parent)


def ask_yesno(parent, title, msg):
    return _modal(messagebox.askyesno, title, msg, parent=parent)


def ask_string(parent, title, prompt, initialvalue=""):
    return _modal(simpledialog.askstring, title, prompt,
                  initialvalue=initialvalue, parent=parent)


def ask_videos(parent, title):
    pat = " ".join("*" + e for e in sorted(VIDEO_EXTS))
    return _modal(filedialog.askopenfilenames, title=title, parent=parent,
                  filetypes=[("動画", pat), ("すべてのファイル", "*.*")])


def scan_all():
    """一覧に必要な情報をまとめて集める(ディスク走査だけ)。
    別スレッドから呼ぶので、この中では tkinter に触らないこと。"""
    folders = sorted([f for f in WORK.iterdir() if f.is_dir()],
                     key=lambda p: p.name.lower()) if WORK.is_dir() else []
    return {"exists": WORK.is_dir(),
            "rows": [scan_one(f) for f in folders],
            "ledger": read_ledger(),
            "avg": estimate_minutes()}


# ── GUI ──────────────────────────────────────────────
class App(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("video100 パイプライン管理")
        self.geometry("920x700")
        # 固まり・多重ウィンドウ防止用の状態
        self._busy = False          # 重い処理(素材コピー)の実行中か
        self._busy_what = ""
        self._dialogs = {}          # 種類ごとに1つだけ開くダイアログ
        self._settings_proc = None  # 設定ウィンドウ(二重起動させない)
        self._pipe_proc = None      # パイプライン(二重起動を確認する)
        self._scanning = False      # 一覧スキャン中(別スレッド)
        self._scan_again = False    # スキャン中に来た更新要求
        self._select_after = None   # スキャン後に選択する番号
        self._status_seen = ""      # 走査開始時のメッセージ
        self._build()
        self._setup_dnd()
        self.refresh()
        self._auto()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="➕ 新しい動画を作る",
                   command=self.new_video).pack(side="left")
        ttk.Button(top, text="▶ 選択を処理",
                   command=lambda: self.run_pipeline(selected=True)
                   ).pack(side="left", padx=(12, 2))
        ttk.Button(top, text="▶ 全部処理",
                   command=lambda: self.run_pipeline(selected=False)
                   ).pack(side="left", padx=2)
        ttk.Button(top, text="🌙 夜バッチ(演出前まで)",
                   command=lambda: self.run_pipeline(selected=False, wait=True)
                   ).pack(side="left", padx=2)
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自動更新", variable=self.auto_var
                        ).pack(side="right", padx=4)
        self.filter_var = tk.StringVar(value="全部")
        fil = ttk.Combobox(top, textvariable=self.filter_var, width=8,
                           state="readonly",
                           values=["全部", "要確認", "完成", "処理中",
                                   "未処理", "素材入り"])
        fil.pack(side="right", padx=4)
        fil.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(top, text="表示:").pack(side="right")
        ttk.Button(top, text="🔄 更新", command=self.refresh
                   ).pack(side="right")
        ttk.Button(top, text="⚙ 設定", command=self.open_settings
                   ).pack(side="right", padx=4)

        cols = ("番号", "A素材", "B", "C", "進み具合", "状態", "時間",
                "備考", "メモ")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 selectmode="extended")
        widths = (60, 50, 35, 35, 165, 78, 55, 130, 120)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<Delete>", lambda e: self.clear_dialog())
        self.tree.bind("<F2>", lambda e: self.edit_memo())

        add = ttk.LabelFrame(
            self, text="素材の追加(元ファイルはコピーされて残る) — "
                       "一覧の行へ動画をドロップ=そのAへ / 空きへドロップ=新規番号",
            padding=6)
        add.pack(fill="x", padx=6, pady=2)
        ttk.Button(add, text="Aに動画を追加(本編クリップ)",
                   command=lambda: self.add_to("A")).pack(side="left", padx=3)
        ttk.Button(add, text="Bに追加(フィニッシュ)",
                   command=lambda: self.add_to("B")).pack(side="left", padx=3)
        ttk.Button(add, text="Cに追加(その後)",
                   command=lambda: self.add_to("C")).pack(side="left", padx=3)
        # B/C用ドロップ枠(選択中の番号に入る)
        self.drop_zones = {}
        for part, label in (("B", "B⬇ここにドロップ"), ("C", "C⬇ここにドロップ")):
            z = tk.Label(add, text=label, relief="groove", padx=10, pady=4,
                         bg="#e8edf5")
            z.pack(side="left", padx=4)
            self.drop_zones[part] = z
        ttk.Button(add, text="🗑 素材をクリア",
                   command=self.clear_dialog).pack(side="left", padx=(14, 3))
        ttk.Button(add, text="🎯 ズーム先を指定(camera.txt)",
                   command=self.set_camera).pack(side="left", padx=(14, 3))
        ttk.Button(add, text="🖼 ズームプレビューを開く",
                   command=self.open_preview).pack(side="left", padx=3)

        mid = ttk.LabelFrame(self, text="選択した番号をやり直す(必要なファイルを自動削除)",
                             padding=6)
        mid.pack(fill="x", padx=6, pady=2)
        for level in REDO:
            ttk.Button(mid, text=level,
                       command=lambda lv=level: self.redo(lv)
                       ).pack(side="left", padx=3)

        chk = ttk.LabelFrame(self, text="要確認の処理", padding=6)
        chk.pack(fill="x", padx=6, pady=2)
        ttk.Button(chk, text="✔ 合格にする(要確認→完成)",
                   command=self.approve_selected).pack(side="left", padx=3)
        ttk.Button(chk, text="📋 チェックレポートを開く",
                   command=self.open_check_report).pack(side="left", padx=3)
        ttk.Button(chk, text="🎤 声を選ぶ(ランダム/指定)",
                   command=self.choose_voice).pack(side="left", padx=(18, 3))
        ttk.Button(chk, text="📝 メモ (F2)",
                   command=self.edit_memo).pack(side="left", padx=3)

        low = ttk.Frame(self, padding=6)
        low.pack(fill="x")
        ttk.Button(low, text="▶ 再生(完成品)",
                   command=self.play_selected).pack(side="left", padx=2)
        ttk.Button(low, text="🖼 完成ギャラリー",
                   command=self.open_gallery).pack(side="left", padx=2)
        ttk.Button(low, text="📂 workを開く",
                   command=lambda: subprocess.Popen(["explorer", str(WORK)])
                   ).pack(side="left", padx=2)
        ttk.Button(low, text="📂 finalを開く",
                   command=lambda: subprocess.Popen(["explorer", str(FINAL)])
                   ).pack(side="left", padx=2)
        ttk.Button(low, text="🎯 演出チェックを開く",
                   command=self.open_report).pack(side="left", padx=2)
        ttk.Button(low, text="📂 選択フォルダを開く",
                   command=self.open_selected).pack(side="left", padx=2)
        self.status = tk.StringVar(value="")
        ttk.Label(low, textvariable=self.status).pack(side="right")

    # ── 固まり・多重ウィンドウ防止の共通部品 ─────────────
    # 2026-08-19: 素材を登録するとウィンドウがいくつも開いて固まる問題の対策。
    #   ・重いコピーは別スレッド(メインループを止めない)
    #   ・同じダイアログは1つだけ / 処理中は新しい操作を受け付けない
    #   ・まとめて開く操作(再生・フォルダ)は件数を確認してから

    def report_callback_exception(self, exc, val, tb):
        """ボタン操作などで起きた例外を握り潰さずに知らせる(pythonw対策)。"""
        text = "".join(traceback.format_exception(exc, val, tb))
        report_error(text, "操作中にエラーが起きました")
        try:
            self.status.set("エラーが起きました(起動エラー.txt を参照)")
        except Exception:
            pass

    def _guard(self):
        """コピー中・ダイアログ表示中なら False(その操作は受け付けない)。"""
        if self._busy:
            self.bell()
            self.status.set(f"{self._busy_what}の途中です。"
                            "終わってから操作してください")
            return False
        if _MODAL:              # 詰まったクリックが後からまとめて届いた場合
            self.bell()
            return False
        return True

    def _single(self, key, title):
        """同じ種類のダイアログを1つだけ開く。
        戻り値は (Toplevel, 閉じる関数)。既に開いていれば前面に出して
        (None, None) を返す。"""
        old = self._dialogs.get(key)
        if old is not None and old.winfo_exists():
            old.deiconify()
            old.lift()
            old.focus_force()
            return None, None
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)

        def close():
            self._dialogs.pop(key, None)
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.bind("<Escape>", lambda e: close())
        dlg.grab_set()
        self._dialogs[key] = dlg
        return dlg, close

    def _confirm_many(self, names, what, limit=3):
        """複数選択のまま「開く」を押したとき、窓が一度に開くのを防ぐ。"""
        if len(names) <= 1:
            return list(names)
        if not ask_yesno(self, "まとめて開く",
                         f"{len(names)}件選ばれています。\n"
                         f"先頭{min(len(names), limit)}件の{what}を"
                         "同時に開きます。よろしいですか?"):
            return []
        return list(names[:limit])

    def open_settings(self):
        """設定ウィンドウ(二重に開かない)。"""
        if self._settings_proc is not None \
                and self._settings_proc.poll() is None:
            self.bell()
            self.status.set("設定ウィンドウは既に開いています")
            return
        exe = Path(sys.executable)
        if exe.name.lower() == "python.exe":
            exe = exe.with_name("pythonw.exe")
        try:
            self._settings_proc = subprocess.Popen(
                [str(exe), str(BASE / "パイプライン設定.pyw")], cwd=str(BASE))
        except Exception as exc:
            show_info(self, "設定を開けません", str(exc))

    # ── 素材コピー(別スレッド) ────────────────────────────
    def _copy_async(self, jobs, done_msg="", select=None):
        """[(フォルダ, パート, ファイル一覧), …] を別スレッドでコピーする。
        コピー中も画面は動き、進捗ウィンドウから中止もできる。
        done_msg の {n} はコピーした本数に置き換わる。"""
        jobs = [(f, p, list(paths)) for f, p, paths in jobs if paths]
        if not jobs:
            return
        total = sum(len(paths) for _, _, paths in jobs)
        self._busy = True
        self._busy_what = "素材のコピー"
        cancel = threading.Event()
        q = queue.Queue()
        win, label, bar = self._progress_window(total, cancel)
        st = {"n": 0, "total": total, "err": ""}

        def work():
            copied = 0
            try:
                for folder, part, paths in jobs:
                    copied += add_clips(
                        folder, part, paths, cancel=cancel,
                        progress=lambda name, i, pt=part:
                            q.put(("p", f"{pt}: {name}")))
            except Exception as exc:    # 容量不足・権限・切断など
                q.put(("err", str(exc)))
            q.put(("done", copied))

        threading.Thread(target=work, daemon=True).start()
        self._poll_copy(q, win, label, bar, cancel, done_msg, select, st)

    def _progress_window(self, total, cancel):
        win = tk.Toplevel(self)
        win.title("素材をコピー中")
        win.transient(self)
        win.resizable(False, False)
        label = tk.StringVar(value=f"0 / {total} 本")
        ttk.Label(win, textvariable=label, padding=(14, 12, 14, 4),
                  width=48, anchor="w").pack(fill="x")
        bar = ttk.Progressbar(win, mode="determinate", length=380,
                              maximum=max(total, 1))
        bar.pack(padx=14, pady=4)
        ttk.Button(win, text="中止", command=cancel.set).pack(pady=(2, 12))
        win.protocol("WM_DELETE_WINDOW", cancel.set)   # ×でも中止扱い
        return win, label, bar

    def _poll_copy(self, q, win, label, bar, cancel, done_msg, select, st):
        done = None
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "p":
                    st["n"] += 1
                    bar["value"] = st["n"]
                    label.set(f"{st['n']} / {st['total']} 本  {payload}")
                elif kind == "err":
                    st["err"] = payload      # 完了通知は次の回に来ることがある
                else:
                    done = payload
        except queue.Empty:
            pass
        if done is None:
            self.after(120, lambda: self._poll_copy(
                q, win, label, bar, cancel, done_msg, select, st))
            return
        win.destroy()
        self._busy = False
        self._busy_what = ""
        err = st["err"]
        self.refresh(select=select)
        if err:
            self.status.set(f"コピーに失敗しました({done}本コピー済み)")
            show_info(self, "コピーできませんでした",
                      f"{done}本コピーしたところでエラーになりました。\n{err}")
        elif cancel.is_set():
            self.status.set(f"中止しました({done}本コピー済み)")
        else:
            self.status.set(done_msg.format(n=done) if done_msg
                            else f"{done}本追加しました")

    # ── ドラッグ&ドロップ ─────────────────────────────────
    def _setup_dnd(self):
        if not TkinterDnD:
            return
        self.tree.drop_target_register(DND_FILES)
        self.tree.dnd_bind("<<Drop>>", self._on_drop_tree)
        for part, zone in self.drop_zones.items():
            zone.drop_target_register(DND_FILES)
            zone.dnd_bind("<<Drop>>",
                          lambda e, p=part: self._on_drop_zone(p, e))

    def _drop_paths(self, data):
        """ドロップイベントのdataを動画ファイル一覧に展開する。
        フォルダは中の動画(名前順)に展開。"""
        out = []
        for raw in self.tk.splitlist(data):
            p = Path(raw)
            if p.is_dir():
                out += sorted([f for f in p.iterdir() if f.is_file()
                               and f.suffix.lower() in VIDEO_EXTS],
                              key=lambda x: x.name.lower())
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                out.append(p)
        return out

    def _on_drop_tree(self, event):
        # ドロップのコールバックの中でコピーやダイアログを行うと、送り元
        # (エクスプローラー)がドロップ完了を待ち続けて両方固まる。
        # ここではデータを控えて即座に返し、実処理はイベント処理後に回す。
        data = event.data
        y = event.y_root - self.tree.winfo_rooty()
        self.after_idle(lambda: self._drop_tree_later(data, y))
        return getattr(event, "action", "copy")

    def _drop_tree_later(self, data, y):
        if not self._guard():
            return
        paths = self._drop_paths(data)
        if not paths:
            self.status.set("動画ファイルが見つかりませんでした")
            return
        row = self.tree.identify_row(y)
        if row and self.tree.exists(row):
            folder = WORK / row
            msg = f"{row}\\A"
        else:
            folder = next_empty_number()
            msg = f"新規 {folder.name}\\A"
        self._copy_async([(folder, "A", paths)], select=folder.name,
                         done_msg=f"{msg} に {{n}}本追加しました(ドロップ)")

    def _on_drop_zone(self, part, event):
        data = event.data
        self.after_idle(lambda: self._drop_zone_later(part, data))
        return getattr(event, "action", "copy")

    def _drop_zone_later(self, part, data):
        if not self._guard():
            return
        paths = self._drop_paths(data)
        if not paths:
            self.status.set("動画ファイルが見つかりませんでした")
            return
        names = self._selected(quiet=True)
        if len(names) != 1:
            # ドロップ処理を抜けた後なので、ここならダイアログを出しても安全
            show_info(self, "番号を選択",
                      f"{part}に入れる番号を一覧で1つ選んでからドロップしてください")
            return
        self._copy_async([(WORK / names[0], part, paths)], select=names[0],
                         done_msg=f"{names[0]}\\{part} に "
                                  "{n}本追加しました(ドロップ)")

    # ── 一覧 ────────────────────────────────────────────
    def refresh(self, select=None):
        """一覧を作り直す。フォルダ走査は別スレッドなので画面は固まらない。
        select を渡すとスキャン完了後にその番号を選択・表示する。"""
        if select:
            self._select_after = select
        if self._scanning:      # 走査中に重ねて走らせない(遅いドライブ対策)
            self._scan_again = True
            return
        self._scanning = True
        # 走査中に出したメッセージ(「〜しました」)を後から消さないよう記録
        self._status_seen = self.status.get()
        q = queue.Queue()

        def work():
            try:
                q.put(("ok", scan_all()))
            except Exception as exc:
                q.put(("err", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self._poll_scan(q)

    def _poll_scan(self, q):
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            self.after(120, lambda: self._poll_scan(q))
            return
        self._scanning = False
        if kind == "ok":
            self._show_rows(payload)
        else:
            self.status.set(f"一覧を読み込めませんでした: {payload}")
        if self._scan_again:    # 走査中に来た更新要求をここで1回だけ処理
            self._scan_again = False
            self.refresh()

    def _show_rows(self, data):
        """スキャン結果を一覧に反映する(メインスレッド)。"""
        sel = set(self.tree.selection())
        if self._select_after:
            sel.add(self._select_after)
        self.tree.delete(*self.tree.get_children())
        if not data["exists"]:
            self.status.set(f"workフォルダがありません: {WORK}")
            return
        ledger = data["ledger"]
        flt = self.filter_var.get()
        n_done = n_flag = n_todo = 0
        pcts = []
        for s in data["rows"]:
            has_src = bool(s["a"] or s["b"] or s["c"])
            if "完成" in s["state"]:
                n_done += 1
            if "要確認" in s["state"]:
                n_flag += 1
                # 要確認の理由を台帳から補完
                why = flag_reason(ledger.get(s["name"]))
                if why:
                    s["note"] = (s["note"] + "・" + why).strip("・")
            if has_src or s["pct"]:
                pcts.append(s["pct"])   # 素材が入っている番号だけ全体進捗の対象
                if s["pct"] < 100:
                    n_todo += 1
            # 表示フィルタ
            show = (flt == "全部"
                    or (flt == "要確認" and "要確認" in s["state"])
                    or (flt == "完成" and "完成" in s["state"])
                    or (flt == "処理中" and 0 < s["pct"] < 100)
                    or (flt == "未処理" and s["pct"] == 0 and has_src)
                    or (flt == "素材入り" and has_src))
            if not show:
                continue
            bar = ("█" * round(s["pct"] / 12.5)
                   + "░" * (8 - round(s["pct"] / 12.5)))
            mins = (ledger.get(s["name"]) or {}).get("処理分", "")
            self.tree.insert("", "end", iid=s["name"], values=(
                s["name"], s["a"] or "", s["b"] or "", s["c"] or "",
                f"{bar} {s['pct']:>3}% {s['prog']}",
                s["state"], f"{mins}分" if mins else "",
                s["note"], s["memo"]))
        for iid in sel:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        if self._select_after and self.tree.exists(self._select_after):
            self.tree.see(self._select_after)
        self._select_after = None
        total = f" / 全体 {sum(pcts) // len(pcts)}% ({len(pcts)}本中)" \
            if pcts else ""
        eta = ""
        avg = data["avg"]
        if n_todo and avg:
            m = n_todo * avg
            eta = (f" / 残り{n_todo}本≈"
                   + (f"{m / 60:.1f}時間" if m >= 60 else f"{m:.0f}分"))
        if not self._busy and self.status.get() == self._status_seen:
            # 走査中に新しいメッセージが出ていればそちらを残す
            self.status.set(f"完成 {n_done} / 要確認 {n_flag}{total}{eta}")

    def _auto(self):
        # モーダル表示中・コピー中は走らせない(裏で一覧を作り直すと
        # ダイアログ操作が固まったように見えるため)
        try:
            if self.auto_var.get() and not _MODAL and not self._busy:
                self.refresh()
        finally:
            self.after(10000, self._auto)

    def _selected(self, quiet=False):
        names = list(self.tree.selection())
        if not names and not quiet:
            show_info(self, "選択なし", "一覧から番号を選択してください\n"
                      "(Ctrl+クリックで複数、Shift+クリックで範囲)")
        return names

    # ── 素材投入(最初) ────────────────────────────────────
    def new_video(self):
        if not self._guard():
            return
        paths = ask_videos(self, "Aパートの本編クリップを選択(複数可・名前順に結合)")
        if not paths:
            return
        # コピーの前に B/C も聞いてしまう(あとでまとめて1回のコピーにする)
        extra = []
        if ask_yesno(self, "Bパート",
                     f"Aクリップ{len(paths)}本を選びました。\n"
                     "続けてB(フィニッシュ)動画も入れますか?"):
            bp = ask_videos(self, "B動画を選択")
            if bp:
                extra.append(("B", bp))
                cp = ask_videos(self, "C動画を選択(キャンセルで無し)")
                if cp:
                    extra.append(("C", cp))
        folder = next_empty_number()
        jobs = [(folder, "A", paths)] + [(folder, p, v) for p, v in extra]
        self._copy_async(
            jobs, select=folder.name,
            done_msg=f"{folder.name} に素材{{n}}本を入れました。▶で処理できます")

    def add_to(self, part):
        if not self._guard():
            return
        names = self._selected()
        if not names:
            return
        if len(names) > 1:
            show_info(self, "1つだけ選択", "素材の追加は1番号ずつです")
            return
        paths = ask_videos(self, f"{names[0]} の {part} に追加する動画を選択")
        if not paths:
            return
        self._copy_async([(WORK / names[0], part, paths)], select=names[0],
                         done_msg=f"{names[0]}\\{part} に {{n}}本追加しました")

    # ── 実行 ────────────────────────────────────────────
    def run_pipeline(self, selected, wait=False):
        # 二重起動すると同じ番号を2つのプロセスが同時に触って壊れるので確認する
        if self._pipe_proc is not None and self._pipe_proc.poll() is None:
            if not ask_yesno(self, "すでに処理中",
                             "パイプラインを実行中のウィンドウがあります。\n"
                             "同じ番号を二重に処理するとファイルが壊れます。\n"
                             "それでももう1つ起動しますか?"):
                return
        cmd = [sys.executable, str(BASE / "pipeline.py")]
        if selected:
            names = self._selected()
            if not names:
                return
            cmd += ["--only", ",".join(names)]
        if wait:
            cmd += ["--camera-wait"]
        self._pipe_proc = subprocess.Popen(cmd, cwd=str(BASE),
                                           creationflags=CREATE_NEW_CONSOLE)
        self.status.set("パイプラインを別ウィンドウで起動しました(一覧は自動更新)")

    # ── やり直し ─────────────────────────────────────────
    def redo(self, level):
        names = self._selected()
        if not names:
            return
        all_files, all_dirs = [], []
        for n in names:
            files, dirs = redo_targets(WORK / n, level)
            all_files += files
            all_dirs += dirs
        if not all_files and not all_dirs:
            show_info(self, "対象なし",
                      f"{'/'.join(names)} に消せる中間ファイルはありません")
            return
        lines = "\n".join(f"  {p.name}" for p in all_files[:12])
        more = f"\n  …ほか{len(all_files) - 12}件" if len(all_files) > 12 else ""
        if not ask_yesno(self,
                f"{level}やり直し",
                f"{', '.join(names)} を「{level}」やり直します。\n\n"
                f"削除されるファイル({len(all_files)}件):\n{lines}{more}\n\n"
                f"※素材(A/B/C)とcamera.txtは消えません。よろしいですか？"):
            return
        for p in all_files:
            try:
                p.unlink()
            except Exception:
                pass
        for d in all_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.refresh()
        self.status.set(f"{', '.join(names)} を{level}やり直せる状態にしました "
                        "(▶で再実行)")

    # ── 要確認の処理・メモ・ギャラリー ─────────────────────
    def approve_selected(self):
        names = self._selected()
        if not names:
            return
        targets = [n for n in names if (FLAGGED / f"{n}.mp4").is_file()]
        if not targets:
            show_info(self, "対象なし", "選択の中に要確認の動画がありません")
            return
        if not ask_yesno(self,
                "合格にする",
                f"{', '.join(targets)} を目視合格として final\\ へ移動します。"
                "\nよろしいですか？(台帳にも記録されます)"):
            return
        for n in targets:
            approve(n)
        self.refresh()
        self.status.set(f"{', '.join(targets)} を合格にしました")

    def open_check_report(self):
        names = self._selected()
        if not names:
            return
        opened = False
        for n in self._confirm_many(names, "レポート"):
            rep = WORK / n / "check" / "report" / "report.html"
            if rep.is_file():
                subprocess.Popen(["cmd", "/c", "start", "", str(rep)],
                                 creationflags=NO_WINDOW)
                opened = True
        if not opened:
            show_info(self, "レポートなし",
                      "チェックレポートが見つかりません\n"
                      "(合格して掃除済みの動画にはありません)")

    def edit_memo(self):
        if not self._guard():
            return
        names = self._selected()
        if not names:
            return
        if len(names) > 1:
            show_info(self, "1つだけ選択", "メモは1番号ずつです")
            return
        path = WORK / names[0] / "メモ.txt"
        cur = ""
        try:
            cur = path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        memo = ask_string(self, f"{names[0]} のメモ",
                          "短文メモ(空にすると削除):", cur)
        if memo is None:
            return
        if memo.strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(memo.strip() + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        self.refresh()
        self.status.set(f"{names[0]} のメモを保存しました")

    def choose_voice(self):
        """連続ボイスを「ランダム」か「特定の素材」から選んで voice.txt に保存。
        複数番号を選択していれば同じ声をまとめて設定できる。"""
        if not self._guard():
            return
        names = self._selected()
        if not names:
            return
        voices = sorted([f for f in VOICE_LONG.iterdir()
                         if f.is_file() and f.suffix.lower() in AUDIO_EXTS],
                        key=lambda p: p.name.lower()) \
            if VOICE_LONG.is_dir() else []
        if not voices:
            show_info(self, "素材なし",
                      f"連続ボイスの素材がありません。\n{VOICE_LONG}\n"
                      "に長い喘ぎ声(wav/mp3)を入れてください")
            return

        dlg, close = self._single("voice", f"{', '.join(names)} の声を選ぶ")
        if dlg is None:      # 既に開いている(前面に出した)
            return
        ttk.Label(dlg, text="使う声を選んでください(ダブルクリック=試聴):",
                  padding=(10, 8, 10, 2)).pack(anchor="w")
        lb = tk.Listbox(dlg, width=52, height=min(22, len(voices) + 1),
                        activestyle="dotbox")
        lb.pack(padx=10, pady=4, fill="both", expand=True)
        lb.insert("end", "🎲 ランダム(おまかせ)")
        for v in voices:
            lb.insert("end", v.name)
        # 現在の設定を初期選択
        cur = ""
        try:
            cur = (WORK / names[0] / "voice.txt").read_text(
                encoding="utf-8").strip()
        except Exception:
            pass
        idx = 0
        for i, v in enumerate(voices, start=1):
            if v.name == cur:
                idx = i
                break
        lb.selection_set(idx)
        lb.see(idx)

        def listen(_e=None):
            i = lb.curselection()
            if i and i[0] > 0:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", str(voices[i[0] - 1])],
                    creationflags=NO_WINDOW)
        lb.bind("<Double-1>", listen)

        def decide():
            i = lb.curselection()
            if not i:
                return
            close()
            pick = "" if i[0] == 0 else voices[i[0] - 1].name
            redo_needed = []
            for n in names:
                p = WORK / n / "voice.txt"
                if pick:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(pick + "\n", encoding="utf-8")
                else:
                    p.unlink(missing_ok=True)
                if (WORK / n / "05_sound.mp4").is_file() \
                        or final_video(n):
                    redo_needed.append(n)
            if redo_needed and ask_yesno(
                    self, "音を作り直す",
                    f"{', '.join(redo_needed)} は音当て済みです。\n"
                    "新しい声で音だけ作り直せる状態にしますか？\n"
                    "(映像はそのまま。▶で再実行すれば数十秒/本)"):
                for n in redo_needed:
                    self.redo_silent(n, "音当てから(音だけ数十秒)")
            self.refresh()
            self.status.set(f"{', '.join(names)} の声を"
                            f"「{pick or 'ランダム'}」にしました")

        btns = ttk.Frame(dlg, padding=(10, 2, 10, 10))
        btns.pack(fill="x")
        ttk.Button(btns, text="🔊 試聴", command=listen).pack(side="left")
        ttk.Button(btns, text="決定", command=decide).pack(side="left", padx=8)
        ttk.Button(btns, text="キャンセル",
                   command=close).pack(side="left")

    def open_gallery(self):
        g = build_gallery()
        if g:
            subprocess.Popen(["cmd", "/c", "start", "", str(g)],
                             creationflags=NO_WINDOW)
        else:
            show_info(self, "まだありません", "完成品がまだ1本もありません")

    # ── 素材クリア(間違えて置いた時) ───────────────────────
    def clear_dialog(self):
        if not self._guard():
            return
        names = self._selected()
        if not names:
            return
        if len(names) > 1:
            show_info(self, "1つだけ選択", "素材のクリアは1番号ずつです")
            return
        name = names[0]
        folder = WORK / name
        counts = {p: len(part_files(folder, p)) for p in ("A", "B", "C")}
        if not any(counts.values()):
            show_info(self, "素材なし", f"{name} に素材は入っていません")
            return

        dlg, close = self._single("clear", f"{name} の素材をクリア")
        if dlg is None:      # 既に開いている(前面に出した)
            return
        dlg.resizable(False, False)
        ttk.Label(dlg, text=f"{name} の素材: "
                            f"A={counts['A']}本 / B={counts['B']}本 / "
                            f"C={counts['C']}本\nどれを空にしますか？"
                            "(削除した素材は戻せません)",
                  padding=12).pack()
        btns = ttk.Frame(dlg, padding=(12, 0, 12, 12))
        btns.pack()

        def do(parts):
            close()
            label = "・".join(parts)
            if not ask_yesno(self, "確認",
                             f"{name} の {label} の素材"
                             f"({sum(counts[p] for p in parts)}本)を削除します。"
                             "よろしいですか？"):
                return
            n = clear_parts(folder, parts)
            # 処理済みなら中間ファイル・完成品も掃除して未処理に戻すか確認
            files, dirs = redo_targets(folder, "最初から")
            if files or dirs:
                if ask_yesno(self, "未処理に戻す",
                             "この番号は処理済みデータがあります。\n"
                             "中間ファイルと完成品も消して未処理に戻しますか？\n"
                             "(残すと古い素材のままの動画が完成扱いで残ります)"):
                    self.redo_silent(name, "最初から")
            self.refresh()
            self.status.set(f"{name} の {label} から素材{n}本を削除しました")

        for p in ("A", "B", "C"):
            ttk.Button(btns, text=f"{p}だけ ({counts[p]}本)",
                       state="normal" if counts[p] else "disabled",
                       command=lambda pp=p: do([pp])).pack(side="left", padx=4)
        ttk.Button(btns, text="全部",
                   command=lambda: do([p for p in "ABC" if counts[p]])
                   ).pack(side="left", padx=(12, 4))
        ttk.Button(btns, text="キャンセル",
                   command=close).pack(side="left", padx=4)

    # ── ズーム先指定 ──────────────────────────────────────
    def set_camera(self):
        if not self._guard():
            return
        names = self._selected()
        if not names:
            return
        if len(names) > 1:
            show_info(self, "1つだけ選択", "ズーム先の指定は1番号ずつです")
            return
        folder = WORK / names[0]
        cur_m, cur_f = "0.5,0.6", "0.5,0.25"
        try:
            plan = json.loads((folder / "camera_preview" / "plan.json")
                              .read_text(encoding="utf-8"))
            cur_m = f"{plan['mosaic'][0]},{plan['mosaic'][1]}"
            cur_f = f"{plan['face'][0]},{plan['face'][1]}"
        except Exception:
            pass
        txt = folder / "camera.txt"
        if txt.is_file():
            saved = txt.read_text(encoding="utf-8")
            for line in saved.splitlines():
                if line.startswith("mosaic="):
                    cur_m = line.split("=", 1)[1]
                if line.startswith("face="):
                    cur_f = line.split("=", 1)[1]
        m = ask_string(self, "モザイク部ズームの座標",
                       "モザイク部(股間)の位置: 左からの割合,上からの割合\n"
                       "例 0.62,0.55  (画面中央=0.5,0.5)", cur_m)
        if not m:
            return
        f = ask_string(self, "顔ズームの座標",
                       "顔の位置: 左からの割合,上からの割合\n例 0.5,0.2", cur_f)
        if not f:
            return
        txt.write_text(f"mosaic={m.strip()}\nface={f.strip()}\n",
                       encoding="utf-8")
        # 演出済みなら作り直しが要る
        if (folder / "05_upslide.mp4").is_file() or final_video(names[0]):
            if ask_yesno(self, "作り直し",
                         "既に演出済みです。新しいズーム先で"
                         "作り直せる状態にしますか?"):
                self.redo_silent(names[0], "カメラ演出から")
        self.refresh()
        self.status.set(f"{names[0]} のズーム先を保存しました(camera.txt)")

    def redo_silent(self, name, level):
        files, dirs = redo_targets(WORK / name, level)
        for p in files:
            try:
                p.unlink()
            except Exception:
                pass
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)

    # ── 表示系 ───────────────────────────────────────────
    def play_selected(self):
        names = self._selected()
        if not names:
            return
        for n in self._confirm_many(names, "動画"):
            v = final_video(n)
            if v:
                subprocess.Popen(["cmd", "/c", "start", "", str(v)],
                                 creationflags=NO_WINDOW)
            else:
                subprocess.Popen(["explorer", str(WORK / n)])

    def open_preview(self):
        for n in self._confirm_many(self._selected() or [], "プレビュー"):
            d = WORK / n / "camera_preview"
            if d.is_dir():
                subprocess.Popen(["explorer", str(d)])
            else:
                show_info(self, "まだありません",
                          f"{n} のプレビューは演出プラン作成後にできます")
                return

    def open_report(self):
        if REPORT.is_file():
            subprocess.Popen(["cmd", "/c", "start", "", str(REPORT)],
                             creationflags=NO_WINDOW)
        else:
            show_info(self, "まだありません",
                      "演出チェック.htmlは夜バッチ実行後に作られます")

    def open_selected(self):
        for n in self._confirm_many(self._selected() or [], "フォルダ"):
            subprocess.Popen(["explorer", str(WORK / n)])


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception:
        # ここに来るのは起動そのものに失敗した場合(Tcl/Tkが壊れている等)
        report_error(traceback.format_exc(), "パイプライン管理を起動できません")
        raise
