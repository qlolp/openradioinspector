"""Owned stdlib WS protocol fixture. No production Core/hardware claims.

All synthetic source bytes are created in TemporaryDirectory before listening.
Only loopback GET/upgrade and ACK are accepted. Stdin EOF owns shutdown.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import math
from pathlib import Path
import select
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs,urlsplit

PROTOCOL="ori.measurements.v1.protobuf"
CAP=16*1024*1024+64*1024
DESCRIPTOR=dict(version=1,transport="websocket",encoding="protobuf",path="/api/v1/events/ws",
    subprotocol=PROTOCOL,max_frame_bytes=CAP)

def varint(value):
    output=bytearray()
    while value>=128:output.append((value&127)|128);value>>=7
    return bytes(output)+bytes([value])
def blob(tag,value):return varint(tag*8+2)+varint(len(value))+value
def frame(sequence,kind="sweep.received",*,generic=False,wide=False,opaque_settings=False,whole_second=False):
    stamp="2026-11-11T12:00:00Z"if whole_second else f"2026-11-11T12:00:00.{sequence%1000000:06d}Z"
    sweep=dict(id=("é" if sequence==1 else "e\u0301" if sequence==5 else f"sweep-{sequence}"),
        session_id="synthetic-é",stream_id="a",interval=dict(started_at=stamp,ended_at=stamp),
        level_unit="dBm",settings=dict(adapter="synthetic",generator_output=False,rbw_hz=10000))
    if opaque_settings:sweep["settings"]={"é":"NFC","e\u0301":"NFD","huge":2**100}
    payload={"sweep":sweep}
    spectrum=None
    if kind=="future.opaque":
        payload={"unknown":{"é":"NFC","e\u0301":"NFD","huge":2**100,"negative_zero":-0.0}}
    elif kind=="observation.received":
        payload={"observation":dict(id="observation",session_id="synthetic-é",stream_id="r",
            interval=dict(started_at=stamp,ended_at=stamp),frequency_hz=100000003,mode="FM",
            level=137.0,level_unit="s_meter_count",squelch_open=True,decoded_mode=None,
            metadata={"synthetic":True})}
    elif generic:sweep.update(frequencies_hz=[2**80,2**80+1],levels=[-90.0,-89.0])
    else:
        frequencies=[0,2**53+1,2**63-1]if wide else[100000001,100000003,100000005]
        levels=[-0.0,math.nextafter(1.0,2.0),-100.125]
        spectrum=blob(1,b"".join(varint(v)for v in frequencies))+blob(2,b"".join(struct.pack("<d",v)for v in levels))
    envelope=dict(contract="openradioinspector-platform",contract_version=1,
        data=dict(sequence=sequence,event_type=kind,occurred_at=stamp,session_id="synthetic-é",payload=payload))
    raw=json.dumps(envelope,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
    output=b"\x08\x01\x10"+varint(sequence)+blob(3,raw)
    return output if spectrum is None else output+blob(4,spectrum)
def ws(payload,opcode=2):
    length=len(payload)
    header=bytes([0x80|opcode])
    if length<126:header+=bytes([length])
    elif length<65536:header+=b"\x7e"+struct.pack("!H",length)
    else:header+=b"\x7f"+struct.pack("!Q",length)
    return header+payload
def exact(connection,count):
    output=bytearray()
    while len(output)<count:
        chunk=connection.recv(count-len(output))
        if not chunk:raise EOFError
        output.extend(chunk)
    return bytes(output)
def received(connection):
    first,second=exact(connection,2);length=second&127
    if length==126:length=struct.unpack("!H",exact(connection,2))[0]
    elif length==127:length=struct.unpack("!Q",exact(connection,8))[0]
    if not second&128 or length>125:raise ValueError("bounded masked client frame required")
    mask=exact(connection,4);payload=exact(connection,length)
    return first&15,bytes(v^mask[i%4]for i,v in enumerate(payload))

class State:
    def __init__(self,mode,count):
        self.lock=threading.Lock();self.mode=mode;self.connections=set();self.requests=[];self.cursors=[]
        self.acks=[];self.ack_texts=[];self.sent=0;self.bad_acks=0;self.active=0;self.traps=0;self.mutations=0
        self.records=[frame(i)for i in range(1,count+1)]
        if mode=="mixed":self.records=[frame(1),frame(2,"future.opaque"),frame(3,generic=True),frame(4,"observation.received"),frame(5)]
        elif mode=="wide-int64":self.records=[frame(1,wide=True)]
        elif mode=="max-cursor":self.records=[frame(2**64-1)]
        elif mode=="opaque-settings":self.records=[frame(1,opaque_settings=True)]
        elif mode=="cursor-boundary":self.records=[frame(2**63-1),frame(2**63)]
        elif mode=="whole-second":self.records=[frame(1,whole_second=True)]
    def stats(self):
        with self.lock:return dict(acks=list(self.acks),ack_texts=list(self.ack_texts),sent=self.sent,bad_acks=self.bad_acks,
            active=self.active,requests=list(self.requests),cursors=list(self.cursors),traps=self.traps,mutations=self.mutations)
    def close_connections(self):
        with self.lock:connections=list(self.connections)
        for connection in connections:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            connection.close()

def handler(state,trap_url):
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"
        def log_message(self,*args):pass
        def response(self,status,value):
            raw=json.dumps(value,separators=(",",":")).encode();self.send_response(status)
            self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)))
            self.send_header("Connection","close");self.end_headers();self.wfile.write(raw);self.close_connection=True
        def do_POST(self):
            with state.lock:state.mutations+=1
            self.response(405,{})
        def do_GET(self):
            parsed=urlsplit(self.path)
            with state.lock:state.requests.append(parsed.path)
            if parsed.path=="/api/v1/capabilities":
                self.response(200,dict(contract="openradioinspector-platform",contract_version=1,
                    data=dict(features={"measurement_binary_stream":DESCRIPTOR})));return
            if parsed.path=="/trap":
                with state.lock:state.traps+=1
                self.response(500,{});return
            if parsed.path!="/api/v1/events/ws":self.response(404,{});return
            if state.mode=="redirect":
                self.send_response(302);self.send_header("Location",trap_url);self.send_header("Content-Length","0");self.end_headers();self.close_connection=True;return
            if state.mode=="auth":self.response(401,{"error":{"code":"unauthorized","message":"owned fixture rejects upgrade"}});return
            if self.headers.get("Sec-WebSocket-Protocol")!=PROTOCOL:self.response(400,{});return
            after=parse_qs(parsed.query).get("after",["0"])[0]
            if not after.isascii() or not after.isdigit() or str(int(after))!=after or int(after)>2**64-1:self.response(400,{});return
            with state.lock:state.cursors.append(after)
            key=self.headers.get("Sec-WebSocket-Key","")
            accept=base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            selected=PROTOCOL if state.mode!="wrong-protocol" else "other.protocol"
            headers=("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: {selected}\r\n\r\n").encode()
            connection=self.connection;connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1);connection.settimeout(30)
            with state.lock:state.connections.add(connection);state.active+=1
            try:
                records=list(enumerate(state.records,1))[int(after):]
                if state.mode=="max-cursor":records=[(2**64-1,state.records[0])]if int(after)<2**64-1 else[]
                elif state.mode=="cursor-boundary":records=[(2**63-1+i,value)for i,value in enumerate(state.records)if 2**63-1+i>int(after)]
                if state.mode=="malformed":records=[(int(after)+1,b"\x08\x01")]
                elif state.mode=="text":records=[(int(after)+1,b"not a binary event")]
                elif state.mode=="oversized":records=[(int(after)+1,b"\0"*(CAP+1))]
                first=True
                for sequence,payload in records:
                    outgoing=ws(payload,1 if state.mode=="text" else 2)
                    connection.sendall((headers if first else b"")+outgoing);first=False
                    with state.lock:state.sent+=1
                    while True:
                        opcode,ack=received(connection)
                        if opcode==9:connection.sendall(ws(ack,10));continue
                        if opcode==10:continue
                        if opcode==8:return
                        break
                    if opcode!=1 or not 1<=len(ack)<=20 or ack!=str(sequence).encode():
                        with state.lock:state.bad_acks+=1
                        connection.sendall(ws(struct.pack("!H",1008),8));return
                    with state.lock:state.acks.append(sequence);state.ack_texts.append(ack.decode("ascii"))
                if first:connection.sendall(headers)
                connection.sendall(ws(struct.pack("!H",1001)+b"owned fixture ended",8))
            except (EOFError,OSError,ValueError):pass
            finally:
                with state.lock:state.active-=1;state.connections.discard(connection)
                self.close_connection=True
    return Handler

class Server(ThreadingHTTPServer):
    allow_reuse_address=True;daemon_threads=True
    def handle_error(self,*args):pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--mode",default="replay");parser.add_argument("--count",type=int,default=5);parser.add_argument("--tls",action="store_true");args=parser.parse_args()
    if not 1<=args.count<=2000:raise ValueError("bounded fixture count")
    state=State(args.mode,args.count)
    with tempfile.TemporaryDirectory(prefix="ori-native-binary-") as temporary:
        root=Path(temporary);source=root/"synthetic-source.bin";source.write_bytes(b"".join(state.records));before=hashlib.sha256(source.read_bytes()).hexdigest()
        trap=Server(("127.0.0.1",0),handler(state,"http://127.0.0.1/unused"));trap_thread=threading.Thread(target=trap.serve_forever,daemon=True);trap_thread.start()
        trap_url=f"http://127.0.0.1:{trap.server_port}/trap"
        context=None
        if args.tls:
            subprocess.run(["openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:P-256","-nodes","-days","1","-subj","/CN=owned-native-fixture","-addext","subjectAltName=IP:127.0.0.1","-keyout",str(root/"key.pem"),"-out",str(root/"cert.pem")],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/"cert.pem",root/"key.pem")
        def start(port=0):
            server=Server(("127.0.0.1",port),handler(state,trap_url))
            if context:server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();return server,thread
        server,thread=start();port=server.server_port
        print(json.dumps(dict(url=f"{'https'if args.tls else 'http'}://127.0.0.1:{port}",events=len(state.records),source_sha256=before)),flush=True)
        try:
            for line in sys.stdin:
                command=json.loads(line)
                if command.get("restart"):
                    state.close_connections();server.shutdown();server.server_close();thread.join(3);server,thread=start(port)
                    print(json.dumps({"restarted":True}),flush=True)
                elif command.get("append"):
                    if state.mode!="replay"or len(state.records)>=2000:raise ValueError("bounded replay fixture append only")
                    # Explicit fixture-owner production of one new live event;
                    # the sealed seed file and every prior source byte remain unchanged.
                    with state.lock:
                        sequence=len(state.records)+1;state.records.append(frame(sequence))
                    print(json.dumps({"appended":True,"sequence":sequence}),flush=True)
                elif command.get("stats"):print(json.dumps(state.stats()),flush=True)
                else:raise ValueError("unknown owned fixture command")
        finally:
            state.close_connections();server.shutdown();server.server_close();thread.join(3);trap.shutdown();trap.server_close();trap_thread.join(3)
            print(json.dumps(dict(closed=True,source_unchanged=hashlib.sha256(source.read_bytes()).hexdigest()==before,stats=state.stats())),flush=True)
if __name__=="__main__":main()
