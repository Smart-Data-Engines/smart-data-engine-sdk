"""Schedule local stage/cutover while the separate application processes keep issuing traffic."""

from __future__ import annotations

import json
import select
import subprocess
import time
from pathlib import Path
from typing import Any

from sde import StagingPlan, _local_state

from . import packets


class Transition:
    def __init__(
        self, args: Any, configured: dict[str, Any], origin: int, environment: dict[str, str]
    ):
        self.args, self.configured, self.origin, self.environment = (
            args,
            configured,
            origin,
            environment,
        )
        self.staged: StagingPlan | None = None
        self.finished = False
        self.events: list[dict[str, Any]] = []
        self.operator = Path(__file__).with_name("operator_worker.py")

    def record(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        _local_state.write_text(
            self.configured["directory"] / "events.json",
            json.dumps(self.events, indent=2, allow_nan=False),
        )

    def command(self, action: str, document: dict[str, Any] | None = None) -> list[str]:
        arguments = [
            str(self.args.python),
            str(self.operator),
            "--project-dir",
            str(self.configured["directory"] / "state"),
            "--config",
            str(self.configured["config"]),
            action,
        ]
        if document is not None:
            path = self.configured["directory"] / (action + "-packet.json")
            path.write_text(json.dumps(document))
            arguments.extend(["--plan", str(path)])
        return arguments

    def execute(self, action: str, document: dict[str, Any] | None = None) -> dict[str, Any]:
        began = time.monotonic_ns()
        result = subprocess.run(
            self.command(action, document),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        (self.configured["directory"] / (action + ".stderr")).write_text(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"local {action} did not finish; inspect its local stderr")
        receipt: dict[str, Any] = json.loads(result.stdout)
        self.record(
            {
                "action": action,
                "start_ns": str(began),
                "end_ns": str(time.monotonic_ns()),
                "receipt": receipt,
            }
        )
        if "observe" in self.configured:
            self.configured["observe"](action, receipt)
        return receipt

    def step(self) -> None:
        if self.args.mode == "baseline" or self.finished:
            return
        now = time.monotonic_ns()
        if self.staged is None and now >= self.origin + 10_000_000_000:
            self.staged = packets.stage(self.configured, version=2)
            receipt = self.execute("stage", self.staged.as_record())
            if receipt["map_fingerprint"] != self.staged.prepared.fingerprint:
                raise AssertionError("staging did not publish its authorized map")
            print("STAGED fresh copy under application traffic", flush=True)
        if self.staged is None or time.monotonic_ns() < self.origin + 25_000_000_000:
            return
        plan = packets.cutover(self.configured, self.staged)
        if self.args.mode == "success":
            receipt = self.execute("execute", plan.as_record())
        else:
            checkpoint = (
                "repair:intent" if self.args.mode == "before_decision" else "decision:success"
            )
            command = self.command("execute", plan.as_record())
            command[2:2] = ["--checkpoint", checkpoint]
            began = time.monotonic_ns()
            stderr_path = self.configured["directory"] / "crashed-operator.stderr"
            with stderr_path.open("w") as errors:
                process = subprocess.Popen(
                    command, env=self.environment, stdout=subprocess.PIPE, stderr=errors, text=True
                )
                try:
                    assert process.stdout is not None
                    if (
                        not select.select([process.stdout], [], [], 40)[0]
                        or process.stdout.readline() != "CRASH_READY\n"
                    ):
                        raise RuntimeError("operator missed the selected real crash boundary")
                    time.sleep(1)
                    process.kill()
                    process.wait(timeout=10)
                    if process.returncode != -9:
                        raise AssertionError("the operator was not terminated by SIGKILL")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    if process.stdout is not None:
                        process.stdout.close()
            self.record(
                {
                    "action": "SIGKILL",
                    "checkpoint": checkpoint,
                    "start_ns": str(began),
                    "end_ns": str(time.monotonic_ns()),
                }
            )
            receipt = self.execute("resume")
        expected = "abort" if self.args.mode == "before_decision" else "success"
        candidate = plan.abort if expected == "abort" else plan.success
        if (
            receipt["outcome"] != expected
            or receipt["plan_fingerprint"] != plan.fingerprint
            or receipt["map_fingerprint"] != candidate.fingerprint
        ):
            raise AssertionError("cutover recovery did not preserve the authorized outcome")
        if self.args.mode != "success" and receipt["recovered"] is not True:
            raise AssertionError("an interrupted operator must report recovery")
        self.finished = True
        print(f"CUTOVER {expected}: continuing traffic on the final authorized map", flush=True)
