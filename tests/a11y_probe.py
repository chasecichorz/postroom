"""Press real keys at the app and print what a screen reader is told, like an Orca transcript.

Run with no screen of its own:
    xvfb-run -a dbus-run-session -- python3 -m tests.a11y_probe
"""
import os
import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

os.environ["GTK_A11Y"] = "atspi"
os.environ.pop("NO_AT_BRIDGE", None)

KEYS = [
    ("start", None),
    ("F10 menu", "F10"), ("Down menu", "Down"),
    ("Right menu", "Right"), ("Down menu", "Down"),
    ("Escape submenu", "Escape"), ("Escape menu bar", "Escape"),
    ("Shift+F10 context", "shift+F10"), ("Down context", "Down"),
    ("Escape context", "Escape"),
    ("Down", "Down"), ("Down", "Down"), ("Up", "Up"), ("Up", "Up"),
    ("Right (expand)", "Right"), ("Right (into)", "Right"), ("Down", "Down"),
    ("Left (parent)", "Left"), ("Left (collapse)", "Left"),
    ("Enter (open)", "Return"), ("Escape", "Escape"),
    ("Ctrl+2 folders", "ctrl+2"), ("Down", "Down"), ("Alt+I inbox", "alt+i"),
    ("Ctrl+2 folders", "ctrl+2"), ("Down", "Down"), ("Ctrl+1 accounts", "ctrl+1"),
    ("F6", "F6"), ("F6", "F6"), ("F6", "F6"), ("Shift+F6", "shift+F6"),
    ("Ctrl+3 messages", "ctrl+3"), ("K star", "k"), ("Ctrl+Q read", "ctrl+q"),
    ("Ctrl+U unread", "ctrl+u"), ("type N", "n"),
    ("Ctrl+N compose", "ctrl+n"), ("Tab", "Tab"), ("Alt+U subject", "alt+u"),
    ("Escape (close)", "Escape"),
    ("Ctrl+9 status", "ctrl+9"),
    ("Ctrl+F search", "ctrl+f"), ("Escape search", "Escape"),
    ("File menu", "alt+f"), ("Add Account", "a"),
    ("Account type", "alt+t"), ("Open account type", "space"),
    ("Next account type", "Down"), ("Choose type", "Return"),
    ("Close account setup", "Escape"),
]

log: list[str] = []


def describe(acc) -> str:
    try:
        role = acc.get_role_name()
        name = acc.get_name() or ""
        desc = acc.get_description() or ""
        states = acc.get_state_set()
        extra = [s for s, st in (("expanded", Atspi.StateType.EXPANDED),
                                 ("collapsed", Atspi.StateType.COLLAPSED),
                                 ("selected", Atspi.StateType.SELECTED)) if states.contains(st)]
        if not name and role in ("text", "label"):
            try:
                t = acc.get_text_iface() or acc
                name = "«" + Atspi.Text.get_text(t, 0, 60) + "»"
            except Exception:  # noqa: BLE001
                pass
        s = f"{role}: {name}"
        if desc:
            s += f" — {desc}"
        if extra:
            s += f" [{', '.join(extra)}]"
        return s
    except GLib.Error as e:
        return f"(gone: {e.message})"


def on_focus(event):
    if event.detail1:
        log.append("  focus → " + describe(event.source))


def on_announce(event):
    text = event.any_data if isinstance(event.any_data, str) else str(event.any_data)
    log.append(f"  says  → {text}")


def main():
    Atspi.init()
    app = subprocess.Popen([sys.executable, "-m", "tests.fake_app"],
                           env=dict(os.environ, GTK_A11Y="atspi"), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
    fl = Atspi.EventListener.new(on_focus)
    fl.register("object:state-changed:focused")
    al = Atspi.EventListener.new(on_announce)
    al.register("object:announcement")
    loop = GLib.MainLoop()
    steps = iter(KEYS)

    def next_step():
        try:
            label, key = next(steps)
        except StopIteration:
            loop.quit()
            return False
        log.append(f"[{label}]")
        if key:
            subprocess.run(["xdotool", "key", "--clearmodifiers", key], check=False)
        GLib.timeout_add(700, next_step)
        return False

    GLib.timeout_add(3500, next_step)
    GLib.timeout_add(60000, loop.quit)
    loop.run()
    app.terminate()
    try:
        out = app.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        app.kill()
        out = ""
    print("\n".join(log))
    if out.strip():
        print("app output:", out.strip())


if __name__ == "__main__":
    main()
