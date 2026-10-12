"""Eval-only fault wrapper around the actual watch CLI in an owned subprocess.

No production source/config hooks. Parent supplies an isolated cwd, clean env,
owned loopback endpoint and UUID markers. No Settings override or lease shortcut.
"""

import argparse
import json
import re
import time
from pathlib import Path

from app.chat import vector_sync, vector_worker
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from evals.check_memory_qdrant_faults import require

MODES = ("none", "before_remote", "after_remote")


def mark(spec, phase):
    path = Path(spec["directory"]) / (spec["token"] + "-" + phase + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"token": spec["token"], "phase": phase}), encoding="utf-8")
    temporary.replace(path)


def hold(spec):
    mark(spec, "gate")
    release = Path(spec["directory"]) / (spec["token"] + "-release.json")
    deadline = time.monotonic()+25
    while time.monotonic() < deadline:
        if release.exists():
            require(json.loads(release.read_text())["token"] == spec["token"], "release_identity_mismatch")
            return
        time.sleep(.02)
    raise RuntimeError("eval_gate_not_released")


def install_hooks(spec):
    preflight = vector_worker.check_vector_worker_target
    def prepared(*args):
        target = preflight(*args)
        mark(spec, "ready")
        return target
    vector_worker.check_vector_worker_target = prepared
    original_stop = vector_sync.VectorWorkerStop
    class ObservedStop(original_stop):
        marked = False
        def observe(self, grace):
            window = super().observe(grace)
            if window is not None and not self.marked:
                self.marked = True
                mark(spec, "stop")  # Supervisor path, never inside the signal handler.
            return window
    vector_sync.VectorWorkerStop = ObservedStop
    apply = QdrantVersionedVectorBackend.apply_mutation
    fired = [False]
    def gated(self, mutation):
        if not fired[0] and spec["mode"] == "before_remote":
            fired[0] = True
            hold(spec)
        revision = apply(self, mutation)
        if not fired[0] and spec["mode"] == "after_remote":
            fired[0] = True
            hold(spec)
        return revision
    QdrantVersionedVectorBackend.apply_mutation = gated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    require(Path(spec["directory"]).resolve() == Path.cwd() and args.spec.resolve().parent == Path.cwd(),
            "child_directory_mismatch")
    require(not (Path.cwd()/".env").exists(), "child_private_env_forbidden")
    require(re.fullmatch(r"[a-f0-9]{32}", spec["token"]) and spec["mode"] in MODES, "invalid_child_spec")
    install_hooks(spec)
    vector_sync.main(["--watch", "--database", spec["database"],
                      "--model-fingerprint", spec["model"], "--dimensions", str(spec["dimensions"])])


if __name__ == "__main__":
    main()
