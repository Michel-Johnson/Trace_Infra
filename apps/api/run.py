"""Run from any working directory: python apps/api/run.py."""
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(Path(__file__).resolve().parent)]
import uvicorn
from trace_hunter_api.app import create_app
from trace_hunter.otlp_grpc import start as start_otlp_grpc

if __name__ == '__main__':
    # Operator compatibility trusts the socket peer, never client-supplied XFF.
    app = create_app()
    grpc_server = start_otlp_grpc(app.state.otlp, app.state.projects,
                                  address=os.environ.get('OTLP_GRPC_ADDRESS', '127.0.0.1:4317'))
    try:
        uvicorn.run(app, host=os.environ.get('API_HOST', '127.0.0.1'),
                    port=int(os.environ.get('API_PORT', '8767')), access_log=False, proxy_headers=False)
    finally:
        grpc_server.stop(grace=5)
