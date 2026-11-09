"""Own synthetic serial fixture, for opt-in full-browser checks only."""
import argparse
import json
import threading
import unittest
from tests.test_tinysa_instrument_api import InstrumentHTTPTests

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=0);parser.parse_args()
    case=InstrumentHTTPTests();case.setUp()
    print(json.dumps(dict(base_url=f'http://127.0.0.1:{case.server.server_address[1]}',session_id=case.session.id,stream_id=case.stream.id)),flush=True)
    try:input()
    except EOFError:pass
    finally:case.doCleanups()

if __name__=='__main__':main()
