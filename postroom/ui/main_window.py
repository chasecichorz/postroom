"""The main window: accounts and folders on the left, messages and the reading pane on the right."""
from __future__ import annotations

import logging
import time

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from .. import credentials, folder_tree, mime, passwords, threads
from ..config import APP_NAME, Account
from ..folder_tree import FolderNode
from ..models import Folder, Message
from ..session import AccountSession
from .common import (TypeAhead, announce, focus_list, focus_within, label_menu_items, mods,
                     select_and_focus, set_description, set_label)

log = logging.getLogger(__name__)


# -- list items ------------------------------------------------------------------------------
class FolderItem(GObject.Object):
    __gtype_name__ = "PostroomFolderItem"
    revision = GObject.Property(type=int, default=0)

    def __init__(self, node: FolderNode):
        super().__init__()
        self.node = node
        self.unread = 0
        self.total = 0

    @property
    def folder(self) -> Folder | None:
        return self.node.folder


class RowItem(GObject.Object):
    """A message-list row: a whole conversation, or one message (top level or inside one)."""
    __gtype_name__ = "PostroomRowItem"
    revision = GObject.Property(type=int, default=0)

    def __init__(self, messages: list[Message], is_child: bool = False):
        super().__init__()
        self.messages = messages
        self.is_child = is_child
        self._children: Gio.ListStore | None = None

    @property
    def is_thread(self) -> bool:
        return not self.is_child and len(self.messages) > 1

    @property
    def message(self) -> Message:
        """The message the reading pane shows for this row (a conversation's newest)."""
        return self.messages[-1]

    @property
    def unread(self) -> bool:
        return any(not m.seen for m in self.messages)

    @property
    def key(self) -> tuple:
        return ("t", threads.thread_key(self.message)) if self.is_thread else ("m", self.message.key)

    def children(self) -> Gio.ListStore:
        if self._children is None:
            self._children = Gio.ListStore(item_type=RowItem)
            for m in self.messages:
                self._children.append(RowItem([m], is_child=True))
        return self._children

    def bump(self):
        self.revision += 1


class FolderRow(Gtk.TreeExpander):
    def __init__(self):
        super().__init__()
        # The row itself takes focus, so the screen reader reads the row's label.
        self.set_can_focus(False)
        self.label = Gtk.Label(xalign=0)
        self.set_child(self.label)
        self.handlers: list = []


class MessageRow(Gtk.TreeExpander):
    def __init__(self):
        super().__init__()
        # The row itself takes focus, so the screen reader reads the row's label.
        self.set_can_focus(False)
        box = Gtk.Box(spacing=12)
        box.add_css_class("msg-row")
        self.sender = Gtk.Label(xalign=0, width_chars=24, max_width_chars=24, ellipsize=3)
        self.subject = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.date = Gtk.Label(xalign=1, width_chars=16)
        self.date.add_css_class("dim")
        for w in (self.sender, self.subject, self.date):
            box.append(w)
        self.set_child(box)
        self.handlers: list = []


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.cfg = app.cfg
        self.store = app.store
        self.sessions: dict[str, AccountSession] = {}
        self.account: Account | None = None
        self.folder: Folder | None = None
        self.tree: list[FolderNode] = []
        self.folder_items: dict[str, FolderItem] = {}
        self.messages: list[Message] = []
        self.items_by_msg: dict[tuple, list[RowItem]] = {}
        self.shown: tuple[Message, mime.ParsedMessage] | None = None
        self.shown_key: tuple | None = None
        self._populating = False
        self._show_timer = 0
        self._sync_timer = 0
        self._first_focus_done = False
        self.set_default_size(self.cfg.settings.window_width, self.cfg.settings.window_height)

        self._build_actions()
        self._build_ui()
        self._build_shortcuts()
        self.connect("close-request", self._on_close)
        self._reload_accounts()
        GLib.timeout_add_seconds(max(1, self.cfg.settings.check_minutes) * 60, self._periodic_check)

    # ===== construction =====================================================================
    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        self.menubar = Gtk.PopoverMenuBar.new_from_model(self.app.menubar_model)
        outer.append(self.menubar)

        hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=260, vexpand=True,
                           shrink_start_child=False, shrink_end_child=False)
        outer.append(hpaned)

        # Left: accounts above folders.
        left = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=130)
        hpaned.set_start_child(left)
        self.account_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        set_label(self.account_list, "Accounts")
        self.account_list.connect("row-selected", self._on_account_selected)
        left.set_start_child(self._pane("Accounts", self.account_list))

        self.folder_root = Gio.ListStore(item_type=FolderItem)
        self.folder_model = Gtk.TreeListModel.new(self.folder_root, False, False,
                                                  self._folder_children)
        self.folder_sel = Gtk.SingleSelection(model=self.folder_model, autoselect=False)
        self.folder_sel.connect("notify::selected", self._on_folder_selected)
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: self._setup_row(li, FolderRow()))
        f.connect("bind", self._bind_folder)
        f.connect("unbind", self._unbind)
        self.folder_view = Gtk.ListView(model=self.folder_sel, factory=f,
                                    tab_behavior=Gtk.ListTabBehavior.ITEM)
        set_label(self.folder_view, "Folders")
        self.folder_typeahead = TypeAhead(self.folder_view, self.folder_sel,
                                          lambda row: row.get_item().node.name)
        self._keys(self.folder_view, self._folder_key)
        left.set_end_child(self._pane("Folders", self.folder_view))

        # Right: message list above reading pane.
        right = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=330)
        hpaned.set_end_child(right)
        self.msg_root = Gio.ListStore(item_type=RowItem)
        self.msg_model = Gtk.TreeListModel.new(self.msg_root, False, False,
                                               lambda item: item.children() if item.is_thread else None)
        self.msg_sel = Gtk.SingleSelection(model=self.msg_model, autoselect=False)
        self.msg_sel.connect("notify::selected", self._on_message_selected)
        f = Gtk.SignalListItemFactory()
        f.connect("setup", lambda _f, li: self._setup_row(li, MessageRow()))
        f.connect("bind", self._bind_message)
        f.connect("unbind", self._unbind)
        self.msg_view = Gtk.ListView(model=self.msg_sel, factory=f,
                                    tab_behavior=Gtk.ListTabBehavior.ITEM)
        set_label(self.msg_view, "Messages")
        self.msg_view.connect("activate", lambda *_: self.focus_reading())
        self.msg_typeahead = TypeAhead(self.msg_view, self.msg_sel,
                                       lambda row: row.get_item().message.sender)
        self._keys(self.msg_view, self._message_key)
        self.msg_empty = Gtk.Label(label="Loading…", selectable=True, vexpand=True)
        self.msg_stack = Gtk.Stack()
        sw = Gtk.ScrolledWindow(child=self.msg_view, vexpand=True)
        self.msg_stack.add_named(sw, "list")
        self.msg_stack.add_named(self.msg_empty, "empty")
        self.msg_stack.set_visible_child_name("empty")
        self.msg_heading = Gtk.Label(label="Messages", xalign=0)
        self.msg_heading.add_css_class("pane-heading")
        msg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        msg_box.append(self.msg_heading)
        msg_box.append(self.msg_stack)
        right.set_start_child(msg_box)
        self.msg_menu = Gtk.PopoverMenu.new_from_model(self.app.message_menu_model)
        self.msg_menu.set_parent(self.msg_view)
        self.msg_menu.set_has_arrow(False)
        label_menu_items(self.menubar)
        label_menu_items(self.msg_menu)

        # Reading pane: attachments (when any), then the message as text.
        reading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.reading_pane = reading
        self.att_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
        att_head = Gtk.Label(label="Attachments", xalign=0)
        att_head.add_css_class("pane-heading")
        self.att_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        set_label(self.att_list, "Attachments")
        self.att_list.connect("row-activated", self._save_attachment)
        self.att_box.append(att_head)
        self.att_box.append(self.att_list)
        reading.append(self.att_box)
        self.body = Gtk.TextView(editable=False, cursor_visible=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=12, right_margin=12, top_margin=8, bottom_margin=8,
                                 monospace=False)
        set_label(self.body, "Message")
        self._keys(self.body, self._reading_key)
        self._keys(self.att_list, self._reading_key)
        reading.append(Gtk.ScrolledWindow(child=self.body, vexpand=True))
        right.set_end_child(reading)
        fc = Gtk.EventControllerFocus()
        fc.connect("enter", lambda *_: self._opened())
        reading.add_controller(fc)

        self.status = Gtk.Label(xalign=0, selectable=True)
        self.status.add_css_class("statusbar")
        set_label(self.status, "Status")
        # Like QuickMail, the status bar is reached with Ctrl+9 or F6, not Tab: it only takes
        # focus while you are on it.
        self.status.set_focusable(False)
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda *_: self.status.set_focusable(False))
        self.status.add_controller(leave)
        outer.append(self.status)

    @staticmethod
    def _setup_row(li: Gtk.ListItem, child: Gtk.Widget):
        # Focus goes to the row, which carries the label the screen reader speaks.
        li.set_focusable(True)
        li.set_child(child)

    def _pane(self, title: str, child: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Label(label=title, xalign=0)
        head.add_css_class("pane-heading")
        box.append(head)
        box.append(Gtk.ScrolledWindow(child=child, vexpand=True))
        return box

    def _keys(self, widget, handler):
        kc = Gtk.EventControllerKey()
        kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        kc.connect("key-pressed", lambda _c, kv, _code, state: handler(kv, state))
        widget.add_controller(kc)

    def _build_actions(self):
        simple = {
            "compose": self.compose_new, "reply": lambda: self.reply(False),
            "reply-all": lambda: self.reply(True), "forward": self.forward,
            "delete": self.delete_selected, "archive": self.archive_selected,
            "move": self.move_selected, "mark-read": lambda: self.mark_selected(True),
            "mark-unread": lambda: self.mark_selected(False), "toggle-star": self.toggle_star,
            "refresh": self.refresh, "next-unread": lambda: self.jump_unread(1),
            "prev-unread": lambda: self.jump_unread(-1), "links": self.show_links,
            "next-pane": lambda: self.cycle_pane(1), "prev-pane": lambda: self.cycle_pane(-1),
            "focus-accounts": self.focus_accounts, "focus-folders": self.focus_folders,
            "focus-messages": self.focus_messages, "focus-status": self.focus_status,
            "focus-attachments": self.focus_attachments, "go-inbox": self.go_inbox,
            "add-account": self.add_account,
            "edit-account": self.edit_account, "remove-account": self.remove_account,
            "shortcuts": self.show_shortcuts, "context-menu": self.popup_message_menu,
        }
        for name, fn in simple.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
        s = self.cfg.settings
        for name, value, fn in (("conversations", s.conversations, self._set_conversations),
                                ("prefer-html", s.prefer_html, self._set_prefer_html)):
            a = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(value))
            a.connect("change-state", lambda act, v, fn=fn: (act.set_state(v), fn(v.get_boolean())))
            self.add_action(a)

    def _build_shortcuts(self):
        # Gtk.Paned claims F6 for itself; catch pane keys before any widget sees them.
        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trigger, action in (("F6", "win.next-pane"), ("<Shift>F6", "win.prev-pane"),
                                ("<Control>1", "win.focus-accounts"), ("<Control>2", "win.focus-folders"),
                                ("<Control>3", "win.focus-messages"), ("<Control>9", "win.focus-status"),
                                ("<Alt>a", "win.focus-attachments"), ("<Alt>i", "win.go-inbox")):
            sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                             Gtk.NamedAction.new(action)))
        self.add_controller(sc)

        # GTK 4 stops at the last control instead of wrapping; wrap like QuickMail does.
        tab = Gtk.EventControllerKey()
        tab.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        tab.connect("key-pressed", self._tab_key)
        self.add_controller(tab)

        # Escape on the menu bar (with no menu open) goes back to where you were.
        self._last_focus = None
        self.connect("notify::focus-widget", self._track_focus)
        esc = Gtk.EventControllerKey()
        esc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        esc.connect("key-pressed", self._menubar_escape)
        self.add_controller(esc)

    def _track_focus(self, *_):
        w = self.get_focus()
        if w is not None and not focus_within(self, self.menubar):
            self._last_focus = w

    def _menubar_escape(self, _c, keyval, _code, state):
        if keyval != Gdk.KEY_Escape or mods(state) or not focus_within(self, self.menubar):
            return False
        child = self.menubar.get_first_child()
        while child is not None:
            pop = child.get_first_child()
            while pop is not None:
                if isinstance(pop, Gtk.Popover) and pop.get_mapped():
                    return False  # let GTK close the open menu first
                pop = pop.get_next_sibling()
            child = child.get_next_sibling()
        w = self._last_focus
        if w is not None and w.get_root() is self and w.is_visible() and w.grab_focus():
            return True
        self.focus_messages()
        return True

    def _tab_key(self, _c, keyval, _code, state):
        if keyval not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_KP_Tab):
            return False
        if mods(state) & ~Gdk.ModifierType.SHIFT_MASK:
            return False
        back = keyval == Gdk.KEY_ISO_Left_Tab or bool(state & Gdk.ModifierType.SHIFT_MASK)
        before = self.get_focus()
        moved = self.child_focus(Gtk.DirectionType.TAB_BACKWARD if back else Gtk.DirectionType.TAB_FORWARD)
        # The menu bar is reached with F10 or Alt, not Tab (as in QuickMail).
        now = self.get_focus()
        if not moved or now is None or now is before or focus_within(self, self.menubar):
            (self.focus_reading if back else self.focus_accounts)()
        return True

    # ===== accounts =========================================================================
    def _reload_accounts(self, select_id: str | None = None):
        for acct_id in list(self.sessions):
            if not self.cfg.account(acct_id):
                self.sessions.pop(acct_id).shutdown()
        for a in self.cfg.accounts:
            if a.id not in self.sessions:
                self.sessions[a.id] = AccountSession(a, self.store)
            else:
                self.sessions[a.id].account = a
        self.account_list.remove_all()
        target = None
        for a in self.cfg.accounts:
            row = Gtk.ListBoxRow()
            row.account = a
            row.set_child(Gtk.Label(label=a.name, xalign=0, margin_start=8, margin_end=8,
                                    margin_top=3, margin_bottom=3))
            set_label(row, f"{a.name}, {a.email}")
            self.account_list.append(row)
            if a.id == (select_id or (self.account.id if self.account else None)):
                target = row
        if not self.cfg.accounts:
            self.account = None
            self._show_empty("No accounts yet. Use File, Add Account.")
            self._set_status("No accounts. Open the File menu and choose Add Account.")
            return
        self.account_list.select_row(target or self.account_list.get_row_at_index(0))

    def _on_account_selected(self, _lb, row):
        if row is None:
            return
        acct = row.account
        if self.account and self.account.id == acct.id and self.tree:
            return
        self.account = acct
        self.folder = None
        self.tree = []
        cached = self.store.load_folders(acct.id)
        if cached:
            self.sessions[acct.id].folders = cached
            self._show_folders(cached)
        else:
            self._show_empty("Connecting…")
            self._set_status(f"Connecting to {acct.email}…")
        sess = self.sessions[acct.id]
        sess.submit(sess.list_folders, on_done=lambda f, a=acct: self._folders_loaded(a, f),
                    on_error=self._error)

    def _folders_loaded(self, acct: Account, folders: list[Folder]):
        if self.account is None or acct.id != self.account.id:
            return
        self._show_folders(folders)
        sess = self.sessions[acct.id]
        paths = [f.path for f in folders if f.selectable]
        sess.submit(sess.folder_counts, paths, on_done=lambda c, a=acct: self._counts_loaded(a, c))
        sess.submit(sess.backfill_contacts)

    def _counts_loaded(self, acct: Account, counts: dict):
        if self.account is None or acct.id != self.account.id:
            return
        for path, (total, unseen) in counts.items():
            item = self.folder_items.get(path)
            if item and (item.unread, item.total) != (unseen, total):
                item.unread, item.total = unseen, total
                item.revision += 1

    # ===== folders ==========================================================================
    def _folder_children(self, item: FolderItem):
        if not item.node.children:
            return None
        store = Gio.ListStore(item_type=FolderItem)
        for child in item.node.children:
            store.append(self._folder_item(child))
        return store

    def _folder_item(self, node: FolderNode) -> FolderItem:
        item = FolderItem(node)
        if node.folder:
            old = self.folder_items.get(node.folder.path)
            if old:
                item.unread, item.total = old.unread, old.total
            self.folder_items[node.folder.path] = item
        return item

    def _show_folders(self, folders: list[Folder]):
        expanded = set()
        for i in range(self.folder_model.get_n_items()):
            row = self.folder_model.get_item(i)
            if row.get_expanded():
                expanded.add(row.get_item().node.path_key)
        keep = self.folder.path if self.folder else None
        self.tree = folder_tree.build(folders)
        self._populating = True
        try:
            self.folder_root.remove_all()
            for node in self.tree:
                self.folder_root.append(self._folder_item(node))
            i = 0
            while i < self.folder_model.get_n_items():
                row = self.folder_model.get_item(i)
                if row.is_expandable() and row.get_item().node.path_key in expanded:
                    row.set_expanded(True)
                i += 1
        finally:
            self._populating = False
        target = None
        for i in range(self.folder_model.get_n_items()):
            f = self.folder_model.get_item(i).get_item().folder
            if f and (f.path == keep or (keep is None and f.special == "inbox")):
                target = i
                break
        if target is None and self.folder_model.get_n_items():
            target = 0
        if target is not None:
            had = focus_within(self, self.folder_view)
            self.folder_sel.set_selected(target)
            if had:
                select_and_focus(self.folder_view, target)
            if keep is None or self.folder is None or self.folder.path != keep:
                self._on_folder_selected()

    def _bind_folder(self, _f, li):
        row = li.get_item()
        w: FolderRow = li.get_child()
        w.set_list_row(row)
        item = row.get_item()
        update = lambda *_: self._render_folder(li, w, row, item)  # noqa: E731
        w.handlers = [(item, item.connect("notify::revision", update)),
                      (row, row.connect("notify::expanded", update))]
        update()

    def _render_folder(self, li, w: FolderRow, row, item: FolderItem):
        name = item.node.name
        w.label.set_text(f"{name}  ({item.unread})" if item.unread else name)
        if item.unread:
            w.label.add_css_class("unread")
        else:
            w.label.remove_css_class("unread")
        speech = name + (f", {item.unread} unread" if item.unread else "")
        if row.is_expandable():
            speech += ", expanded" if row.get_expanded() else ", collapsed"
        li.set_accessible_label(speech)

    def _unbind(self, _f, li):
        w = li.get_child()
        for obj, hid in w.handlers:
            obj.disconnect(hid)
        w.handlers = []

    def _on_folder_selected(self, *_):
        if self._populating:
            return
        row = self.folder_sel.get_selected_item()
        if row is None or self.account is None:
            return
        item: FolderItem = row.get_item()
        if item.folder is None:
            self.folder = None
            self._show_empty(f"{item.node.name} is a group of folders. Press Right to open it.")
            return
        if self.folder and self.folder.path == item.folder.path:
            return
        self.folder = item.folder
        self.msg_heading.set_text(f"{item.node.name}, {self.account.name}")
        # Orca speaks the list's name on entering it, so say which folder this is.
        set_label(self.msg_view, f"{item.node.name} messages")
        self.shown_key = None
        self._populate(self.store.messages(self.account.id, self.folder.path))
        if not self.messages:
            self._show_empty(f"Loading {item.node.name}…")
        if self._sync_timer:
            GLib.source_remove(self._sync_timer)
        self._sync_timer = GLib.timeout_add(250, self._sync_now)

    def _sync_now(self):
        self._sync_timer = 0
        self.sync_current()
        return GLib.SOURCE_REMOVE

    def _folder_key(self, keyval, state):
        m = mods(state)
        if not m and keyval in (Gdk.KEY_Right, Gdk.KEY_Left):
            return self._tree_arrow(self.folder_view, self.folder_sel, keyval == Gdk.KEY_Right)
        if not m and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.focus_messages()
            return True
        return self.folder_typeahead.handle(keyval, state)

    def _tree_arrow(self, view, sel, right: bool) -> bool:
        """Right expands (or steps into) a node; Left collapses (or goes to the parent)."""
        row = sel.get_selected_item()
        if row is None:
            return False
        if right:
            if row.is_expandable() and not row.get_expanded():
                row.set_expanded(True)
                announce(view, "expanded")
            elif row.is_expandable():
                select_and_focus(view, row.get_position() + 1)
            return True
        if row.is_expandable() and row.get_expanded():
            row.set_expanded(False)
            announce(view, "collapsed")
        elif row.get_parent() is not None:
            select_and_focus(view, row.get_parent().get_position())
        return True

    # ===== message list =====================================================================
    def _populate(self, msgs: list[Message], keep_key=None, keep_pos=None):
        """Rebuild the message list, keeping expanded conversations and the selection."""
        had_focus = focus_within(self, self.msg_stack)
        expanded = set()
        for i in range(self.msg_model.get_n_items()):
            row = self.msg_model.get_item(i)
            if row.get_expanded():
                expanded.add(row.get_item().key)
        if keep_key is None and keep_pos is None:
            cur = self._selected_item()
            keep_key = cur.key if cur else None
        self.messages = msgs
        self.items_by_msg = {}
        groups = threads.group(msgs, self.cfg.settings.conversations)
        items = [RowItem(g) for g in groups]
        for it in items:
            for m in it.messages:
                self.items_by_msg.setdefault(m.key, []).append(it)
        self._populating = True
        try:
            self.msg_root.splice(0, self.msg_root.get_n_items(), items)
            pos, i = None, 0
            while i < self.msg_model.get_n_items():
                row = self.msg_model.get_item(i)
                it = row.get_item()
                if it.is_child:
                    self.items_by_msg.setdefault(it.message.key, []).append(it)
                if row.is_expandable() and it.key in expanded:
                    row.set_expanded(True)
                if pos is None and keep_key is not None and it.key == keep_key:
                    pos = i
                i += 1
            n = self.msg_model.get_n_items()
            if pos is None:
                pos = min(keep_pos, n - 1) if keep_pos is not None else 0
            if n:
                self.msg_sel.set_selected(max(0, pos))
        finally:
            self._populating = False
        if not msgs:
            name = self.folder.display_name if self.folder else "This folder"
            self._show_empty(f"{name} is empty.")
            self._clear_reading()
        else:
            self.msg_stack.set_visible_child_name("list")
            if had_focus or not self._first_focus_done:
                self._first_focus_done = True
                focus_list(self.msg_view, self.msg_sel)
            self._on_message_selected()
        self._update_status()

    def _show_empty(self, text: str):
        had_focus = focus_within(self, self.msg_stack)
        self.msg_root.remove_all()
        self.msg_empty.set_text(text)
        self.msg_stack.set_visible_child_name("empty")
        if had_focus:
            self.msg_empty.grab_focus()

    def _bind_message(self, _f, li):
        row = li.get_item()
        w: MessageRow = li.get_child()
        w.set_list_row(row)
        item = row.get_item()
        update = lambda *_: self._render_message(li, w, row, item)  # noqa: E731
        w.handlers = [(item, item.connect("notify::revision", update))]
        if item.is_thread:
            w.handlers.append((row, row.connect("notify::expanded", update)))
        update()

    def _render_message(self, li, w: MessageRow, row, item: RowItem):
        m = item.message
        if item.is_thread:
            w.sender.set_text(threads.senders(item.messages, 2))
            w.subject.set_text(f"{m.subject or '(no subject)'}  ({len(item.messages)})")
            li.set_accessible_label(threads.thread_speech(item.messages, row.get_expanded()))
        else:
            w.sender.set_text(("★ " if m.flagged else "") + m.sender)
            w.subject.set_text(m.subject or "(no subject)")
            li.set_accessible_label(threads.message_speech(m))
        w.date.set_text(threads.friendly_date(m.date_ts))
        for lbl in (w.sender, w.subject):
            if item.unread:
                lbl.add_css_class("unread")
            else:
                lbl.remove_css_class("unread")

    def _selected_item(self) -> RowItem | None:
        row = self.msg_sel.get_selected_item()
        return row.get_item() if row else None

    def _selected_messages(self) -> list[Message]:
        it = self._selected_item()
        if not it or self.msg_stack.get_visible_child_name() != "list":
            return []
        return list(it.messages)

    def _message_key(self, keyval, state):
        m = mods(state)
        if not m and keyval in (Gdk.KEY_Right, Gdk.KEY_Left):
            return self._tree_arrow(self.msg_view, self.msg_sel, keyval == Gdk.KEY_Right)
        if (keyval == Gdk.KEY_Menu and not m) or (keyval == Gdk.KEY_F10 and m == Gdk.ModifierType.SHIFT_MASK):
            self.popup_message_menu()
            return True
        if keyval in (Gdk.KEY_k, Gdk.KEY_K) and not m and not self.msg_typeahead.active:
            self.toggle_star()
            return True
        return self.msg_typeahead.handle(keyval, state)

    def _on_message_selected(self, *_):
        if self._populating:
            return
        if self._show_timer:
            GLib.source_remove(self._show_timer)
        self._show_timer = GLib.timeout_add(120, self._show_selected)

    # ===== reading pane =====================================================================
    def _show_selected(self):
        self._show_timer = 0
        it = self._selected_item()
        if it is None:
            self._clear_reading()
            return GLib.SOURCE_REMOVE
        m = it.message
        if m.key == self.shown_key:
            return GLib.SOURCE_REMOVE
        self.shown_key = m.key
        self.shown = None
        sess = self.sessions.get(m.account_id)
        if not sess:
            return GLib.SOURCE_REMOVE
        sess.submit(sess.fetch_body, m.folder, m.uid,
                    on_done=lambda raw, m=m: self._body_loaded(m, raw),
                    on_error=lambda e, m=m: self._body_failed(m, e))
        return GLib.SOURCE_REMOVE

    def _body_loaded(self, m: Message, raw: bytes):
        if self.shown_key != m.key:
            return
        pm = mime.parse(raw, self.cfg.settings.prefer_html)
        self.shown = (m, pm)
        lines = [f"From: {pm.from_ or m.sender}"]
        if pm.to:
            lines.append(f"To: {pm.to}")
        if pm.cc:
            lines.append(f"Cc: {pm.cc}")
        if m.date_ts:
            lines.append("Date: " + time.strftime("%A, %B %-d, %Y, %-I:%M %p", time.localtime(m.date_ts)))
        lines.append(f"Subject: {pm.subject or '(no subject)'}")
        text = "\n".join(lines) + "\n\n" + (pm.text or "(This message has no text.)") + "\n"
        buf = self.body.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_start_iter())
        self.body.scroll_to_iter(buf.get_start_iter(), 0, False, 0, 0)
        set_description(self.body, f"{len(pm.links)} links" if pm.links else "")
        self.att_list.remove_all()
        for att in pm.attachments:
            row = Gtk.ListBoxRow()
            row.attachment = att
            desc = f"{att.filename}, {mime.human_size(att.size)}"
            row.set_child(Gtk.Label(label=desc, xalign=0, margin_start=8))
            set_label(row, desc)
            self.att_list.append(row)
        self.att_box.set_visible(bool(pm.attachments))
        if focus_within(self, self.reading_pane):
            self._opened()

    def _body_failed(self, m: Message, e: Exception):
        if self.shown_key == m.key:
            self.shown_key = None
            self.body.get_buffer().set_text(f"This message couldn't be loaded.\n\n{e}")
        self._error(e)

    def _opened(self):
        """Like QuickMail: a message is marked read when you open it (Enter, or moving into
        the reading pane), not merely because it is selected in the list."""
        if self.cfg.settings.mark_read_on_open and self.shown and not self.shown[0].seen:
            self._set_flag([self.shown[0]], "\\Seen", True)

    def _clear_reading(self):
        self.shown_key = None
        self.shown = None
        self.body.get_buffer().set_text("")
        self.att_list.remove_all()
        self.att_box.set_visible(False)

    def _reading_key(self, keyval, state):
        if keyval == Gdk.KEY_Escape and not mods(state):
            self.focus_messages()
            return True
        return False

    def _save_attachment(self, _lb, row):
        att = row.attachment
        dialog = Gtk.FileDialog(title=f"Save {att.filename}", initial_name=att.filename)

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            try:
                with open(f.get_path(), "wb") as out:
                    out.write(att.data)
                self.say(f"Saved {att.filename}")
            except OSError as e:
                self._error(e)
        dialog.save(self, None, done)

    # ===== focus ============================================================================
    def focus_accounts(self):
        row = self.account_list.get_selected_row() or self.account_list.get_row_at_index(0)
        if row:
            row.grab_focus()
        else:
            announce(self, "No accounts. Open the File menu and choose Add Account.")

    def focus_folders(self):
        if not focus_list(self.folder_view, self.folder_sel):
            announce(self, "No folders yet")

    def focus_messages(self):
        if self.msg_stack.get_visible_child_name() == "list" and focus_list(self.msg_view, self.msg_sel):
            return
        self.msg_empty.grab_focus()

    def go_inbox(self):
        """Alt+I: open the current account's Inbox and move to its message list."""
        if not self.account:
            return self.say("No accounts. Open the File menu and choose Add Account.")
        for i in range(self.folder_model.get_n_items()):
            f = self.folder_model.get_item(i).get_item().folder
            if f and f.special == "inbox":
                self.folder_sel.set_selected(i)
                self.focus_messages()
                return
        self.say("This account's Inbox hasn't loaded yet")

    def focus_reading(self):
        self.body.grab_focus()

    def focus_attachments(self):
        row = self.att_list.get_row_at_index(0)
        if row and self.att_box.get_visible():
            row.grab_focus()
        else:
            announce(self, "No attachments")

    def focus_status(self):
        self.status.set_focusable(True)
        self.status.grab_focus()

    def cycle_pane(self, step: int):
        panes = [(self.account_list, self.focus_accounts), (self.folder_view, self.focus_folders),
                 (self.msg_stack, self.focus_messages), (self.reading_pane, self.focus_reading),
                 (self.status, self.focus_status)]
        cur = next((i for i, (w, _) in enumerate(panes) if focus_within(self, w)), -1)
        panes[(cur + step) % len(panes)][1]()

    # ===== message actions ==================================================================
    def _session(self) -> AccountSession | None:
        return self.sessions.get(self.account.id) if self.account else None

    def _set_flag(self, msgs: list[Message], flag: str, on: bool):
        sess = self._session()
        if not sess or not self.folder:
            return
        key = flag.lower()
        changed = [m for m in msgs if (key in m.flags) != on]
        if not changed:
            return
        for m in changed:
            (m.flags.add if on else m.flags.discard)(key)
            for it in self.items_by_msg.get(m.key, []):
                it.bump()
        self.store.update_flags(self.account.id, self.folder.path, {m.uid: m.flags for m in changed})
        if key == "\\seen":
            item = self.folder_items.get(self.folder.path)
            if item:
                item.unread = max(0, item.unread + (-len(changed) if on else len(changed)))
                item.revision += 1
            self._update_status()
        sess.submit(sess.set_flag, self.folder.path, [m.uid for m in changed], flag, on,
                    on_error=lambda e: (self._error(e), self.sync_current()))

    def mark_selected(self, read: bool):
        msgs = self._selected_messages()
        if not msgs:
            return self.say("No message selected")
        self._set_flag(msgs, "\\Seen", read)
        self.say("Marked as read" if read else "Marked as unread")

    def toggle_star(self):
        msgs = self._selected_messages()
        if not msgs:
            return self.say("No message selected")
        on = not any(m.flagged for m in msgs)
        self._set_flag(msgs, "\\Flagged", on)
        self.say("Starred" if on else "Star removed")

    def _special(self, kind: str) -> Folder | None:
        node = folder_tree.find(self.tree, kind)
        return node.folder if node else None

    def _remove(self, msgs: list[Message], dest: Folder | None, spoken: str, forever=False):
        sess = self._session()
        if not sess or not self.folder or not msgs:
            return
        pos = self.msg_sel.get_selected()
        uids = [m.uid for m in msgs]
        gone = {m.key for m in msgs}
        path = self.folder.path
        self.store.delete(self.account.id, path, uids)
        unread = sum(1 for m in msgs if not m.seen)
        item = self.folder_items.get(path)
        if item and unread:
            item.unread = max(0, item.unread - unread)
            item.revision += 1
        self._populate([m for m in self.messages if m.key not in gone], keep_pos=pos)
        what = f"conversation, {len(msgs)} messages" if len(msgs) > 1 else ""
        self.say(f"{spoken} {what}".strip())
        on_error = lambda e: (self._error(e), self.sync_current())  # noqa: E731
        if forever:
            sess.submit(sess.delete_forever, path, uids, on_error=on_error)
        else:
            sess.submit(sess.move, path, uids, dest.path, on_error=on_error)

    def delete_selected(self):
        msgs = self._selected_messages()
        if not msgs:
            return self.say("No message selected")
        trash = self._special("trash")
        if trash is None or self.folder.path == trash.path or self.folder.special == "junk":
            n = len(msgs)
            self._confirm(f"Delete {'these ' + str(n) + ' messages' if n > 1 else 'this message'} forever?",
                          "It can't be recovered.", "_Delete Forever",
                          lambda: self._remove(msgs, None, "Deleted forever", forever=True))
            return
        self._remove(msgs, trash, "Deleted")

    def archive_selected(self):
        msgs = self._selected_messages()
        if not msgs:
            return self.say("No message selected")
        dest = self._special("archive") or (self._special("all") if self.account.is_gmail else None)
        if dest is None:
            return self.say("This account has no Archive folder", important=True)
        if dest.path == self.folder.path:
            return self.say(f"Already in {dest.display_name}")
        self._remove(msgs, dest, "Archived")

    def move_selected(self):
        msgs = self._selected_messages()
        if not msgs:
            return self.say("No message selected")
        from .pickers import FolderPicker
        current = self.folder.path

        def chosen(folder: Folder):
            if folder.path == current:
                return self.say("The message is already in that folder")
            self._remove(msgs, folder, f"Moved to {folder.display_name}")
        FolderPicker(self, self.tree, chosen).present()

    def jump_unread(self, step: int):
        n = self.msg_model.get_n_items()
        if not n or self.msg_stack.get_visible_child_name() != "list":
            return self.say("No messages")
        cur = self.msg_sel.get_selected()
        cur = -1 if cur == Gtk.INVALID_LIST_POSITION else cur
        i = cur + step
        while 0 <= i < n:
            row = self.msg_model.get_item(i)
            it = row.get_item()
            if it.unread and not (it.is_thread and row.get_expanded()):
                select_and_focus(self.msg_view, i)
                return
            i += step
        self.say("No more unread messages")

    def popup_message_menu(self):
        if self._selected_messages():
            self.msg_menu.popup()

    # ===== compose ==========================================================================
    def _with_parsed(self, cb):
        it = self._selected_item()
        if it is None:
            return self.say("No message selected")
        m = it.message
        if self.shown and self.shown[0].key == m.key:
            return cb(m, self.shown[1])
        sess = self.sessions[m.account_id]
        sess.submit(sess.fetch_body, m.folder, m.uid,
                    on_done=lambda raw: cb(m, mime.parse(raw, self.cfg.settings.prefer_html)),
                    on_error=self._error)

    def compose_new(self):
        from ..sending import Draft
        self._open_compose(Draft())

    def reply(self, reply_all: bool):
        from .. import sending

        def go(m, pm):
            acct = self.cfg.account(m.account_id) or self.account
            self._open_compose(sending.reply(acct, pm, reply_all, source=m.key), acct)
        self._with_parsed(go)

    def forward(self):
        from .. import sending
        self._with_parsed(lambda m, pm: self._open_compose(sending.forward(pm),
                                                           self.cfg.account(m.account_id)))

    def _open_compose(self, draft, account: Account | None = None):
        if not self.cfg.accounts:
            return self.say("Add an account first", important=True)
        from .compose import ComposeWindow
        ComposeWindow(self, account or self.account, draft).present()

    def message_sent(self, draft):
        self.say("Message sent")
        if draft.reply_source:
            for m in self.messages:
                if m.key == draft.reply_source:
                    m.flags.add("\\answered")
                    for it in self.items_by_msg.get(m.key, []):
                        it.bump()

    # ===== links, sync, settings ============================================================
    def show_links(self):
        if not self.shown:
            return self.say("Open a message first")
        links = self.shown[1].links
        if not links:
            return self.say("This message has no links")
        from .pickers import LinkPicker
        LinkPicker(self, links).present()

    def sync_current(self, announce_new: bool = False):
        sess = self._session()
        if not sess or not self.folder:
            return
        acct, path = self.account, self.folder.path
        if not self.messages:
            self._set_status(f"Loading {self.folder.display_name}…")
        sess.submit(sess.sync_folder, path,
                    on_done=lambda r: self._synced(acct, path, r, announce_new),
                    on_error=self._error)

    def _synced(self, acct: Account, path: str, result, announce_new: bool):
        if not (self.account and self.folder and acct.id == self.account.id and path == self.folder.path):
            return
        if result.changed or not self.messages:
            self._populate(self.store.messages(acct.id, path))
        self.last_sync = time.time()
        self._update_status()
        sess = self.sessions[acct.id]
        sess.submit(sess.folder_counts, [path], on_done=lambda c: self._counts_loaded(acct, c))
        fresh = [m for m in result.new if not m.seen]
        if announce_new and fresh:
            self.say(f"{len(fresh)} new message{'s' if len(fresh) > 1 else ''}")

    def refresh(self):
        if not self.account:
            return
        self.say("Checking for mail")
        sess = self._session()
        sess.submit(sess.list_folders, on_done=lambda f, a=self.account: self._folders_loaded(a, f),
                    on_error=self._error)
        self.sync_current(announce_new=True)

    def _periodic_check(self):
        if self.account and self.folder:
            self.sync_current(announce_new=True)
        return GLib.SOURCE_CONTINUE

    def _set_conversations(self, on: bool):
        self.cfg.settings.conversations = on
        self.cfg.save()
        self._populate(self.messages)
        self.say("Conversations on" if on else "Conversations off")

    def _set_prefer_html(self, on: bool):
        self.cfg.settings.prefer_html = on
        self.cfg.save()
        self.shown_key = None
        self._show_selected()
        self.say("Reading the formatted version" if on else "Reading the plain text version")

    # ===== accounts dialogs =================================================================
    def add_account(self):
        from .account_dialog import AccountDialog
        AccountDialog(self, None, self._account_saved).present()

    def edit_account(self):
        if not self.account:
            return self.say("No account selected")
        from .account_dialog import AccountDialog
        AccountDialog(self, self.account, self._account_saved).present()

    def _account_saved(self, acct: Account, is_new: bool):
        if is_new:
            self.cfg.accounts.append(acct)
        self.cfg.save()
        if not is_new and acct.id in self.sessions:
            self.sessions.pop(acct.id).shutdown()
            self.account = None
        self._reload_accounts(select_id=acct.id)
        self.say(f"Account {acct.name} {'added' if is_new else 'saved'}")

    def remove_account(self):
        acct = self.account
        if not acct:
            return self.say("No account selected")

        def go():
            self.cfg.accounts = [a for a in self.cfg.accounts if a.id != acct.id]
            self.cfg.save()
            credentials.forget(acct)
            self.store.forget_account(acct.id)
            self.account = None
            self.folder = None
            self.folder_root.remove_all()
            self._reload_accounts()
            self.say(f"Removed {acct.name}")
        self._confirm(f"Remove the account {acct.name}?",
                      "Your mail stays on the server. Only this computer forgets the account.",
                      "_Remove", go)

    # ===== helpers ==========================================================================
    def _confirm(self, message: str, detail: str, ok_label: str, on_ok):
        d = Gtk.AlertDialog(message=message, detail=detail, buttons=["_Cancel", ok_label],
                            cancel_button=0, default_button=0, modal=True)

        def done(dlg, res):
            try:
                if dlg.choose_finish(res) == 1:
                    on_ok()
            except GLib.Error:
                pass
        d.choose(self, None, done)

    def show_shortcuts(self):
        from .pickers import ShortcutsWindow
        ShortcutsWindow(self).present()

    def say(self, text: str, important: bool = False):
        self._set_status(text)
        announce(self, text, important)

    def _error(self, e: Exception):
        log.warning("error: %s", e)
        self.say(str(e) or e.__class__.__name__, important=True)

    def _set_status(self, text: str):
        self.status.set_text(text)

    def _update_status(self):
        if not self.folder:
            return
        unread = sum(1 for m in self.messages if not m.seen)
        parts = [self.folder.display_name, f"{len(self.messages)} messages"]
        if unread:
            parts.append(f"{unread} unread")
        if getattr(self, "last_sync", None):
            parts.append("checked " + time.strftime("%-I:%M %p", time.localtime(self.last_sync)))
        self._set_status(", ".join(parts))

    def _on_close(self, *_):
        w, h = self.get_default_size()
        self.cfg.settings.window_width, self.cfg.settings.window_height = w, h
        self.cfg.save()
        for s in self.sessions.values():
            s.shutdown()
        return False
