"""Fake host bootstrap for OFFLINE regression tests. Models the real hermes_bootstrap
import-time behavior: prepare_launch, and while the environment is unsettled, a
pid-preserving re-exec that re-runs the script. Prints a marker each time it FIRES so
tests can count startup work. FAKE_BOOTSTRAP=once|never; absent module = no bootstrap."""
import os, sys

_MODE = os.environ.get("FAKE_BOOTSTRAP", "once")
_STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fake_settled")

_out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
if _out and os.path.exists(os.path.join(_out, "run.token")):
    print("FAKE-BOOTSTRAP-RAN-AFTER-EVIDENCE-CLAIM", flush=True)

if _MODE == "once" and not os.path.exists(_STAMP):
    open(_STAMP, "w").write("settled")
    print("FAKE-BOOTSTRAP-FIRED", flush=True)
    os.execv(sys.executable, [sys.executable] + [os.path.abspath(sys.argv[0])] + sys.argv[1:])
elif _MODE == "never":
    print("FAKE-BOOTSTRAP-FIRED", flush=True)
    os.execv(sys.executable, [sys.executable] + [os.path.abspath(sys.argv[0])] + sys.argv[1:])
