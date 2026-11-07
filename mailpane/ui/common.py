"""Small accessibility and keyboard helpers shared by every window."""
from __future__ import annotations

import time

from gi.repository import Gdk, GObject, Gtk

CSS = b"""
.unread { font-weight: bold; }
.pane-heading { font-weight: bold; padding: 4px 8px; }
.msg-row { padding: 3px 0; }
.dim { opacity: 0.7; }
.statusbar { padding: 3px 8px; }
"""


def install_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def announce(widget: Gtk.Widget, text: str, important: bool = False) -> None:
    """Have the screen reader speak `text` without moving focus."""
    if not text:
        return
    prio = (Gtk.AccessibleAnnouncementPriority.HIGH if important
            else Gtk.AccessibleAnnouncementPriority.MEDIUM)
    try:
        widget.announce(text, prio)
    except (AttributeError, TypeError):
        pass


def set_label(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


def set_description(widget: Gtk.Widget, text: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])


def _walk(widget: Gtk.Widget):
    stack = [widget]
    while stack:
        w = stack.pop()
        yield w
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def _name_items(popover: Gtk.Widget) -> None:
    for w in _walk(popover):
        if GObject.type_name(w.__gtype__) == "GtkModelButton":
            text = (w.get_property("text") or "").replace("_", "")
            if text:
                w.reset_relation(Gtk.AccessibleRelation.LABELLED_BY)
                set_label(w, text)


def label_menu_items(widget: Gtk.Widget) -> None:
    """Name every item in the popover menus under `widget`.

    In GTK 4.24 a popover menu item takes its name from an internal label that reports an
    empty name, so screen readers hear only "menu item" and the shortcut. Give each item
    its text directly. GTK restores the broken link when it fills in shortcuts, so do it
    each time a menu opens.
    """
    for w in _walk(widget):
        if isinstance(w, Gtk.Popover) and not getattr(w, "_mailpane_labels", False):
            w._mailpane_labels = True
            w.connect("map", _name_items)
            _name_items(w)


def standalone_window(win: Gtk.Window, parent: Gtk.Window) -> None:
    """Make `win` an ordinary window rather than a dialog over `parent`.

    Tiling window managers such as i3 float dialogs, and Alt+Tab (focus left/right) cannot
    leave a floating window. An ordinary window tiles like any other, so Alt+Tab works.
    When it closes, the main window is brought back.
    """
    win.connect("close-request", lambda *_: (parent.present(), False)[1])


def focus_within(window: Gtk.Window, widget: Gtk.Widget) -> bool:
    f = window.get_focus()
    return f is not None and (f is widget or f.is_ancestor(widget))


def focus_list(view: Gtk.ListView, sel: Gtk.SingleSelection) -> bool:
    """Put keyboard focus on the selected row (selecting the first if none)."""
    n = sel.get_n_items()
    if n == 0:
        return False
    pos = sel.get_selected()
    if pos == Gtk.INVALID_LIST_POSITION or pos >= n:
        pos = 0
    select_and_focus(view, pos)
    return True


def select_and_focus(view: Gtk.ListView, pos: int) -> None:
    """Select row `pos` and give it keyboard focus.

    ListView.scroll_to(FOCUS) only moves focus when the list already has it, so pull focus
    into the list first.
    """
    root = view.get_root()
    if root is not None and not focus_within(root, view):
        view.grab_focus()
    view.scroll_to(pos, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)


def mods(state: Gdk.ModifierType) -> Gdk.ModifierType:
    return state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK |
                    Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.SUPER_MASK)


class TypeAhead:
    """Type letters to jump to the next row whose text starts with them, like QuickMail.

    Letters typed within a second build a longer prefix; pressing the same letter again
    steps through the matches; the search wraps around.
    """

    TIMEOUT = 1.0

    def __init__(self, view: Gtk.ListView, sel: Gtk.SingleSelection, text_of):
        self.view, self.sel, self.text_of = view, sel, text_of
        self.buf = ""
        self.last = 0.0

    @property
    def active(self) -> bool:
        return bool(self.buf) and time.monotonic() - self.last < self.TIMEOUT

    def handle(self, keyval: int, state: Gdk.ModifierType) -> bool:
        if mods(state) & ~Gdk.ModifierType.SHIFT_MASK:
            return False
        code = Gdk.keyval_to_unicode(keyval)
        if not code:
            return False
        ch = chr(code)
        if not ch.isprintable() or (ch == " " and not self.active):
            return False
        now = time.monotonic()
        if now - self.last >= self.TIMEOUT:
            self.buf = ""
        self.last = now
        repeat = self.buf == ch.lower() * len(self.buf) and self.buf != ""
        self.buf += ch.lower()
        prefix = ch.lower() if repeat else self.buf
        n = self.sel.get_n_items()
        if n == 0:
            return True
        cur = self.sel.get_selected()
        cur = 0 if cur == Gtk.INVALID_LIST_POSITION else cur
        start = cur + 1 if (repeat or len(self.buf) == 1) else cur
        for i in range(n):
            pos = (start + i) % n
            if self.text_of(self.sel.get_item(pos)).lower().startswith(prefix):
                select_and_focus(self.view, pos)
                break
        return True
