import unittest
from unittest.mock import Mock, patch

from huicr.config import Config
from huicr.ui import UI, cell_width, clean, wrap_text
from test_review import RepositoryFixture


class WrapTextTest(unittest.TestCase):
    def test_unicode_tabs_and_controls_wrap_by_cells_without_losing_text(self):
        text = "\t界e\u0301界\x1b[31m long text"
        rows = wrap_text(text, 5)
        self.assertEqual("".join(rows), clean(text))
        self.assertTrue(all(sum(cell_width(char) for char in row) <= 5 for row in rows))
        self.assertFalse(any(row.startswith("\u0301") for row in rows))
        self.assertNotIn("\x1b", "".join(rows))
        self.assertEqual(wrap_text("abcd", 4), ["abcd"])
        self.assertEqual(wrap_text("", 4), [""])


class WrappingTest(RepositoryFixture):
    def make_ui(self, text, width=52, height=18):
        self.write("file.txt", text + "\nsecond\nthird\n")
        review = self.review()
        screen = Mock()
        screen.getmaxyx.return_value = height, width
        with patch("huicr.ui.curses.curs_set"), patch("huicr.ui.curses.set_escdelay"), patch("huicr.ui.Theme") as theme:
            theme.return_value.pairs = {"selected": 1, "add": 2}
            ui = UI(screen, review, Config())
        ui.load_file()
        ui.cursor = next(i for i, line in enumerate(ui.lines) if line.kind == "add")
        return ui

    def render(self, ui):
        with patch.object(ui, "row", wraps=ui.row) as rows:
            ui.draw()
        content_x = ui.navigator_width(ui.screen.getmaxyx()[1]) + 1
        return [call.args for call in rows.call_args_list if call.args[1] == content_x]

    def test_scrolls_through_a_line_taller_than_the_pane_and_comments_on_source_line(self):
        text = "x" * 370 + " WRAP_END"
        ui = self.make_ui(text, height=14)
        source_line = ui.cursor
        self.assertTrue(ui.wrap)
        self.assertNotIn("WRAP_END", "\n".join(row[3] for row in self.render(ui)))
        for _ in range(30):
            ui.handle("j")
            if "WRAP_END" in "\n".join(row[3] for row in self.render(ui)):
                break
        else:
            self.fail("The end of the wrapped source line was not reachable")
        self.assertEqual(ui.cursor, source_line)
        self.assertGreater(ui.cursor_row, 0)
        with patch.object(ui, "comment_editor", return_value="Comment on a continuation row"):
            ui.handle("c")
        anchor = self.store.comments(str(self.root))[0]["anchor"]
        self.assertEqual((anchor["side"], anchor["start"], anchor["end"]), ("new", 1, 1))
        self.assertEqual(anchor["snippet"], "+" + text)

    def test_selection_from_a_continuation_keeps_original_line_numbers(self):
        text = "x" * 100
        ui = self.make_ui(text)
        ui.cursor_row = 999
        self.render(ui)  # clamp to this source line's last visual row
        ui.handle("v")
        ui.handle("j")
        with patch.object(ui, "comment_editor", return_value="Review both source lines"):
            ui.handle("c")
        anchor = self.store.comments(str(self.root))[0]["anchor"]
        self.assertEqual((anchor["start"], anchor["end"]), (1, 2))
        self.assertEqual(anchor["snippet"], "+" + text + "\n second")

    def test_toggle_preserves_source_selection_and_restores_horizontal_scroll(self):
        ui = self.make_ui("x" * 100)
        ui.handle("v")
        ui.handle("j")
        cursor, selection = ui.cursor, ui.selection
        ui.handle("l")
        self.assertEqual(ui.horizontal, 0)
        ui.handle("w")
        self.assertFalse(ui.wrap)
        self.assertEqual((ui.cursor, ui.selection, ui.cursor_row), (cursor, selection, 0))
        ui.handle("l")
        self.assertEqual(ui.horizontal, 8)
        ui.save_ui()
        self.assertFalse(self.store.get(str(self.root), "ui")["wrap"])
        ui.handle("w")
        self.assertTrue(ui.wrap)
        self.assertEqual(ui.horizontal, 0)
        self.assertEqual((ui.cursor, ui.selection), (cursor, selection))

    def test_search_reveals_a_match_on_a_later_wrapped_row(self):
        ui = self.make_ui("x" * 370 + " WRAP_END", height=14)
        ui.query = "WRAP_END"
        ui.search()
        self.assertGreater(ui.cursor_row, 0)
        self.assertIn("WRAP_END", "\n".join(row[3] for row in self.render(ui)))

    def test_resize_reflows_unicode_and_blame_rows_inside_the_panel(self):
        ui = self.make_ui("界e\u0301" * 100)
        ui.handle("a")
        source_line = ui.cursor
        for size in ((18, 52), (14, 38), (30, 110)):
            with self.subTest(size=size):
                ui.screen.getmaxyx.return_value = size
                rows = self.render(ui)
                self.assertEqual(ui.cursor, source_line)
                self.assertTrue(all(sum(cell_width(char) for char in row[3]) <= row[2] for row in rows))
                self.assertTrue(all(4 <= row[0] < size[0] - 5 for row in rows))
                ui.handle("j")
                self.assertEqual(ui.cursor, source_line)

    def test_inline_editor_stays_visible_on_a_long_wrapped_line(self):
        ui = self.make_ui("x" * 370, height=18)
        ui.cursor_row = 8
        anchor = ui.review.anchor(ui.file, ui.lines, ui.cursor)
        ui.editor = {"anchor": anchor, "text": "An inline comment " * 10, "cursor": 180}
        rows = self.render(ui)
        self.assertIsNotNone(ui.editor_caret)
        y, x = ui.editor_caret
        height, width = ui.screen.getmaxyx()
        self.assertTrue(4 <= y < height - 5)
        self.assertTrue(0 < x < width - 2)
        self.assertTrue(all(4 <= row[0] < height - 5 for row in rows))
