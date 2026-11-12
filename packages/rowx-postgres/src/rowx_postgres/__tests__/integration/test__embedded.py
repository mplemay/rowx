from importlib import import_module

import pytest
from rowx_postgres._embedded import EXPECTED_API_VERSION

pytestmark = [pytest.mark.embedded]


def test_the_companion_speaks_the_api_version_this_adapter_expects() -> None:
    assert import_module("rowx_embedded").API_VERSION == EXPECTED_API_VERSION
