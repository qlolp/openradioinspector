"""One sealed input, one derived job, one result; the supervisor owns lifecycle."""
from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from .cache import (build_cache, identity, prepare_root, publish, remove_abandoned,
                    remove_owned, tree_bytes, verify_cache)
from .queries import query
from .resources import exclusive_lock
from .source import open_snapshot


def run(request, budget):
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    budget.checkpoint()
    root = prepare_root(request,budget)
    with exclusive_lock(root):
        remove_abandoned(root,budget)
        stage = None
        try:
            with open_snapshot(request['snapshot_path'],request['filters'],budget) as (db,source):
                key = identity(source,request['filters'])
                destination = root/('cache-'+key)
                cached = verify_cache(destination,key,source,request['filters'],budget)
                rebuilt = False
                if cached:
                    manifest,manifest_sha = cached
                    hit = True
                else:
                    if destination.exists():
                        remove_owned(destination,root)
                        budget.cache_used = tree_bytes(root)
                        rebuilt = True
                    stage,manifest,manifest_sha = build_cache(db,root,key,source,request['filters'],budget)
                    hit = False
            # open_snapshot rechecks its sealed source before this commit.
            if stage is not None:
                publish(stage,destination)
                stage = None
            if request['operation'] == 'export':
                return {'cache_key':key,'cache_path':str(destination),'cache_hit':hit,
                        'cache_rebuilt':rebuilt,'manifest':manifest,'manifest_sha256':manifest_sha}
            request['_source_watermark'] = source['event_watermark']
            result = query(request,destination,key,root,budget)
            return result | {'cache_hit':hit,'cache_rebuilt':rebuilt,'manifest_sha256':manifest_sha}
        finally:
            if stage is not None:
                remove_owned(stage,root)
                budget.cache_used = tree_bytes(root)
