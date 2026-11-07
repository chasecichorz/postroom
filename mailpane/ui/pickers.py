"""Small keyboard-first windows: move to folder, links in a message, keyboard shortcuts."""
from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk

from ..folder_tree import FolderNode
from ..models import Folder
from .common import announce, focus_within, mods, set_label


class _Picker(Gtk.Window):
    """A filter box above a list. Type to narrow, Down to reach the list, Enter to choose."""

    def __init__(self, parent, title: str, filter_label: str, entries: list[tuple[str, object]], on_pick):
        super().__init__(transient_for=parent, modal=True, title=title, default_width=480,
                         default_height=420)
        self.entries, self.on_pick = entries, on_pick
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(box)
        lbl = Gtk.Label(label=filter_label, use_underline=True, xalign=0)
        self.search = Gtk.SearchEntry()
        lbl.set_mnemonic_widget(self.search)
        set_label(self.search, filter_label.replace("_", ""))
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        set_label(self.list, title)
        self.list.connect("row-activated", lambda _l, row: self._pick(row))
        box.append(lbl)
        box.append(self.search)
        box.append(Gtk.ScrolledWindow(child=self.list, vexpand=True))
        self.search.connect("search-changed", lambda *_: self._fill())
        self.search.connect("activate", lambda *_: self._pick(self.list.get_row_at_index(0)))
        self.search.connect("stop-search", lambda *_: self.close())
        kc = Gtk.EventControllerKey()
        kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        kc.connect("key-pressed", self._key)
        self.add_controller(kc)
        self._fill()
        self.search.grab_focus()

    def _fill(self):
        q = self.search.get_text().strip().lower()
        self.list.remove_all()
        for text, value in self.entries:
            if q and q not in text.lower():
                continue
            row = Gtk.ListBoxRow()
            row.value = value
            row.set_child(Gtk.Label(label=text, xalign=0, margin_start=6, margin_top=2, margin_bottom=2))
            set_label(row, text)
            self.list.append(row)
        n = sum(1 for _ in self._rows())
        if q:
            announce(self, f"{n} match{'es' if n != 1 else ''}")

    def _rows(self):
        i = 0
        while (row := self.list.get_row_at_index(i)) is not None:
            yield row
            i += 1

    def _key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval == Gdk.KEY_Down and focus_within(self, self.search):
            row = self.list.get_row_at_index(0)
            if row:
                self.list.select_row(row)
                row.grab_focus()
            return True
        return False

    def _pick(self, row):
        if row is None:
            announce(self, "Nothing matches", True)
            return
        value = row.value
        self.destroy()
        self.on_pick(value)


class FolderPicker(_Picker):
    def __init__(self, parent, tree: list[FolderNode], on_pick):
        entries: list[tuple[str, Folder]] = []

        def walk(nodes, prefix):
            for n in nodes:
                label = f"{prefix}{n.name}"
                if n.folder:
                    entries.append((label, n.folder))
                walk(n.children, label + " / ")
        walk(tree, "")
        super().__init__(parent, "Move to Folder", "_Find folder", entries, on_pick)


class LinkPicker(_Picker):
    def __init__(self, parent, links):
        entries = []
        for link in links:
            text = link.text if link.text == link.url else f"{link.text} ({_short(link.url)})"
            entries.append((text, link.url))
        self.parent_win = parent
        super().__init__(parent, "Links in This Message", "_Find link", entries, self._open)

    def _open(self, url: str):
        launcher = Gtk.UriLauncher(uri=url)
        launcher.launch(self.parent_win, None, lambda l, r: _finish(l, r))
        announce(self.parent_win, "Opening link in your browser")


def _finish(launcher, res):
    try:
        launcher.launch_finish(res)
    except GLib.Error:
        pass


def _short(url: str) -> str:
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


SHORTCUTS = """Main window
F6 / Shift+F6: next / previous pane (accounts, folders, messages, reading pane)
Ctrl+1: account list.  Ctrl+2: folder tree.  Ctrl+3: message list.  Ctrl+9: status bar
Alt+A: attachments of the open message
F10: menu bar.  Shift+F10 or the Menu key: actions for the selected message

Folder tree and message list
Up / Down: move.  Right: expand, or step into.  Left: collapse, or go to the parent
Type letters: jump to a folder or sender starting with them
Enter in the message list: read the message in the reading pane
Escape in the reading pane: back to the message list

Messages
Ctrl+N: new message
Ctrl+R: reply.  Ctrl+Shift+R: reply all.  Ctrl+Shift+F: forward
Delete: delete (move to Trash).  Ctrl+Shift+M: archive
Ctrl+Q: mark as read.  Ctrl+U: mark as unread.  K: star or unstar
Alt+Down / Alt+Up: next / previous unread message
Ctrl+Shift+O: list the links in the open message
Ctrl+Shift+H: switch between the plain text and formatted versions
F5: check for new mail

Compose window
Alt+M: From.  Alt+T: To.  Alt+C: Cc.  Alt+U: Subject.  Alt+Y: message body
Alt+A: attach a file
F6 / Shift+F6: move between fields
Ctrl+Enter or Alt+S: send.  Escape: close (asks before discarding)
"""


class ShortcutsWindow(Gtk.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Keyboard Shortcuts",
                         default_width=640, default_height=560)
        view = Gtk.TextView(editable=False, cursor_visible=True, wrap_mode=Gtk.WrapMode.WORD,
                            left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
        set_label(view, "Keyboard shortcuts")
        view.get_buffer().set_text(SHORTCUTS)
        self.set_child(Gtk.ScrolledWindow(child=view))
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", lambda _c, kv, _k, s: (self.close(), True)[1]
                   if kv == Gdk.KEY_Escape and not mods(s) else False)
        self.add_controller(kc)
        view.grab_focus()
