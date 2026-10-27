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
                  ┌─ maintenance analyst ──  release_history, commit_activity, project_snapshot*
pypi:requests ───►├─ security analyst ─────  vulnerability_record, advisory_details, security_practices*
 + analysis date  └─ supply-chain analyst ─  release_metadata, dependency_footprint
                          │  3 × AnalystReport, run concurrently (quick model)
                          ▼
                 advocate ⇄ skeptic          N rounds, no tools (quick model)
                          │
                          ▼
                        chair  ───────────►  Decision: adopt | adopt_with_conditions | avoid
                                             (deep model)
        * present-day data: available to live runs, withheld from historical ones
```

The data comes from three free sources: [deps.dev](https://deps.dev) (releases, licenses,
dependency graphs, OpenSSF Scorecard), [OSV](https://osv.dev) (security advisories) and the
GitHub API (commit history).

## Point-in-time discipline

A backtest is only meaningful if the council can't see the future. That rule is enforced in the
data layer, not left to the prompt:

- **Tools never take a date or package argument.** They read both from `RunContext.deps`
  (`agents/tools.py`), so the model can't ask for data past the analysis date.
- **Releases, commits and advisories are filtered to the date** (`timeline.py`, `evidence.py`).
  GitHub is queried with `until=<date>`, and a fix counts only if its release shipped by then.
- **One vulnerability, one record, dated by its earliest disclosure.** OSV lists the same CVE
  under several ids (GHSA, PYSEC, …) with different publish dates. The records are merged
  through their aliases, and the merged advisory is dated by its earliest record.
- **Present-day snapshots are withheld from historical runs.** Stars, Scorecard, and the
  archive and deprecation flags describe the package's future when seen from a past date.
- **A guessed future advisory id learns nothing.** It gets the same answer as an id that
  doesn't exist.

Known gaps, which the tool results also state in a `note`: advisory text and severity come from
today's records, and deps.dev resolves dependency graphs against today's registry. The model's
own training data is the remaining leak: it may simply *know* how a famous package turned out.

## Quickstart

```bash
uv sync
cp .env.example .env            # add ANTHROPIC_API_KEY (and optionally GITHUB_TOKEN)

uv run depcouncil analyze pypi:requests --use "HTTP client in a long-lived backend service"
uv run depcouncil analyze npm:request --as-of 2020-01-15        # as it stood on that date
uv run depcouncil outcome npm:request --as-of 2020-06-01        # what happened next (no LLM)
uv run depcouncil backtest npm:request pypi:requests --as-of 2022-01-01

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
| `GITHUB_TOKEN` | – | Raises GitHub's limit from 60 to 5,000 requests an hour |

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
- **No checkpointing.** A run is roughly 10–20 model calls, cheap enough to rerun.

## Layout

```
src/depcouncil/
  schemas.py      PackageRef and every agent's output model
  settings.py     DEPCOUNCIL_* configuration (pydantic-settings)
  timeline.py     what was knowable on a date
  data/           raw clients: deps.dev, OSV, GitHub (responses parsed into Pydantic models)
  evidence.py     point-in-time facts built from the raw data; the only place dates are applied
  agents/         deps, data tools, analysts, advocate/skeptic/chair
  council.py      the workflow
  outcomes.py     outcome measurement, scoring, backtest
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

- **Memory and reflection.** Store each decision with its later outcome, and feed the lessons
  to the chair.
- **Anonymized reviews.** Hide the package name from the debaters and the chair, to cut the
  model's own hindsight out of backtests.
- **More signal.** Issue and PR responsiveness (needs point-in-time issue queries), CVSS
  scoring for advisories that lack a GHSA severity, and repositories hosted outside GitHub.
