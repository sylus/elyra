#
# Copyright 2018-2020 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import io
import json
import os
import re
import shutil
import warnings

from abc import ABC, abstractmethod
from jsonschema import validate, ValidationError, draft7_format_checker
from jupyter_core.paths import jupyter_data_dir, jupyter_path
from traitlets import HasTraits, Unicode, Dict, Type, log
from traitlets.config import SingletonConfigurable, LoggingConfigurable


METADATA_TEST_NAMESPACE = "metadata-tests"  # exposed via METADATA_TESTING env
DEFAULT_SCHEMA_NAME = 'kfp'


class Metadata(HasTraits):
    name = None
    resource = None
    display_name = Unicode()
    schema_name = Unicode()
    metadata = Dict()
    reason = None

    def __init__(self, **kwargs):
        super(Metadata, self).__init__(**kwargs)
        if 'display_name' not in kwargs:
            raise AttributeError("Missing required 'display_name' attribute")

        self.display_name = kwargs.get('display_name')
        self.schema_name = kwargs.get('schema_name') or DEFAULT_SCHEMA_NAME
        self.metadata = kwargs.get('metadata', Dict())
        self.name = kwargs.get('name')
        self.resource = kwargs.get('resource')
        self.reason = kwargs.get('reason')

    def to_dict(self, trim=False):
        # Exclude name, resource, and reason only if trim is True since we don't want to persist that information.
        # Only include schema_name if it has a value (regardless of trim).
        d = dict(display_name=self.display_name, metadata=self.metadata, schema_name=self.schema_name)
        if not trim:
            if self.name:
                d['name'] = self.name
            if self.resource:
                d['resource'] = self.resource
            if self.reason:
                d['reason'] = self.reason

        return d

    def to_json(self, trim=False):
        j = json.dumps(self.to_dict(trim=trim), indent=2)

        return j


class MetadataManager(LoggingConfigurable):

    # System-owned namespaces
    NAMESPACE_RUNTIMES = "runtimes"
    NAMESPACE_CODE_SNIPPETS = "code-snippets"

    metadata_class = Type(Metadata, config=True,
                          help="""The metadata class.  This is configurable to allow subclassing of
                          the MetadataManager for customized behavior.""")

    def __init__(self, namespace, store=None, **kwargs):
        """
        Generic object to read Notebook related metadata
        :param namespace: the partition where it is stored, this might have
        a unique meaning for each of the supported metadata storage
        :param store: the metadata store to be used
        :param kwargs: additional arguments to be used to instantiate a metadata store
        """
        super(MetadataManager, self).__init__(**kwargs)

        self.namespace = namespace
        if store:
            self.metadata_store = store
        else:
            self.metadata_store = FileMetadataStore(namespace, **kwargs)

    def namespace_exists(self):
        return self.metadata_store.namespace_exists()

    @property
    def get_metadata_location(self):
        return self.metadata_store.get_metadata_location

    def get_all_metadata_summary(self, include_invalid=False):
        return self.metadata_store.get_all_metadata_summary(include_invalid=include_invalid)

    def get_all(self):
        return self.metadata_store.get_all()

    def get(self, name):
        return self.metadata_store.read(name)

    def add(self, name, metadata, replace=True):
        return self.metadata_store.save(name, metadata, replace)

    def remove(self, name):
        return self.metadata_store.remove(name)


class MetadataStore(ABC):
    def __init__(self, namespace, **kwargs):
        self.schema_mgr = SchemaManager.instance()
        if not self.schema_mgr.is_valid_namespace(namespace):
            raise ValueError("Namespace '{}' is not in the list of valid namespaces: {}".
                             format(namespace, self.schema_mgr.get_namespaces()))

        self.namespace = namespace
        self.log = log.get_logger()

    @abstractmethod
    def namespace_exists(self):
        pass

    @abstractmethod
    def get_metadata_location(self):
        pass

    @abstractmethod
    def get_all_metadata_summary(self):
        pass

    @abstractmethod
    def get_all(self):
        pass

    @abstractmethod
    def read(self, name):
        pass

    @abstractmethod
    def save(self, name, metadata, replace=True):
        pass

    @abstractmethod
    def remove(self, name):
        pass

    # FIXME - we should rework this area so that its more a function of the processor provider
    # since its the provider that knows what is 'valid' or not.  Same goes for _get_schema() below.
    def validate(self, name, schema_name, schema, metadata):
        """Ensure metadata is valid based on its schema.  If invalid, ValidationError will be raised. """
        self.log.debug("Validating metadata resource '{}' against schema '{}'...".format(name, schema_name))
        try:
            validate(instance=metadata, schema=schema, format_checker=draft7_format_checker)
        except ValidationError as ve:
            # Because validation errors are so verbose, only provide the first line.
            first_line = str(ve).partition('\n')[0]
            msg = "Schema validation failed for metadata '{}' in namespace '{}' with error: {}.".\
                format(name, self.namespace, first_line)
            self.log.error(msg)
            raise ValidationError(msg)


class FileMetadataStore(MetadataStore):

    def __init__(self, namespace, **kwargs):
        super(FileMetadataStore, self).__init__(namespace, **kwargs)
        self.metadata_dir = os.path.join(jupyter_data_dir(), 'metadata', self.namespace)
        self.log.debug("Namespace '{}' is using metadata directory: {}".format(self.namespace, self.metadata_dir))

    @property
    def get_metadata_location(self):
        return self.metadata_dir

    def namespace_exists(self):
        is_valid_namespace = False

        all_metadata_dirs = jupyter_path(os.path.join('metadata', self.namespace))
        for d in all_metadata_dirs:
            if os.path.isdir(d):
                is_valid_namespace = True
                break

        return is_valid_namespace

    def get_all_metadata_summary(self, include_invalid=False):
        metadata_list = self._load_metadata_resources(include_invalid=include_invalid)
        metadata_summary = {}
        for metadata in metadata_list:
            metadata_summary.update(
                {
                    'name': metadata.name,
                    'display_name': metadata.display_name,
                    'location': self._get_resource(metadata)
                }
            )
        return metadata_list

    def get_all(self):
        return self._load_metadata_resources()

    def read(self, name):
        if not name:
            raise ValueError('Name of metadata was not provided')
        return self._load_metadata_resources(name=name)

    def save(self, name, metadata, replace=True):
        if not name:
            raise ValueError('Name of metadata was not provided.')

        match = re.search("^[a-z][a-z0-9-_]*[a-z,0-9]$", name)
        if match is None:
            raise ValueError("Name of metadata must be lowercase alphanumeric, beginning with alpha and can include "
                             "embedded hyphens ('-') and underscores ('_').")

        if not metadata:
            raise ValueError("An instance of class 'Metadata' was not provided.")

        if not isinstance(metadata, Metadata):
            raise TypeError("'metadata' is not an instance of class 'Metadata'.")

        metadata_resource_name = '{}.json'.format(name)
        resource = os.path.join(self.metadata_dir, metadata_resource_name)

        if os.path.exists(resource):
            if replace:
                os.remove(resource)
            else:
                self.log.error("Metadata resource '{}' already exists. Use the replace flag to overwrite.".
                               format(resource))
                return None

        created_namespace_dir = False
        if not self.namespace_exists():  # If the namespaced directory is not present, create it and note it.
            self.log.debug("Creating metadata directory: {}".format(self.metadata_dir))
            os.makedirs(self.metadata_dir, mode=0o700, exist_ok=True)
            created_namespace_dir = True

        try:
            with io.open(resource, 'w', encoding='utf-8') as f:
                f.write(metadata.to_json(trim=True))  # Only persist necessary items
        except Exception:
            if created_namespace_dir:
                shutil.rmtree(self.metadata_dir)
        else:
            self.log.debug("Created metadata resource: {}".format(resource))

        # Now that its written, attempt to load it so, if a schema is present, we can validate it.
        try:
            self._load_from_resource(resource)
        except ValidationError:
            self.log.error("Removing metadata resource '{}' due to previous error.".format(resource))
            # If we just created the directory, include that during cleanup
            if created_namespace_dir:
                shutil.rmtree(self.metadata_dir)
            else:
                os.remove(resource)
            resource = None

        return resource

    def remove(self, name):
        self.log.info("Removing metadata resource '{}' from namespace '{}'.".format(name, self.namespace))
        try:
            metadata = self._load_metadata_resources(name=name, validate_metadata=False)  # Don't validate on remove
        except KeyError:
            self.log.warning("Metadata resource '{}' in namespace '{}' was not found!".format(name, self.namespace))
            return

        resource = self._get_resource(metadata)
        os.remove(resource)

        return resource

    def _get_resource(self, metadata):
        metadata_resource_name = '{}.json'.format(metadata.name)
        resource = os.path.join(self.metadata_dir, metadata_resource_name)
        return resource

    def _load_metadata_resources(self, name=None, validate_metadata=True, include_invalid=False):
        """Loads metadata files with .json suffix and return requested items.
           if 'name' is provided, the single file is loaded and returned, else
           all files ending in '.json' are loaded and returned in a list.
        """
        resources = []
        if self.namespace_exists():
            all_metadata_dirs = jupyter_path(os.path.join('metadata', self.namespace))
            for metadata_dir in all_metadata_dirs:
                if os.path.isdir(metadata_dir):
                    for f in os.listdir(metadata_dir):
                        path = os.path.join(metadata_dir, f)
                        if path.endswith(".json"):
                            if name:
                                if os.path.splitext(os.path.basename(path))[0] == name:
                                    return self._load_from_resource(path, validate_metadata=validate_metadata)
                            else:
                                metadata = None
                                try:
                                    metadata = self._load_from_resource(path, validate_metadata=validate_metadata,
                                                                        include_invalid=include_invalid)
                                except Exception:
                                    pass  # Ignore ValidationError and others when loading all resources
                                if metadata is not None:
                                    resources.append(metadata)
        else:  # namespace doesn't exist, treat as KeyError
            raise KeyError("Metadata namespace '{}' was not found!".format(self.namespace))

        if name:  # If we're looking for a single metadata and we're here, then its not found
            raise KeyError("Metadata '{}' in namespace '{}' was not found!".format(name, self.namespace))

        return resources

    def _get_schema(self, schema_name):
        """Loads the schema based on the schema_name and returns the loaded schema json.
           Throws ValidationError if schema file is not present.
        """

        schema_json = self.schema_mgr.get_schema(self.namespace, schema_name)
        if schema_json is None:
            schema_file = os.path.join(os.path.dirname(__file__), 'schemas', schema_name + '.json')
            if not os.path.exists(schema_file):
                raise ValidationError("Metadata schema file '{}' is missing!".format(schema_file))

            self.log.debug("Loading metadata schema from: '{}'".format(schema_file))
            with io.open(schema_file, 'r', encoding='utf-8') as f:
                schema_json = json.load(f)
            self.schema_mgr.add_schema(self.namespace, schema_name, schema_json)

        return schema_json

    def _load_from_resource(self, resource, validate_metadata=True, include_invalid=False):
        # This is always called with an existing resource (path) so no need to check existence.
        self.log.debug("Loading metadata resource from: '{}'".format(resource))
        with io.open(resource, 'r', encoding='utf-8') as f:
            metadata_json = json.load(f)

        # Always take name from resource so resources can be copied w/o having to change content
        name = os.path.splitext(os.path.basename(resource))[0]

        reason = None
        if validate_metadata:
            schema_name = metadata_json.get('schema_name')
            if schema_name:
                schema = self._get_schema(schema_name)  # returns a value or throws
                try:
                    self.validate(name, schema_name, schema, metadata_json)
                except ValidationError as ve:
                    if include_invalid:
                        reason = ve.__class__.__name__
                    else:
                        raise ve
            else:
                self.log.debug("No schema found in metadata resource '{}' - skipping validation.".format(resource))

        metadata = Metadata(name=name,
                            display_name=metadata_json['display_name'],
                            schema_name=metadata_json['schema_name'],
                            resource=resource,
                            metadata=metadata_json['metadata'],
                            reason=reason)
        return metadata


class SchemaManager(SingletonConfigurable):
    """Singleton used to store all schemas for all metadata types.
       Note: we currently don't refresh these entries.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # namespace_schemas is a dict of namespace keys to dict of schema_name keys of JSON schema
        self.namespace_schemas = SchemaManager.load_namespace_schemas()

    def is_valid_namespace(self, namespace):
        return namespace in self.namespace_schemas.keys()

    def get_namespaces(self):
        return list(self.namespace_schemas.keys())

    def get_namespace_schemas(self, namespace):
        self.log.debug("SchemaManager: Fetching all schemas from namespace '{}'".format(namespace))
        if not self.is_valid_namespace(namespace):
            raise ValueError("Namespace '{}' is not in the list of valid namespaces: '{}'".
                             format(namespace, self.get_namespaces()))
        schemas = self.namespace_schemas.get(namespace)
        return schemas

    def get_schema(self, namespace, schema_name):
        schema_json = None
        self.log.debug("SchemaManager: Fetching schema '{}' from namespace '{}'".format(schema_name, namespace))
        if not self.is_valid_namespace(namespace):
            raise ValueError("Namespace '{}' is not in the list of valid namespaces: '{}'".
                             format(namespace, self.get_namespaces()))
        schemas = self.namespace_schemas.get(namespace)
        if schema_name not in schemas.keys():
            raise KeyError("Schema '{}' in namespace '{}' was not found!".format(schema_name, namespace))
        schema_json = schemas.get(schema_name)

        return schema_json

    def add_schema(self, namespace, schema_name, schema):
        """Adds (updates) schema to set of stored schemas. """
        if not self.is_valid_namespace(namespace):
            raise ValueError("Namespace '{}' is not in the list of valid namespaces: '{}'".
                             format(namespace, self.get_namespaces()))
        self.log.debug("SchemaManager: Adding schema '{}' to namespace '{}'".format(schema_name, namespace))
        self.namespace_schemas[namespace][schema_name] = schema

    def clear_all(self):
        """Primarily used for testing, this method reloads schemas from initial values. """
        self.log.debug("SchemaManager: Reloading all schemas for all namespaces.")
        self.namespace_schemas = SchemaManager.load_namespace_schemas()

    def remove_schema(self, namespace, schema_name):
        """Removes the schema entry associated with namespace & schema_name. """
        self.log.debug("SchemaManager: Removing schema '{}' from namespace '{}'".format(schema_name, namespace))
        if not self.is_valid_namespace(namespace):
            raise ValueError("Namespace '{}' is not in the list of valid namespaces: '{}'".
                             format(namespace, self.get_namespaces()))
        self.namespace_schemas[namespace].pop(schema_name)

    @classmethod
    def load_namespace_schemas(cls, schema_dir=None):
        """Loads the static schema files into a dictionary indexed by namespace.
           If schema_dir is not specified, the static location relative to this
           file will be used.
           Note: The schema file must have a top-level string-valued attribute
           named 'namespace' to be included in the resulting dictionary.
        """
        # The following exposes the metadata-test namespace if true or 1.
        # Metadata testing will enable this env.  Note: this cannot be globally
        # defined, else the file could be loaded before the tests have enable the env.
        metadata_testing_enabled = bool(os.getenv("METADATA_TESTING", 0))

        namespace_schemas = {}
        if schema_dir is None:
            schema_dir = os.path.join(os.path.dirname(__file__), 'schemas')
        if not os.path.exists(schema_dir):
            raise RuntimeError("Metadata schema directory '{}' was not found!".format(schema_dir))

        schema_files = [json_file for json_file in os.listdir(schema_dir) if json_file.endswith('.json')]
        for json_file in schema_files:
            schema_file = os.path.join(schema_dir, json_file)
            with io.open(schema_file, 'r', encoding='utf-8') as f:
                schema_json = json.load(f)

            # Elyra schema files are required to have a namespace property (see test_validate_factory_schema)
            namespace = schema_json.get('namespace')
            if namespace is None:
                warnings.warn("Schema file '{}' is missing its namespace attribute!  Skipping...".format(schema_file))
                continue
            # Skip test namespace unless we're testing metadata
            if namespace == METADATA_TEST_NAMESPACE and not metadata_testing_enabled:
                continue
            if namespace not in namespace_schemas:  # Create the namespace dict
                namespace_schemas[namespace] = {}
            # Add the schema file indexed by name within the namespace
            name = schema_json.get('name')
            if name is None:
                # If schema is missing a name attribute, use file's basename.
                name = os.path.splitext(os.path.basename(schema_file))[0]
            namespace_schemas[namespace][name] = schema_json

        return namespace_schemas.copy()
