"""Application entry point."""

from __future__ import annotations

import tkinter as tk

from smsdesk.app import SmsDeskApp


def main() -> None:
    root = tk.Tk()
    SmsDeskApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
