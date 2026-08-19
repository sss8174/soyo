# -*- coding: utf-8 -*-
"""パイプライン管理.pyw を修正版に入れ替える(このファイルをダブルクリック)。

同じフォルダの パイプライン管理.pyw を GitHub の修正版で置き換え、
元のファイルは 旧_パイプライン管理_日付.pyw として残す。
原因確認用の 起動テスト.bat も一緒に置く。
"""
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
RAW = ("https://raw.githubusercontent.com/sss8174/soyo/"
       "claude/window-registration-freeze-on3zuh/video100/")
FILES = [
    ("パイプライン管理.pyw",
     "%E3%83%91%E3%82%A4%E3%83%97%E3%83%A9%E3%82%A4%E3%83%B3%E7%AE%A1%E7%90%86"
     ".pyw", 40000),
    ("起動テスト.bat", "%E8%B5%B7%E5%8B%95%E3%83%86%E3%82%B9%E3%83%88.bat", 100),
]


def fetch(name, quoted, least):
    data = urllib.request.urlopen(RAW + quoted, timeout=60).read()
    if len(data) < least:
        raise RuntimeError(f"{name} の内容が短すぎます({len(data)}バイト)")
    target = BASE / name
    if target.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        backup = BASE / f"旧_{target.stem}_{stamp}{target.suffix}"
        shutil.copy(target, backup)
        print(f"  元のファイルを控えました: {backup.name}")
    target.write_bytes(data)
    print(f"  置きました: {name} ({len(data):,}バイト)")


print(f"取り込み先フォルダ: {BASE}\n")
try:
    for name, quoted, least in FILES:
        print(f"{name} を取得中…")
        fetch(name, quoted, least)
    print("\n完了しました。ショートカットから起動してみてください。")
    print("起動しない場合は 起動テスト.bat をダブルクリックして、")
    print("表示されたメッセージを教えてください。")
except Exception as exc:
    print(f"\n失敗しました: {exc}")
    print("(社内ネットワーク等でGitHubに繋がらない場合は、"
          "チャットの添付ファイルを手で置いてください)")
try:
    input("\nEnterキーで閉じます: ")
except Exception:
    pass
