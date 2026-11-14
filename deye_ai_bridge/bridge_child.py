"""Run current or retained older app releases behind the pinned ingress port."""
import http.server
import os
import runpy
import sys
from pathlib import Path

original = http.server.ThreadingHTTPServer


class ChildServer(original):
    def __init__(self, address, *args, **kwargs):
        if address[1] == 8099:
            address = ('127.0.0.1', int(os.environ.get('DEYE_APP_PORT','8098')))
        super().__init__(address,*args,**kwargs)


if __name__ == '__main__':
    http.server.ThreadingHTTPServer = ChildServer
    app = Path(sys.argv[1]).resolve()
    sys.path.insert(0,str(app.parent))
    runpy.run_path(str(app),run_name='__main__')
