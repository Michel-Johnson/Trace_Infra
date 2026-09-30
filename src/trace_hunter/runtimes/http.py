"""Explicit, bounded remote capability reads. No dispatch or source data is sent."""
import asyncio
import json
import os
from pathlib import Path

import httpx

from .contracts import capabilities

CAPABILITIES_PATH = '/.well-known/trace-hunter-runtime.json'
MAX_RESPONSE_BYTES = 128*1024


class ProbeFailure(Exception):
    def __init__(self,code,http_status=None):
        self.code=code;self.http_status=http_status
        super().__init__(code)


class FileRuntimeSecrets:
    def __init__(self,path=None):
        self.path=path if path is not None else os.environ.get('TRACE_HUNTER_RUNTIME_SECRETS_FILE')

    def bearer(self,reference):
        try:
            if not self.path:raise ValueError()
            with Path(self.path).open('rb') as source:raw=source.read(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError()
            value=json.loads(raw)[reference]
            if not isinstance(value,str) or not 1<=len(value)<=8192 or any(not 33<=ord(char)<=126 for char in value):
                raise ValueError()
            return value
        except (OSError,ValueError,KeyError,TypeError,RecursionError):
            raise ProbeFailure('credential_unavailable') from None


class CapabilityReader:
    def __init__(self,secrets=None,*,deadline_seconds=10):
        self.secrets=secrets if secrets is not None else FileRuntimeSecrets()
        self.deadline_seconds=deadline_seconds

    def read(self,config):
        # Convenience for synchronous callers. The API awaits aread directly,
        # so a cancelled DNS lookup never delays request completion while a
        # per-request event loop waits for its executor to shut down.
        return asyncio.run(self.aread(config))

    async def aread(self,config):
        headers={'Accept':'application/json','Accept-Encoding':'identity'}
        if config['auth']['kind']=='bearer':
            headers['Authorization']='Bearer '+self.secrets.bearer(config['auth']['secret_ref'])
        try:
            async with asyncio.timeout(self.deadline_seconds):
                async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=5) as client:
                    async with client.stream('GET',config['endpoint'].rstrip('/')+CAPABILITIES_PATH,headers=headers) as response:
                        if not 100<=response.status_code<=599:raise ProbeFailure('invalid_document')
                        if response.status_code!=200:raise ProbeFailure('http_error',response.status_code)
                        if response.headers.get('content-encoding','identity').lower() not in ('identity',''):
                            raise ProbeFailure('unsupported_encoding',200)
                        if response.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json':
                            raise ProbeFailure('invalid_document',200)
                        raw=bytearray()
                        async for chunk in response.aiter_raw():
                            if len(raw)+len(chunk)>MAX_RESPONSE_BYTES:raise ProbeFailure('response_too_large',200)
                            raw.extend(chunk)
                        try:document=capabilities(json.loads(raw))
                        except (ValueError,TypeError,KeyError,RecursionError):raise ProbeFailure('invalid_document',200) from None
            return document
        except (TimeoutError,httpx.TimeoutException):raise ProbeFailure('timeout') from None
        except (httpx.HTTPError,httpx.InvalidURL):raise ProbeFailure('connection_error') from None
