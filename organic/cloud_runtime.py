"""Cloud execution with a durable write barrier; credentials stay in secrets.

GitHub Actions concurrency is supplemented by a CAS lease in encrypted state.
Before *every* Meta mutation the SQLite intent must be acknowledged remotely.
An unknown HTTP outcome is never retried as a new publication.
"""
import argparse
import base64
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import engine


class DurabilityError(RuntimeError):
    pass


class GitHubState:
    FILES=('config.json','topic-catalog.json','frequency-decision.json','editorial-analysis.json','evidence/native-queue-reconciliation.json',
        'evidence/meta-api-connection.json','evidence/access-state.json','evidence/legacy-monitoraggio.json')

    def __init__(self, repo, token, root, *, run_id=None, branch='organic-state', opener=None, state_key=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):
            raise ValueError('Repository GitHub non valido')
        self.repo,self.token,self.root,self.branch=repo,token,Path(root),branch
        self.run_id=run_id or secrets.token_hex(16)
        self.opener=opener or build_opener(engine.NoRedirect())
        self.sha=None
        self.document=None
        self.path='cloud/state.json'
        if state_key is not None and len(state_key)!=32:
            raise ValueError('Chiave stato AES-256 non valida')
        self.state_key=state_key

    def encode(self, document):
        plain=json.dumps(document,separators=(',',':')).encode()
        if self.state_key is None:
            return plain
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce=os.urandom(12)
        aad=f'bc-organic-state-v2:{self.repo}:{self.branch}:{self.path}'.encode()
        ciphertext=AESGCM(self.state_key).encrypt(nonce,gzip.compress(plain,mtime=0),aad)
        return json.dumps({'schema':2,'encryption':'AES-256-GCM',
            'nonce':base64.b64encode(nonce).decode(),'ciphertext':base64.b64encode(ciphertext).decode()},separators=(',',':')).encode()

    def decode(self, payload):
        envelope=json.loads(payload)
        if envelope.get('schema')==1 and self.state_key is None:
            return envelope
        if envelope.get('schema')!=2 or self.state_key is None:
            raise DurabilityError('Cifratura dello stato inattesa')
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            aad=f'bc-organic-state-v2:{self.repo}:{self.branch}:{self.path}'.encode()
            plain=AESGCM(self.state_key).decrypt(base64.b64decode(envelope['nonce'],validate=True),
                base64.b64decode(envelope['ciphertext'],validate=True),aad)
            return json.loads(gzip.decompress(plain))
        except Exception:
            raise DurabilityError('Stato cifrato non autentico o chiave errata') from None

    def request(self, method, body=None):
        url=f'https://api.github.com/repos/{self.repo}/contents/{self.path}'
        if method=='GET':
            url+='?ref='+self.branch
        headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        req=Request(url,method=method,data=json.dumps(body).encode() if body else None,headers=headers)
        for attempt in range(3 if method=='GET' else 1):
            try:
                with self.opener.open(req,timeout=25) as response:
                    return json.load(response)
            except HTTPError as exc:
                if method=='GET' and exc.code>=500 and attempt<2:
                    time.sleep(2**attempt)
                    continue
                raise DurabilityError('GitHub state HTTP '+str(exc.code)+'; nessuna scrittura Meta autorizzata') from None
            except (URLError,OSError,TimeoutError,json.JSONDecodeError):
                if method=='GET' and attempt<2:
                    time.sleep(2**attempt)
                    continue
                raise DurabilityError('Persistenza remota non confermata; nessuna ripetizione automatica') from None

    def assert_repository(self, public_approved=False):
        req=Request('https://api.github.com/repos/'+self.repo,headers={
            'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json'})
        try:
            with self.opener.open(req,timeout=25) as response:
                info=json.load(response)
        except (HTTPError,URLError,OSError,TimeoutError,json.JSONDecodeError):
            raise DurabilityError('Privacy del repository non verificabile') from None
        if info.get('full_name')!=self.repo:
            raise DurabilityError('Repository diverso dalla destinazione autorizzata')
        if public_approved and info.get('private') is not False:
            raise DurabilityError('Repository non più pubblico: gratuità del runner non confermata')
        if info.get('private') is not True and not (public_approved and self.state_key is not None):
            raise DurabilityError('Runner pubblico richiede consenso esplicito e stato cifrato')

    def load(self):
        result=self.request('GET')
        document=self.decode(base64.b64decode(result['content']))
        if document.get('schema')!=1 or document.get('repo')!=self.repo:
            raise DurabilityError('Stato remoto inatteso')
        database=base64.b64decode(document['database_b64'],validate=True)
        if document.get('database_encoding')=='gzip':
            database=gzip.decompress(database)
        if hashlib.sha256(database).hexdigest()!=document['database_sha256']:
            raise DurabilityError('Hash database remoto non valido')
        self.sha,self.document=result['sha'],document
        return database

    def save_document(self, document):
        payload=self.encode(document)
        if len(payload)>900000:
            raise DurabilityError('Stato remoto vicino al limite: archiviazione richiesta prima di altre scritture')
        encoded=base64.b64encode(payload).decode()
        result=self.request('PUT',{'message':'Organic runtime checkpoint','content':encoded,
            'branch':self.branch,'sha':self.sha})
        new_sha=result.get('content',{}).get('sha')
        if not new_sha:
            raise DurabilityError('Checkpoint remoto senza conferma SHA')
        self.sha,self.document=new_sha,document

    def acquire(self):
        database=self.load()
        lease=self.document.get('lease') or {}
        if lease.get('until',0)>time.time():
            raise DurabilityError('Un altro ciclo possiede la lease remota')
        document=dict(self.document,lease={'owner':self.run_id,'until':time.time()+900},phase='lease_acquired')
        self.save_document(document)
        runtime=self.root/'runtime';runtime.mkdir(exist_ok=True)
        target=runtime/'state.sqlite'
        # Never merge a different local database into the authoritative state.
        for suffix in ('-wal','-shm'):
            side=runtime/('state.sqlite'+suffix)
            if side.exists():side.unlink()
        target.write_bytes(database)
        for name,value in self.document.get('files',{}).items():
            if name not in self.FILES and not re.fullmatch(r'reports/\d{4}-\d{2}-\d{2}\.md',name):
                raise DurabilityError('File di stato non consentito')
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(value,encoding='utf8')

    def database_bytes(self):
        source=sqlite3.connect(self.root/'runtime/state.sqlite',timeout=30)
        target=sqlite3.connect(':memory:')
        try:
            source.backup(target)
            return target.serialize()
        finally:
            source.close();target.close()

    def checkpoint(self, phase='checkpoint', receipt=None, release=False):
        if not self.document or self.document.get('lease',{}).get('owner')!=self.run_id:
            raise DurabilityError('Lease remota non posseduta')
        if self.document['lease']['until']<=time.time():
            raise DurabilityError('Lease remota scaduta')
        database=self.database_bytes()
        receipts=list(self.document.get('receipts',[]))
        if receipt:
            receipts.append(receipt)
        files={}
        for name in self.FILES:
            path=self.root/name
            if path.exists():files[name]=path.read_text(encoding='utf-8-sig')
        for path in (self.root/'reports').glob('????-??-??.md'):
            files[path.relative_to(self.root).as_posix()]=path.read_text(encoding='utf8')
        document=dict(self.document,database_b64=base64.b64encode(gzip.compress(database,mtime=0)).decode(),database_encoding='gzip',
            database_sha256=hashlib.sha256(database).hexdigest(),phase=phase,updated_at=engine.stamp(),
            receipts=receipts[-200:],files=files,lease={} if release else {'owner':self.run_id,'until':time.time()+900})
        self.save_document(document)


class DurableGraph(engine.Graph):
    def __init__(self, version, token, state, *, read_only=False):
        super().__init__(version,token)
        self.state,self.read_only=state,read_only

    def before_write(self, operation):
        if self.read_only:
            raise ValueError('Modalità cloud di sola lettura')
        self.state.checkpoint('before_meta_write',receipt={'operation':operation,'state':'intent','at':engine.stamp()})

    def after_write(self, operation, result):
        self.state.checkpoint('after_meta_write',receipt={'operation':operation,'state':'response',
            'id':result.get('id'),'at':engine.stamp()})
        return result

    def request(self, method, path, params=None):
        if method!='GET':
            self.before_write(method+' '+path)
        result=super().request(method,path,params)
        return self.after_write(method+' '+path,result) if method!='GET' else result

    def upload_photo(self, page_id, path, alt=''):
        operation='POST '+page_id+'/photos'
        self.before_write(operation)
        result=super().upload_photo(page_id,path,alt)
        return self.after_write(operation,result)


def seed_document(repo, database):
    return {'schema':1,'repo':repo,'database_b64':base64.b64encode(gzip.compress(database,mtime=0)).decode(),'database_encoding':'gzip',
        'database_sha256':hashlib.sha256(database).hexdigest(),'lease':{},'receipts':[],
        'phase':'seeded','updated_at':engine.stamp()}


def preflight_registered_photos(graph,cfg,root):
    assets=cfg.get('meta_photo_assets',{})
    if not cfg.get('meta_photo_staging_enabled') or not assets:
        raise ValueError('Nessun media Meta collaudato e registrato')
    verified=[]
    for photo_id,entry in list(assets.items())[:10]:
        if entry.get('visual_checked') is not True or entry.get('page_id')!=cfg['page_id']:
            raise ValueError('QA o proprietario media Meta non verificati')
        engine.verify_media({'platform':'instagram','media':[{
            'type':'IMAGE','meta_photo_id':photo_id,'sha256':entry['sha256']}]},cfg,root,graph=graph)
        verified.append({'photo_id':photo_id,'sha256':entry['sha256']})
    store=engine.Store(root)
    try:
        key='meta-media-preflight:'+engine.now().astimezone(engine.ROME).date().isoformat()+':'+engine.digest(verified)
        if store.observe(key,{'verified':verified,'at':engine.stamp()}):
            store.event('meta_media_preflight_verified',reason=str(len(verified))+' media Meta: proprietario, JPEG e hash verificati in sola lettura')
    finally:
        store.db.close()
    return verified


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=engine.ROOT)
    parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--media-preflight',action='store_true')
    args=parser.parse_args()
    root=args.root.resolve()
    cfg=engine.read_json(root/'config.json')
    # A repository variable is an activation gate, not proof of a free budget.
    if os.environ.get('BC_CLOUD_ENABLED')!='true' or os.environ.get('BC_ZERO_SPEND_VERIFIED')!='true':
        raise DurabilityError('Cloud non attivato o blocco spesa zero non verificato')
    repo=os.environ.get('GITHUB_REPOSITORY','')
    if not cfg.get('cloud_repository') or repo!=cfg['cloud_repository']:
        raise DurabilityError('Repository cloud diverso da quello configurato')
    state_key=base64.b64decode(os.environ['BC_STATE_KEY'],validate=True) if os.environ.get('BC_STATE_KEY') else None
    state=GitHubState(repo,os.environ['GITHUB_TOKEN'],root,state_key=state_key)
    public_approved=os.environ.get('BC_PUBLIC_RUNNER_APPROVED')=='true'
    state.assert_repository(public_approved=public_approved)
    state.acquire()
    try:
        cfg=engine.read_json(root/'config.json')
        if cfg.get('cloud_repository')!=repo:
            raise DurabilityError('Configurazione cifrata destinata a un altro repository')
        graph=DurableGraph(cfg['graph_version'],os.environ['BC_META_PAGE_TOKEN'],state,read_only=args.read_only)
        identity=graph.get(cfg['page_id'],fields='id,name,instagram_business_account')
        if identity.get('id')!=cfg['page_id'] or identity.get('name')!=cfg['page_name'] or identity.get('instagram_business_account',{}).get('id')!=cfg['ig_id']:
            raise ValueError('Identità cloud Meta non corrispondente agli asset BC autorizzati')
        engine.check_credential_dates(cfg.get('cloud_credentials_metadata',{}))
        if args.media_preflight:
            preflight_registered_photos(graph,cfg,root)
        result=engine.run(root,graph=graph,allow_publishing=not args.read_only)
        state.checkpoint('complete',release=True)
        summary=os.environ.get('GITHUB_STEP_SUMMARY')
        report=root/'reports'/(engine.now().astimezone(engine.ROME).date().isoformat()+'.md')
        if summary and report.exists():
            body='BC Organic: '+result+'\n\nReport operativo conservato nello stato cifrato. Nessuna metrica o calendario privato nei log pubblici.' if public_approved else report.read_text(encoding='utf8')
            Path(summary).write_text(body,encoding='utf8')
        return 2 if result.startswith('blocked') or result=='partial' else 0
    except Exception:
        # If final persistence cannot be acknowledged, retain the remote intent
        # and lease. The next run cannot silently retry an ambiguous operation.
        try:state.checkpoint('failed',release=True)
        except DurabilityError:pass
        raise


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Cloud interrotto: '+type(error).__name__+'; controllare stato e log sanificati.',flush=True)
        raise SystemExit(1)
