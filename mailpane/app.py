"""Application entry point: menus, keyboard shortcuts, startup."""
from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

from .config import APP_ID, APP_NAME, Config, cache_dir  # noqa: E402
from .store import Store  # noqa: E402
from .ui.common import install_css  # noqa: E402

ACCELS = {
    "win.compose": ["<Control>n"],
    "win.reply": ["<Control>r"],
    "win.reply-all": ["<Control><Shift>r"],
    "win.forward": ["<Control><Shift>f"],
    "win.delete": ["Delete", "KP_Delete"],
    "win.archive": ["<Control><Shift>m"],
    "win.mark-read": ["<Control>q"],
    "win.mark-unread": ["<Control>u"],
    "win.refresh": ["F5"],
    "win.next-unread": ["<Alt>Down"],
    "win.prev-unread": ["<Alt>Up"],
    "win.links": ["<Control><Shift>o"],
    "win.prefer-html": ["<Control><Shift>h"],
    "win.shortcuts": ["F1"],
    "win.next-pane": ["F6"],
    "win.prev-pane": ["<Shift>F6"],
    "win.focus-accounts": ["<Control>1"],
    "win.focus-folders": ["<Control>2"],
    "win.focus-messages": ["<Control>3"],
    "win.focus-status": ["<Control>9"],
    "win.focus-attachments": ["<Alt>a"],
    "win.go-inbox": ["<Alt>i"],
}


def _menu(sections):
    menu = Gio.Menu()
    for section in sections:
        sec = Gio.Menu()
        for label, action in section:
            sec.append(label, action)
        menu.append_section(None, sec)
    return menu


MESSAGE_SECTIONS = [
    [("_Reply", "win.reply"), ("Reply _All", "win.reply-all"), ("_Forward", "win.forward")],
    [("_Delete", "win.delete"), ("Arc_hive", "win.archive"), ("_Move to Folder…", "win.move")],
    [("Mark as R_ead", "win.mark-read"), ("Mark as _Unread", "win.mark-unread"),
     ("_Star or Unstar  (K)", "win.toggle-star")],
    [("_Links in Message…", "win.links")],
]


class MailpaneApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.cfg = Config.load()
        self.store = Store(cache_dir() / "cache.db")

    def do_startup(self):
        Gtk.Application.do_startup(self)
        install_css()
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)
        for name, fn in (("quit", self.quit), ("about", self._about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)

        self.message_menu_model = _menu(MESSAGE_SECTIONS)
        bar = Gio.Menu()
        bar.append_submenu("_File", _menu([
            [("_New Message", "win.compose")],
            [("_Add Account…", "win.add-account"), ("_Edit Account…", "win.edit-account"),
             ("_Remove Account…", "win.remove-account")],
            [("_Quit", "app.quit")]]))
        bar.append_submenu("_Message", _menu(MESSAGE_SECTIONS))
        view = _menu([
            [("_Check for Mail", "win.refresh")],
            [("Group into _Conversations", "win.conversations"),
             ("Read the _Formatted Version", "win.prefer-html")],
            [("Next _Unread Message", "win.next-unread"),
             ("_Previous Unread Message", "win.prev-unread")],
            [("Go to _Inbox", "win.go-inbox"), ("Go to _Accounts", "win.focus-accounts"), ("Go to F_olders", "win.focus-folders"),
             ("Go to _Messages", "win.focus-messages"), ("Go to Attac_hments", "win.focus-attachments"),
             ("Go to _Status Bar", "win.focus-status")]])
        bar.append_submenu("_View", view)
        bar.append_submenu("_Help", _menu([
            [("_Keyboard Shortcuts", "win.shortcuts"), (f"_About {APP_NAME}", "app.about")]]))
        self.menubar_model = bar

    def do_activate(self):
        from .ui.main_window import MainWindow
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()
        if not self.cfg.accounts:
            self.window.add_account()

    def _about(self):
        Gtk.AboutDialog(transient_for=self.window, modal=True, program_name=APP_NAME,
                        comments="A keyboard-first email client, laid out like QuickMail.",
                        license_type=Gtk.License.MIT_X11).present()


def main(argv=None) -> int:
    log_dir = cache_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir / "mailpane.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return MailpaneApp().run(argv if argv is not None else sys.argv)
