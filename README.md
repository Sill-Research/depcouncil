# depcouncil

A council of AI agents that decides whether your team should adopt an open-source dependency,
and can be backtested: review a package *as it stood on a past date*, then check the call
against what actually happened next.

Built on [Pydantic AI](https://ai.pydantic.dev).

> Status: prototype. The pipeline, data layer and backtest are tested with Pydantic AI's
> `TestModel`/`FunctionModel` against a fake of each data source, and run against the live
> data sources.

## How it works

```
                  ┌─ maintenance analyst ──  release_history, commit_activity, issue_activity, project_snapshot*
pypi:requests ───►├─ security analyst ─────  vulnerability_record, advisory_details, security_practices*
 + analysis date  └─ supply-chain analyst ─  release_metadata, dependency_footprint
                          │  3 × AnalystReport, run concurrently (quick model)
                          ▼
                 advocate ⇄ skeptic          N rounds, no tools (quick model)
                          │
                          ▼
   past decisions ───►  chair  ───────────►  Decision: adopt | adopt_with_conditions | avoid
   (memory, optional)                        (deep model)
        * present-day data: available to live runs, withheld from historical ones
```

The data comes from three free sources: [deps.dev](https://deps.dev) (releases, licenses,
dependency graphs, OpenSSF Scorecard), [OSV](https://osv.dev) (security advisories, rated by
GHSA's severity or, failing that, their CVSS score) and the GitHub API (commit history, and
issue and pull request search).

## Point-in-time discipline

A backtest is only meaningful if the council can't see the future. That rule is enforced in the
data layer, not left to the prompt:

- **Tools never take a date or package argument.** They read both from `RunContext.deps`
  (`agents/tools.py`), so the model can't ask for data past the analysis date.
- **Releases, commits, issues and advisories are filtered to the date** (`timeline.py`,
  `evidence.py`). GitHub is queried with `until=<date>`, every date in an issue search is bounded
  by it, and a fix counts only if its release shipped by then.
- **One vulnerability, one record, dated by its earliest disclosure.** OSV lists the same CVE
  under several ids (GHSA, PYSEC, …) with different publish dates. The records are merged
  through their aliases, and the merged advisory is dated by its earliest record.
- **Present-day snapshots are withheld from historical runs.** Stars, Scorecard, and the
  archive and deprecation flags describe the package's future when seen from a past date.
- **A guessed future advisory id learns nothing.** It gets the same answer as an id that
  doesn't exist.
- **Memory recalls only what had played out.** The chair sees a past decision only if its
  outcome was known by the analysis date (`memory.py`).

Known gaps, which the tool results also state in a `note`: advisory text and severity come from
today's records, deps.dev resolves dependency graphs against today's registry, and GitHub keeps
only an issue's latest closing, so an issue closed by the date but reopened later counts as open.

## Anonymized reviews

The model's own training data is the remaining leak: it may simply *know* how a famous package
turned out. Anonymized reviews narrow it, and backtests use them by default (`--named` opts
out; `analyze --anonymize` opts in):

- The debaters and the chair are told only the package's ecosystem ("an anonymized PyPI
  package").
- The analysts see the name in their tool results, so they are told to write their reports
  without it. A report that still names the package or its source repository is sent back for
  a rewrite and, on the last attempt, redacted (`anonymity.py`).
- Past decisions about the package itself reach the chair redacted, and the reflector that
  writes memory's lessons is never told the name.

Advisory ids, dependency names and version numbers stay in the reports as evidence the debate
can check, so a well-known package can still be recognized from them.

## Quickstart

```bash
uv sync
cp .env.example .env            # add ANTHROPIC_API_KEY (and optionally GITHUB_TOKEN)

uv run depcouncil analyze pypi:requests --use "HTTP client in a long-lived backend service"
uv run depcouncil analyze npm:request --as-of 2020-01-15        # as it stood on that date
uv run depcouncil outcome npm:request --as-of 2020-06-01        # what happened next (no LLM)
uv run depcouncil backtest npm:request pypi:requests --as-of 2022-01-01   # anonymized

# Dry-run the whole pipeline against live data with Pydantic AI's TestModel: no key, no cost.
DEPCOUNCIL_QUICK_MODEL=test DEPCOUNCIL_DEEP_MODEL=test uv run depcouncil analyze pypi:requests
```

From Python:

```python
import asyncio
from datetime import date
from depcouncil import run_council

result = asyncio.run(run_council("npm:left-pad", as_of=date(2016, 3, 1)))
print(result.decision.rating, result.decision.rationale)
```

Packages are `ecosystem:name`. The supported ecosystems are `pypi`, `npm`, `cargo`, `go`,
`maven` (`maven:group:artifact`), `nuget` and `rubygems`.

## Configuration

Set these in the environment or in `.env`:

| Variable | Default | |
| --- | --- | --- |
| `DEPCOUNCIL_QUICK_MODEL` | `anthropic:claude-sonnet-5-5` | Analysts and debaters |
| `DEPCOUNCIL_DEEP_MODEL` | `anthropic:claude-opus-5-5` | The chair |
| `DEPCOUNCIL_QUICK_EFFORT` / `_DEEP_EFFORT` | `medium` / `high` | Thinking effort per tier |
| `DEPCOUNCIL_DEBATE_ROUNDS` | `1` | Advocate/skeptic exchanges |
| `DEPCOUNCIL_MAX_TOOL_ROUNDS` | `4` | Turns an analyst may spend on tools before it must report |
| `DEPCOUNCIL_MEMORY_PATH` | – | JSON Lines file for the council's [memory](#memory); off when unset |
| `GITHUB_TOKEN` | – | Raises GitHub's limits from 60 to 5,000 requests an hour, and from 10 to 30 searches a minute |

Models are any Pydantic AI `provider:model` string (`openai:gpt-5`, `google-gla:...` with the
`google` extra, `ollama:...`), and the provider's usual API-key variable applies.

## Backtesting

`depcouncil outcome` measures what happened in the horizon (540 days by default) after the
analysis date, from the same sources and with no model calls. The outcome is **adverse** if
either of these happened:

- the package went **dormant**: no release in the horizon and no commits in its last 180 days, or
- a **high or critical advisory** disclosed in the horizon went **more than 90 days without a fix**.

`depcouncil backtest` measures the outcome first (it's free, and it rejects a horizon that hasn't
ended yet), then convenes the council as of the start date and scores its call:

| | Not adverse | Adverse |
| --- | --- | --- |
| adopt | hit | miss |
| adopt with conditions | hit | partial |
| avoid | miss | hit |

These definitions are deliberately simple and live in `outcomes.py`; tune them to the risks you
care about.

## Memory

With `DEPCOUNCIL_MEMORY_PATH` set, the council remembers its decisions and learns from how they
turned out:

- `backtest` stores each decision already settled: its outcome, its score, and a lesson of two
  to four sentences that a reflector agent draws from them (one extra quick-model call).
- `analyze` stores each decision pending. Once its horizon has ended, `depcouncil settle`
  measures the outcome and draws the lesson.
- Before it rules, the chair is shown up to five settled decisions on the same package and
  three on others, most recent first, each with its lesson.

Recall is point-in-time too: a review dated *D* sees only the decisions whose outcome was known
by *D*. Backtests over successive dates learn from earlier ones and never from later ones:

```bash
uv run depcouncil backtest npm:request pypi:requests --as-of 2019-01-01   # known by 2020-06-24
uv run depcouncil backtest npm:left-pad --as-of 2021-01-01                # recalls both
uv run depcouncil settle                         # grade live decisions whose horizon has ended
```

The memory is a plain JSON Lines file. A later line about the same review (package, date and
intended use) replaces an earlier one, so reruns and settlements never count twice. Memory is
off by default so that backtests stay reproducible; from Python, recall and record explicitly:

```python
from depcouncil import Memory, MemoryEntry, PackageRef, run_council

memory = Memory(Path("memory.jsonl"))
past = memory.recall(PackageRef.parse("npm:left-pad"), date(2021, 1, 1))
result = asyncio.run(run_council("npm:left-pad", as_of=date(2021, 1, 1), past_decisions=past))
memory.add(MemoryEntry.of(result))
```

## Design

- **The workflow is plain async code** (`council.py`). `asyncio.TaskGroup` runs the analysts
  side by side, a loop runs the debate, and each step passes typed outputs to the next.
- **Agents are defined once, with no model** (`agents/`). The council passes the configured
  model and settings on each run, which is also what lets the tests swap in test models.
- **Tools are `FunctionToolset`s that read `RunContext[CouncilDeps]`.** `toolset.prepared(...)`
  withdraws them after `DEPCOUNCIL_MAX_TOOL_ROUNDS` turns so the analyst has to report, with
  `UsageLimits` as a backstop.
- **Outputs are Pydantic models** (`AnalystReport`, `Decision`), validated and retried.
- **Models are `provider:model` strings**, with `ModelSettings(thinking=...)` set per tier.
- **Memory is passed in, not built in.** `run_council` takes the recalled `PastDecision`s as
  data; the store and the reflector sit outside the council.
- **No checkpointing.** A run is roughly 10–20 model calls, cheap enough to rerun.

## Layout

```
src/depcouncil/
  schemas.py      PackageRef, every agent's output model, outcomes and past decisions
  settings.py     DEPCOUNCIL_* configuration (pydantic-settings)
  timeline.py     what was knowable on a date
  anonymity.py    what names a package, and how the name is kept from the debaters and chair
  data/           raw clients: deps.dev, OSV, GitHub (responses parsed into Pydantic models)
  evidence.py     point-in-time facts built from the raw data; the only place dates are applied
  agents/         deps, data tools, analysts, advocate/skeptic/chair, reflector
  council.py      the workflow
  outcomes.py     outcome measurement, scoring, backtest
  memory.py       remembered decisions: storage, point-in-time recall, reflection, settlement
  render.py, cli.py
tests/            fake data sources + TestModel/FunctionModel tests (no network, no API key)
```

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright          # strict on src/
```

## Next steps

- **More signal.** Repositories hosted outside GitHub.
