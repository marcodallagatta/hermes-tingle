#!/usr/bin/env python3
"""Controlled quality check of the look-back model step on SYNTHETIC mixed-language mail.

No mailbox, no Tingle install and no real data are involved: the corpus below is invented. By default this
only prints what a run WOULD send (exactly 2 logical requests: 16 conversations in batches of 12 and 4; characters) and calls no model. A real run needs both
--run and --i-approve-model-calls, a Hermes runtime on PYTHONPATH, and spends tokens on the model that
Hermes has configured (cost depends on that provider and cannot be capped or read from here).
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hermes_tingle_assess as assess

# (id, expected_action_needed, messages newest first: (direction, text))
CORPUS = [
 ("S1", True, [("received", "Pat. Subject: Contract. Could you send me the signed contract before Friday?")]),
 ("S2", True, [("received", "Subject: Compravendita. Puoi inviarmi la visura aggiornata entro giovedi'? Serve per il notaio.")]),
 ("S3", True, [("received", "Landlord. Subject: Deposit. Please confirm your new bank details so we can return the deposit.")]),
 ("S4", True, [("received", "Bank. Subject: Documento scaduto. Devi caricare il documento d'identita' aggiornato entro il 30.")]),
 ("S5", True, [("received", "Alex. Subject: Weekend trip. Can you check the two hotel options and tell me which one you prefer?")]),
 ("S6", False, [("received", "Thanks, got it, all done on my side."), ("received", "Pat. Could you send me the signed contract?")]),
 ("S7", False, [("received", "Shop. Subject: Weekend sale. Can you believe these prices? Shop now, 40% off everything!")]),
 ("S8", False, [("received", "Newsletter. Subject: Free webinar. Please register now to save your seat for our free webinar.")]),
 ("S9", False, [("sent_by_owner", "Sure, I will send it tomorrow."), ("received", "Pat. Could you send the file?")]),
 ("S10", False, [("received", "Do not sign the contract until legal has approved it. No action needed from you now.")]),
 ("S11", False, [("received", "Non devi pagare la fattura, e' stata gia' saldata. Nessuna azione richiesta.")]),
 ("S12", False, [("received", "Order #123 has shipped. Track your parcel here. Thank you for your purchase.")]),
 ("S13", False, [("received", "Colleague. Hope you are well! Could you possibly, maybe sometime, think about things in general?")]),
 ("S14", True, [("received", "Accountant. Subject: Tax return. Please upload your 2025 income statement to the portal by 15 December.")]),
 ("S15", False, [("received", "Bot. Subject: Security notice. A new device signed in. If this was you, no action is needed.")]),
 ("S16", False, [("sent_by_owner", "Yes, Saturday works, see you at 8."), ("received", "Friend. Subject: Dinner? Are you free on Saturday?")]),
]


def build():
    records, evidence = [], {}
    for cid, _, msgs in CORPUS:
        record, texts = assess.conversation_record(
            cid, [dict(date=f"2026-11-{20 - i:02d}", direction=d, text=t) for i, (d, t) in enumerate(msgs)],
            automated=False, acquisition_complete=True)
        records.append(record); evidence[cid] = texts
    return records, evidence


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--i-approve-model-calls", action="store_true")
    args = ap.parse_args()
    records, evidence = build()
    batches = assess.batches(records)
    chars = sum(len(json.dumps(b)) for b in batches)
    print(f"{len(records)} synthetic conversations, {len(batches)} request(s), about {chars} input characters, "
          f"output limit {assess.MAX_TOKENS} tokens per request. Host retries/fallback can add attempts.")
    if not (args.run and args.i_approve_model_calls):
        print("Dry run: no model was called.")
        return 0
    from agent.plugin_llm import PluginLlm
    ctx = type("Ctx", (), {"llm": PluginLlm(plugin_id="hermes-tingle")})()
    hits = false_pos = misses = 0
    for batch in batches:
        got = assess.validate(assess.request(ctx, batch), {r["id"]: evidence[r["id"]] for r in batch})
        for cid, expected, _ in [c for c in CORPUS if c[0] in {r["id"] for r in batch}]:
            picked = bool(got.get(cid, {}).get("supported"))
            hits += picked and expected; false_pos += picked and not expected; misses += expected and not picked
            print(("PICK " if picked else "skip ") + cid + (" (expected)" if expected else ""))
    print(f"correct picks {hits}, false picks {false_pos}, missed {misses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
