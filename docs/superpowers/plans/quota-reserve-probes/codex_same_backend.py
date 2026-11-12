from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,subprocess,threading,time,os,pty,fcntl,termios,struct,signal,select,sys,tempfile,atexit
BASE=Path(tempfile.mkdtemp(prefix='codex-quota-native-probe-')); binary=str(Path(sys.argv[2]).resolve()); print('scratch',BASE,flush=True)
requests=[]; holding=threading.Event(); done=threading.Event()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  requests.append((self.command,self.path)); self.send_error(503)
 do_CONNECT=do_GET
 def do_POST(self):
  n=int(self.headers.get('Content-Length','0')); body=self.rfile.read(n)
  requests.append((self.command,self.path))
  if self.path.endswith('/responses'):
   holding.set(); self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
   try:
    self.wfile.write(b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_fixture","object":"response","status":"in_progress","output":[]}}\n\n'); self.wfile.flush()
    while not done.wait(.1): self.wfile.write(b': fixture heartbeat\n\n'); self.wfile.flush()
   except (BrokenPipeError,ConnectionResetError): pass
  else: self.send_error(503)
 def log_message(self,*args): pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler); threading.Thread(target=server.serve_forever,daemon=True).start()
endpoint=f'http://127.0.0.1:{server.server_port}'
mode=sys.argv[1]; run=BASE/mode; run.mkdir(exist_ok=True); run.chmod(0o700); home=run/'home'; ch=run/'codex'; wd=run/'workspace'
for p in (home,ch,wd): p.mkdir(exist_ok=True,mode=0o700)
config=f'''model = "gpt-5.4"
model_provider = "offline_fixture"
cli_auth_credentials_store = "file"
approval_policy = "never"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.offline_fixture]
name = "offline_fixture"
base_url = "{endpoint}/v1"
env_key = "CODEX_NATIVE_TEST_KEY"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[features]
plugins = false
recommended_plugins = false
apps = false
enable_mcp_apps = false
memories = false
shell_snapshot = false
shell_snapshot_v2 = false
network_proxy = false
code_mode_prewarm = false
[memories]
generate_memories = false
use_memories = false
[analytics]
enabled = false
[projects."{wd}"]
trust_level = "trusted"
'''
(ch/'config.toml').write_text(config)
env={'PATH':'/usr/bin:/bin','HOME':str(home),'CODEX_HOME':str(ch),'CODEX_SQLITE_HOME':str(ch),'XDG_CONFIG_HOME':str(run/'config'),'XDG_CACHE_HOME':str(run/'cache'),'TMPDIR':str(run),'TERM':'xterm-256color','CODEX_NATIVE_TEST_KEY':'fictional-offline-test-key','NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost',**{x:endpoint for x in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')}}
master,slave=pty.openpty(); fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,0,0))
def child_setup(): os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0)
import importlib.util,socket,base64,hashlib
spec=importlib.util.spec_from_file_location('quota_probe_handoff',str(Path(sys.argv[3])/'lib/codex-as/handoff.py')); h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
class Client(h.Client):
 def __init__(self,path):
  self.timeout=5; self.deadline=time.monotonic()+5; self.buffer=bytearray(); self.next_id=0
  self.sock=socket.socket(socket.AF_UNIX); self.sock.settimeout(5); self.sock.connect(str(path))
  key=base64.b64encode(os.urandom(16)).decode()
  self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: "+key+"\r\n\r\n").encode())
  while b"\r\n\r\n" not in self.buffer: self.buffer.extend(self._recv())
  head,_,rest=self.buffer.partition(b"\r\n\r\n"); self.buffer=bytearray(rest)
  assert b'101 Switching Protocols' in head
  self.request('initialize',{'clientInfo':{'name':'quota-offline-probe','version':'1'},'capabilities':{'experimentalApi':True}})
  self.send({'method':'initialized','params':{}})
sockpath=run/'backend.sock'; backerr=(run/'backend.stderr').open('wb')
backend=subprocess.Popen([binary,'app-server','--listen','unix://'+str(sockpath)],env=env,cwd=wd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=backerr,start_new_session=True)
def cleanup_backend():
 if backend.poll() is None:
  backend.terminate()
  try: backend.wait(timeout=3)
  except subprocess.TimeoutExpired: backend.kill(); backend.wait(timeout=3)
atexit.register(cleanup_backend)
for _ in range(100):
 if sockpath.exists() or backend.poll() is not None: break
 time.sleep(.05)
ctl=Client(sockpath)
args=[binary,'--remote','unix://'+str(sockpath),'--no-alt-screen','-C',str(wd),'Hold this fabricated test request.']
proc=subprocess.Popen(args,env=env,cwd=wd,stdin=slave,stdout=slave,stderr=slave,preexec_fn=child_setup); os.close(slave)
out=bytearray(); stop_reader=threading.Event()
def read():
 while not stop_reader.is_set():
  if not select.select([master],[],[],.1)[0]: continue
  try: b=os.read(master,65536)
  except OSError: break
  if not b: break
  out.extend(b)
  if b'\x1b[6n' in b: os.write(master,b'\x1b[1;1R')
  if b'\x1b[c' in b: os.write(master,b'\x1b[?1;2c')
threading.Thread(target=read,daemon=True).start()
try:
 got=holding.wait(10); print('held_response',got,'pid',proc.pid,'exit_before',proc.poll(),'requests',requests,flush=True)
 if not got: print('startup_output',repr(bytes(out[-6000:])),flush=True)
 elif mode=='sigint': os.kill(proc.pid,signal.SIGINT)
 elif mode=='samebackend':
  ids=ctl.request('thread/loaded/list',{})['data']; print('loaded',ids,flush=True)
  th=ctl.request('thread/read',{'threadId':ids[0],'includeTurns':True})['thread']; turns=th['turns']; print('turns',[(x['id'],x['status']) for x in turns],flush=True)
  print('interrupt_result',ctl.request('turn/interrupt',{'threadId':ids[0],'turnId':turns[-1]['id']}),flush=True)
  time.sleep(.8)
  print('native_ui_alive_after_rpc',proc.poll() is None,'interrupted_banner',b'Conversation interrupted' in out,flush=True)
  ctl.close(); print('controller_disconnected',flush=True)
  holding.clear(); os.write(master,b'Another fabricated test request.'); time.sleep(.5); os.write(master,b'\r')
  print('new_request_after_control_loss',holding.wait(3),flush=True)
 time.sleep(1)
 print('exit_after',proc.poll(),'interrupted_banner',b'Conversation interrupted' in out,'idle_event',b'state=idle' in out,flush=True)
 if proc.poll() is None:
  os.write(master,b'/status\r'); time.sleep(.4); print('usable_after_status',b'Session' in out[-5000:] or b'Model' in out[-5000:], 'alive',proc.poll() is None,flush=True)
finally:
 done.set(); stop_reader.set()
 if proc.poll() is None: os.killpg(proc.pid,signal.SIGTERM)
 try: proc.wait(timeout=3)
 except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL); proc.wait(timeout=3)
 os.close(master)
 if backend.poll() is None: os.killpg(backend.pid,signal.SIGTERM)
 try: backend.wait(timeout=3)
 except subprocess.TimeoutExpired: os.killpg(backend.pid,signal.SIGKILL); backend.wait(timeout=3)
 backerr.close(); server.shutdown(); server.server_close()
 (run/'output.bin').write_bytes(out); (run/'requests.json').write_text(json.dumps(requests))
