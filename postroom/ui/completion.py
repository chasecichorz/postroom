"""Address suggestions in To and Cc, made for screen readers.

Type part of a name or address and the best match is spoken ("Ann Lee, ann@example.com,
1 of 3"). Down and Up step through the matches, Enter or Tab accepts one, and Escape
dismisses them. The suggestion is also shown as text under the fields.
"""
from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk

from ..contacts import Contact, current_token
from .common import announce, mods

MIN_CHARS = 2
DELAY_MS = 250


class AddressCompleter:
    def __init__(self, entry: Gtk.Entry, search, show_text):
        """`search(text)` returns contacts; `show_text(str | None)` shows the visible hint."""
        self.entry, self.search, self.show_text = entry, search, show_text
        self.matches: list[Contact] = []
        self.index = 0
        self._span = (0, 0)
        self._timer = 0
        self._busy = False
        entry.connect("changed", self._changed)
        kc = Gtk.EventControllerKey()
        kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        kc.connect("key-pressed", self._key)
        entry.add_controller(kc)
        fc = Gtk.EventControllerFocus()
        fc.connect("leave", lambda *_: self.dismiss(quiet=True))
        entry.add_controller(fc)

    @property
    def active(self) -> bool:
        return bool(self.matches)

    def _changed(self, *_):
        if self._busy:
            return
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(DELAY_MS, self._lookup)

    def _lookup(self):
        self._timer = 0
        text = self.entry.get_text()
        start, end, token = current_token(text, self.entry.get_position())
        if len(token) < MIN_CHARS or "<" in token:
            self.dismiss(quiet=True)
            return False
        found = [c for c in self.search(token)
                 if c.email != token.lower()]  # already typed in full
        if not found:
            self.dismiss(quiet=True)
            return False
        self.matches, self.index, self._span = found, 0, (start, end)
        self._say()
        return False

    def _say(self):
        c = self.matches[self.index]
        n = len(self.matches)
        spoken = f"{c.spoken}, {self.index + 1} of {n}"
        self.show_text(f"Suggestion: {c.address} ({self.index + 1} of {n}). Down for the next, "
                       "Enter or Tab to use it, Escape to dismiss.")
        announce(self.entry, spoken)

    def dismiss(self, quiet: bool = False):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        was = self.active
        self.matches = []
        self.show_text(None)
        if was and not quiet:
            announce(self.entry, "Suggestions dismissed")

    def accept(self):
        c = self.matches[self.index]
        text = self.entry.get_text()
        start, end = self._span
        rest = text[end:].lstrip(", ")
        new = text[:start] + c.address + ", " + (rest if rest else "")
        self._busy = True
        self.entry.set_text(new)
        self.entry.set_position(start + len(c.address) + 2)
        self._busy = False
        self.matches = []
        self.show_text(None)
        announce(self.entry, f"Added {c.name or c.email}")

    def _key(self, _c, keyval, _code, state):
        if not self.active or mods(state) & ~Gdk.ModifierType.SHIFT_MASK:
            return False
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.index = (self.index + 1) % len(self.matches)
            self._say()
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.index = (self.index - 1) % len(self.matches)
            self._say()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) or (
                keyval == Gdk.KEY_Tab and not state & Gdk.ModifierType.SHIFT_MASK):
            self.accept()
            return True
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False
