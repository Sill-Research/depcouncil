from collections.abc import AsyncIterator

import pydantic_ai
import pytest
from pydantic_ai import models

from depcouncil import Evidence, Settings, open_evidence
from tests.fakes import FakeSources, widget

# Tests run on Pydantic AI's TestModel and FunctionModel; a real provider call is a bug.
models.ALLOW_MODEL_REQUESTS = False
pydantic_ai.BANNER_ENABLED = False


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sources() -> FakeSources:
    return widget()


@pytest.fixture
async def evidence(sources: FakeSources) -> AsyncIterator[Evidence]:
    async with open_evidence(Settings(), transport=sources.transport()) as opened:
        yield opened
