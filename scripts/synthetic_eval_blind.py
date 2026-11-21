#!/usr/bin/env python3
"""Blind synthetic quality check of the look-back model step.

Runs a prepared input.json of INVENTED conversations through the exact production
assessment request (same instructions, system prompt, schema, bounds and validator as
hermes_tingle_assess.request) and preserves the first RETURNED raw model output of each
request verbatim for independent scoring. If the host rejects a call before a response
returns, no raw text exists and only the generic failure type is recorded. No expected
labels live here: scoring happens outside, against a withheld answer key. No mailbox,
plugin install, tools, source fetches or side effects.

Re-entry hardening: one run gets one FRESH evidence directory, created exclusively; any
process that finds the directory present refuses before any model call, whatever token or
environment it inherited. There is no continuation or resume: an interrupted attempt is
terminal for its directory, and a rerun is a new human decision with a new --out. The
manifest is written before the first call, then BEFORE each request is dispatched
(write-ahead: the entry sits at "in_flight" while the provider outcome is unknown) and
again after every returned request, so an interrupted attempt always leaves evidence that
marks exactly which request, if any, has an ambiguous outcome. First returned outputs are
never overwritten, because no second process can ever run into the same directory.

Dry run by default: prints the request plan and calls nothing. A real run needs both
--run and --i-approve-model-calls plus a Hermes runtime on PYTHONPATH, and spends tokens
on the model Hermes has configured. The cost depends on that provider, cannot be capped
or read from here, and host retry/fallback can add attempts. An actual run (--run)
performs the supported Hermes startup (hermes_bootstrap) before claiming any evidence
directory; startup may finish a pending host update or repair. If startup cannot
settle, the run refuses before any evidence directory is created. A dry run calls
nothing and does no startup work.
"""
import argparse, hashlib, importlib, json, os, sys, time, uuid
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hermes_tingle_assess as assess

DEFAULT_INPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic_eval_blind_input.json")


def load(path):
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    data = json.loads(raw)
    records = data["conversations"]
    if not isinstance(records, list) or not records:
        raise SystemExit("input.json must hold a non-empty conversations list")
    evidence = {r["id"]: {m["n"]: (m["text"], m["direction"]) for m in r["messages"]} for r in records}
    return records, evidence, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan(records):
    batches = assess.batches(records)
    chars = sum(len(json.dumps({"conversations": b}, ensure_ascii=False, separators=(",", ":"))) for b in batches)
    return batches, chars


def _fresh_evidence_dir(out):
    """Claim a fresh evidence directory, or refuse before any model call.

    There is deliberately no continuation path: a second process - same host re-exec,
    spawned child, inherited environment, anything - finds the directory and exits.
    """
    try:
        os.makedirs(out)
    except FileExistsError:
        raise SystemExit(_REFUSE_MSG)
    except OSError as exc:
        raise SystemExit(f"cannot create --out: {exc}")
    # Ownership stamp for forensics only. It grants no continuation rights to anyone.
    with open(os.path.join(out, "run.token"), "w", encoding="utf-8") as fh:
        json.dump({"token": uuid.uuid4().hex, "pid": os.getpid(), "ppid": os.getppid()}, fh)


_REFUSE_MSG = ("--out already exists; refusing to overwrite or continue earlier evidence. "
               "An interrupted attempt is terminal for its directory; a rerun is a new "
               "decision with a new --out.")
_SETTLE_MSG = ("host startup (hermes_bootstrap) did not settle after {max_hops} warm-up "
               "attempts; refusing to continue. No evidence directory was created or "
               "claimed; this failure is not resumable.")
_BAD_HOPS_MSG = ("host startup hop marker TINGLE_EVAL_BOOTSTRAP_HOPS is malformed "
                 "(not a non-negative integer); refusing to continue. No evidence "
                 "directory was created or claimed.")
_HOPS_ENV = "TINGLE_EVAL_BOOTSTRAP_HOPS"  # hop marker inherited across os.execv
_MAX_HOPS = 3


def _warm_host_bootstrap():
    """Run the supported Hermes startup before claiming evidence, in every actual process.

    hermes_bootstrap performs the host's supported launch preparation and may os.execv
    this process when startup work is pending. We import it before any evidence claim so
    a re-exec cannot strand an in-flight directory the way the first host attempts did.
    The hop marker survives execv; exceeding the bound fails closed BEFORE claim. It is
    never used to skip startup. A missing hermes_bootstrap fails closed on the pinned host.
    """
    raw = os.environ.get(_HOPS_ENV)
    if raw is None:
        hops = 1
    else:
        try:
            hops = int(raw) + 1
        except ValueError:
            raise SystemExit(_BAD_HOPS_MSG)
        if hops < 1:
            raise SystemExit(_BAD_HOPS_MSG)
    if hops > _MAX_HOPS:
        raise SystemExit(_SETTLE_MSG.format(max_hops=_MAX_HOPS))
    os.environ[_HOPS_ENV] = str(hops)
    importlib.import_module("hermes_bootstrap")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--out", help="fresh output directory for raw responses and metadata (required with --run)")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--i-approve-model-calls", action="store_true")
    args = ap.parse_args()
    records, evidence, digest = load(args.input)
    batches, chars = plan(records)
    print(f"{len(records)} synthetic conversations, {len(batches)} request(s) "
          f"({', '.join(str(len(b)) for b in batches)}), about {chars} input characters, "
          f"requested output budget {assess.MAX_TOKENS} tokens per request, requested timeout "
          f"{assess.TIMEOUT:.0f}s each (request parameters; host forwarding and provider "
          f"enforcement vary, neither is an end-to-end cap). "
          f"Host retries/fallback can add attempts. input sha256 {digest} (pid {os.getpid()}, ppid {os.getppid()})")
    if not (args.run and args.i_approve_model_calls):
        print("Dry run: no model was called.")
        return 0
    if not args.out:
        raise SystemExit("--out is required with --run and must not pre-exist")
    if os.path.exists(args.out):
        raise SystemExit(_REFUSE_MSG)  # cheap repeat refusal before any host startup work
    _warm_host_bootstrap()  # every actual process; may re-exec or fail closed before claim
    _fresh_evidence_dir(args.out)
    from agent.plugin_llm import PluginLlm
    llm = PluginLlm(plugin_id="hermes-tingle")
    manifest = {"input_sha256": digest, "conversations": len(records), "requests": [],
                "purpose": assess.PURPOSE, "schema_name": assess.SCHEMA_NAME,
                "max_tokens": assess.MAX_TOKENS, "timeout": assess.TIMEOUT,
                "status": "running", "pid": os.getpid(), "ppid": os.getppid(),
                "note": "first returned raw outputs preserved verbatim; scoring happens outside with a withheld key"}

    def save_manifest():
        with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)

    save_manifest()  # Evidence exists before the first call, whatever the host does next.
    try:
        for i, batch in enumerate(batches, 1):
            # Same call shape as hermes_tingle_assess.request: no overrides, no tools, bounded.
            payload = json.dumps({"conversations": batch}, ensure_ascii=False, separators=(",", ":"))
            meta = {"request": i, "conversations": len(batch), "input_chars": len(payload),
                    "status": "in_flight", "pid": os.getpid()}
            manifest["requests"].append(meta)
            save_manifest()  # Write-ahead: a death during the call leaves this request marked ambiguous.
            started = time.monotonic()
            try:
                result = llm.complete_structured(
                    instructions=assess.INSTRUCTIONS, input=[{"type": "text", "text": payload}],
                    json_schema=assess.SCHEMA, schema_name=assess.SCHEMA_NAME,
                    system_prompt=assess.SYSTEM_PROMPT, max_tokens=assess.MAX_TOKENS,
                    timeout=assess.TIMEOUT, purpose=assess.PURPOSE)
                meta["seconds"] = round(time.monotonic() - started, 3)
                meta["status"] = "returned"
            except Exception as exc:  # record failure honestly, never echo model/error text
                meta["seconds"] = round(time.monotonic() - started, 3)
                meta["status"] = "failed"
                meta["failure"] = type(exc).__name__
                manifest["status"] = "failed"
                save_manifest()
                print(f"request {i} failed ({type(exc).__name__}); stopped without further spend. Evidence in {args.out}")
                return 1
            raw = result.get("text") if isinstance(result, dict) else getattr(result, "text", None)
            parsed = result.get("parsed") if isinstance(result, dict) else getattr(result, "parsed", None)
            for attr in ("provider", "model"):
                value = result.get(attr) if isinstance(result, dict) else getattr(result, attr, None)
                if value:
                    meta[attr] = value
            usage = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)
            if usage is not None:
                meta["usage"] = usage if isinstance(usage, dict) else getattr(usage, "__dict__", str(usage))
            with open(os.path.join(args.out, f"request-{i}.raw.txt"), "w", encoding="utf-8") as fh:
                fh.write(raw if isinstance(raw, str) else repr(raw))
            with open(os.path.join(args.out, f"request-{i}.parsed.json"), "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, ensure_ascii=False, indent=1)
            # Production scopes validation to the batch's own conversations and evidence,
            # and a malformed response ends the run instead of being scored around.
            batch_evidence = {r["id"]: evidence[r["id"]] for r in batch}
            try:
                validated = assess.validate(parsed, batch_evidence)
            except assess.AssessmentError:
                meta["status"] = "rejected"
                meta["validation"] = "rejected: malformed assessment"
                manifest["status"] = "failed"
                save_manifest()
                print(f"request {i} rejected by the production validator; stopped without further spend. Evidence in {args.out}")
                return 1
            meta["validated"] = {cid: {"disposition": v["disposition"], "supported": v["supported"]}
                                 for cid, v in validated.items()}
            meta["validation"] = "ok"
            save_manifest()
            print(f"request {i}: returned raw output and validation outcome recorded")
    except BaseException as exc:  # unexpected host behavior still leaves a record
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            manifest["status"] = "interrupted"
        else:
            manifest["status"] = "failed"
            manifest["failure"] = type(exc).__name__
        save_manifest()
        raise
    manifest["status"] = "done"
    save_manifest()
    print(f"Done. Return the whole {args.out} directory unmodified for independent scoring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
