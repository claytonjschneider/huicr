"""Dependency-free terminal UI. Git views stay frozen until an explicit refresh."""

import curses
import os
import signal
import time
import unicodedata

from .config import HuicrError
from .git import DiffLine
from .github import publish, reconcile_publication
from .herdr import call, identity, repo_for_pane, send
from .review import Review
from .theme import Theme


HELP = """huicr — personal code review

Scopes
  u  unstaged + untracked       U  all uncommitted (vs HEAD)
  i  staged                    b  branch (commits first)
  t  last agent turn           B  choose comparator / base
  o  open branch, SHA, BASE..HEAD, BASE...HEAD, or GitHub PR URL
  m  whole range / commit      , .  previous / next commit

Navigation
  Tab  cycle commits / files / diff     j k / arrows  move
  Enter  focus diff            g G  top / bottom
  Ctrl+D / Ctrl+U  half page    h l  horizontal scroll
  [ ]  previous / next hunk    { } or F f  previous / next file
  /  find in diff or blame     n N  next / previous match
  z  hide / show navigator     r  refresh captured review
  a  toggle full-file blame    H  switch blame old / new side

Comments (durable immediately)
  v  select a line range       c  comment on line / selection
  C  comment on file           e  edit comment at cursor
  d  delete comment at cursor  L  all comments + individual send
  R  mark file reviewed        A  choose agent recipient
  s  paste all unsent drafts   S  submit all unsent drafts
  P  post whole-PR drafts to GitHub (file comments in review summary)
  X  reconcile an uncertain agent delivery / GitHub publication
  In the comment editor: Enter newline, Ctrl+S save, Esc cancel

Comments keep the original commit, trees, side, line range, and snippet.
Sending keeps the pane open and marks only that version as delivered.
Editing a sent comment makes its new version pending again.
Agent and GitHub delivery are tracked independently. q closes; drafts survive.
"""


def clean(text):
    # Do not let file content or commit messages act as terminal escape codes.
    return "".join(ch if ch.isprintable() else " " for ch in str(text).expandtabs(4))


def clip(text, width):
    result = ""
    used = 0
    for char in clean(text):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        result += char
        used += size
    return result


def detect_target(text):
    if text.startswith("https://") or text.isdigit():
        return "pr"
    if ".." in text or (len(text) >= 7 and all(c in "0123456789abcdefABCDEF" for c in text)):
        return "range"
    return "branch"


def comment_status(comment):
    if comment["resolved"]:
        return "resolved"
    status = "agent draft" if comment["version"] > comment["sent_version"] else "agent sent"
    anchor = comment["anchor"]
    if anchor.get("pr") and anchor["scope"] == "pr" and not anchor["commit"]:
        version = comment["github_version"]
        status += " · GitHub " + ("posted" if version >= comment["version"] else "edited" if version else "draft")
    return status


def editor_layout(text, width, cursor=0):
    """Soft-wrap without changing text; retain a Unicode-cell-aware caret."""
    rows = [""]
    column = 0
    caret = (0, 0)
    for index, char in enumerate(text):
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if char != "\n" and column + cells > width:
            rows.append("")
            column = 0
        if index == cursor:
            caret = (len(rows) - 1, column)
        if char == "\n":
            rows.append("")
            column = 0
        else:
            rows[-1] += char
            column += cells
    if cursor == len(text):
        if column >= width:
            rows.append("")
            column = 0
        caret = (len(rows) - 1, column)
    return rows, caret


def edit_text(text, cursor, key):
    """Editing operations shared by inline comment boxes."""
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        if cursor:
            text, cursor = text[:cursor - 1] + text[cursor:], cursor - 1
    elif key == curses.KEY_DC:
        text = text[:cursor] + text[cursor + 1:]
    elif key == curses.KEY_LEFT:
        cursor = max(0, cursor - 1)
    elif key == curses.KEY_RIGHT:
        cursor = min(len(text), cursor + 1)
    elif key in (curses.KEY_HOME, "\x01"):
        cursor = text.rfind("\n", 0, cursor) + 1
    elif key in (curses.KEY_END, "\x05"):
        end = text.find("\n", cursor)
        cursor = len(text) if end < 0 else end
    elif key in (curses.KEY_UP, curses.KEY_DOWN):
        start = text.rfind("\n", 0, cursor) + 1
        column = cursor - start
        if key == curses.KEY_UP and start:
            previous = text.rfind("\n", 0, start - 1) + 1
            cursor = min(start - 1, previous + column)
        elif key == curses.KEY_DOWN:
            end = text.find("\n", cursor)
            if end >= 0:
                next_end = text.find("\n", end + 1)
                cursor = min(len(text) if next_end < 0 else next_end, end + 1 + column)
    elif key == "\x15":
        start = text.rfind("\n", 0, cursor) + 1
        text, cursor = text[:start] + text[cursor:], start
    elif key == "\x0b":
        end = text.find("\n", cursor)
        text = text[:cursor] + (text[end:] if end >= 0 else "")
    elif key == "\x17":
        prefix = text[:cursor].rstrip()
        boundary = max(prefix.rfind(" "), prefix.rfind("\n")) + 1
        text, cursor = text[:boundary] + text[cursor:], boundary
    elif isinstance(key, str) and (key.isprintable() or key in ("\n", "\r", "\t")):
        key = "\n" if key == "\r" else "    " if key == "\t" else key
        text = text[:cursor] + key + text[cursor:]
        cursor += len(key)
    return text, cursor


class UI:
    def __init__(self, screen, review, config):
        self.screen, self.review, self.config = screen, review, config
        self.store, self.repo = review.store, review.repo
        self.repo_key = str(self.repo.root)
        self.origin = review.origin
        self.file_index = self.cursor = self.top = self.horizontal = 0
        self.lines = []
        self.blame_rows = []
        self.blame_side = "new"
        self.blame = False
        self.selection = None
        self.editor = None
        self.editor_caret = None
        self.focus = "diff"
        self.navigator = True
        self.message = "? help · c comment · s/S agent · P GitHub"
        self.query = ""
        self.running = True
        self.last_poll = 0
        self.request_id = self.store.get(self.repo_key, "request", {}).get("id")
        self.comments = []
        self.theme = Theme()
        self.colors = self.theme.pairs
        curses.curs_set(0)
        # ncurses defaults to a one-second wait after Esc while looking for an
        # arrow/function-key sequence. Local key sequences arrive as one burst.
        curses.set_escdelay(25)
        screen.bkgd(" ", self.colors.get("normal", 0))
        screen.keypad(True)
        screen.timeout(250)

    @property
    def view(self):
        return self.review.view

    @property
    def file(self):
        if self.view and self.view.files:
            return self.view.files[self.file_index]
        return None

    def put(self, y, x, text, attr=None, width=None):
        height, columns = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < columns:
            width = min(width if width is not None else columns - x, columns - x)
            try:
                self.screen.addstr(y, x, clip(text, max(0, width)), self.colors.get("normal", 0) if attr is None else attr)
            except curses.error:
                pass  # resize/lower-right-cell race

    def box(self, y, x, height, width, title, active=False, interactive=False):
        if height < 2 or width < 3:
            return
        fill = self.colors.get("interactive" if interactive else "panel", 0)
        border = self.colors.get("popup_border" if interactive else "focus_border" if active else "border", 0)
        for row in range(y + 1, y + height - 1):
            self.put(row, x + 1, " " * (width - 2), fill, width - 2)
            self.put(row, x, "│", border)
            self.put(row, x + width - 1, "│", border)
        self.put(y, x, "┌" + "─" * (width - 2) + "┐", border, width)
        self.put(y + height - 1, x, "└" + "─" * (width - 2) + "┘", border, width)
        self.put(y, x + 2, " " + title + " ", border | curses.A_BOLD, width - 4)

    def row(self, y, x, width, text, attr):
        self.put(y, x, " " * max(0, width), attr, width)
        self.put(y, x, text, attr, width)

    def busy(self, message):
        self.message = message
        self.draw()
        self.screen.refresh()

    def reload(self):
        self.busy("Capturing review…")
        current = self.file.path if self.file else None
        self.review.load()
        self.file_index = next((i for i, f in enumerate(self.view.files) if f.path == current), 0)
        self.load_file()
        self.message = "Captured. r refreshes; comments stay anchored to this revision."

    def load_file(self, reset=True):
        self.file_index = min(max(0, self.file_index), max(0, len(self.view.files) - 1)) if self.view else 0
        if reset:
            self.cursor = self.top = self.horizontal = 0
        self.selection = None
        self.lines, self.blame_rows = [], []
        if self.file:
            try:
                if self.blame:
                    self.blame_rows = self.repo.blame(self.view, self.file, self.blame_side, self.config.max_file_bytes)
                    self.lines = [DiffLine(text, "context", old=i + 1 if self.blame_side == "old" else None,
                                           new=i + 1 if self.blame_side == "new" else None)
                                  for i, (_, _, text) in enumerate(self.blame_rows)]
                else:
                    self.lines = self.repo.diff(self.view, self.file, self.config.context_lines, self.config.max_file_bytes)
            except HuicrError as e:
                self.lines = [DiffLine(str(e))]
        self.cursor = min(self.cursor, max(0, len(self.lines) - 1))
        self.comments = self.store.comments(self.repo_key)

    def current_comments(self):
        return [c for c in self.comments if self.file and c["anchor"]["view"] == self.view.key
                and c["anchor"]["path"] == self.file.path and not c["resolved"]]

    def at_cursor(self):
        line = self.lines[self.cursor] if self.lines else None
        file_comment = None
        for c in self.current_comments():
            a = c["anchor"]
            if a["side"] == "file":
                file_comment = file_comment or c
                continue
            number = line.old if line and a["side"] == "old" else line.new if line else None
            if number is not None and a["start"] <= number <= a["end"]:
                return c
        return file_comment

    def draw_comment(self, y, x, height, width, inline):
        anchor = inline["anchor"]
        location = "file" if anchor["side"] == "file" else f"{anchor['side']}:{anchor['start']}-{anchor['end']}"
        title = f"COMMENT · {location}" + (f" · {inline['status']}" if inline.get("status") else "")
        self.box(y, x, height, width, title, interactive=True)
        rows, (cy, cx) = editor_layout(inline["text"], max(1, width - 4), inline.get("cursor", 0))
        room = max(1, height - 2)
        offset = max(0, cy - room + 1) if self.editor else 0
        for row, text in enumerate(rows[offset:offset + room], y + 1):
            self.put(row, x + 2, text, self.colors.get("interactive", 0), width - 4)
        if self.editor:
            self.editor_caret = (y + 1 + cy - offset, x + 2 + cx)

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 10 or width < 38:
            self.put(0, 0, "huicr — enlarge pane (minimum 38×10)")
            self.put(1, 0, "q closes; saved comments are retained")
            return
        drafts = sum(c["version"] > c["sent_version"] and not c["resolved"] for c in self.comments)
        recipient = self.origin["pane_id"] if self.origin else "A: choose agent"
        self.put(0, 0, f" huicr  {self.repo.root.name}  ·  {drafts} drafts  → {recipient}", curses.A_BOLD | self.colors.get("accent", 0))
        self.put(1, 0, f" u unstaged  b branch  t turn  o range/PR  B base: {self.review.base or 'auto'}")
        label = self.view.label if self.view else "No review loaded — choose a scope or comparator"
        if self.review.commits:
            label = f"[{self.review.commit_index + 1}/{len(self.review.commits)}] " + label
        self.put(2, 0, f" {label}", self.colors.get("dim", 0))
        nav = min(38, max(22, width // 4)) if self.navigator and width >= 65 else 0
        box_height = height - 7
        body = max(1, box_height - 2)
        if nav:
            commit_height = min(max(3, box_height // 3), len(self.review.commits) + 2) if self.review.commits and box_height >= 8 else 0
            if commit_height:
                self.box(3, 0, commit_height, nav - 1, "COMMITS  , .", self.focus == "commits")
                room = commit_height - 2
                offset = max(0, self.review.commit_index - room + 1)
                for row, c in enumerate(self.review.commits[offset:offset + room], 4):
                    index = row - 4 + offset
                    attr = self.colors.get("selected" if index == self.review.commit_index and not self.review.whole else "panel", 0)
                    self.row(row, 1, nav - 3, f" {c.oid[:7]} {c.subject}", attr)
            file_y = 3 + commit_height + (1 if commit_height else 0)
            file_height = box_height - commit_height - (1 if commit_height else 0)
            self.box(file_y, 0, file_height, nav - 1, "FILES  { }", self.focus == "files")
            files = self.view.files if self.view else []
            room = max(0, file_height - 2)
            offset = max(0, self.file_index - room + 1)
            for row, file in enumerate(files[offset:offset + room], file_y + 1):
                index = row - file_y - 1 + offset
                marked = self.store.get(self.repo_key, f"reviewed:{self.view.key}:{file.path}", False)
                attr = self.colors.get("selected" if index == self.file_index else "panel", 0)
                self.row(row, 1, nav - 3, f" {'✓' if marked else file.status[0]} {file.path}", attr)
        heading = self.file.path if self.file else "No changed files"
        if self.blame:
            heading += f"  [blame: {self.blame_side}; H switches side]"
        self.box(3, nav, box_height, width - nav, heading, self.focus == "diff")
        content_x = nav + 1
        content_width = width - nav - 2
        comment = self.at_cursor()
        inline = self.editor
        if not inline and comment:
            inline = {"anchor": comment["anchor"], "text": comment["body"],
                      "status": comment_status(comment)}
        inline_height = 0
        if inline and body >= 7:
            wrapped, _ = editor_layout(inline["text"], max(1, content_width - 4), inline.get("cursor", 0))
            # A blank comment is exactly one input row plus its two borders.
            # Grow only when content wraps; retain at least two rows of code.
            inline_height = min(max(3, len(wrapped) + 2), min(14, body - 2))
        code_room = max(1, body - inline_height)
        self.top = max(0, min(self.top, self.cursor))
        if self.cursor >= self.top + code_room:
            self.top = self.cursor - code_room + 1
        relevant = self.current_comments()
        row = 4
        if inline_height and inline["anchor"]["side"] == "file":
            self.draw_comment(row, content_x, inline_height, content_width, inline)
            row += inline_height
        for index in range(self.top, min(len(self.lines), self.top + code_room)):
            line = self.lines[index]
            attr = self.colors.get(line.kind, self.colors.get("panel", 0))
            selected = self.selection is not None and min(self.selection, self.cursor) <= index <= max(self.selection, self.cursor)
            if selected or index == self.cursor:
                attr = self.colors.get("selected", curses.A_BOLD)
            annotated = any((c["anchor"]["side"] == "file" and index == 0) or
                            (c["anchor"]["start"] is not None and
                             (line.old if c["anchor"]["side"] == "old" else line.new) is not None and
                             c["anchor"]["start"] <= (line.old if c["anchor"]["side"] == "old" else line.new) <= c["anchor"]["end"])
                            for c in relevant)
            prefix = "●" if annotated else " "
            if self.blame:
                oid, author, _ = self.blame_rows[index] if index < len(self.blame_rows) else ("", "", "")
                gutter = f"{prefix}{index + 1:4} {oid[:8]:8} {author[:12]:12} │ "
            else:
                sign = {"add": "+", "delete": "-", "context": " "}.get(line.kind, " ")
                gutter = f"{prefix}{str(line.old or ''):>4} {str(line.new or ''):>4} {sign} "
            self.row(row, content_x, content_width, gutter + line.text[self.horizontal:], attr)
            row += 1
            if inline_height and inline["anchor"]["side"] != "file" and index == self.cursor:
                self.draw_comment(row, content_x, inline_height, content_width, inline)
                row += inline_height
        hint = "Ctrl+S save · Enter newline · Esc cancel" if self.editor else self.message
        self.put(height - 3, 0, f" {hint}", self.colors.get("comment", 0))
        self.put(height - 2, 0, " c comment  C file  v select  L comments  s paste  S submit  P GitHub")
        self.put(height - 1, 0, " Tab focus  j/k move  ,/. commits  m whole  r refresh  ? help  q close", self.colors.get("dim", 0))

    def modal(self, title, rows, index=0, extra=None):
        """Return (key, selected-index). Escape and q cancel."""
        while self.running:
            self.draw()
            height, width = self.screen.getmaxyx()
            box_width = max(4, width - 4)
            box_height = max(4, min(height - 2, len(rows) + 4))
            box_y, box_x = max(0, (height - box_height) // 2), 2
            self.box(box_y, box_x, box_height, box_width, title, interactive=True)
            room = max(1, box_height - 3)
            offset = max(0, index - room + 1)
            for y, row in enumerate(rows[offset:offset + room], box_y + 1):
                attr = self.colors.get("selected" if y - box_y - 1 + offset == index else "interactive", 0)
                self.row(y, box_x + 1, box_width - 2, row, attr)
            self.put(box_y + box_height - 2, box_x + 1, extra or " j/k move · Enter choose · Esc cancel",
                     self.colors.get("interactive", 0), box_width - 2)
            self.screen.refresh()
            key = self.get_key()
            if key in ("q", "\x1b"):
                return None, index
            if key in ("j", curses.KEY_DOWN):
                index = min(max(0, len(rows) - 1), index + 1)
            elif key in ("k", curses.KEY_UP):
                index = max(0, index - 1)
            elif key in ("\x04", curses.KEY_NPAGE):
                index = min(max(0, len(rows) - 1), index + room)
            elif key in ("\x15", curses.KEY_PPAGE):
                index = max(0, index - room)
            elif key is not None and key != curses.KEY_RESIZE:
                return key, index
        return None, index

    def prompt(self, title, initial="", multiline=False):
        text, cursor = initial, len(initial)
        curses.curs_set(1)
        try:
            while self.running:
                self.draw()
                height, width = self.screen.getmaxyx()
                box_width = max(6, min(width - 4, 110))
                box_height = max(5, height - 6 if multiline else 6)
                box_y, box_x = max(0, (height - box_height) // 2), max(0, (width - box_width) // 2)
                self.box(box_y, box_x, box_height, box_width, title, interactive=True)
                usable = max(2, box_width - 4)
                # Character wrapping keeps caret addressing deterministic. Curses
                # handles wide glyphs; a resize never changes the stored text.
                before = text[:cursor]
                logical = text.split("\n")
                visual = []
                for line in logical:
                    visual.extend([line[i:i + usable] for i in range(0, len(line), usable)] or [""])
                prior = before.split("\n")
                cy = sum(max(1, (len(line) + usable - 1) // usable) for line in prior[:-1]) + len(prior[-1]) // usable
                cx = len(prior[-1]) % usable
                room = max(1, box_height - 4)
                offset = max(0, cy - room + 1)
                for y, line in enumerate(visual[offset:offset + room], box_y + 1):
                    self.put(y, box_x + 2, line, self.colors.get("interactive", 0), usable)
                self.put(box_y + box_height - 2, box_x + 1, " Ctrl+S save · Enter newline · Esc cancel" if multiline else " Enter accept · Esc cancel",
                         self.colors.get("interactive", 0), box_width - 2)
                try:
                    self.screen.move(min(height - 2, box_y + cy - offset + 1), min(width - 2, box_x + cx + 2))
                except curses.error:
                    pass
                self.screen.refresh()
                key = self.get_key()
                if key == "\x1b":
                    return None
                if key == "\x13" or (not multiline and key in ("\n", "\r", curses.KEY_ENTER)):
                    return text.strip()
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if cursor:
                        text = text[:cursor - 1] + text[cursor:]
                        cursor -= 1
                elif key == curses.KEY_DC:
                    text = text[:cursor] + text[cursor + 1:]
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(text), cursor + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = text.rfind("\n", 0, cursor) + 1
                elif key in (curses.KEY_END, "\x05"):
                    next_line = text.find("\n", cursor)
                    cursor = next_line if next_line >= 0 else len(text)
                elif key == "\x15":
                    text, cursor = text[cursor:], 0
                elif key == "\x17":
                    prefix = text[:cursor].rstrip()
                    boundary = max(prefix.rfind(" "), prefix.rfind("\n")) + 1
                    text, cursor = text[:boundary] + text[cursor:], boundary
                elif isinstance(key, str) and (key.isprintable() or (multiline and key in ("\n", "\r"))):
                    if key == "\r":
                        key = "\n"
                    text = text[:cursor] + key + text[cursor:]
                    cursor += len(key)
        finally:
            curses.curs_set(0)

    def get_key(self):
        try:
            return self.screen.get_wch()
        except curses.error:
            return None

    def comment_editor(self, anchor, initial=""):
        if self.screen.getmaxyx()[0] < 16 or self.screen.getmaxyx()[1] < 44:
            raise HuicrError("Enlarge the pane to at least 44×16 to write an inline comment")
        self.editor = {"text": initial, "cursor": len(initial), "anchor": anchor}
        self.focus = "diff"
        curses.curs_set(1)
        try:
            while self.running:
                self.editor_caret = None
                self.draw()
                if self.editor_caret:
                    try:
                        self.screen.move(*self.editor_caret)
                    except curses.error:
                        pass
                self.screen.refresh()
                key = self.get_key()
                if key == "\x1b":
                    return None
                if key == "\x13":
                    return self.editor["text"].strip()
                self.editor["text"], self.editor["cursor"] = edit_text(self.editor["text"], self.editor["cursor"], key)
        finally:
            self.editor = None
            self.editor_caret = None
            curses.curs_set(0)

    def save_ui(self):
        if not self.view:
            return
        self.store.put(self.repo_key, "ui", {"scope": self.review.scope, "base": self.review.base,
            "target": self.review.target, "whole": self.review.whole, "commit_index": self.review.commit_index,
            "file": self.file.path if self.file else None, "cursor": self.cursor,
            "navigator": self.navigator})

    def switch(self, scope, target=None, base=None):
        candidate = Review(self.repo, self.store, scope, base if base is not None else self.review.base,
                           target, self.origin)
        self.busy(f"Loading {scope}…")
        candidate.load()  # a failed request leaves the previous view intact
        self.review = candidate
        self.file_index = 0
        self.focus = "diff"
        self.blame = False
        self.load_file()
        self.message = "Commit-wise review · ,/. step commits · m toggles whole range" if candidate.commits else "Review loaded"

    def open_target(self, target):
        self.switch(detect_target(target), target)

    def move_commit(self, delta):
        if not self.review.commits:
            raise HuicrError("Choose branch, commit range, or PR first")
        self.review.commit_index = min(max(0, self.review.commit_index + delta), len(self.review.commits) - 1)
        self.review.whole = False
        self.review.select_view()
        self.file_index = 0
        self.load_file()

    def move_file(self, delta):
        if self.view:
            self.file_index = min(max(0, self.file_index + delta), max(0, len(self.view.files) - 1))
            self.load_file()

    def move(self, delta):
        if self.focus == "files":
            self.move_file(delta)
        elif self.focus == "commits":
            self.move_commit(delta)
        else:
            self.cursor = min(max(0, self.cursor + delta), max(0, len(self.lines) - 1))

    def edit_comment(self, comment=None, file_level=False):
        if not self.file:
            raise HuicrError("No file selected")
        if comment:
            anchor = comment["anchor"]
        else:
            anchor = self.review.anchor(self.file, self.lines,
                None if file_level else self.cursor, self.selection,
                self.blame_side if self.blame else None)
        body = self.comment_editor(anchor, comment["body"] if comment else "")
        if body:
            if comment:
                self.store.edit(comment["id"], body)
            else:
                self.store.add(self.repo_key, anchor, body)
            self.selection = None
            self.comments = self.store.comments(self.repo_key)
            self.message = "Comment saved · s/S sends to agent · P posts whole-PR drafts to GitHub"

    def deliver(self, mode, ids=None):
        if not self.origin:
            self.choose_agent()
        self.busy(f"Sending {mode} feedback…")
        delivery = send(self.store, self.repo_key, self.origin, mode, ids)
        self.comments = self.store.comments(self.repo_key)
        self.message = f"{'Pasted — press Enter in agent' if mode == 'paste' else 'Submitted'} · batch {delivery[:8]} · review still open"

    def post_to_github(self, ids=None):
        pr = self.store.comment(ids[0])["anchor"].get("pr") if ids else self.view.pr if self.view else None
        if not pr:
            raise HuicrError("Open a GitHub PR (o), switch to its whole diff (m), and add comments to publish.")
        self.busy("Posting whole-PR comments to GitHub…")
        url = publish(self.repo, self.store, pr["url"], ids)
        self.comments = self.store.comments(self.repo_key)
        self.message = f"GitHub review: {url}"

    def choose_agent(self):
        agents = call("agent", "list")["agents"]
        candidates = []
        for agent in agents:
            if agent["pane_id"] == os.environ.get("HERDR_PANE_ID"):
                continue
            try:
                if repo_for_pane(agent).root == self.repo.root:
                    candidates.append(agent)
            except HuicrError:
                continue
        if not candidates:
            raise HuicrError("No agents in this worktree")
        rows = [f" {a['pane_id']}  {a.get('agent')}  {a.get('agent_status')}  {a.get('terminal_title_stripped', '')}" for a in candidates]
        key, index = self.modal("Choose feedback recipient (same worktree)", rows)
        if key in ("\n", "\r", curses.KEY_ENTER):
            self.origin = identity(candidates[index], self.repo)
            self.review.origin = self.origin
            self.message = f"Recipient: {self.origin['pane_id']}"

    def show_comments(self):
        index = 0
        while self.running:
            comments = self.store.comments(self.repo_key)
            if not comments:
                self.message = "No saved comments"
                return
            rows = []
            for c in comments:
                a = c["anchor"]
                status = comment_status(c)
                rows.append(f" {status}  {a.get('commit', '')[:8] or a['scope']} {a['path']}:{a.get('start') or 'file'}  {c['body']}")
            key, index = self.modal("Saved comments — Enter opens the original captured revision", rows,
                min(index, len(rows) - 1), " Enter view · e edit · d delete · x resolve · s/S agent · P GitHub · Esc back")
            if key is None:
                break
            comment = comments[index]
            if key in ("\n", "\r", curses.KEY_ENTER, "e"):
                anchor = comment["anchor"]
                self.review.restore_anchor(anchor)
                self.file_index = next((i for i, file in enumerate(self.view.files) if file.path == anchor["path"]), 0)
                self.blame = False
                self.load_file()
                self.cursor = next((i for i, line in enumerate(self.lines)
                                    if (line.old if anchor["side"] == "old" else line.new) == anchor["start"]), 0)
                self.focus = "diff"
                self.message = "Original comment snapshot · r returns to a refreshed review"
                if key == "e":
                    self.edit_comment(comment)
                break
            if key == "x":
                self.store.resolve(comment["id"], not comment["resolved"])
            elif key == "d":
                self.store.delete(comment["id"])
            elif key in ("s", "S"):
                self.deliver("paste" if key == "s" else "submit", [comment["id"]])
            elif key == "P":
                self.post_to_github([comment["id"]])
        self.comments = self.store.comments(self.repo_key)

    def reconcile(self):
        github = self.store.github_deliveries(self.repo_key, unresolved=True)
        with self.store.lock(f"delivery:{self.repo_key}"):
            deliveries = self.store.deliveries(self.repo_key, unresolved=True)
            if not deliveries and not github:
                self.message = "No uncertain deliveries"
                return
            for delivery in deliveries:
                answer = self.prompt(f"Batch {delivery['id'][:8]}: check agent, then type 'sent' or 'retry' (Esc leaves it pending)")
                if answer in ("sent", "retry"):
                    self.store.finish(delivery["id"], "sent" if answer == "sent" else "failed", "manually reconciled")
        for delivery in github:
            self.busy("Checking GitHub for the interrupted review…")
            url = reconcile_publication(self.repo, self.store, delivery["pr"])
            if url:
                self.message = f"GitHub review recovered: {url}"
                continue
            self.message = f"No matching review found. Check {delivery['pr']}"
            answer = self.prompt("After checking the PR, type 'retry' to allow another post (Esc keeps it pending)")
            if answer == "retry":
                url = reconcile_publication(self.repo, self.store, delivery["pr"], retry=True)
                self.message = f"GitHub review recovered: {url}" if url else "Retry enabled. P posts the unpublished whole-PR comments."
        self.comments = self.store.comments(self.repo_key)

    def search(self, backwards=False):
        if not self.query or not self.lines:
            return
        direction = -1 if backwards else 1
        for offset in range(1, len(self.lines) + 1):
            index = (self.cursor + offset * direction) % len(self.lines)
            if self.query.casefold() in self.lines[index].text.casefold():
                self.cursor = index
                self.focus = "diff"
                return
        self.message = f"Not found: {self.query}"

    def handle(self, key):
        if key == "q":
            self.running = False
        elif key == "?":
            self.modal("huicr shortcuts — q / Esc back", HELP.splitlines())
        elif key in ("j", curses.KEY_DOWN):
            self.move(1)
        elif key in ("k", curses.KEY_UP):
            self.move(-1)
        elif key in ("\x04", curses.KEY_NPAGE):
            self.move(max(1, (self.screen.getmaxyx()[0] - 8) // 2))
        elif key in ("\x15", curses.KEY_PPAGE):
            self.move(-max(1, (self.screen.getmaxyx()[0] - 8) // 2))
        elif key in ("g", curses.KEY_HOME):
            self.move(-10000000)
        elif key in ("G", curses.KEY_END):
            self.move(10000000)
        elif key == "\t":
            focuses = (["commits"] if self.review.commits else []) + ["files", "diff"]
            self.focus = focuses[(focuses.index(self.focus) + 1) % len(focuses)]
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.focus = "diff"
        elif key in ("h", curses.KEY_LEFT):
            self.horizontal = max(0, self.horizontal - 8)
        elif key in ("l", curses.KEY_RIGHT):
            self.horizontal += 8
        elif key in ("u", "U", "i", "b", "t"):
            self.switch({"u": "unstaged", "U": "worktree", "i": "staged", "b": "branch", "t": "turn"}[key])
        elif key == "B":
            refs = self.repo.refs()
            answer = self.prompt("Comparator: Git ref (examples: " + ", ".join(refs[:6]) + ")", self.review.base)
            if answer:
                self.repo.resolve(answer)
                self.store.put(self.repo_key, "base", answer)
                self.review.base = answer
                self.switch("branch", self.review.target if self.review.scope == "branch" else "HEAD", answer)
        elif key == "o":
            target = self.prompt("Open branch, commit, BASE..HEAD, BASE...HEAD, or GitHub PR URL")
            if target:
                self.open_target(target)
        elif key == "m" and self.review.scope in ("branch", "range", "pr"):
            self.review.whole = not self.review.whole
            self.review.select_view()
            self.file_index = 0
            self.load_file()
        elif key in (",", "."):
            self.move_commit(-1 if key == "," else 1)
        elif key in ("{", "}", "f", "F"):
            self.move_file(-1 if key in ("{", "F") else 1)
        elif key in ("[", "]"):
            direction = -1 if key == "[" else 1
            indices = range(self.cursor + 1, len(self.lines)) if direction == 1 else range(self.cursor - 1, -1, -1)
            self.cursor = next((i for i in indices if self.lines[i].kind == "hunk"), self.cursor)
            self.focus = "diff"
        elif key == "v":
            self.selection = self.cursor if self.selection is None else None
            self.focus = "diff"
        elif key == "\x1b":
            self.selection = None
        elif key in ("c", "C"):
            self.edit_comment(file_level=key == "C")
        elif key == "e":
            comment = self.at_cursor()
            if comment:
                self.edit_comment(comment)
        elif key == "d":
            comment = self.at_cursor()
            if comment:
                self.store.delete(comment["id"])
                self.comments = self.store.comments(self.repo_key)
        elif key == "L":
            self.show_comments()
        elif key == "R" and self.file:
            state_key = f"reviewed:{self.view.key}:{self.file.path}"
            self.store.put(self.repo_key, state_key, not self.store.get(self.repo_key, state_key, False))
        elif key in ("s", "S"):
            self.deliver("paste" if key == "s" else "submit")
        elif key == "P":
            self.post_to_github()
        elif key == "A":
            self.choose_agent()
        elif key == "X":
            self.reconcile()
        elif key == "a":
            line = self.lines[self.cursor] if self.lines else None
            self.blame_side = "old" if line and line.kind == "delete" else "new"
            number = (line.old if self.blame_side == "old" else line.new) if line else None
            self.blame = not self.blame
            self.load_file()
            if self.blame and number:
                self.cursor = min(number - 1, max(0, len(self.lines) - 1))
        elif key == "H" and self.blame:
            self.blame_side = "old" if self.blame_side == "new" else "new"
            self.load_file()
        elif key == "r":
            if self.review.scope == "pr":
                self.review.right = ""  # fetch a fresh, internally consistent PR
            self.reload()
        elif key == "z":
            self.navigator = not self.navigator
        elif key == "/":
            query = self.prompt("Find in diff / blame", self.query)
            if query:
                self.query = query
                self.search()
        elif key in ("n", "N"):
            self.search(key == "N")

    def loop(self, saved=None):
        try:
            self.reload()
            if saved:
                self.navigator = saved.get("navigator", True)
                self.file_index = next((i for i, f in enumerate(self.view.files) if f.path == saved.get("file")), 0)
                self.load_file()
                self.cursor = min(saved.get("cursor", 0), max(0, len(self.lines) - 1))
        except HuicrError as e:
            self.message = str(e)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        try:
            while self.running:
                try:
                    if time.monotonic() - self.last_poll > 1:
                        self.comments = self.store.comments(self.repo_key)
                        request = self.store.get(self.repo_key, "request", {})
                        if request.get("id") != self.request_id:
                            self.request_id = request.get("id")
                            if request.get("origin"):
                                self.origin = request["origin"]
                                self.review.origin = self.origin
                            if any(k in request for k in ("scope", "target", "base")):
                                self.switch(request.get("scope", "branch" if request.get("base") else "unstaged"),
                                            request.get("target"), request.get("base"))
                        self.last_poll = time.monotonic()
                    self.draw()
                    self.screen.refresh()
                    key = self.get_key()
                    if key is not None:
                        self.handle(key)
                        self.save_ui()
                except HuicrError as e:
                    self.message = str(e)
        finally:
            self.save_ui()
            signal.signal(signal.SIGTERM, previous)
            self.theme.restore()


def launch(review, config, saved=None):
    # Disable software flow control so Ctrl+S is usable in the comment editor.
    import termios
    import sys
    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    changed = list(settings)
    changed[0] &= ~(termios.IXON | termios.IXOFF)
    termios.tcsetattr(fd, termios.TCSANOW, changed)
    try:
        curses.wrapper(lambda screen: UI(screen, review, config).loop(saved))
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, settings)
