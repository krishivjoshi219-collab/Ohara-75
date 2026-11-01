# firmware_production.py -- Ohara-75 production reader firmware
#
# Kept here for reference. The beginner-facing file is ../frimware.py.
#
# Ohara-75 wiring (from Ohara-75.kicad_pcb):
#   EPD      SCK 12, MOSI 11, CS 10, DC 9, RST 4, BUSY 3
#   Page     NEXT 6, PREV 7
#   5-way    UP 5, DOWN 8, LEFT 16, RIGHT 17, PRESS 1
#   microSD  CLK 14, CMD 15, D0 2  (SDMMC slot 0, 1-bit)
#
# Q1 MUST be fixed before the panel will work:
#   G -> EPD_GDR, S -> EPD_RESE, D -> switch node (L1/D1)
#
# Uses MicroPython's machine/framebuf/os/json/re/gc, machine.SDCard,
# and a panel init sequence adapted from Waveshare's MIT epd7in5_V2 driver.
#
# Controls:
#   Home/menus: up/down move, press/right select, left back
#   Reader: left/right or page keys turn pages, press opens menu
#   Settings: up/down move, right/press change, left back
#
# 7.5" panels are not LCD-smooth. This uses the fast waveform for normal
# updates, then a full refresh every N fast updates. Settings lets you
# switch to Quality (full refresh every time) or tune the interval.

import gc
import os
import time
import machine
import framebuf

try:
    import json
except ImportError:
    import ujson as json

try:
    import re
except ImportError:
    import ure as re

WIDTH = 800
HEIGHT = 480
BUF = WIDTH * HEIGHT // 8

EPD_SCK = 12
EPD_MOSI = 11
EPD_CS = 10
EPD_DC = 9
EPD_RST = 4
EPD_BUSY = 3

BTN_NEXT = 6
BTN_PREV = 7
NAV_UP = 5
NAV_DOWN = 8
NAV_LEFT = 16
NAV_RIGHT = 17
NAV_PRESS = 1

BOOKS_DIR = "/sd/books"
STATE_DIR = "/sd/.ohara"

M = 24
CHARS = (WIDTH - 2 * M) // 8
TOP = 38
BOTTOM = HEIGHT - 38

DEFAULTS = {
    "theme": "light",
    "fast": True,
    "line_h": 14,
    "refresh_every": 6,
    "auto_sleep": 10,
    "sort": "name",
}

# ---------------------------------------------------------------- watchdog

WDT = None


def feed():
    if WDT:
        WDT.feed()


def start_wdt():
    global WDT
    try:
        WDT = machine.WDT(timeout=60000)
    except Exception:
        WDT = None


# ---------------------------------------------------------------- e-paper

class EPD:
    def __init__(self, spi, cs, dc, rst, busy):
        self.spi = spi
        self.cs = cs
        self.dc = dc
        self.rst = rst
        self.busy = busy
        self.cs.init(machine.Pin.OUT, value=1)
        self.dc.init(machine.Pin.OUT, value=0)
        self.rst.init(machine.Pin.OUT, value=1)
        self.busy.init(machine.Pin.IN, machine.Pin.PULL_UP)

        self.buf = bytearray(BUF)
        self.inv = bytearray(BUF)
        self.fb = framebuf.FrameBuffer(
            self.buf, WIDTH, HEIGHT, framebuf.MONO_HMSB
        )
        self.mode = None
        self.fast_count = 0
        self.fast_limit = 6
        self.init_full()

    def cmd(self, c):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes([c]))
        self.cs.value(1)

    def data(self, d):
        self.dc.value(1)
        self.cs.value(0)
        self.spi.write(bytes([d]) if isinstance(d, int) else d)
        self.cs.value(1)

    def wait(self, timeout=20000):
        start = time.ticks_ms()
        while self.busy.value() == 0:
            feed()
            if time.ticks_diff(time.ticks_ms(), start) > timeout:
                raise OSError("epd busy timeout")
            time.sleep_ms(20)

    def reset(self):
        self.rst.value(0)
        time.sleep_ms(20)
        self.rst.value(1)
        time.sleep_ms(20)

    def init_full(self):
        self.reset()
        self.cmd(0x01)
        self.data(bytearray([0x07, 0x07, 0x3F, 0x3F]))
        self.cmd(0x06)
        self.data(bytearray([0x17, 0x17, 0x28, 0x17]))
        self.cmd(0x04)
        time.sleep_ms(100)
        self.wait()
        self.cmd(0x00)
        self.data(0x1F)
        self.cmd(0x61)
        self.data(bytearray([0x03, 0x20, 0x01, 0xE0]))
        self.cmd(0x15)
        self.data(0x00)
        self.cmd(0x50)
        self.data(bytearray([0x10, 0x07]))
        self.cmd(0x60)
        self.data(0x22)
        self.mode = "full"
        self.fast_count = 0

    def init_fast(self):
        self.reset()
        self.cmd(0x00)
        self.data(0x1F)
        self.cmd(0x50)
        self.data(bytearray([0x10, 0x07]))
        self.cmd(0x04)
        time.sleep_ms(100)
        self.wait()
        self.cmd(0x06)
        self.data(bytearray([0x27, 0x27, 0x18, 0x17]))
        self.cmd(0xE0)
        self.data(0x02)
        self.cmd(0xE5)
        self.data(0x5A)
        self.mode = "fast"

    def send(self):
        for i in range(BUF):
            self.inv[i] = self.buf[i] ^ 0xFF
        self.cmd(0x10)
        self.data(self.buf)
        self.cmd(0x13)
        self.data(self.inv)
        self.cmd(0x12)
        time.sleep_ms(100)
        self.wait(30000)

    def show(self, fast=False):
        if fast and self.fast_limit > 1 and self.fast_count < self.fast_limit:
            if self.mode != "fast":
                self.init_fast()
            self.send()
            self.fast_count += 1
        else:
            if self.mode != "full":
                self.init_full()
            self.send()
            self.fast_count = 0

    def clear(self, color=1):
        self.fb.fill(color)

    def sleep(self):
        self.cmd(0x50)
        self.data(0xF7)
        self.cmd(0x02)
        self.wait()
        self.cmd(0x07)
        self.data(0xA5)
        self.mode = None


# ---------------------------------------------------------------- buttons

class Buttons:
    def __init__(self):
        self.pins = {
            "up": machine.Pin(NAV_UP, machine.Pin.IN, machine.Pin.PULL_UP),
            "down": machine.Pin(NAV_DOWN, machine.Pin.IN, machine.Pin.PULL_UP),
            "left": machine.Pin(NAV_LEFT, machine.Pin.IN, machine.Pin.PULL_UP),
            "right": machine.Pin(NAV_RIGHT, machine.Pin.IN, machine.Pin.PULL_UP),
            "press": machine.Pin(NAV_PRESS, machine.Pin.IN, machine.Pin.PULL_UP),
            "next": machine.Pin(BTN_NEXT, machine.Pin.IN, machine.Pin.PULL_UP),
            "prev": machine.Pin(BTN_PREV, machine.Pin.IN, machine.Pin.PULL_UP),
        }
        self.last = {k: 1 for k in self.pins}
        self.stable = {k: 1 for k in self.pins}
        self.changed = {k: time.ticks_ms() for k in self.pins}
        self.repeat = {k: 0 for k in self.pins}

    def get(self):
        now = time.ticks_ms()
        rep = None
        for k, p in self.pins.items():
            raw = p.value()
            if raw != self.last[k]:
                self.last[k] = raw
                self.changed[k] = now

            if time.ticks_diff(now, self.changed[k]) >= 25 and raw != self.stable[k]:
                self.stable[k] = raw
                if raw == 0:
                    self.repeat[k] = now + 450
                    return k

            if self.stable[k] == 0 and k in ("up", "down"):
                if time.ticks_diff(now, self.repeat[k]) >= 0 and rep is None:
                    rep = k
        if rep:
            self.repeat[rep] = now + 120
        return rep


# ---------------------------------------------------------------- storage

class Storage:
    def __init__(self):
        self.sd = None
        self.ok = False

    def mount(self):
        try:
            os.umount("/sd")
        except Exception:
            pass

        try:
            # SDMMC slot 0's fixed ESP32-S3 pins are 14/15/2.
            self.sd = machine.SDCard(slot=0, width=1)
            try:
                os.mkdir("/sd")
            except OSError:
                pass
            os.mount(self.sd, "/sd")
            self.ok = True
            for d in (BOOKS_DIR, STATE_DIR):
                try:
                    os.mkdir(d)
                except OSError:
                    pass
        except Exception as e:
            print("SD mount failed:", e)
            self.ok = False
            try:
                if self.sd:
                    self.sd.deinit()
            except Exception:
                pass
            self.sd = None
        return self.ok

    def load(self, name, default):
        if not self.ok:
            return default
        try:
            with open(STATE_DIR + "/" + name, "r") as f:
                return json.load(f)
        except (OSError, ValueError, UnicodeError):
            return default

    def save(self, name, obj):
        if not self.ok:
            return False
        path = STATE_DIR + "/" + name
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(obj, f)
            try:
                os.remove(path)
            except OSError:
                pass
            os.rename(tmp, path)
            return True
        except (OSError, TypeError, ValueError) as e:
            print("save failed:", e)
            return False

    def books(self):
        if not self.ok:
            return []
        out = []
        try:
            for n in os.listdir(BOOKS_DIR):
                if n.startswith("."):
                    continue
                low = n.lower()
                if low.endswith(".txt") or low.endswith(".md"):
                    out.append(n)
        except OSError:
            pass
        out.sort(key=str.lower)
        return out

    def read_book(self, path):
        size = os.stat(path)[6]
        if size > 900000:
            raise ValueError("Book too large for RAM")
        with open(path, "rb") as f:
            data = f.read()
        try:
            return data.decode("utf-8")
        except UnicodeError:
            return "".join(
                chr(b) if (32 <= b < 127 or b in (9, 10, 13)) else "?"
                for b in data
            )

    def free_kb(self):
        try:
            s = os.statvfs("/sd")
            return s[0] * s[3] // 1024
        except (OSError, IndexError, AttributeError):
            return 0


# ---------------------------------------------------------------- text

_NONASCII = re.compile(r"[^\n\x20-\x7E]")
_SMART = {
    "\uFEFF": "", "\u2018": "'", "\u2019": "'",
    "\u201C": '"', "\u201D": '"', "\u2013": "-",
    "\u2014": "-", "\u2026": "...", "\u00A0": " ",
    "\u2022": "*",
}


def clean_text(text):
    for a, b in _SMART.items():
        text = text.replace(a, b)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ")
    return _NONASCII.sub("?", text)


def next_line(text, pos, width):
    n = len(text)
    if pos >= n:
        return n
    limit = pos + width
    nl = text.find("\n", pos, limit + 1)
    if nl != -1:
        return nl + 1
    if limit >= n:
        return n
    sp = text.rfind(" ", pos, limit)
    if sp > pos:
        return sp + 1
    return limit


class Book:
    def __init__(self, title, text):
        self.title = title
        self.text = text
        self.pages = []
        self.cpl = 0
        self.lpp = 0

    def layout(self, cpl, lpp):
        self.cpl = cpl
        self.lpp = max(4, lpp)
        pages = []
        n = len(self.text)
        pos = 0
        if n == 0:
            pages.append((0, 0))
            self.pages = pages
            return
        while pos < n:
            start = pos
            for _ in range(self.lpp):
                if pos >= n:
                    break
                pos = next_line(self.text, pos, cpl)
            pages.append((start, pos))
            feed()
        self.pages = pages

    def count(self):
        return len(self.pages)

    def lines(self, page):
        page = max(1, min(page, len(self.pages)))
        start, end = self.pages[page - 1]
        pos = start
        while pos < end:
            nxt = next_line(self.text, pos, self.cpl)
            yield self.text[pos:nxt].rstrip("\n").rstrip()
            pos = nxt


def cut(s, n):
    if len(s) <= n:
        return s
    if n < 4:
        return s[:n]
    return s[:n - 3] + "..."


def wrap(text, width):
    out = []
    pos = 0
    n = len(text)
    while pos < n:
        nxt = next_line(text, pos, width)
        out.append(text[pos:nxt].rstrip())
        pos = nxt
    return out


def title_from_name(name):
    i = name.rfind(".")
    if i > 0:
        name = name[:i]
    return name.replace("_", " ").strip()


# ---------------------------------------------------------------- app

class App:
    def __init__(self):
        spi = machine.SPI(
            1, baudrate=4000000, polarity=0, phase=0,
            sck=machine.Pin(EPD_SCK), mosi=machine.Pin(EPD_MOSI)
        )
        self.epd = EPD(
            spi,
            machine.Pin(EPD_CS), machine.Pin(EPD_DC),
            machine.Pin(EPD_RST), machine.Pin(EPD_BUSY)
        )
        self.buttons = Buttons()
        self.storage = Storage()
        self.storage.mount()

        self.settings = self._load_settings()
        self.state = self._load_state()
        self.theme()
        self.epd.fast_limit = self.settings["refresh_every"]

        self.screen = "home"
        self.home_i = 0
        self.lib_i = 0
        self.lib_top = 0
        self.set_i = 0
        self.menu_i = 0
        self.mark_i = 0
        self.mark_top = 0
        self.mark_items = []
        self.marks_back = "home"
        self.jump_to = 1

        self.books = self.storage.books()
        self.book = None
        self.book_path = None
        self.page = 1
        self.message_back = "home"
        self.return_to = "home"
        self.last_input = time.ticks_ms()

        self.draw_home(True)

    def theme(self):
        self.bg = 0 if self.settings["theme"] == "dark" else 1
        self.fg = 1 - self.bg

    def _load_settings(self):
        raw = self.storage.load("settings.json", {})
        s = dict(DEFAULTS)
        if isinstance(raw, dict):
            if raw.get("theme") in ("light", "dark"):
                s["theme"] = raw["theme"]
            if isinstance(raw.get("fast"), bool):
                s["fast"] = raw["fast"]
            if raw.get("line_h") in (12, 14, 16):
                s["line_h"] = raw["line_h"]
            if raw.get("refresh_every") in (3, 6, 10, 20):
                s["refresh_every"] = raw["refresh_every"]
            if raw.get("auto_sleep") in (0, 5, 10, 30):
                s["auto_sleep"] = raw["auto_sleep"]
            if raw.get("sort") in ("name", "recent"):
                s["sort"] = raw["sort"]
        return s

    def _load_state(self):
        raw = self.storage.load("state.json", {})
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("books"), dict):
            raw["books"] = {}
        if not isinstance(raw.get("bookmarks"), dict):
            raw["bookmarks"] = {}
        if not isinstance(raw.get("last_book"), str):
            raw.pop("last_book", None)
        if not isinstance(raw.get("seq"), int):
            raw["seq"] = 0
        for k in list(raw["bookmarks"]):
            if not isinstance(raw["bookmarks"][k], list):
                raw["bookmarks"][k] = []
        return raw

    def _save_state(self):
        self.storage.save("state.json", self.state)

    def _save_settings(self):
        self.storage.save("settings.json", self.settings)

    def _progress(self, path):
        e = self.state["books"].get(path)
        return e if isinstance(e, dict) else {}

    def _at(self, path):
        v = self._progress(path).get("at", 0)
        return v if isinstance(v, int) else 0

    def _save_progress(self):
        if not self.book or not self.book_path:
            return
        seq = self.state.get("seq", 0) + 1
        self.state["seq"] = seq
        self.state["books"][self.book_path] = {
            "page": self.page,
            "total": self.book.count(),
            "at": seq,
        }
        self.state["last_book"] = self.book_path
        self._save_state()

    def _marks(self):
        m = self.state["bookmarks"].get(self.book_path)
        return m if isinstance(m, list) else []

    def txt(self, s, x, y, c=None):
        self.epd.fb.text(s, x, y, self.fg if c is None else c)

    def head(self, left, right=""):
        self.txt(left, M, 8)
        if right:
            self.txt(right, WIDTH - M - len(right) * 8, 8)
        self.epd.fb.hline(M, 28, WIDTH - 2 * M, self.fg)

    def hint(self, s):
        self.epd.fb.hline(M, HEIGHT - 24, WIDTH - 2 * M, self.fg)
        self.txt(s, M, HEIGHT - 17)

    def draw_list(self, items, sel, y=80, dy=44, values=None, bar=32, off=6):
        fb = self.epd.fb
        for i, label in enumerate(items):
            yy = y + i * dy
            v = values[i] if values else ""
            if i == sel:
                fb.fill_rect(M - 8, yy - off, WIDTH - 2 * M + 16, bar, self.fg)
                self.txt(cut(label, 88), M, yy, self.bg)
                if v:
                    self.txt(v, WIDTH - M - len(v) * 8, yy, self.bg)
            else:
                self.txt(cut(label, 88), M, yy)
                if v:
                    self.txt(v, WIDTH - M - len(v) * 8, yy)

    # ---- home

    def home_items(self):
        items = ["Continue"]
        last = self.state.get("last_book")
        if last:
            name = last.split("/")[-1]
            items[0] = "Continue: " + cut(title_from_name(name), 40)
        return items + ["Library", "Bookmarks", "Settings", "About", "Sleep"]

    def draw_home(self, full=False):
        self.epd.clear(self.bg)
        self.txt("OHARA-75", M, 18)
        self.txt("E-INK READER", M, 36)
        status = ("SD OK  %d books" % len(self.books)) if self.storage.ok else "SD NOT MOUNTED"
        self.txt(status, WIDTH - M - len(status) * 8, 18)
        self.epd.fb.hline(M, 58, WIDTH - 2 * M, self.fg)

        items = self.home_items()
        self.home_i %= len(items)
        self.draw_list(items, self.home_i, 90, 44, bar=30)
        self.txt("Books: /sd/books", M, HEIGHT - 60)
        self.hint("UP/DOWN MOVE    PRESS SELECT")
        self.epd.show(not full and self.settings["fast"])

    def on_home(self, ev):
        n = len(self.home_items())
        if ev == "up":
            self.home_i = (self.home_i - 1) % n
            self.draw_home()
        elif ev == "down":
            self.home_i = (self.home_i + 1) % n
            self.draw_home()
        elif ev in ("press", "right"):
            if self.home_i == 0:
                path = self.state.get("last_book")
                if not path:
                    self.show_message("NO BOOK", ["Pick something from the library."], "home")
                    return
                try:
                    os.stat(path)
                except OSError:
                    self.show_message("MISSING", ["Last book is gone.", cut(path, 70)], "home")
                    return
                self.open_book(path, back="home")
            elif self.home_i == 1:
                self.open_library()
            elif self.home_i == 2:
                self.open_marks("home")
            elif self.home_i == 3:
                self.open_settings("home")
            elif self.home_i == 4:
                self.open_about()
            else:
                self.sleep_now()

    # ---- library

    def open_library(self):
        self.books = self.storage.books()
        if self.settings["sort"] == "recent":
            self.books.sort(
                key=lambda n: self._at(BOOKS_DIR + "/" + n),
                reverse=True
            )

        self.lib_i = 0
        if self.book_path:
            name = self.book_path.split("/")[-1]
            if name in self.books:
                self.lib_i = self.books.index(name)
        self.lib_top = max(0, self.lib_i - 9)
        self.screen = "library"
        self.draw_library(True)

    def draw_library(self, full=False):
        self.epd.clear(self.bg)
        self.head("LIBRARY", "%d FILES" % len(self.books))

        if not self.books:
            self.txt("No .txt or .md files in /sd/books", M, 100)
            self.txt("Copy books to the card, then reopen.", M, 124)
        else:
            if self.lib_i >= len(self.books):
                self.lib_i = len(self.books) - 1
            if self.lib_i < self.lib_top:
                self.lib_top = self.lib_i
            if self.lib_i >= self.lib_top + 10:
                self.lib_top = self.lib_i - 9

            names = []
            vals = []
            for i in range(10):
                idx = self.lib_top + i
                if idx >= len(self.books):
                    break
                name = self.books[idx]
                names.append(cut(name, 60))
                path = BOOKS_DIR + "/" + name
                p = self._progress(path)
                total = p.get("total", 0)
                page = p.get("page", 1)
                s = ("%d%%" % (page * 100 // total)) if total else "new"
                if self.state["bookmarks"].get(path):
                    s += "*"
                vals.append(s)

            self.draw_list(
                names, self.lib_i - self.lib_top,
                44, 40, vals, bar=30, off=4
            )

        self.hint("UP/DOWN MOVE   PRESS OPEN   LEFT BACK")
        self.epd.show(not full and self.settings["fast"])

    def on_library(self, ev):
        n = len(self.books)
        if ev == "up" and n:
            self.lib_i = (self.lib_i - 1) % n
            self.draw_library()
        elif ev == "down" and n:
            self.lib_i = (self.lib_i + 1) % n
            self.draw_library()
        elif ev == "next" and n:
            self.lib_i = min(n - 1, self.lib_i + 10)
            self.draw_library()
        elif ev == "prev" and n:
            self.lib_i = max(0, self.lib_i - 10)
            self.draw_library()
        elif ev in ("press", "right") and n:
            self.open_book(BOOKS_DIR + "/" + self.books[self.lib_i])
        elif ev == "left":
            self.open_home()

    # ---- reader

    def _layout_book(self):
        lpp = (BOTTOM - TOP) // self.settings["line_h"]
        self.book.layout(CHARS, lpp)

    def open_book(self, path, page=None, back="library"):
        self.book = None
        self.book_path = None
        gc.collect()

        try:
            text = clean_text(self.storage.read_book(path))
            book = Book(title_from_name(path.split("/")[-1]), text)
            lpp = (BOTTOM - TOP) // self.settings["line_h"]
            book.layout(CHARS, lpp)
        except (OSError, ValueError, MemoryError) as e:
            self.show_message("CANNOT OPEN", [str(e)], back)
            return

        self.book = book
        self.book_path = path
        if page is None:
            page = self._progress(path).get("page", 1)
        try:
            page = int(page)
        except Exception:
            page = 1
        self.page = max(1, min(page, self.book.count()))
        self.screen = "reader"
        self._save_progress()
        self.draw_reader(True)

    def draw_reader(self, full=False):
        self.epd.clear(self.bg)
        marks = self._marks()
        star = "*" if self.page in marks else ""
        self.head(
            cut(self.book.title, 64),
            "%s%d/%d" % (star, self.page, self.book.count())
        )

        y = TOP
        for line in self.book.lines(self.page):
            self.txt(line, M, y)
            y += self.settings["line_h"]

        total = self.book.count()
        by = HEIGHT - 34
        self.epd.fb.rect(M, by, WIDTH - 2 * M, 6, self.fg)
        if total:
            w = (WIDTH - 2 * M - 2) * self.page // total
            if w > 0:
                self.epd.fb.fill_rect(M + 1, by + 1, w, 4, self.fg)

        self.hint("PRESS MENU   LEFT/RIGHT TURN   PAGE KEYS TURN")
        self.epd.show(not full and self.settings["fast"])

    def turn(self, delta):
        if not self.book:
            return
        p = max(1, min(self.page + delta, self.book.count()))
        if p == self.page:
            return
        self.page = p
        self._save_progress()
        self.draw_reader()

    def on_reader(self, ev):
        if ev in ("next", "right"):
            self.turn(1)
        elif ev in ("prev", "left"):
            self.turn(-1)
        elif ev == "press":
            self.open_menu()

    # ---- reader menu

    def menu_items(self):
        marks = self._marks()
        bm = "Remove bookmark" if self.page in marks else "Bookmark this page"
        return [
            "Resume", bm, "Jump to page", "Bookmarks",
            "Go to start", "Go to end", "Library", "Settings"
        ]

    def open_menu(self):
        self.menu_i = 0
        self.screen = "menu"
        self.draw_menu(True)

    def draw_menu(self, full=False):
        self.epd.clear(self.bg)
        self.head("BOOK MENU", "%d/%d" % (self.page, self.book.count()))
        items = self.menu_items()
        self.menu_i %= len(items)
        self.draw_list(items, self.menu_i, 48, 40, bar=30)
        self.hint("UP/DOWN MOVE   PRESS SELECT   LEFT BACK")
        self.epd.show(not full and self.settings["fast"])

    def on_menu(self, ev):
        n = len(self.menu_items())
        if ev == "up":
            self.menu_i = (self.menu_i - 1) % n
            self.draw_menu()
        elif ev == "down":
            self.menu_i = (self.menu_i + 1) % n
            self.draw_menu()
        elif ev == "left":
            self.screen = "reader"
            self.draw_reader(True)
        elif ev == "press":
            self.menu_action(self.menu_i)

    def menu_action(self, i):
        if i == 0:
            self.screen = "reader"
            self.draw_reader(True)
        elif i == 1:
            self.toggle_bookmark()
            self.draw_menu(True)
        elif i == 2:
            self.open_jump()
        elif i == 3:
            self.open_marks("menu")
        elif i == 4:
            self.go_page(1)
        elif i == 5:
            self.go_page(self.book.count())
        elif i == 6:
            self._save_progress()
            self.open_library()
        elif i == 7:
            self.open_settings("menu")

    def toggle_bookmark(self):
        marks = self.state["bookmarks"].get(self.book_path)
        if not isinstance(marks, list):
            marks = []
            self.state["bookmarks"][self.book_path] = marks
        if self.page in marks:
            marks.remove(self.page)
        else:
            marks.append(self.page)
            marks.sort()
        self._save_state()

    def go_page(self, p):
        p = max(1, min(int(p), self.book.count()))
        if p != self.page:
            self.page = p
            self._save_progress()
        self.screen = "reader"
        self.draw_reader(True)

    # ---- jump to page

    def open_jump(self):
        self.jump_to = self.page
        self.screen = "jump"
        self.draw_jump(True)

    def draw_jump(self, full=False):
        self.epd.clear(self.bg)
        self.head("JUMP TO PAGE", "%d/%d" % (self.page, self.book.count()))
        s = str(self.jump_to)
        x = (WIDTH - len(s) * 8) // 2
        self.epd.fb.rect(M, 120, WIDTH - 2 * M, 44, self.fg)
        self.txt(s, x, 138)
        self.txt("of %d" % self.book.count(), M, 184)
        self.hint("UP/DOWN 1   LEFT/RIGHT 10   PRESS GO   PREV CANCEL")
        self.epd.show(not full and self.settings["fast"])

    def on_jump(self, ev):
        if ev == "up":
            self.jump_to = min(self.book.count(), self.jump_to + 1)
            self.draw_jump()
        elif ev == "down":
            self.jump_to = max(1, self.jump_to - 1)
            self.draw_jump()
        elif ev == "right":
            self.jump_to = min(self.book.count(), self.jump_to + 10)
            self.draw_jump()
        elif ev == "left":
            self.jump_to = max(1, self.jump_to - 10)
            self.draw_jump()
        elif ev == "press":
            self.go_page(self.jump_to)
        elif ev == "prev":
            self.screen = "menu"
            self.draw_menu(True)

    # ---- bookmarks

    def build_marks(self):
        out = []
        for path, pages in self.state["bookmarks"].items():
            if not isinstance(pages, list):
                continue
            name = title_from_name(path.split("/")[-1])
            for p in pages:
                try:
                    p = int(p)
                except Exception:
                    continue
                out.append((name, path, p))
        out.sort(key=lambda x: (x[0].lower(), x[2]))
        return out

    def open_marks(self, back="home"):
        self.marks_back = back
        self.mark_items = self.build_marks()
        self.mark_i = 0
        self.mark_top = 0
        self.screen = "marks"
        self.draw_marks(True)

    def draw_marks(self, full=False):
        self.epd.clear(self.bg)
        self.head("BOOKMARKS", "%d" % len(self.mark_items))

        if not self.mark_items:
            self.txt("No bookmarks yet.", M, 100)
            self.txt("Press right in the reader menu to add one.", M, 124)
        else:
            if self.mark_i >= len(self.mark_items):
                self.mark_i = len(self.mark_items) - 1
            if self.mark_i < self.mark_top:
                self.mark_top = self.mark_i
            if self.mark_i >= self.mark_top + 10:
                self.mark_top = self.mark_i - 9

            labels = []
            for i in range(10):
                idx = self.mark_top + i
                if idx >= len(self.mark_items):
                    break
                name, path, page = self.mark_items[idx]
                labels.append("%s  p.%d" % (cut(name, 60), page))
            self.draw_list(
                labels, self.mark_i - self.mark_top,
                44, 40, bar=30, off=4
            )

        if self.mark_items:
            self.hint("UP/DOWN MOVE   PRESS OPEN   RIGHT REMOVE   LEFT BACK")
        else:
            self.hint("LEFT BACK")
        self.epd.show(not full and self.settings["fast"])

    def on_marks(self, ev):
        n = len(self.mark_items)
        if ev == "up" and n:
            self.mark_i = (self.mark_i - 1) % n
            self.draw_marks()
        elif ev == "down" and n:
            self.mark_i = (self.mark_i + 1) % n
            self.draw_marks()
        elif ev == "next" and n:
            self.mark_i = min(n - 1, self.mark_i + 10)
            self.draw_marks()
        elif ev == "prev" and n:
            self.mark_i = max(0, self.mark_i - 10)
            self.draw_marks()
        elif ev == "press" and n:
            name, path, page = self.mark_items[self.mark_i]
            self.open_book(path, page=page, back=self.marks_back)
        elif ev == "right" and n:
            name, path, page = self.mark_items[self.mark_i]
            pages = self.state["bookmarks"].get(path)
            if isinstance(pages, list) and page in pages:
                pages.remove(page)
            if not pages:
                self.state["bookmarks"].pop(path, None)
            self._save_state()
            self.open_marks(self.marks_back)
        elif ev == "left":
            if self.marks_back == "menu" and self.book:
                self.screen = "menu"
                self.draw_menu(True)
            else:
                self.open_home()

    # ---- settings

    def setting_rows(self):
        return [
            ("Theme", "Dark" if self.settings["theme"] == "dark" else "Light"),
            ("Page turn", "Fast" if self.settings["fast"] else "Quality"),
            ("Line spacing", "%d px" % self.settings["line_h"]),
            ("Full refresh", "Every %d" % self.settings["refresh_every"]),
            ("Library sort", "Recent" if self.settings["sort"] == "recent" else "Name"),
            ("Auto sleep", "Off" if not self.settings["auto_sleep"] else "%d min" % self.settings["auto_sleep"]),
            ("Back", ""),
        ]

    def open_settings(self, back="home"):
        self.return_to = back
        self.set_i = 0
        self.screen = "settings"
        self.draw_settings(True)

    def draw_settings(self, full=False):
        self.epd.clear(self.bg)
        self.head("SETTINGS")
        rows = self.setting_rows()
        self.set_i %= len(rows)
        names = [r[0] for r in rows]
        vals = [r[1] for r in rows]
        self.draw_list(names, self.set_i, 50, 40, vals, bar=30)
        self.hint("UP/DOWN MOVE   RIGHT CHANGE   LEFT BACK")
        self.epd.show(not full and self.settings["fast"])

    def on_settings(self, ev):
        n = len(self.setting_rows())
        if ev == "up":
            self.set_i = (self.set_i - 1) % n
            self.draw_settings()
        elif ev == "down":
            self.set_i = (self.set_i + 1) % n
            self.draw_settings()
        elif ev == "left":
            self.leave_settings()
        elif ev == "press":
            if self.set_i == n - 1:
                self.leave_settings()
            else:
                self.change_setting(1)
        elif ev == "right" and self.set_i < n - 1:
            self.change_setting(1)

    def change_setting(self, d):
        full = False

        if self.set_i == 0:
            self.settings["theme"] = (
                "dark" if self.settings["theme"] == "light" else "light"
            )
            self.theme()
            full = True
        elif self.set_i == 1:
            self.settings["fast"] = not self.settings["fast"]
        elif self.set_i == 2:
            vals = [12, 14, 16]
            i = vals.index(self.settings["line_h"])
            self.settings["line_h"] = vals[(i + d) % len(vals)]
            if self.book:
                oldn = self.book.count()
                oldp = self.page
                self._layout_book()
                if oldn:
                    self.page = max(1, min(
                        self.book.count(),
                        (oldp * self.book.count() + oldn - 1) // oldn
                    ))
                self._save_progress()
        elif self.set_i == 3:
            vals = [3, 6, 10, 20]
            i = vals.index(self.settings["refresh_every"])
            self.settings["refresh_every"] = vals[(i + d) % len(vals)]
            self.epd.fast_limit = self.settings["refresh_every"]
        elif self.set_i == 4:
            self.settings["sort"] = (
                "recent" if self.settings["sort"] == "name" else "name"
            )
        elif self.set_i == 5:
            vals = [0, 5, 10, 30]
            i = vals.index(self.settings["auto_sleep"])
            self.settings["auto_sleep"] = vals[(i + d) % len(vals)]

        self._save_settings()
        self.draw_settings(full)

    def leave_settings(self):
        if self.return_to == "menu" and self.book:
            self.screen = "menu"
            self.draw_menu(True)
        else:
            self.screen = "home"
            self.draw_home(True)

    # ---- about

    def open_about(self):
        self.screen = "about"
        self.draw_about(True)

    def draw_about(self, full=False):
        self.epd.clear(self.bg)
        self.head("ABOUT")
        sd = (
            "mounted, %d KB free" % self.storage.free_kb()
            if self.storage.ok else "not mounted"
        )
        try:
            rc = str(machine.reset_cause())
        except Exception:
            rc = "?"
        lines = [
            "Ohara-75 handheld reader",
            "ESP32-S3 / 7.5 inch UC8179",
            "800x480 1-bit e-paper",
            "",
            "MicroPython + framebuf + SDCard",
            "EPD sequences: Waveshare MIT driver",
            "",
            "SD: " + sd,
            "Heap: %d KB" % (gc.mem_free() // 1024),
            "Books: %d" % len(self.books),
            "Reset cause: " + rc,
            "",
            "Q1 must be G->GDR, S->RESE, D->switch.",
            "Battery sense is not wired on this board.",
        ]
        y = 48
        for ln in lines:
            self.txt(ln, M, y)
            y += 18
        self.hint("LEFT/PRESS BACK")
        self.epd.show(not full and self.settings["fast"])

    def on_about(self, ev):
        if ev in ("left", "press"):
            self.open_home()

    # ---- messages

    def show_message(self, title, lines, back="home"):
        self.message_back = back
        self.screen = "message"
        self.epd.clear(self.bg)
        self.head(title)
        y = 60
        for ln in wrap("\n".join(lines), CHARS):
            if y > HEIGHT - 40:
                break
            self.txt(ln, M, y)
            y += 18
        self.hint("PRESS/LEFT BACK")
        self.epd.show(False)

    def on_message(self, ev):
        if ev not in ("press", "left"):
            return
        if self.message_back == "library":
            self.open_library()
        elif self.message_back == "menu" and self.book:
            self.screen = "menu"
            self.draw_menu(True)
        else:
            self.open_home()

    def open_home(self):
        self.screen = "home"
        self.draw_home(True)

    # ---- sleep / loop

    def sleep_now(self):
        self._save_progress()
        self._save_settings()
        try:
            import esp32
            pins = tuple(self.buttons.pins.values())
            esp32.wake_on_ext1(pins, esp32.WAKEUP_ALL_LOW)
            self.epd.sleep()
            machine.deepsleep()
        except Exception as e:
            print("sleep failed:", e)
            self.last_input = time.ticks_ms()
            try:
                self.epd.init_full()
                self.open_home()
            except Exception:
                pass

    def run(self):
        handlers = {
            "home": self.on_home,
            "library": self.on_library,
            "reader": self.on_reader,
            "menu": self.on_menu,
            "jump": self.on_jump,
            "marks": self.on_marks,
            "settings": self.on_settings,
            "about": self.on_about,
            "message": self.on_message,
        }

        while True:
            feed()
            ev = self.buttons.get()
            if ev:
                self.last_input = time.ticks_ms()
                try:
                    handlers[self.screen](ev)
                except Exception as e:
                    print("app error:", e)
                    try:
                        self.show_message("ERROR", [str(e)], "home")
                    except Exception:
                        pass

            if self.settings["auto_sleep"]:
                idle = time.ticks_diff(time.ticks_ms(), self.last_input)
                if idle > self.settings["auto_sleep"] * 60000:
                    self.sleep_now()

            time.sleep_ms(8)


def main():
    gc.collect()
    start_wdt()
    App().run()


if __name__ == "__main__":
    main()
