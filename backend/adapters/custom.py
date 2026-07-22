"""Custom / proprietary cable adapter -- PLACEHOLDER template.

A starting point for a proprietary manufacturer cable. It stores free-form
notes (and tolerates an optional command string in its config) but has no
automated connectivity test yet, so ``test()`` simply reports that the config
was saved.
"""

from backend.adapters.base import Adapter
from backend.core.models import AdapterField, AdapterTestResult


class CustomAdapter(Adapter):
    id = "custom"
    name = "Custom / proprietary cable (placeholder)"
    description = (
        "PLACEHOLDER template for a proprietary manufacturer cable. Stores your "
        "notes; there is no automated test for it yet."
    )
    builtin = True
    fields = [
        AdapterField(key="notes", label="Notes", type="text", required=False),
    ]

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        return AdapterTestResult(
            ok=True,
            detail="saved - no automated test for a custom adapter yet.",
            info=dict(config or {}),
        )
