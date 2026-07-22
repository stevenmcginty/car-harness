"""Base class for connection adapters.

An adapter describes HOW car-harness physically reaches a car. Each concrete
adapter declares its metadata (id / name / description / config ``fields``) and
implements ``test(config)`` -- a read-only connectivity check the wizard runs
before saving. A profile that wants to run its own check over a given adapter
calls the ``probe(config)`` hook, which by default is the same connectivity
test; native transports (e.g. ENET/DoIP) expose richer helpers of their own.
"""

from typing import List

from backend.core.models import AdapterField, AdapterTestResult, AdapterType


class Adapter:
    """Base adapter. Concrete adapters set the class attributes and override
    ``test``. Everything here is read-only -- adapters never write to a car."""

    id: str = ""
    name: str = ""
    description: str = ""
    builtin: bool = True
    fields: List[AdapterField] = []

    @classmethod
    def meta(cls) -> AdapterType:
        """This adapter type as an API model (GET /api/adapter-types)."""
        return AdapterType(
            id=cls.id,
            name=cls.name,
            description=cls.description,
            fields=list(cls.fields),
            builtin=cls.builtin,
        )

    @classmethod
    def test(cls, config: dict) -> AdapterTestResult:
        """Read-only connectivity check for ``config``. Must be honest about how
        far it actually got. Overridden by every concrete adapter."""
        raise NotImplementedError(f"{cls.__name__}.test() is not implemented")

    @classmethod
    def probe(cls, config: dict) -> AdapterTestResult:
        """Hook a profile calls to run its connection check over this adapter.

        The default is the same connectivity test the wizard uses; a profile
        turns the returned AdapterTestResult into its own ConnectionStatus.
        """
        return cls.test(config or {})
