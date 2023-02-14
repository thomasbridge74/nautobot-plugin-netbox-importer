"""DiffSync adapters for nautobot-netbox-importer."""

from packaging import version

from .nautobot import NautobotDiffSync
from .netbox import NetBox210DiffSync, Netbox34DiffSync

netbox_adapters = {
    version.parse("2.10.3"): NetBox210DiffSync,
    version.parse("2.10.4"): NetBox210DiffSync,
    version.parse("2.10.5"): NetBox210DiffSync,
    version.parse("2.10.6"): NetBox210DiffSync,
    version.parse("2.10.7"): NetBox210DiffSync,
    version.parse("2.10.8"): NetBox210DiffSync,
    version.parse("3.4"): Netbox34DiffSync,
}

__all__ = (
    "netbox_adapters",
    "NautobotDiffSync",
)
