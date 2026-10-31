"""Task-local API and pool share the same sixteen-worker configuration."""
import os
import sys
from pathlib import Path
from remote_backend import RemoteEnginePool
from resident_worker import resident_worker
import recipe_api.backend as backend
def factory(config,worker_python):
    if (config.ep,config.dp)!=(16,16):raise ValueError('Expected authoritative EP16/DP16 API config')
    return RemoteEnginePool(config,worker_python,
        bind=(os.environ['DEEPINFER_CONTROL_BIND'],int(os.environ['DEEPINFER_CONTROL_PORT'])),
        peer=os.environ['DEEPINFER_CONTROL_PEER'],run_id=os.environ['DEEPINFER_CONTROL_RUN_ID'],worker_target=resident_worker)
backend.EnginePool=factory
from serve_recipe_ep16 import main
if __name__=='__main__':main()
