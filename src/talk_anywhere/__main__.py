"""Talk Anywhere entry point."""

from __future__ import annotations

import os
import sys


def main() -> None:
    args = sys.argv[1:]
    if args == ["install"]:
        from talk_anywhere.install_desktop import install

        install()
        return
    if args == ["selftest"]:
        from talk_anywhere.committer import selftest
        from talk_anywhere.mics import list_mics

        selftest()
        print("MICROPHONES")
        for mic in list_mics():
            print(f"{mic.description}\t{mic.name}\t{mic.spec}")
        return
    from talk_anywhere.single_instance import send_command

    if args[:1] == ["toggle"]:
        if send_command("toggle") is not None:
            return
        os.environ["TALK_ANYWHERE_ON_READY"] = "toggle"
    elif args[:1] == ["show"]:
        if send_command("show") is not None:
            return
    elif send_command("show") is not None:
        return

    from talk_anywhere.app import run

    run()


if __name__ == "__main__":
    main()
