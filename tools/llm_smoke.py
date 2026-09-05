"""Real HTTPX socket to a temporary fake server; no model or secret required."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from noveltool.domain import ProjectConfig
from noveltool.llm import LLMClient


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path == '/v1/chat/completions'
        data = json.loads(self.rfile.read(int(self.headers['content-length'])))
        assert data['model'] == 'temporary-fake-model'
        body = json.dumps({'choices':[{'message':{'content':'实际 HTTP 通路已验证'},'finish_reason':'stop'}]},ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass


async def check(base):
    async with LLMClient(ProjectConfig(api_base_url=base,writer_model='temporary-fake-model')) as client:
        result=await client.complete(model='temporary-fake-model',messages=[{'role':'user','content':'hello'}])
        assert result.text=='实际 HTTP 通路已验证'
        print('PASS: real HTTPX request to local fake Chat Completions endpoint; not a real model quality test')


if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:asyncio.run(check(f'http://127.0.0.1:{server.server_port}/v1'))
    finally:server.shutdown();server.server_close();thread.join()
