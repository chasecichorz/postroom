"""Keyboard and draft guards that do not need a display or real account."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk

from postroom.ui.compose import ComposeWindow
from postroom.ui.main_window import MainWindow


class MenuTests(unittest.TestCase):
    def test_context_menu_arrows_do_not_expand_underlying_conversation(self):
        win = SimpleNamespace(_menu_active=lambda: True, _tree_arrow=Mock(),
                              msg_typeahead=Mock())
        for key in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_r):
            self.assertFalse(MainWindow._message_key(win, key, 0))
        win._tree_arrow.assert_not_called()
        win.msg_typeahead.handle.assert_not_called()

    def test_tab_in_menu_is_left_to_gtk(self):
        win = SimpleNamespace(_menu_active=lambda: True, child_focus=Mock())
        for key, state in ((Gdk.KEY_Tab, 0),
                           (Gdk.KEY_ISO_Left_Tab, Gdk.ModifierType.SHIFT_MASK)):
            self.assertFalse(MainWindow._tab_key(win, None, key, 0, state))
        win.child_focus.assert_not_called()

    def test_menu_focus_does_not_replace_return_location(self):
        previous = object()
        win = SimpleNamespace(get_focus=lambda: object(), _menu_active=lambda: True,
                              _last_focus=previous)
        MainWindow._track_focus(win)
        self.assertIs(win._last_focus, previous)


class ComposeTests(unittest.TestCase):
    def test_cannot_close_during_send(self):
        win = SimpleNamespace(send_btn=SimpleNamespace(get_sensitive=lambda: False),
                              sent=False, _say=Mock())
        self.assertTrue(ComposeWindow._on_close_request(win))
        win._say.assert_called_once()

    def test_repeated_discard_reuses_prompt(self):
        prompt = Mock()
        win = SimpleNamespace(send_btn=SimpleNamespace(get_sensitive=lambda: True),
                              sent=False, _snapshot=lambda: ('changed',),
                              _initial=('original',), _discard_prompt=prompt)
        ComposeWindow.discard(win)
        prompt.present.assert_called_once()
