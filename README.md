# locking-claim-audit

Offline audit of documented cross-process exclusion promises against Python source.

Version 0.1.0. Requires Python 3.9 or newer; runtime dependencies: standard library only.
The auditor reads Markdown and parses Python ASTs. It never executes target code,
uses the network, or reads `.env` contents. Symlinked files and directories are skipped.

## Usage

```sh
python3 -m pip install .
locking-claim-audit /path/to/project
python3 -m locking_claim_audit /path/to/project --json
locking-claim-audit . --ignore 'vendor/*' --ignore 'generated/*' --verbose
locking-claim-audit . --fail-on HIGH
locking-claim-audit --version
```

The console entry point calls `locking_claim_audit.__main__:main`.
The optional path defaults to the current directory. `--ignore` is repeatable;
patterns use case-sensitive glob matching against relative paths with `/` separators.
`--verbose` adds the complete analysis to text output. `--json` emits one JSON object
with `findings`, `verified_claims`, `claims`, `production_evidence`,
`test_only_evidence`, and `source_errors`. Findings contain `rule`, `severity`,
`family`, `claims`, and `evidence`. Each claim includes path, line, and sentence;
evidence includes path, line, and kind. Evidence is deduplicated by path, line,
and kind. Paths and results have deterministic traversal order.

Exit 0 means no findings reached the selected threshold; exit 2 means at least
one did. Exit 1 means invalid usage, unreadable input, or a source parse failure.
The default threshold is MEDIUM. `--fail-on HIGH` retains MEDIUM findings in
output but does not fail for them. A project with no extracted claims passes,
regardless of detected mechanisms. Each family is evaluated independently.

## Rules

Rule identifiers are fenced to keep the documentation self-audit neutral:

```text
L1 documented-locked-claim-false
L2 documented-locked-claim-false-test-only
N1 documented-nolock-claim-false
N2 documented-nolock-claim-false-test-only
```

| Rule | Severity | Condition |
| --- | --- | --- |
| L1 | HIGH | Positive family present, no production or test-only evidence |
| L2 | MEDIUM | Positive family present, evidence exists only in tests or fixtures |
| N1 | HIGH | Negative family present, production evidence exists |
| N2 | MEDIUM | Negative family present, evidence exists only in tests or fixtures |

Each finding aggregates all claim sentences for its family. N1 includes every
production evidence item; L2 and N2 include every test-only item. If there is
no production Python source, each documented family gets one HIGH finding
under L1 or N1, with `{"kind": "no-source"}` evidence. This takes precedence
over test-only evidence. Positive claims with production evidence are verified;
negative claims with zero evidence anywhere are verified. Contradictory families
can produce two findings, for example when neither has production source.

## Claim extraction

Only root `README.md` and `docs/**/*.md` are considered. Backtick and tilde
fenced blocks are excluded; inline code spans remain prose. Sentences are
lowercased, `n't` becomes ` not`, hyphens become spaces, and whitespace is
collapsed. Wrapped prose is supported. Exact phrases with word boundaries
are required; optional inserted words are never inferred.

These are the complete normalized phrases, listed once per family:

```text
locked:
file locking
uses locking
uses a lock
uses locks
locking support
supports locking
safe for concurrent runs
safe to run concurrently
concurrent safe
prevents concurrent runs
prevents concurrent access
serializes concurrent access
serialises concurrent access
uses a lock file
creates a lock file
protected by a lock
guarded by a lock
acquires a lock

nolock:
no locking
does not lock
does not use locking
does not use a lock
without locking
no lock file
no lock
not safe for concurrent runs
not safe to run concurrently
do not run concurrently
must not run concurrently
```

Negation is asymmetric: a sentence containing `no`, `not`, `never`, or
`without` suppresses the positive family, while listed negative phrases still
count. Negative phrases are matched first and their spans masked before positive
matching to avoid overlap. The following examples illustrate the boundary:

```text
"does not use a lock" -> nolock only
"doesn't lock" -> nolock via contraction normalization
"file-locking" -> locked
"concurrent-safe" -> locked
"never locked" -> no claim
"does not claim to use locking" -> no claim
"lock" -> no claim
"lock file" -> no claim
```

## Evidence

Production source consists of `.py` files outside `tests/`, `fixtures/`, `docs/`,
and `examples/`, excluding `test_*.py` and `*_test.py`. Test directories, fixture
directories, and test filename patterns are collected separately. Python files
under documentation and examples are excluded altogether. Ignore patterns apply
to both documentation and source. Files named `.env` or starting with `.env.`
are never read, including when they have a source-like suffix.

| Evidence kind | Static mechanism |
| --- | --- |
| `os-lock-call` | Calls to `fcntl.flock`, `fcntl.lockf`, or `msvcrt.locking` |
| `library-import` | Imports from `filelock`, `portalocker`, `fasteners`, or `flufl.lock`, including submodules and names |
| `lock-constructor` | Dotted names ending in `FileLock`, or `portalocker.Lock`, `fasteners.InterProcessLock`, `flufl.lock.Lock` |
| `exclusive-create` | `os.open` with both literal flag names `O_CREAT` and `O_EXCL` in its arguments |
| `lock-file-path` | `open`, `io.open`, `os.open`, or Path open/write methods with a string argument containing `.lock` |
| `lock-directory-path` | `os.mkdir` or `os.makedirs` with a string argument containing `.lock` |

## Honest limitations

This is a heuristic existence check, not a proof of mutual exclusion or correct
coverage. Dead code counts. A mechanism guarding only a small part of a program
counts. An import of one of the named third-party libraries is sufficient,
even if unused. Constructor suffix matching can also produce false positives.

Per-file import aliases are tracked. A bare `flock` or `lockf` call is accepted
only with an import from `fcntl` or of that module; a bare `locking` call requires
an import from `msvcrt` or of that module. Qualified spellings are recognized
statically without requiring an import. Alias tracking does not model scope,
rebinding, execution order, or dynamic imports. Flags held only in a variable
are not resolved: the required flag names must appear in the call arguments.
Computed path values and arbitrary object types are not inferred.

`threading.Lock`, `multiprocessing.Lock`, and `asyncio.Lock` do not qualify
under this tool's deliberately restricted mechanism model. A bare `Lock()` call,
a `lock` variable, a string mentioning a mechanism, and comments do not count.
Neither a `LockTimeout` name alone nor releasing/checking an existing object is
sufficient. An `acquire()` call on an arbitrary variable is insufficient.
Shell commands invoking `flock(1)` are not parsed. Native extensions, non-Python
implementations, semantic correctness, lock lifetime, and platform behavior are
outside scope. Exact-phrase extraction can miss paraphrases and Markdown edge
cases; this is not a complete Markdown parser. Sentence boundaries use punctuation
and paragraph breaks, so abbreviations can split a sentence.

## Differentiation

| Neighbour | Distinction |
| --- | --- |
| `concurrency-claim-audit` | Parallelism inside one run; this tool checks exclusion between separate processes |
| `thread-safety-claim-audit` | In-process synchronization rather than OS or filesystem exclusion |
| `atomic-claim-audit` | Crash safety of one write rather than mutual exclusion |
| Bandit / Ruff | Source issues rather than documentation promises |
| `filelock` / `portalocker` / `fasteners` documentation | Explains primitives rather than comparing project documentation to source |

## Development and packaging

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m pip install build
python3 -m build
python3 tests/smoke.py
python3 -m venv /tmp/locking-audit-venv
/tmp/locking-audit-venv/bin/python3 -m pip install dist/locking_claim_audit-0.1.0-py3-none-any.whl
/tmp/locking-audit-venv/bin/locking-claim-audit tests/fixtures/good/locked --json
PYTHONPATH=src python3 -m locking_claim_audit . --json
```

The build produces a wheel and an sdist in `dist/`. If isolated build dependencies
are unavailable offline, install the build backend beforehand and use
`python3 -m build --no-isolation`. `MANIFEST.in` includes the unittest suite and
fixture trees in the sdist. Fixtures cover positive and negative contracts,
contradictions, and test-only evidence. The tool's own README yields zero claims.

MIT licensed; see [LICENSE](LICENSE).
