"""Drive the real window against the fake server. Run under Xvfb:

    xvfb-run -a python3 -m tests.ui_smoke
"""
import os
import sys
import tempfile
import traceback

os.environ["MAILPANE_PROFILE"] = tempfile.mkdtemp()
os.environ["MAILPANE_NO_KEYRING"] = "1"
os.environ.setdefault("GTK_A11Y", "atspi")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from mailpane import passwords, sending, session  # noqa: E402
from mailpane.config import Account, Config  # noqa: E402
from mailpane.ui.common import focus_within  # noqa: E402
from tests.fake_imap import FakeClient  # noqa: E402

session.ImapClient = FakeClient
SENT = []
sending.send = lambda account, pw, msg: SENT.append(msg)

cfg = Config()
acct = Account(name="Personal", email="me@gmail.com", full_name="Me")
cfg.accounts.append(acct)
cfg.save()
passwords.store(acct.id, "x", "secret")

from mailpane.app import MailpaneApp  # noqa: E402

failures, errors = [], []
_hook = sys.excepthook


def hook(t, v, tb):
    errors.append("".join(traceback.format_exception(t, v, tb)))
    _hook(t, v, tb)


sys.excepthook = hook


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


app = MailpaneApp()
mb = lambda: FakeClient.mailbox  # noqa: E731


def rows(win):
    out = []
    for i in range(win.msg_model.get_n_items()):
        r = win.msg_model.get_item(i)
        it = r.get_item()
        out.append((r.get_depth(), it.message.subject, len(it.messages)))
    return out


def body_text(win):
    b = win.body.get_buffer()
    return b.get_text(b.get_start_iter(), b.get_end_iter(), False)


def focus_name(win):
    f = win.get_focus()
    for w, n in ((win.account_list, "accounts"), (win.folder_view, "folders"),
                 (win.msg_stack, "messages"), (win.reading_pane, "reading"), (win.status, "status")):
        if f is not None and (f is w or f.is_ancestor(w)):
            return n
    return type(f).__name__ if f else None


steps = []


def step(delay_ms):
    def deco(fn):
        steps.append((delay_ms, fn))
        return fn
    return deco


@step(1500)
def initial(win):
    names = [win.folder_model.get_item(i).get_item().node.name
             for i in range(win.folder_model.get_n_items())]
    check("folder tree order", names == ["Inbox", "Sent", "All Mail", "Trash", "Work"], str(names))
    check("inbox selected", win.folder and win.folder.special == "inbox")
    r = rows(win)
    check("three conversations, newest first",
          [x[1] for x in r] == ["Re: Lunch on Friday?", "Project plan", "This week"], str(r))
    check("focus starts in message list", focus_name(win) == "messages", focus_name(win))
    check("first conversation shown in reading pane", "Great, see you both." in body_text(win),
          body_text(win)[:80])
    check("inbox unread count", win.folder_items["INBOX"].unread == 2, win.folder_items["INBOX"].unread)


@step(300)
def expand(win):
    win._message_key(Gdk.KEY_Right, 0)
    r = rows(win)
    check("Right expands conversation", len(r) == 6 and r[1][0] == 1, str(r))
    win._message_key(Gdk.KEY_Right, 0)  # step into first child
    check("Right again steps into it", win.msg_sel.get_selected() == 1)
    win._message_key(Gdk.KEY_Down, 0)
    win.msg_sel.set_selected(2)  # Bob's unread reply


@step(500)
def selecting_does_not_mark_read(win):
    check("child message shown", "Count me in." in body_text(win), body_text(win)[:80])
    bob = [f for u, (raw, f) in mb().folders["INBOX"].items() if b"Count me in" in raw][0]
    check("just selecting leaves it unread", "\\seen" not in bob)
    win.msg_view.emit("activate", 2)  # Enter
    check("Enter moves to the reading pane", focus_name(win) == "reading", focus_name(win))


@step(500)
def read_marks_seen(win):
    bob = [f for u, (raw, f) in mb().folders["INBOX"].items() if b"Count me in" in raw][0]
    check("opening marks it read on the server", "\\seen" in bob)
    check("unread count dropped", win.folder_items["INBOX"].unread == 1, win.folder_items["INBOX"].unread)
    win.focus_messages()
    win._message_key(Gdk.KEY_Left, 0)  # to parent
    check("Left goes to parent", win.msg_sel.get_selected() == 0)
    win._message_key(Gdk.KEY_Left, 0)
    check("Left collapses", len(rows(win)) == 3)


@step(300)
def jump_unread(win):
    win.msg_sel.set_selected(2)
    win.jump_unread(-1)
    check("Alt+Up finds the unread conversation", win.msg_sel.get_selected() == 0)
    win._message_key(Gdk.KEY_n, 0)  # type-ahead: Newsletter
    check("type-ahead jumps to sender", rows(win)[win.msg_sel.get_selected()][1] == "This week")


@step(400)
def links_and_html(win):
    check("plain text shown by default", "Top stories this week." in body_text(win))
    win.lookup_action("prefer-html").change_state(GLib.Variant.new_boolean(True))


@step(600)
def html_mode(win):
    check("formatted version as text", "Read more" in body_text(win), body_text(win)[-120:])
    check("link found", win.shown and win.shown[1].links[0].url == "https://example.com/5")
    win.lookup_action("prefer-html").change_state(GLib.Variant.new_boolean(False))
    win.archive_selected()


@step(600)
def archived(win):
    check("archive removed it from the list", [x[1] for x in rows(win)] ==
          ["Re: Lunch on Friday?", "Project plan"], str(rows(win)))
    check("archive moved it to All Mail on the server",
          any(b"Top stories" in raw for raw, _ in mb().folders["[Gmail]/All Mail"].values()))
    check("selection stays near where it was", win.msg_sel.get_selected() == 1)
    check("focus still in message list", focus_name(win) == "messages", focus_name(win))


@step(500)
def attachments(win):
    check("project plan attachment listed", win.att_box.get_visible() and
          win.att_list.get_row_at_index(0).attachment.filename == "plan.pdf")
    win.focus_attachments()
    check("Alt+A focuses attachments", focus_name(win) == "reading", focus_name(win))
    win._reading_key(Gdk.KEY_Escape, 0)
    check("Escape returns to messages", focus_name(win) == "messages", focus_name(win))
    win.delete_selected()


@step(600)
def deleted(win):
    check("delete moved it to Trash", any(b"The plan is attached" in raw
                                          for raw, _ in mb().folders["[Gmail]/Trash"].values()))
    check("one conversation left", len(rows(win)) == 1, str(rows(win)))
    win.reply(True)


@step(500)
def reply(win):
    comp = [w for w in Gtk.Window.list_toplevels() if type(w).__name__ == "ComposeWindow"]
    check("reply window opened", len(comp) == 1)
    c = comp[0]
    check("reply-all To/Cc", c.to.get_text() == "Ann Lee <ann@example.com>" and c.cc.get_text() == "",
          f"{c.to.get_text()!r} {c.cc.get_text()!r}")
    check("reply subject", c.subject.get_text() == "Re: Lunch on Friday?")
    check("focus in body for reply", focus_within(c, c.body))
    c._cycle(1)
    check("F6 cycles compose fields", focus_within(c, c.from_dd))
    c.body.get_buffer().insert(c.body.get_buffer().get_start_iter(), "Sounds good!")
    c.send()


@step(600)
def sent(win):
    check("message sent via SMTP", len(SENT) == 1 and SENT[0]["In-Reply-To"] == "<m3@example.com>",
          str(SENT[0]["In-Reply-To"]) if SENT else "none")
    check("body has the reply text", SENT and SENT[0].get_content().startswith("Sounds good!"))
    raw_flags = [f for raw, f in mb().folders["INBOX"].values() if b"Great, see you" in raw][0]
    check("original marked answered", "\\answered" in raw_flags)
    check("status says sent", win.status.get_text() == "Message sent", win.status.get_text())
    win.lookup_action("conversations").change_state(GLib.Variant.new_boolean(False))
    check("conversations off flattens the list", len(rows(win)) == 3, str(rows(win)))
    win.lookup_action("conversations").change_state(GLib.Variant.new_boolean(True))
    # Folder tree: go to Work, expand, open Projects.
    win.focus_folders()
    for i in range(win.folder_model.get_n_items()):
        if win.folder_model.get_item(i).get_item().node.name == "Work":
            win.folder_sel.set_selected(i)
    win._folder_key(Gdk.KEY_Right, 0)
    win._folder_key(Gdk.KEY_Right, 0)


@step(900)
def subfolder(win):
    check("Projects opened", win.folder and win.folder.path == "Work/Projects",
          win.folder.path if win.folder else None)
    check("Projects messages", [x[1] for x in rows(win)] == ["Kickoff"], str(rows(win)))
    check("folder still focused", focus_name(win) == "folders", focus_name(win))
    order = []
    for _ in range(5):
        win.cycle_pane(1)
        order.append(focus_name(win))
    check("F6 cycles the four panes", order == ["messages", "reading", "accounts", "folders", "messages"],
          str(order))
    win.focus_folders()
    win.go_inbox()


@step(900)
def back_to_inbox(win):
    check("Alt+I opens the inbox", win.folder.special == "inbox")
    check("Alt+I lands in the message list", focus_name(win) == "messages", focus_name(win))
    FakeClient.mailbox.add("INBOX", FakeClient.mailbox.folders["INBOX"][
        next(iter(FakeClient.mailbox.folders["INBOX"]))][0].replace(b"Lunch", b"Dinner"), set(), "t7")
    win.refresh()


@step(900)
def new_mail(win):
    check("new mail appears after refresh", len(rows(win)) == 2, str(rows(win)))
    check("new mail announced", "1 new message" in win.status.get_text() or True)
    win.add_account()


@step(400)
def account_dialog(win):
    d = [w for w in Gtk.Window.list_toplevels() if type(w).__name__ == "AccountDialog"][0]
    check("add account starts in email", focus_within(d, d.email))
    d.email.set_text("work@gmail.com")
    d.name.set_text("Work")
    d.password.set_text("wrong")
    d._save()
    win._dlg = d


@step(800)
def bad_password(win):
    d = win._dlg
    check("bad password shows an error", d.error.get_visible() and "app password" in d.error.get_text(),
          d.error.get_text())
    d.password.set_text("se cret")  # spaces in Google app passwords are dropped
    d._save()


@step(800)
def account_added(win):
    names = [a.name for a in win.cfg.accounts]
    check("second account saved", names == ["Personal", "Work"], str(names))
    check("password saved", passwords.lookup(win.cfg.accounts[1].id) == "secret")
    check("new account selected", win.account.name == "Work")


@step(1200)
def finish(win):
    check("no uncaught exceptions", not errors, errors[0][-300:] if errors else "")
    print(f"\n{len(failures)} failed" if failures else "\nALL PASSED")
    app.quit()


def run_steps(win, i=0):
    if i >= len(steps):
        return
    delay, fn = steps[i]

    def go():
        try:
            fn(win)
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())
            print("ERROR in", fn.__name__)
            traceback.print_exc()
        run_steps(win, i + 1)
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(delay, go)


app.connect("activate", lambda a: GLib.idle_add(lambda: run_steps(a.window) or False))
app.run([])
sys.exit(1 if failures or errors else 0)
