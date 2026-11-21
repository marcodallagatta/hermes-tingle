# Contributing

Tingle is a public pre-1.0 beta and is not an approved Hermes catalog release. Tags, catalog submission and live deployment remain separate owner decisions. See [release process](docs/release-process.md).

## Before opening an issue

Read the [README](README.md), [diagnostics](docs/diagnostics.md) and existing issues. Report bugs with the bug form and proposals with the feature form. Use synthetic examples. Never attach a profile directory, database, mailbox export, tokens, private destinations, source bodies or raw logs containing personal data. See [SECURITY.md](SECURITY.md) for security concerns.

## Development

Use a disposable environment and Python supported by the tested Hermes runtime. The declared compatibility matrix is in `.github/workflows/test.yml`; those are tested revisions, not a promise about every release between them. Both workflows run on pushes to `main` and pull requests, and remain available by manual dispatch. Check the actual workflow run before treating a change as CI-verified. Install `requirements-dev.txt`, then run:

```sh
python -m pytest tests/ -q
python tools/check_privacy.py
```

The normal suite blocks unmocked network, provider and process access. Keep fixtures synthetic. A change must not need a personal account, gateway, source grant or credential to pass. Add tests at the actual affected tool/command/helper/provider boundary. Migration tests must preserve later writes, aliases, consent and uncertain effects. Never use an old database restore as an automatic repair.

When Hermes is available, use the isolated [admission checks](docs/release-process.md#automated-evidence). Read warnings as well as failures. Passing tests or the validator does not establish a successful live integration or catalog approval.

## Voice and interaction style

Take care of the person reading it. Every user-facing reply leads with what matters to the reader right now, asks only what needs an answer with the decision facts present, and leaves the rest behind help, details subcommands or docs. Warmth and a little elf charm are seasoning, never the meal, and never at the expense of usefulness: consent prompts, approval decisions, failure and refusal paths stay plain, complete and precise. Do not shorten a decision-time disclosure by moving essential facts away from the choice. When editing any user-facing string, read the whole interaction as the reader receives it and keep fixtures synthetic.

## Pull requests

Use a focused branch and explain the behavior, authority boundaries, migration impact, tests and remaining gaps. Include a synthetic failing example for a bug. Keep runtime code, both helper copies, prompts, skill instructions and docs consistent. Independently review the exact final commit before merging. Do not merge your own test claim as a substitute for review.

Avoid private Hermes internals or runtime core patches. Documented APIs have the compatibility contract; undocumented imports do not. If a safe public seam is missing, describe the required hook upstream only after the owner approves external communication. Never replace bound actor identity with environment fallback to silence a warning.

## Changes requiring an explicit owner decision

Tags or releases, catalog submissions, live tests and sends, new source scopes, spending, paid plans, branch restrictions and license selection are not authorized by a code contribution. Propose them separately. Do not commit personal context or credentials. A clean pattern scan is not proof that current files or retained history are safe to publish.

## License and assets

Existing third-party notices must be preserved. Do not add third-party work without confirming its origin and terms. Public contributions follow the repository license and project guidelines.

The admission runner is POSIX-only (CI uses Linux). Each CLI runs in a new owned process session/group. On timeout, interruption or normal leader exit, remaining group members receive bounded TERM then KILL cleanup, and the direct child is reaped before another stage or scratch cleanup. Deliberately detached sessions are outside this boundary; it is not hostile-code isolation. Windows process-tree behavior is not implemented or claimed.
