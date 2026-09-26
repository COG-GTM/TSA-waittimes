import pytest

from tests.harness import FIXTURE_CREDENTIALS


@pytest.fixture(autouse=True, scope="session")
def fixture_feed_credentials():
    """Recorded fixtures were redacted to these placeholders; replay with the same values."""
    with pytest.MonkeyPatch.context() as mp:
        for name, value in FIXTURE_CREDENTIALS.items():
            mp.setenv(name, value)
        yield
