"""Terminal key decoding shared by all text boxes and the review pane."""

from collections import deque
from contextlib import contextmanager
import curses
from dataclasses import dataclass


SHIFT_ENTER = -1000
ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)


@dataclass(frozen=True)
class Paste:
    text: str


def modified_key(code, modifiers=1, event=1):
    if event == 3:  # Release events must not finish a second form.
        return None
    modifiers -= 1
    if code in (13, 57414):  # Enter / keypad Enter
        return SHIFT_ENTER if modifiers & 1 else "\r"
    if code in (8, 127):
        return "\x17" if modifiers & 6 else "\x7f"
    if modifiers & 4 and 64 <= code <= 127:
        return chr(code & 31)
    if modifiers & 62 or 57344 <= code <= 63743:
        return None
    char = chr(code)
    return char.upper() if modifiers & 1 else char


def decode_sequence(sequence):
    try:
        if sequence.endswith("u"):
            fields = sequence[2:-1].split(";")
            code = int(fields[0].split(":")[0])
            mods = fields[1].split(":") if len(fields) > 1 else ["1"]
            modifiers = int(mods[0] or "1")
            event = int(mods[1]) if len(mods) > 1 else 1
            if event != 3 and len(fields) > 2 and fields[2]:
                text = "".join(chr(int(n)) for n in fields[2].split(":"))
                return text if text.isprintable() else None
            return modified_key(code, modifiers, event)
        if sequence.startswith("\x1b[27;") and sequence.endswith("~"):
            _, modifiers, code = sequence[2:-1].split(";")
            return modified_key(int(code), int(modifiers))  # xterm modifyOtherKeys
        if sequence[-1] in "ABCDHF":
            return {"A": curses.KEY_UP, "B": curses.KEY_DOWN, "C": curses.KEY_RIGHT,
                    "D": curses.KEY_LEFT, "H": curses.KEY_HOME, "F": curses.KEY_END}[sequence[-1]]
        if sequence == "\x1bOM":
            return curses.KEY_ENTER
        if sequence.endswith("~"):
            return {1: curses.KEY_HOME, 3: curses.KEY_DC, 4: curses.KEY_END,
                    5: curses.KEY_PPAGE, 6: curses.KEY_NPAGE, 7: curses.KEY_HOME,
                    8: curses.KEY_END}.get(int(sequence[2:-1].split(";")[0]))
    except (ValueError, OverflowError):
        pass
    return None


class KeyReader:
    def __init__(self, screen):
        self.screen = screen
        self.pending = deque()
        self.sequence = ""
        self.paste = None
        self.paste_tail = ""

    def character(self):
        try:
            return self.screen.get_wch()
        except curses.error:
            return None

    def read(self):
        key = self.next_key()
        if key == "\x03":
            raise KeyboardInterrupt()
        return key

    def next_key(self):
        if self.pending:
            return self.pending.popleft()
        if self.paste is not None:
            return self.read_paste()
        if self.sequence:
            return self.read_sequence()
        key = self.character()
        if key != "\x1b":
            return key
        self.screen.timeout(25)
        try:
            following = self.character()
        finally:
            self.screen.timeout(250)
        if following is None:
            return key
        if following in ("\x7f", "\b", curses.KEY_BACKSPACE):
            return "\x17"  # Option/Alt+Backspace in legacy terminals
        if following in ENTER_KEYS:
            return SHIFT_ENTER  # Alt+Enter is a legacy newline fallback.
        if following in ("[", "O"):
            self.sequence = key + following
            return self.read_sequence()
        self.pending.append(following)
        return key

    def read_sequence(self):
        self.screen.timeout(25)
        try:
            while len(self.sequence) < 64:
                char = self.character()
                if char is None or not isinstance(char, str):
                    return char  # Retain partial sequences across reads/resizes.
                self.sequence += char
                if "@" <= char <= "~":
                    sequence, self.sequence = self.sequence, ""
                    if sequence == "\x1b[200~":
                        self.paste, self.paste_tail = [], ""
                        self.screen.keypad(False)  # Paste is literal text, not keys.
                        return self.read_paste()
                    return decode_sequence(sequence)
            self.sequence = ""
            return None
        finally:
            self.screen.timeout(250)

    def read_paste(self):
        while True:
            char = self.character()
            if char is None or not isinstance(char, str):
                return char
            self.paste.append(char)
            self.paste_tail = (self.paste_tail + char)[-6:]
            if self.paste_tail == "\x1b[201~":
                text = "".join(self.paste[:-6])
                self.paste = None
                self.screen.keypad(True)
                return Paste(text)


@contextmanager
def enhanced_input(stream):
    # Push/pop keyboard mode on the alternate screen. Supporting terminals can
    # distinguish Shift+Enter; legacy terminals ignore the enhancement request.
    stream.write("\x1b[>1u\x1b[?2004h")
    stream.flush()
    try:
        yield
    finally:
        stream.write("\x1b[?2004l\x1b[<u")
        stream.flush()
