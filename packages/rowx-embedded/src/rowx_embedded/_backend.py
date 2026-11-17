from dataclasses import replace
from typing import TYPE_CHECKING

from rowx_postgres._backend import load as load_postgres  # ruff: ignore[import-private-name]

from rowx_embedded._scratch import embedded_service
from rowx_embedded._testing import provisioner

if TYPE_CHECKING:
    from rowx_core.backend import Backend


# the PostgreSQL backend with the embedded engine's testing provisioner and embedded service in place of its server ones
def load() -> Backend:
    return replace(load_postgres(), testing_factory=provisioner, embedded_factory=embedded_service)
