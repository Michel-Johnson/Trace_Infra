"""Canonical runtime metadata, separate from immutable operation implementations."""
from urllib.parse import urlsplit
from ..access.service import display_name
from ..invocations.service import _url_token, operation_ref
from ..resources import json_object, namespace_token
from ..traces.service import MAX_REVISION

PROTOCOL = 'trace-hunter/remote-runtime/1'
BINDING_VERSION = 'trace-hunter/runtime-binding/1'
CAPABILITIES_VERSION = 'trace-hunter/runtime-capabilities/1'


def revision_number(value, *, previous=False):
    minimum,maximum=(0,MAX_REVISION-1) if previous else (1,MAX_REVISION)
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError('Runtime revision is outside the supported range')
    return value


def endpoint_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(char)<33 or ord(char)==127 for char in value):
        raise ValueError('Runtime endpoint must be an HTTP(S) base URL')
    parsed=urlsplit(value)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('Runtime URL must not contain credentials, query or fragment')
    if parsed.port is not None and not 1<=parsed.port<=65535:
        raise ValueError('Invalid runtime endpoint port')
    return value


def binding_config(value):
    if not isinstance(value,dict) or set(value)!={'name','endpoint','enabled','auth','metadata'}:
        raise ValueError('Runtime binding fields do not match the protocol')
    display_name(value['name']);endpoint_url(value['endpoint'])
    if type(value['enabled']) is not bool:raise ValueError('enabled must be boolean')
    auth=value['auth']
    if not isinstance(auth,dict):raise ValueError('Runtime auth must be a reference')
    if auth.get('kind')=='none' and set(auth)=={'kind'}:pass
    elif auth.get('kind')=='bearer' and set(auth)=={'kind','secret_ref'}:
        namespace_token(auth['secret_ref'],'secret_ref')
    else:raise ValueError('Runtime auth accepts none or a bearer secret_ref')
    json_object(value['metadata'],'runtime metadata')
    return json_object(value,'runtime binding')


def capabilities(value):
    if not isinstance(value,dict) or set(value)!={'schema_version','name','version','protocols','operations'}:
        raise ValueError('Invalid runtime capabilities document')
    if value['schema_version']!=CAPABILITIES_VERSION:raise ValueError('Unsupported capabilities schema')
    display_name(value['name']);_url_token(value['version'],'runtime version')
    protocols=value['protocols'];operations=value['operations']
    if not isinstance(protocols,list) or not 1<=len(protocols)<=8:raise ValueError('Declare 1 to 8 protocols')
    for item in protocols:namespace_token(item,'protocol')
    if len(set(protocols))!=len(protocols):raise ValueError('Duplicate protocol')
    if not isinstance(operations,list) or len(operations)>100:raise ValueError('Declare at most 100 operations')
    references=[operation_ref(item) for item in operations]
    if len({(item['operation_id'],item['version']) for item in references})!=len(references):
        raise ValueError('Duplicate operation version')
    return json_object({**value,'operations':references},'runtime capabilities')
