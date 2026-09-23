"""A self-contained blue-gray palette, independent of Herdr's pane background."""

import curses


class Theme:
    def __init__(self):
        self.saved = {}
        self.pairs = {}
        if not curses.has_colors():
            return
        curses.start_color()
        palette = {
            "background": ((27, 35, 48), 235, curses.COLOR_BLACK),
            "panel": ((34, 44, 60), 236, curses.COLOR_BLACK),
            "interactive": ((45, 59, 78), 238, curses.COLOR_BLUE),
            "selection": ((61, 81, 106), 24, curses.COLOR_BLUE),
            "text": ((216, 225, 238), 252, curses.COLOR_WHITE),
            "muted": ((123, 141, 166), 103, curses.COLOR_CYAN),
            "border": ((64, 82, 105), 60, curses.COLOR_BLUE),
            "accent": ((136, 192, 208), 110, curses.COLOR_CYAN),
            "green": ((163, 190, 140), 150, curses.COLOR_GREEN),
            "red": ((210, 132, 140), 174, curses.COLOR_RED),
            "amber": ((229, 193, 131), 180, curses.COLOR_YELLOW),
        }
        colors = {}
        for index, (name, (rgb, fallback, basic)) in enumerate(palette.items(), 240):
            color = fallback if curses.COLORS >= 256 else basic
            if curses.COLORS >= 256 and curses.can_change_color():
                try:
                    self.saved[index] = curses.color_content(index)
                    curses.init_color(index, *(round(channel * 1000 / 255) for channel in rgb))
                    color = index
                except curses.error:
                    pass
            colors[name] = color
        definitions = {
            "normal": ("text", "background"),
            "panel": ("text", "panel"),
            "interactive": ("text", "interactive"),
            "selected": ("text", "selection"),
            "dim": ("muted", "background"),
            "panel_dim": ("muted", "panel"),
            "border": ("border", "panel"),
            "focus_border": ("accent", "panel"),
            "popup_border": ("accent", "interactive"),
            "add": ("green", "panel"),
            "delete": ("red", "panel"),
            "hunk": ("accent", "panel"),
            "accent": ("accent", "background"),
            "comment": ("amber", "background"),
        }
        for index, (name, (fg, bg)) in enumerate(definitions.items(), 1):
            if index >= curses.COLOR_PAIRS:
                break
            curses.init_pair(index, colors[fg], colors[bg])
            self.pairs[name] = curses.color_pair(index)

    def restore(self):
        for index, rgb in self.saved.items():
            try:
                curses.init_color(index, *rgb)
            except curses.error:
                pass
