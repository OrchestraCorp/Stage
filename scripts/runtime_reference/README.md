# Fixed Cordis reference runner

Run from Stage with Python 3.12+ and Node 25 (native experimental TypeScript
transformation):

```sh
.venv/bin/python scripts/runtime_reference/run.py \
  --reference-repository ../Maestro \
  --output docs/reviews/runtime-reference-2026-10-25
```

The runner extracts `vendor/cordis` and `vendor/cosmokit` from the fixed Maestro
commit `6f7124baabca582d43ce84fd59d2f48e4b790763` into a temporary directory.
No npm dependencies are installed, no repository source is rewritten, and the
current Maestro worktree is not executed. A Node import hook resolves cosmokit
to the extracted source from that same commit; type-only schema imports vanish
under native TypeScript transformation.

`reference.mjs` executes one explicitly described reference fragment for each
of the 34 C scenario IDs. Some fragments observe intentional D differences,
including provider resources closing before dependent cleanup, absence of
request leases, and `fiber.await()` returning PENDING. These are reference
observations, **not 34 completed Stage acceptance scenarios**. Their `coverage`
and `deviations` fields state exactly what ran. S scenarios, production IAM,
WASM and persistent business semantics remain separate gates.

`shared-cases.json` defines 35 bounded common operation sequences and nine
expected-difference comparisons. The TS
adapter and `shared_python.py` execute the identical descriptions and compare
normalized observations: effect ownership, asynchronous/late setup and cleanup,
generator inverses, dependency arrival/recovery, availability notification,
provider replacement, committed cleanup views, realms, intercept/inject merging,
caller-owned service contributions, nested owners, and all five event modes.
The comparisons retain raw outcomes: D-01 through D-09 have executable evidence,
including D-06's different native caller-owner APIs producing the same resource
attribution. Expected-difference cases pass only if both actual traces exactly
match the declared distinct expectations. They do not count as shared equality.

`coverage.json` maps every C ID to its reference observation, common operation
sequences and relevant difference comparisons. Equality applies to those
fragments, not all assertions of the corresponding C scenario. Dependency-cycle
diagnostics, request draining, noncooperative tasks, ABA/admission races and
application authorization remain the separate Stage C/S and application gates.

Output includes raw TS traces, both shared traces, comparisons and a manifest
with source/adapter digests, versions and Git state. The manifest explicitly
keeps `stage_acceptance_passed: 0`: complete C/S acceptance and differential
coverage must be reported by their own runners. A failed subprocess, differing
observation or reference assertion makes this command fail.
