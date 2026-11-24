"""Read the remote .7z with HTTP range requests (via curl), so we only download the parts we need.

   python tools/r7z.py list
   python tools/r7z.py extract OUTDIR name1 [name2 ...]
"""
import io
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

URL = "https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z"
BLOCK = 4 * 1024 * 1024
PREFETCH = 14                      # parallel range requests


def curl_range(url, a, b):
    for attempt in range(6):
        r = subprocess.run(["curl.exe", "-s", "--fail", "-r", f"{a}-{b}", url], capture_output=True)
        if r.returncode == 0 and len(r.stdout) == b - a + 1:
            return r.stdout
        time.sleep(2 + 3 * attempt)
    raise IOError(f"range {a}-{b} failed")


def curl_size(url):
    r = subprocess.run(["curl.exe", "-sI", url], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            return int(line.split(":")[1])
    raise IOError("no size")


class RangeFile(io.RawIOBase):
    def __init__(self, url):
        self.url, self.size, self.pos = url, curl_size(url), 0
        self.cache, self.fetched = {}, 0
        self.pool = ThreadPoolExecutor(PREFETCH)
        self.pending = {}
        self.t0 = time.time()

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def _submit(self, i):
        if i in self.cache or i in self.pending or i * BLOCK >= self.size:
            return
        a = i * BLOCK
        b = min(self.size, a + BLOCK) - 1
        self.pending[i] = self.pool.submit(curl_range, self.url, a, b)

    def _block(self, i):
        for j in range(i, i + PREFETCH):
            self._submit(j)
        if i not in self.cache:
            self.cache[i] = self.pending.pop(i).result()
            self.fetched += len(self.cache[i])
            if self.fetched // BLOCK % 25 == 0:
                mb = self.fetched / 1e6
                print(f"  ... {mb:.0f} MB downloaded ({mb / max(1, time.time() - self.t0):.2f} MB/s)", flush=True)
            while len(self.cache) > 4 * PREFETCH:
                self.cache.pop(next(iter(self.cache)))
        return self.cache[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        out = bytearray()
        while n > 0:
            i, o = divmod(self.pos, BLOCK)
            chunk = self._block(i)[o:o + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


if __name__ == "__main__":
    import py7zr
    f = RangeFile(URL)
    z = py7zr.SevenZipFile(f, mode="r")
    if sys.argv[1] == "list":
        for x in z.list():
            print(f"{x.filename}\t{x.uncompressed}\t{x.compressed}")
        ai = z.archiveinfo()
        print("solid:", ai.solid, "blocks:", ai.blocks, "method:", ai.method_names,
              "| MB fetched for header:", round(f.fetched / 1e6, 1))
    else:
        out, names = sys.argv[2], sys.argv[3:]
        t = time.time()
        z.extract(path=out, targets=names)
        print("extracted", names, "| downloaded MB", round(f.fetched / 1e6), "in", round(time.time() - t), "s")
