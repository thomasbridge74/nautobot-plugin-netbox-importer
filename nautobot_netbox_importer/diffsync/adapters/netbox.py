"""DiffSync adapters for NetBox data dumps."""

import json
from uuid import uuid4

from typing import Dict, Set

from diffsync.enum import DiffSyncModelFlags
import structlog

from nautobot_netbox_importer.diffsync.models.abstract import NautobotBaseModel
from nautobot_netbox_importer.diffsync.models.validation import netbox_pk_to_nautobot_pk
from nautobot_netbox_importer.utils import ProgressBar
from .abstract import N2NDiffSync


class NetBox210DiffSync(N2NDiffSync):
    """DiffSync adapter for working with data from NetBox 2.10.x."""

    logger = structlog.get_logger()

    _unsupported_fields = {}

    def __init__(self, *args, source_data=None, **kwargs):
        """Store the provided source_data for use when load() is called later."""
        self.source_data = source_data
        super().__init__(*args, **kwargs)

    @property
    def unsupported_fields(self):
        """Public interface for accessing class attr `_unsupported_fields`."""
        return self.__class__._unsupported_fields  # pylint: disable=protected-access

    @staticmethod
    def _get_ignored_fields(netbox_data: Dict, nautobot_instance: NautobotBaseModel) -> Set[str]:
        """
        Get fields from NetBox JSON that were not handled by the importer.

        This only counts fields that have values.

        Args:
            netbox_data: The NetBox data for a particular database entry.
            nautobot_instance: The Nautobot DiffSync instance created for `netbox_data`.

        Returns:
            set: The NetBox field names ignored by the importer.
        """
        # Get fields passed from NetBox that have values and ignore internal fields
        netbox_fields = {key for key, value in netbox_data.items() if value and not key.startswith("_")}
        # Get fields set on the model instance
        instance_fields = nautobot_instance.__fields_set__
        # Account for aliases when gettig a diff of fields instantiated on the model
        field_aliases = {field.alias for field in nautobot_instance.__fields__.values() if field.alias != field.name}
        return netbox_fields - instance_fields - field_aliases - nautobot_instance.ignored_fields

    def _log_ignored_fields_details(
        self,
        netbox_data: Dict,
        nautobot_instance: NautobotBaseModel,
        model_name: str,
        ignored_fields: Set[str],
    ) -> None:
        """
        Log a debug message for NetBox fields ignored by the importer.

        This will log for every instance of fields that were ignored by the
        importer, so if there are a 100 instances of a model with an ignored
        field, then 100 log entries will be generated. In order to prevent the
        logs from generating too much noise, this is only logged as a debug.

        Args:
            netbox_data: The NetBox data for a particular database entry.
            nautobot_instance: The Nautobot DiffSync instance created for `netbox_data`.
            model_name: The DiffSync modelname for the NetBox entry.
            ignored_fields: The field names in `netbox_data` that were ignored.
        """
        ignored_fields_with_values = (f"{field}={netbox_data[field]}" for field in ignored_fields)
        ignored_fields_data_str = ", ".join(ignored_fields_with_values)
        self.logger.debug(
            "NetBox field not defined for DiffSync Model",
            comment=(
                f"The following fields were defined in NetBox for {model_name} - {str(nautobot_instance)}, "
                f"but they will be ignored by the Nautobot import: {ignored_fields_data_str}"
            ),
            pk=nautobot_instance.pk,
        )

    def _log_ignored_fields_info(
        self,
        model_name: str,
        ignored_fields: Set[str],
    ) -> None:
        """
        Log a warning message for NetBox fields ignored by the importer.

        This will log a warning for each unique field that is ignored by the
        importer, so if there are 100 instances of a model with an ignored field,
        then only 1 entry will be logged. This is used to inform users that the
        field is not supported by the importer, but not flood the logs.

        Args:
            netbox_data: The NetBox data for a particular database entry.
            nautobot_instance: The Nautobot DiffSync instance created for `netbox_data`.
            model_name: The DiffSync modelname for the NetBox entry.
            ignored_fields: The field names in `netbox_data` that were ignored.
        """
        log_message = (
            f"The following fields are defined in NetBox for {model_name}, "
            "but are not supported by this importer: {}"
        )
        # first time instance has ignored fields
        if model_name not in self.unsupported_fields:
            ignored_fields_str = ", ".join(ignored_fields)
            self.logger.warning(log_message.format(ignored_fields_str))
            self.unsupported_fields[model_name] = ignored_fields
        # subsequent instances might have newly ignored fields
        else:
            unlogged_fields = ignored_fields - self.unsupported_fields[model_name]
            if unlogged_fields:
                unlogged_ignored_fields_str = ", ".join(unlogged_fields)
                self.logger.warning(log_message.format(unlogged_ignored_fields_str))
                self.unsupported_fields[model_name].update(unlogged_fields)

    def _log_ignored_fields(
        self,
        netbox_data: Dict,
        nautobot_instance: NautobotBaseModel,
    ) -> None:
        """
        Convenience method for handling logging of ignored fields.

        Args:
            netbox_data: The NetBox data for a particular database entry.
            nautobot_instance: The Nautobot DiffSync instance created for `netbox_data`.
        """
        ignored_fields = self._get_ignored_fields(netbox_data, nautobot_instance)
        if ignored_fields:
            model_name = nautobot_instance._modelname  # pylint: disable=protected-access
            self._log_ignored_fields_details(netbox_data, nautobot_instance, model_name, ignored_fields)
            self._log_ignored_fields_info(model_name, ignored_fields)

    def load_record(self, diffsync_model, record):  # pylint: disable=too-many-branches,too-many-statements
        """Instantiate the given model class from the given record."""
        data = record["fields"].copy()
        data["pk"] = record["pk"]

        # Fixup fields that are actually foreign-key (FK) associations by replacing
        # their FK ids with the DiffSync model unique-id fields.
        for key, target_name in diffsync_model.fk_associations().items():
            if key not in data or not data[key]:
                # Null reference, no processing required.
                continue

            if target_name == "status":
                # Special case as Status is a hard-coded field in NetBox, not a model reference
                # Construct an appropriately-formatted mock natural key and use that instead
                # TODO: we could also do this with a custom validator on the StatusRef model; might be better?
                data[key] = {"slug": data[key]}
                continue

            # In the case of generic foreign keys, we have to actually check a different field
            # on the DiffSync model to determine the model type that this foreign key is referring to.
            # By convention, we label such fields with a '*', as if this were a C pointer.
            if target_name.startswith("*"):
                target_content_type_field = target_name[1:]
                target_content_type_pk = record["fields"][target_content_type_field]
                if not isinstance(target_content_type_pk, int):
                    self.logger.error(f"Invalid content-type PK value {target_content_type_pk}")
                    data[key] = None
                    continue
                target_content_type_record = self.get_by_pk(self.contenttype, target_content_type_pk)
                target_name = target_content_type_record.model

            # Identify the DiffSyncModel class that this FK is pointing to
            try:
                target_class = getattr(self, target_name)
            except AttributeError:
                self.logger.warning("Unknown/unrecognized class name!", name=target_name)
                data[key] = None
                continue

            if isinstance(data[key], list):
                # This field is a one-to-many or many-to-many field, a list of foreign key references.
                if issubclass(target_class, NautobotBaseModel):
                    # Replace each NetBox integer FK with the corresponding deterministic Nautobot UUID FK.
                    data[key] = [netbox_pk_to_nautobot_pk(target_name, pk) for pk in data[key]]
                else:
                    # It's a base Django model such as ContentType or Group.
                    # Since we can't easily control its PK in Nautobot, use its natural key instead.
                    #
                    # Special case: there are ContentTypes in NetBox that don't exist in Nautobot,
                    # skip over references to them.
                    references = [self.get_by_pk(target_name, pk) for pk in data[key]]
                    references = filter(lambda entry: not entry.model_flags & DiffSyncModelFlags.IGNORE, references)
                    data[key] = [entry.get_identifiers() for entry in references]
            elif isinstance(data[key], int):
                # Standard NetBox integer foreign-key reference
                if issubclass(target_class, NautobotBaseModel):
                    # Replace the NetBox integer FK with the corresponding deterministic Nautobot UUID FK.
                    data[key] = netbox_pk_to_nautobot_pk(target_name, data[key])
                else:
                    # It's a base Django model such as ContentType or Group.
                    # Since we can't easily control its PK in Nautobot, use its natural key instead
                    reference = self.get_by_pk(target_name, data[key])
                    if reference.model_flags & DiffSyncModelFlags.IGNORE:
                        data[key] = None
                    else:
                        data[key] = reference.get_identifiers()
            else:
                self.logger.error(f"Invalid PK value {data[key]}")
                data[key] = None

        if diffsync_model == self.user:
            # NetBox has separate User and UserConfig models, but in Nautobot they're combined.
            # Load the corresponding UserConfig into the User record for completeness.
            self.logger.debug("Looking for UserConfig corresponding to User", username=data["username"])
            for other_record in self.source_data:
                if other_record["model"] == "users.userconfig" and other_record["fields"]["user"] == record["pk"]:
                    data["config_data"] = other_record["fields"]["data"]
                    break
            else:
                self.logger.warning("No UserConfig found for User", username=data["username"], pk=record["pk"])
                data["config_data"] = {}
        elif diffsync_model == self.customfield:
            # Because marking a custom field as "required" doesn't automatically assign a value to pre-existing records,
            # we never want to enforce 'required=True' at import time as there may be otherwise valid records that predate
            # the creation of this field. Store it on a private field instead and we'll fix it up at the end.
            data["actual_required"] = data["required"]
            data["required"] = False

            if data["type"] == "select":
                # NetBox stores the choices for a "select" CustomField (NetBox has no "multiselect" CustomFields)
                # locally within the CustomField model, whereas Nautobot has a separate CustomFieldChoices model.
                # So we need to split the choices out into separate DiffSync instances.
                # Since "choices" is an ArrayField, we have to parse it from the JSON string
                # see also models.abstract.ArrayField
                for choice in json.loads(data["choices"]):
                    self.make_model(
                        self.customfieldchoice,
                        {
                            "pk": uuid4(),
                            "field": netbox_pk_to_nautobot_pk("customfield", record["pk"]),
                            "value": choice,
                        },
                    )
                del data["choices"]
        elif diffsync_model == self.virtualmachine:
            # NetBox stores the vCPU value as DecimalField, Nautobot has PositiveSmallIntegerField,
            # so we need to cast here
            if data["vcpus"] is not None:
                data["vcpus"] = int(float(data["vcpus"]))

        instance = self.make_model(diffsync_model, data)
        self._log_ignored_fields(data, instance)
        return instance

    def load(self):
        """Load records from the provided source_data into DiffSync."""
        self.logger.info("Loading imported NetBox source data into DiffSync...")
        for modelname in ("contenttype", "permission", *self.top_level):
            diffsync_model = getattr(self, modelname)
            content_type_label = diffsync_model.nautobot_model()._meta.label_lower
            # Handle a NetBox vs Nautobot discrepancy - the Nautobot target model is 'users.user',
            # but the NetBox data export will have user records under the label 'auth.user'.
            if content_type_label == "users.user":
                content_type_label = "auth.user"
            records = [record for record in self.source_data if record["model"] == content_type_label]
            if records:
                for record in ProgressBar(
                    records,
                    desc=f"{modelname:<25}",  # len("consoleserverporttemplate")
                    verbosity=self.verbosity,
                ):
                    self.load_record(diffsync_model, record)

        self.logger.info("Data loading from NetBox source data complete.")
        # Discard the source data to free up memory
        self.source_data = None
