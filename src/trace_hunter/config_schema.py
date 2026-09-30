"""Offline Draft 2020-12 configuration schemas, traversing schemas rather than data."""
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, UnknownDialect

DIALECT = 'https://json-schema.org/draft/2020-12/schema'


def configuration_validator(schema):
    """Check reachable schemas, including local reference targets and nested IDs.

Examples, defaults and const values remain application data unless a reference
explicitly uses them as a schema. Registry's default retriever rejects missing
resources; it never downloads schemas, including while validating configuration.
"""
    try:
        Draft202012Validator.check_schema(schema)
        root = Resource(schema, DRAFT202012)
        registry = Registry()
        pending = [(root, registry.resolver_with_root(root), False)]
        seen = set()
        while pending:
            resource, resolver, referenced = pending.pop()
            value = resource.contents
            if isinstance(value, bool) or id(value) in seen:
                continue
            seen.add(id(value))
            if referenced:
                Draft202012Validator.check_schema(value)
            if '$schema' in value and value['$schema'] not in (DIALECT, DIALECT+'#'):
                raise ValueError('Only Draft 2020-12 configuration schemas are supported')
            for key in ('$ref', '$dynamicRef'):
                if key not in value:
                    continue
                ref = value[key]
                if not isinstance(ref, str) or not ref.startswith('#'):
                    raise ValueError('Configuration schemas require local references')
                target = resolver.lookup(ref)
                pending.append((Resource(target.contents, DRAFT202012), target.resolver, True))
            for child in DRAFT202012.subresources_of(value):
                subresource = Resource(child, DRAFT202012)
                pending.append((subresource, resolver.in_subresource(subresource), False))
        return Draft202012Validator(schema, registry=registry)
    except (ValueError, TypeError, SchemaError, Unresolvable, UnknownDialect, RecursionError):
        raise ValueError('Invalid offline Draft 2020-12 configuration schema') from None
