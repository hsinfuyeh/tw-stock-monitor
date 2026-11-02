"""把雲端更新時印出的 ::warning:: 另外記下來，發佈時放進 meta.json 的 warnings，網頁才看得到。

附加資料（上櫃、集保、重大訊息…）失敗時只印警告、不擋發佈，這是刻意的；但警告只出現在
GitHub 的執行紀錄裡，沒有人會去翻。2026-10-23 起上櫃與集保因為 DuckDB 2.0 寫入失敗，
整整十天沒人發現，就是因為這樣。

    ci_update.py  開頭呼叫 capture()：之後所有模組 print 出來的 ::warning:: 都會記進 FILE
    publish.py    產出 meta.json 時呼叫 load()，放進 warnings

FILE 每次 capture() 都重新開始，所以只會有「這一次更新」的警告。
"""
import json
import sys

FILE = "ci_warnings.json"
PREFIX = "::warning::"
MAX = 20                  # 網頁只是提醒，不需要整份紀錄


class _Tee:
    """照樣輸出，同時把含 ::warning:: 的那幾行記下來。"""

    def __init__(self, stream, sink):
        self._s, self._sink, self._buf = stream, sink, ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink(line)
        return self._s.write(text)

    def flush(self):
        return self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


def capture():
    got = []

    def sink(line):
        i = line.find(PREFIX)
        if i < 0 or len(got) >= MAX:
            return
        got.append(line[i + len(PREFIX):].strip()[:300])
        try:
            with open(FILE, "w", encoding="utf-8") as f:
                json.dump(got, f, ensure_ascii=False)
        except OSError:
            pass

    try:
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    except OSError:
        pass
    sys.stdout = _Tee(sys.stdout, sink)
    sys.stderr = _Tee(sys.stderr, sink)


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            v = json.load(f)
        return [str(x) for x in v][:MAX] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []
