"""Pydantic models for the car-harness REST API responses.

These mirror the JSON contract the front-end is built against. Field names use
the exact casing the API returns (camelCase where the contract says so).
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class StepStatus(BaseModel):
    """One line in the connection checklist (adapter / link / doip / vin)."""

    key: str
    label: str
    state: str  # "ok" | "fail" | "pending"
    detail: str = ""


class Vehicle(BaseModel):
    """Vehicle identity parsed from a DoIP Vehicle Announcement (read-only)."""

    vin: Optional[str] = None
    logicalAddress: Optional[str] = None
    eid: Optional[str] = None
    gid: Optional[str] = None


class ConnectionStatus(BaseModel):
    """Full result of GET /api/status."""

    profile: str
    mode: str  # "real" | "sim"
    simulated: bool
    overall: str  # "disconnected" | "partial" | "connected"
    steps: List[StepStatus]
    vehicle: Optional[Vehicle] = None
    message: str
    timestamp: str
    # Decoded from the VIN's WMI when the car identified itself. The ENET cable
    # reaches several makes on the same pins, so "a car answered" does not mean
    # "the selected manufacturer is right" -- these let the UI notice and fix it.
    detectedMake: Optional[str] = None
    suggestedProfile: Optional[str] = None


class ModuleSummary(BaseModel):
    """Compact module view embedded inside a profile (GET /api/profiles)."""

    id: str
    name: str
    description: str = ""
    installed: bool = True
    builtin: bool = False
    sizeBytes: int = 0
    status: Optional[str] = None


class ProfileRecord(BaseModel):
    """A manufacturer profile plus its job modules (GET /api/profiles)."""

    id: str
    name: str
    transport: str
    modules: List[ModuleSummary] = Field(default_factory=list)


class ProfilesResponse(BaseModel):
    profiles: List[ProfileRecord]


class ModuleRecord(BaseModel):
    """A full job-module record (GET /api/modules, import result)."""

    id: str
    profile: str
    name: str
    description: str = ""
    installed: bool = True
    builtin: bool = False
    version: Optional[str] = None
    status: Optional[str] = None
    requires: List[str] = Field(default_factory=list)
    entry: Optional[str] = None
    sizeBytes: int = 0
    path: Optional[str] = None


class ModulesResponse(BaseModel):
    modules: List[ModuleRecord]


class InterfaceInfo(BaseModel):
    """A network adapter as seen by GET /api/interfaces."""

    name: str
    addresses: List[str] = Field(default_factory=list)
    isUp: bool = False
    isCandidate: bool = False


class InterfacesResponse(BaseModel):
    interfaces: List[InterfaceInfo]


# --------------------------------------------------------------------------- #
# Adapter layer (HOW we physically talk to a car). Adapter *types* are the
# built-in kinds; adapter *instances* are user-configured via the wizard and
# persisted to adapters.json.
# --------------------------------------------------------------------------- #
class AdapterField(BaseModel):
    """One configurable field an adapter type exposes to the wizard.

    ``type`` is one of: "text", "ip", "select", "comport", "file".
    """

    key: str
    label: str
    type: str
    required: bool = False
    options: Optional[List[str]] = None
    help: Optional[str] = None


class AdapterType(BaseModel):
    """A built-in kind of adapter (GET /api/adapter-types)."""

    id: str
    name: str
    description: str = ""
    fields: List[AdapterField] = Field(default_factory=list)
    builtin: bool = True


class AdapterInstance(BaseModel):
    """A user-configured, persisted adapter (GET /api/adapters)."""

    id: str
    typeId: str
    name: str
    config: dict = Field(default_factory=dict)
    lastTest: Optional[dict] = None
    # The one adapter used when a caller names none. At most one is set.
    default: bool = False


class AdapterTestResult(BaseModel):
    """Result of testing an adapter config (POST /api/adapters/test)."""

    ok: bool
    detail: str = ""
    info: Optional[dict] = None
