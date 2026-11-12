from __future__ import annotations

import json
import os
import socket
from urllib.error import HTTPError,URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler,Request,build_opener

from .config import BenchmarkError
from .source import encode

JSON_LIMIT=16*1024**2
LINE_LIMIT=2*1024**2


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def segment(value):return quote(value,safe='')


class HTTPFailure(BenchmarkError):
    def __init__(self,status):
        self.status=status
        super().__init__('http_failure','HTTP request failed with status '+str(status))


class Client:
    def __init__(self,config):
        self.origin=config.base_url.rstrip('/')
        self.timeout=config.request_timeout
        self.opener=build_opener(NoRedirect())

    def open(self,path,*,method='GET',payload=None,timeout=None):
        if not path.startswith('/api/v1/') or path.startswith('//'):
            raise BenchmarkError('invalid_route','Only typed API v1 paths are allowed')
        raw=None if payload is None else encode(payload)
        if raw is not None and len(raw)>JSON_LIMIT:
            raise BenchmarkError('request_too_large','Bounded JSON request exceeds 16 MiB')
        headers={'Accept':'application/json','Content-Type':'application/json'}
        token=os.environ.get('ORI_BENCH_TOKEN')
        if token:headers['Authorization']='Bearer '+token
        request=Request(self.origin+path,data=raw,headers=headers,method=method)
        try:return self.opener.open(request,timeout=self.timeout if timeout is None else timeout)
        except HTTPError as error:
            status=error.code;error.close();raise HTTPFailure(status) from None
        except (URLError,OSError,TimeoutError,socket.timeout):
            raise BenchmarkError('transport_error','Request transport failed or timed out') from None

    def json(self,path,*,method='GET',payload=None,status=200):
        with self.open(path,method=method,payload=payload) as response:
            if response.status!=status:raise HTTPFailure(response.status)
            raw=response.read(JSON_LIMIT+1)
            if len(raw)>JSON_LIMIT:raise BenchmarkError('invalid_response','JSON response exceeds 16 MiB')
            try:
                value=json.loads(raw)
                return value['data']
            except (ValueError,TypeError,KeyError,RecursionError):
                raise BenchmarkError('invalid_response','Invalid API envelope') from None

    def session_path(self,config):return '/api/v1/sessions/'+segment(config.session_id)
