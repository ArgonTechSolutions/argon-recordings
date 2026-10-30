"""Bounded real mTLS/E5 smoke with IP reassignment and retained synthetic indexes.

Run on personal rootless Docker only. Uses existing read-only models, no downloads.
All fixture containers/networks/volumes are removed in finally; real data is never mounted.
"""
import json, os, subprocess, tempfile, time, uuid
from pathlib import Path
import argparse
from dev_container import docker_command
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image',required=True)
parser.add_argument('--models',type=Path,default=Path(__file__).resolve().parents[1]/'models')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
models=args.models.resolve()
assert (models/'turbo').is_dir() and (models/'e5-large').is_dir()
docker_base=docker_command()
prefix='argon-recordings-discovery-smoke-'+str(os.getpid())+'-'+uuid.uuid4().hex[:8]
image=args.image
containers=[]; networks=[]; volumes=[]
def docker(*args, **kwargs):
 args=list(args)
 if args[0]=='run':
  if '--name' not in args:args[1:1]=['--name',prefix+'-utility-'+uuid.uuid4().hex[:8]]
  args[1:1]=['--label','com.argon.recordings.fixture='+prefix]
 elif args[:2] in (['network','create'],['volume','create']):
  args[2:2]=['--label','com.argon.recordings.fixture='+prefix]
 return subprocess.run([*docker_base,*map(str,args)],check=True,text=True,capture_output=True,timeout=240,**kwargs).stdout.strip()
def run(name,network,args,role):
 name=prefix+'-'+name;containers.append(name)
 docker('run','-d','--name',name,'--network','bridge','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--cpus=2','--memory=4g','--memory-swap=4g','--pids-limit=128','--tmpfs','/tmp:rw,size=64m', '-v',str(root/'backend/argon_recordings')+':/app/argon_recordings:ro',*args,'--entrypoint','python',image,'-m','argon_recordings.'+role)
 docker('network','connect',*(['--alias','api'] if name.endswith('-relay') else []),network,name)
 return name
def execute(name,code):return docker('exec',name,'python','-c',code)
def wait(name,path):
 for _ in range(120):
  try:
   if execute(name,f"from pathlib import Path; print(Path({path!r}).exists())")=='True':return
  except subprocess.CalledProcessError:pass
  time.sleep(.5)
 raise RuntimeError('Not ready: '+name+': '+docker('logs','--tail','10',name))
try:
 with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
  tls=Path(temporary)
  for name,purpose in [('coordinator','serverAuth'),('worker','clientAuth')]:
   subprocess.run(['openssl','req','-x509','-newkey','ed25519','-nodes','-days','1','-keyout',str(tls/(name+'.key')),'-out',str(tls/(name+'.crt')),'-subj','/CN='+name,'-addext','basicConstraints=critical,CA:FALSE','-addext','extendedKeyUsage='+purpose,'-addext','subjectAltName=DNS:coordinator'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for suffix in ('api','link','worker'):
   name=prefix+'-'+suffix;docker('network','create','--internal',name);networks.append(name)
  volume=prefix+'-credentials';docker('volume','create','--driver','local','--opt','type=tmpfs','--opt','device=tmpfs','--opt','o=size=65536,uid=10001,gid=10001,mode=0700',volume);volumes.append(volume)
  uid='0' if 'rootless' in docker('info','--format','{{json .SecurityOptions}}') else str(os.geteuid())
  subnet=json.loads(docker('network','inspect',networks[0]))[0]['IPAM']['Config'][0]['Subnet']
  common=['--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--cpus=2','--memory=4g','--memory-swap=4g','--pids-limit=128','--tmpfs','/tmp:rw,size=64m','-v',str(root/'backend/argon_recordings')+':/app/argon_recordings:ro']
  data=prefix+'-data';docker('volume','create',data);volumes.append(data)
  docker('run','--rm','--network','none','--user','0:0','-v',data+':/data','--entrypoint','chown',image,'10001:10001','/data')
  api=prefix+'-api';containers.append(api)
  docker('run','-d','--name',api,'--network','bridge',*common,'--user','10001:10001','-v',data+':/data','-v',volume+':/run/worker-credentials/1','-e','RECORDINGS_WORKER_COUNT=1','-e','RECORDINGS_WORKER_SUBNETS='+subnet,'-e','RECORDINGS_TRANSPORT_GROUP=true','-e','RECORDINGS_REMOTE_EMBEDDINGS=true','--entrypoint','python',image,'-m','argon_recordings.isolated','api')
  docker('network','connect','--alias','api',networks[0],api)
  def tlsargs(name,peer):
   return ['--user',uid+':10001',*sum((['--mount',f'type=bind,src={tls/source},dst=/run/transport-tls/{target},readonly'] for source,target in [(name+'.key','self.key'),(name+'.crt','self.crt'),(peer+'.crt','peer.crt')]),[])]
  coordinator=run('coordinator',networks[0],[*tlsargs('coordinator','worker'),'--dns','127.0.0.1','-v',volume+':/run/worker-credentials/self:ro','-e','RECORDINGS_TRANSPORT_MODE=coordinator'],'transport')
  docker('network','connect','--alias','coordinator',networks[1],coordinator)
  workersubnet=json.loads(docker('network','inspect',networks[2]))[0]['IPAM']['Config'][0]['Subnet']
  relay=run('relay',networks[2],[*tlsargs('worker','coordinator'),'-e','RECORDINGS_TRANSPORT_MODE=worker','-e','RECORDINGS_WORKER_SUBNETS='+workersubnet,'-e','RECORDINGS_COORDINATOR_ORIGIN=https://coordinator:8443'],'transport')
  docker('network','connect',networks[1],relay)
  worker=prefix+'-worker';containers.append(worker)
  docker('run','-d','--name',worker,'--network',networks[2],*common,'--user','10001:10001','--dns','127.0.0.1','--tmpfs','/work:rw,size=2g,uid=10001,gid=10001','-v',str(models/'turbo')+':/models/turbo:ro','-v',str(models/'e5-large')+':/models/embeddings:ro','-e','RECORDINGS_WORKER_ID=worker-1','-e','RECORDINGS_EMBEDDING_DIR=/models/embeddings','-e','RECORDINGS_REMOTE_INFERENCE=true','--entrypoint','python',image,'-m','argon_recordings.isolated','remote-worker')
  wait(coordinator,'/tmp/transport-ready');wait(relay,'/tmp/transport-ready');wait(worker,'/tmp/worker-ready')
  health="import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8000/api/health'))['workers_online'])"
  for _ in range(30):
   if execute(api,health)=='1':break
   time.sleep(1)
  assert execute(api,health)=='1';print('PASS: private TLS worker connected',flush=True)
  seed="from argon_recordings.store import Store; from pathlib import Path; s=Store(Path('/data')); db=s.db(); c=db.__enter__(); c.execute(\"INSERT INTO projects VALUES('p','Synthetic',0)\"); c.execute(\"INSERT INTO recordings(id,project_id,title,filename,language,status,created) VALUES('r','p','Synthetic','test.wav','es','ready',0)\"); c.execute(\"INSERT INTO segments VALUES('s','r',0,5,'Tamaños de pantalla y diseño adaptable','[]')\"); db.__exit__(None,None,None)"
  execute(api,seed)
  query="import urllib.request,json; r=json.load(urllib.request.urlopen('http://localhost:8000/api/search?project_id=p&q=screen%20sizes&mode=semantic',timeout=180)); assert r[0]['recording_id']=='r',r; print('semantic result OK')"
  print('Real E5:',execute(api,query),flush=True)
  index="from pathlib import Path; from argon_recordings.store import Store; import hashlib; s=Store(Path('/data')); c=s.db(); db=c.__enter__(); print(hashlib.sha256(b''.join(r[0] for r in db.execute('SELECT vector FROM embeddings'))).hexdigest()); c.__exit__(None,None,None)"
  before=execute(api,index)
  def endpoint(name,network):
   return json.loads(docker('inspect',name))[0]['NetworkSettings']['Networks'][network]['IPAddress']
  def move(name,network,alias):
   old=endpoint(name,network)
   docker('network','disconnect',network,name)
   blocker=prefix+'-reserve-'+str(len(containers));containers.append(blocker)
   docker('run','-d','--name',blocker,'--network',network,'--ip',old,
          '--read-only','--cap-drop=ALL','--memory=64m','--cpus=.1',
          '--entrypoint','python',image,'-c','import time; time.sleep(240)')
   docker('network','connect','--alias',alias,network,name)
   new=endpoint(name,network)
   assert new!=old
   print('IP reassigned:',name,old,'->',new,flush=True)
   return new
  def resolves(client,expected):
   for _ in range(30):
    actual=execute(client,"import socket; print(socket.gethostbyname('api'))")
    if actual==expected:return
    time.sleep(.2)
   raise AssertionError((actual,expected))
  resolves(coordinator,endpoint(api,networks[0]))
  resolves(worker,endpoint(relay,networks[2]))
  # The consumer remains running while its upstream's address changes.
  resolves(coordinator,move(api,networks[0],'api'))
  resolves(worker,move(relay,networks[2],'api'))
  # Force a cache miss so recovery must execute real inference on the same worker.
  query2=query.replace('screen%20sizes','responsive%20design')
  print('After IP reassignment:',execute(api,query2),flush=True)
  assert execute(api,index)==before
  # Embedded DNS must not restore general external DNS access to inference workers.
  isolated="import socket; socket.getaddrinfo('example.com',443)"
  try:
   execute(worker,isolated)
  except subprocess.CalledProcessError:
   print('PASS: worker external DNS remains blocked',flush=True)
  else:
   raise AssertionError('Worker unexpectedly resolved external DNS')
  for name in (worker,relay,coordinator):docker('stop','-t','3',name)
  execute(api,"from pathlib import Path; Path('/run/worker-credentials/1/credential.json').unlink()")
  docker('restart',api)
  for name in (coordinator,relay,worker):docker('start',name)
  wait(worker,'/tmp/worker-ready')
  for _ in range(40):
   if execute(api,health)=='1':break
   time.sleep(1)
  assert execute(api,health)=='1'
  print('PASS: existing containers reload host TLS and rotated RAM bearer without launcher',flush=True)
  assert execute(api,index)==before
  print('PASS: stored index unchanged',flush=True)
  resolves(coordinator,endpoint(api,networks[0]))
  resolves(worker,endpoint(relay,networks[2]))
  print('After restart:',execute(api,query2.replace('responsive%20design','display%20dimensions')),flush=True)

finally:
 # Remove only resources bearing this fixture's unique ownership marker.
 for kind,names in [('container',containers),('volume',volumes),('network',networks)]:
  for name in reversed(names):
   inspected=subprocess.run([*docker_base,kind,'inspect',name],capture_output=True,text=True,timeout=15)
   if inspected.returncode:continue
   item=json.loads(inspected.stdout)[0]
   labels=item.get('Config',{}).get('Labels',{}) if kind=='container' else item.get('Labels',{})
   if (labels or {}).get('com.argon.recordings.fixture')!=prefix:
    raise RuntimeError('Fixture ownership mismatch; cleanup refused: '+name)
   command=[*docker_base,'rm','-f',name] if kind=='container' else [*docker_base,kind,'rm',name]
   subprocess.run(command,check=True,stdout=subprocess.DEVNULL,timeout=30)
