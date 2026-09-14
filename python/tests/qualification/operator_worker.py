"""Use the installed public operator CLI, optionally stopping at one test-only durable boundary."""

from __future__ import annotations

import argparse
import os
import signal
from typing import Any
from unittest.mock import patch

from sde.local_cutover import LocalCutover
from sde_operator import __main__ as cli


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--checkpoint")
    selected, arguments = parser.parse_known_args()
    if selected.checkpoint is not None:
        checkpoint = selected.checkpoint

        class StoppedOperator(LocalCutover):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._after_step = self.stop

            def stop(self, step: str) -> None:
                if step == checkpoint:
                    print("CRASH_READY", flush=True)
                    os.kill(os.getpid(), signal.SIGSTOP)

        with patch.object(cli, "LocalCutover", StoppedOperator):
            return cli.main(arguments)
    return cli.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
