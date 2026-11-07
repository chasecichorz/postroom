"""Type into To in a compose window and print what a screen reader is told.

    xvfb-run -a dbus-run-session -- python3 -m tests.completion_probe
"""
import os
import subprocess
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

os.environ["GTK_A11Y"] = "atspi"
STEPS = [("ctrl+n", None), (None, "an"), ("Down", None), ("Up", None), ("Return", None),
         (None, "bo"), ("Escape", None), ("Tab", None)]


def text_of(acc):
    try:
        ti = acc.get_text_iface()
        return Atspi.Text.get_text(ti, 0, 200) if ti else ""
    except Exception:  # noqa: BLE001
        return ""


def main():
    Atspi.init()
    app = subprocess.Popen([sys.executable, "-m", "tests.fake_app"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    last = {}

    def on(ev):
        if ev.type.startswith("object:announcement"):
            print("  says  →", ev.any_data, flush=True)
        elif ev.detail1:
            last["focus"] = ev.source
            print("  focus →", ev.source.get_role_name(), repr(ev.source.get_name()), flush=True)
    listener = Atspi.EventListener.new(on)
    listener.register("object:state-changed:focused")
    listener.register("object:announcement")
    steps = iter(STEPS)

    def step():
        if "focus" in last:
            t = text_of(last["focus"])
            if t:
                print("  field text:", repr(t), flush=True)
        try:
            key, typed = next(steps)
        except StopIteration:
            loop.quit()
            return False
        if key:
            print(f"[{key}]", flush=True)
            subprocess.run(["xdotool", "key", key])
        else:
            print(f"[type {typed!r}]", flush=True)
            subprocess.run(["xdotool", "type", "--delay", "40", typed])
        GLib.timeout_add(900, step)
        return False
    loop = GLib.MainLoop()
    GLib.timeout_add(4000, step)
    GLib.timeout_add(30000, loop.quit)
    loop.run()
    app.terminate()


if __name__ == "__main__":
    main()
