"""Anonymized reviews: what names a package, and how the name is kept from the debaters and chair.

A model may remember how a well-known package turned out, which no date filter can hide. In an
anonymized review the debaters and the chair are told only the package's ecosystem. The
analysts still know the name, since their tool results carry it, so their reports are checked
for it, and any past decision shown to the chair is redacted.

Only the name and the source repository are withheld. Advisory ids, dependency names and
version numbers stay in the reports as evidence the debate can check, so a well-known package
can still be recognized from them.
"""

import re
from collections.abc import Iterable

from depcouncil.schemas import Ecosystem, PackageRef, PastDecision

ALIAS = "the package"


def identifiers(pkg: PackageRef, repository: str | None = None) -> tuple[str, ...]:
    """The ways a report could name the package: its spec, its name, a Maven artifact id, and its
    source repository as ``owner/name`` and as ``name``."""
    names = [str(pkg), pkg.name]
    if pkg.ecosystem is Ecosystem.MAVEN:
        names.append(pkg.name.rpartition(":")[2])
    if repository:
        names += [repository, repository.rpartition("/")[2]]
    return tuple(dict.fromkeys(names))


def named_in(text: str, names: Iterable[str]) -> list[str]:
    """The spellings of ``names`` that occur in ``text`` as whole words, in any case."""
    return sorted({match.group(0) for match in _pattern(names).finditer(text)})


def redact(text: str, names: Iterable[str]) -> str:
    """``text`` with each of ``names`` replaced by ``ALIAS``."""
    return _pattern(names).sub(ALIAS, text)


def redact_past_decision(past: PastDecision, names: Iterable[str]) -> PastDecision:
    """``past`` with ``names`` redacted, so a past review of the package itself reads as being
    about ``ALIAS``."""
    names = tuple(names)
    return past.model_copy(
        update={
            "package": redact(past.package, names),
            "intended_use": redact(past.intended_use, names),
            "deciding_factors": [redact(factor, names) for factor in past.deciding_factors],
            "what_happened": [redact(finding, names) for finding in past.what_happened],
            "lesson": redact(past.lesson, names),
        }
    )


def _pattern(names: Iterable[str]) -> re.Pattern[str]:
    # Longest first, so that "acme/widget" is matched whole rather than as "widget".
    spellings = sorted({name for name in names if name}, key=len, reverse=True)
    if not spellings:
        return re.compile(r"(?!)")
    # PyPI treats "-", "_" and "." as the same character, and so does someone writing from memory.
    body = "|".join(re.sub(r"\\-|\\\.|_", "[-_.]", re.escape(name)) for name in spellings)
    return re.compile(rf"(?<![\w.-])(?:{body})(?![\w-])", re.IGNORECASE)
