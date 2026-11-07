#!/usr/bin/env python3
"""Open a PNG in an X11 window, recovering a stale tmux display once."""

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk


def refresh_tmux_display() -> bool:
    if not os.environ.get("TMUX"):
        return False

    changed = False
    for name in ("DISPLAY", "XAUTHORITY"):
        command = ["tmux", "show-environment"]
        if os.environ.get("TMUX_PANE"):
            command.extend(["-t", os.environ["TMUX_PANE"]])
        try:
            result = subprocess.run(command + [name], capture_output=True, text=True, check=False)
        except OSError:
            return False
        if result.returncode != 0:
            continue
        setting = result.stdout.strip()
        if setting.startswith(f"{name}="):
            value = setting[len(name) + 1:]
            changed |= os.environ.get(name) != value
            os.environ[name] = value
        elif setting == f"-{name}":
            changed |= name in os.environ
            os.environ.pop(name, None)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("png", type=Path, help="PNG file to display")
    args = parser.parse_args()

    if not args.png.is_file():
        parser.error(f"file not found: {args.png}")
    try:
        window = tk.Tk()
    except tk.TclError as error:
        if "display" not in str(error).lower() or not refresh_tmux_display() or not os.environ.get("DISPLAY"):
            print(f"Could not open X11 window: {error}", file=sys.stderr)
            return 1
        try:
            window = tk.Tk()
        except tk.TclError as retry_error:
            print(f"Could not open X11 window after refreshing DISPLAY: {retry_error}", file=sys.stderr)
            return 1

    try:
        image = tk.PhotoImage(file=str(args.png))
        max_width = max(1, int(window.winfo_screenwidth() * 0.7))
        max_height = max(1, int(window.winfo_screenheight() * 0.7))
        scale = max(1, math.ceil(image.width() / max_width), math.ceil(image.height() / max_height))
        if scale > 1:
            image = image.subsample(scale, scale)
    except tk.TclError as error:
        window.destroy()
        print(f"Could not open PNG: {error}", file=sys.stderr)
        return 1

    window.title(args.png.name)
    label = tk.Label(window, image=image)
    label.pack()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
