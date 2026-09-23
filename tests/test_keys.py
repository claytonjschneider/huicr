from collections import deque
import curses
import io
import unittest
from unittest.mock import Mock, patch

from huicr.keys import SHIFT_ENTER, KeyReader, Paste, enhanced_input
from huicr.ui import UI, edit_text


class InputScreen:
    def __init__(self, events=()):
        self.events = deque(events)
        self.timeout = Mock()
        self.keypad = Mock()

    def get_wch(self):
        if not self.events:
            raise curses.error()
        event = self.events.popleft()
        if event is None:
            raise curses.error()
        return event


class KeyTest(unittest.TestCase):
    def test_modified_keys_and_legacy_option_backspace(self):
        examples = {
            "\x1b[13;2u": SHIFT_ENTER,
            "\x1b[13;2:2u": SHIFT_ENTER,
            "\x1b[13;2:3u": None,
            "\x1b[27;2;13~": SHIFT_ENTER,
            "\x1b\r": SHIFT_ENTER,
            "\x1b[13u": "\r",
            "\x1b[57414u": "\r",
            "\x1b[115;5u": "\x13",
            "\x1b[127;3u": "\x17",
            "\x1b[27;3;127~": "\x17",
            "\x1b\x7f": "\x17",
            "\x1b\b": "\x17",
            "\x1b[27u": "\x1b",
            "\x1b": "\x1b",
            "\x1b[D": curses.KEY_LEFT,
        }
        for sequence, expected in examples.items():
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(KeyReader(InputScreen(sequence)).read(), expected)

    def test_partial_sequences_and_unknown_reports_do_not_cancel_forms(self):
        screen = InputScreen([*"\x1b[13;", None])
        reader = KeyReader(screen)
        self.assertIsNone(reader.read())
        screen.events.extend("2u")
        self.assertEqual(reader.read(), SHIFT_ENTER)
        screen.timeout.assert_called_with(250)
        for sequence in ("\x1b[?1u", "\x1b[999999999u", "\x1b[13;2:3u"):
            self.assertIsNone(KeyReader(InputScreen(sequence)).read())

    def test_bracketed_paste_remains_literal_across_chunks(self):
        screen = InputScreen([*"\x1b[200~first\r\n", None])
        reader = KeyReader(screen)
        self.assertIsNone(reader.read())
        screen.keypad.assert_called_with(False)
        screen.events.extend("second π\x1b[D\nP q\x1b[201~\r")
        self.assertEqual(reader.read(), Paste("first\r\nsecond π\x1b[D\nP q"))
        screen.keypad.assert_called_with(True)
        self.assertEqual(reader.read(), "\r")

    def test_enhanced_control_c_still_interrupts(self):
        with self.assertRaises(KeyboardInterrupt):
            KeyReader(InputScreen("\x1b[99;5u")).read()

    def test_keyboard_and_paste_modes_are_restored_on_error(self):
        stream = io.StringIO()
        with self.assertRaises(RuntimeError):
            with enhanced_input(stream):
                raise RuntimeError("interrupted UI")
        self.assertEqual(stream.getvalue(), "\x1b[>1u\x1b[?2004h\x1b[?2004l\x1b[<u")


class EditingTest(unittest.TestCase):
    def test_word_deletion_handles_whitespace_unicode_and_punctuation(self):
        text = "alpha γ_delta!   "
        text, cursor = edit_text(text, len(text), "\x17")
        self.assertEqual((text, cursor), ("alpha γ_delta", 13))
        text, cursor = edit_text(text, cursor, "\x17")
        self.assertEqual((text, cursor), ("alpha ", 6))
        self.assertEqual(edit_text("before middle after", 13, "\x17"), ("before  after", 7))
        self.assertEqual(edit_text("", 0, "\x17"), ("", 0))

    def test_line_editing_preserves_other_lines(self):
        text = "first\nsecond line\nthird"
        self.assertEqual(edit_text(text, 12, "\x15"), ("first\n line\nthird", 6))
        self.assertEqual(edit_text(text, 12, "\x0b"), ("first\nsecond\nthird", 12))
        self.assertEqual(edit_text(text, 12, "\x01"), (text, 6))
        self.assertEqual(edit_text(text, 12, "\x05"), (text, 17))

    def test_only_shift_enter_inserts_a_typed_newline(self):
        self.assertEqual(edit_text("ab", 1, SHIFT_ENTER), ("a\nb", 2))
        for key in ("\r", "\n", curses.KEY_ENTER):
            self.assertEqual(edit_text("ab", 1, key), ("ab", 1))
        self.assertEqual(edit_text("ab", 1, SHIFT_ENTER, multiline=False), ("ab", 1))

    def test_paste_normalizes_line_endings_without_finishing_the_input(self):
        paste = Paste("first\r\nsecond\rthird\nP q\x13")
        text = "first\nsecond\nthird\nP q"
        self.assertEqual(edit_text("", 0, paste), (text, len(text)))
        self.assertEqual(edit_text("", 0, paste, multiline=False), (text.replace("\n", " "), len(text)))


class TextBoxTest(unittest.TestCase):
    def setUp(self):
        self.ui = UI.__new__(UI)
        self.ui.screen = Mock()
        self.ui.screen.getmaxyx.return_value = (25, 100)
        self.ui.colors = {}
        self.ui.running = True
        self.ui.draw, self.ui.put, self.ui.box = Mock(), Mock(), Mock()
        cursor = patch("huicr.ui.curses.curs_set")
        cursor.start()
        self.addCleanup(cursor.stop)

    def test_comment_and_multiline_prompt_share_finish_and_editing(self):
        for form in (lambda: self.ui.comment_editor({}), lambda: self.ui.prompt("Input", multiline=True)):
            keys = [*"hello typo", "\x17", *"world", SHIFT_ENTER, *"next line", "\r"]
            with patch.object(self.ui, "get_key", side_effect=keys):
                self.assertEqual(form(), "hello world\nnext line")

    def test_search_prompt_supports_word_and_line_deletion(self):
        keys = [*"discard this", "\x15", *"wanted junk", "\x17", "\x0b", "\r"]
        with patch.object(self.ui, "get_key", side_effect=keys):
            self.assertEqual(self.ui.prompt("Search"), "wanted")

    def test_save_and_cancel_work_in_both_forms(self):
        for form in (lambda: self.ui.comment_editor({}), lambda: self.ui.prompt("Input")):
            with patch.object(self.ui, "get_key", side_effect=[*"saved", "\x13"]):
                self.assertEqual(form(), "saved")
            with patch.object(self.ui, "get_key", side_effect=[*"cancelled", "\x1b"]):
                self.assertIsNone(form())
