"""BC TECH Studio organic operations. Python 3.11+, Meta Graph API only.

No ads, engagement automation, inbox access or automatic mutation retries.
Missing data stays None. SQLite holds transactional publication intent.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

UTC = timezone.utc
ROME = ZoneInfo('Europe/Rome')
ROOT = Path(__file__).resolve().parent
STATES = {'draft', 'ready', 'inflight', 'uncertain', 'submitted', 'published', 'scheduled', 'external_scheduled', 'blocked'}


def now():
    return datetime.now(UTC)


def stamp():
    return now().isoformat()


def dt(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp senza fuso')
    return result.astimezone(UTC)


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).exists() else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fingerprint(post):
    # Caption, asset bytes and destination define identity; id/date/version do not.
    text = ' '.join(post['caption'].split()).casefold()
    return digest([post['platform'], text, post.get('asset_hashes', []), post.get('destination')])


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Il vault richiede Windows DPAPI; nessun fallback in chiaro')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data)
    source, target = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    arg = None if decrypt else 'BC Organic Meta credentials'
    if not fn(ctypes.byref(source), arg, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Operazione DPAPI fallita')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


class MetaError(Exception):
    def __init__(self, code='transport', status=None, ambiguous=False):
        self.code, self.status, self.ambiguous = code, status, ambiguous
        super().__init__(f'Meta API code={code} http={status}; risposta sanificata')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Graph:
    def __init__(self, version, token):
        if not re.fullmatch(r'v\d+\.\d+', version):
            raise ValueError('Imposta la versione Graph supportata dalla dashboard Meta')
        self.base = f'https://graph.facebook.com/{version}/'
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, params=None):
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path:
            raise ValueError('Path Graph non valido')
        encoded = urlencode(params or {}).encode()
        url = self.base + path
        if method == 'GET' and encoded:
            url += '?' + encoded.decode()
        req = Request(url, data=encoded if method == 'POST' else None, method=method,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Content-Type': 'application/x-www-form-urlencoded'})
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                with self.opener.open(req, timeout=25) as response:
                    result = json.load(response)
                if 'error' in result:
                    raise MetaError(result['error'].get('code'), ambiguous=method == 'POST')
                return result
            except HTTPError as exc:
                try:
                    error = json.loads(exc.read()).get('error', {})
                except Exception:
                    error = {}
                if method == 'GET' and (exc.code == 429 or exc.code >= 500) and attempt < attempts-1:
                    time.sleep(2 ** attempt)
                    continue
                raise MetaError(error.get('code', 'http'), exc.code,
                                ambiguous=method == 'POST' and exc.code >= 500) from None
            except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                if method == 'GET' and attempt < attempts-1:
                    time.sleep(2 ** attempt)
                    continue
                raise MetaError(ambiguous=method == 'POST') from None

    def get(self, path, **params):
        return self.request('GET', path, params)

    def post(self, path, **params):
        return self.request('POST', path, params)

    def upload_photo(self, page_id, path, alt=''):
        if not re.fullmatch(r'\d+',page_id):
            raise ValueError('ID Pagina non valido')
        path=Path(path)
        boundary='bcorganic'+uuid.uuid4().hex
        chunks=[]
        for name,value in [('published','false'),('alt_text_custom',alt)]:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        mime='image/png' if path.suffix.lower()=='.png' else 'image/jpeg'
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="source"; filename="asset{path.suffix.lower()}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),path.read_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
        request=Request(self.base+page_id+'/photos',data=b''.join(chunks),method='POST',
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'multipart/form-data; boundary='+boundary})
        try:
            with self.opener.open(request,timeout=60) as response:
                result=json.load(response)
            if 'error' in result:
                raise MetaError(result['error'].get('code'),ambiguous=True)
            return result
        except HTTPError as error:
            try: code=json.loads(error.read()).get('error',{}).get('code','http')
            except Exception: code='http'
            raise MetaError(code,error.code,ambiguous=error.code>=500) from None
        except (URLError,TimeoutError,OSError,json.JSONDecodeError):
            raise MetaError(ambiguous=True) from None

    def edge(self, path, **params):
        # Never follow paging.next: it may contain a token. Rebuild from cursor.
        rows = []
        for _ in range(100):
            result = self.get(path, **params)
            rows.extend(result.get('data', []))
            paging = result.get('paging', {})
            after = paging.get('cursors', {}).get('after')
            if not paging.get('next'):
                return rows
            if not after:
                raise MetaError('incomplete-pagination')
            params['after'] = after
        raise MetaError('pagination-limit')


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        (self.root / 'runtime').mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'runtime/state.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                native_id TEXT, container_id TEXT, updated TEXT NOT NULL,
                UNIQUE(platform,fingerprint));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, at TEXT NOT NULL,
                kind TEXT NOT NULL, item TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY, at TEXT NOT NULL,
                data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, until REAL NOT NULL);
        ''')
        # A portable delivery excludes runtime (including secrets). Rebuild job
        # state from the exported calendar if this is a fresh installation.
        if not self.db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone():
            for item in read_json(self.root/'calendar.json', []):
                if self.add(item):
                    with self.db:
                        self.db.execute('UPDATE jobs SET native_id=?,container_id=? WHERE id=?',
                            (item.get('native_id'),item.get('container_id'),item['id']))

    def event(self, kind, item=None, **data):
        self.db.execute('INSERT INTO events(at,kind,item,data) VALUES(?,?,?,?)',
                        (stamp(), kind, item, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def lease(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM leases WHERE until < ?', (time.time(),))
            try:
                self.db.execute('INSERT INTO leases VALUES(?,?)', ('run', time.time()+7200))
            except sqlite3.IntegrityError:
                raise ValueError('Un ciclo è già in esecuzione') from None

    def release(self):
        self.db.execute('DELETE FROM leases WHERE name=?', ('run',))
        self.db.commit()

    def add(self, post):
        if post['platform'] not in ('facebook', 'instagram') or post.get('status', 'draft') not in STATES:
            raise ValueError('Piattaforma/stato non valido')
        dt(post['planned_at'])
        try:
            with self.db:
                self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                    (post['id'], post['platform'], fingerprint(post), post.get('status', 'draft'),
                     json.dumps(post, ensure_ascii=False), None, None, stamp()))
            return True
        except sqlite3.IntegrityError:
            return False

    def rows(self):
        return self.db.execute('SELECT * FROM jobs ORDER BY json_extract(payload,\'$.planned_at\')').fetchall()

    def claim(self, job_id):
        # Persist before any write. Crash or unknown response can never auto-retry.
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status='inflight',updated=? WHERE id=? AND status='ready'",
                                     (stamp(), job_id))
        return cursor.rowcount == 1

    def state(self, job_id, status, native=None, container=None):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?, native_id=COALESCE(?,native_id), '
                            'container_id=COALESCE(?,container_id),updated=? WHERE id=?',
                            (status, native, container, stamp(), job_id))
        self.event(status, job_id, native_id=native, container_id=container)

    def observe(self, key, data):
        with self.db:
            result = self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)',
                                    (key, stamp(), json.dumps(data, ensure_ascii=False)))
        return result.rowcount == 1

    def export(self):
        # DB is append-only evidence; exports are rebuildable views.
        write_json(self.root / 'calendar.json', [dict(json.loads(r['payload']), status=r['status'],
                   native_id=r['native_id'], container_id=r['container_id']) for r in self.rows()])
        write_json(self.root / 'runtime/observations.json',
                   [dict(r, data=json.loads(r['data'])) for r in self.db.execute('SELECT * FROM observations')])
        folder=self.root/'records'
        folder.mkdir(exist_ok=True)
        (folder/'publication-events.jsonl').write_text('\n'.join(json.dumps(dict(r),ensure_ascii=False)
            for r in self.db.execute('SELECT * FROM events ORDER BY seq'))+'\n',encoding='utf8')


def vault_path(root):
    return Path(root) / 'runtime/meta.dpapi'


def credentials(root):
    path = vault_path(root)
    if not path.exists():
        return None
    return json.loads(dpapi(path.read_bytes(), decrypt=True))


def connect(root):
    config = read_json(root / 'config.json')
    version = input('Versione Graph supportata nella tua app (es. vNN.0): ').strip()
    token = getpass.getpass('User access token Meta (nascosto, non salvarlo in chat): ').strip()
    graph = Graph(version, token)
    permissions = graph.edge('me/permissions')
    granted = {p['permission'] for p in permissions if p.get('status') == 'granted'}
    pages = graph.edge('me/accounts', fields='id,name,access_token,instagram_business_account', limit=100)
    for i, page in enumerate(pages):
        print(i, page['name'], page['id'])
    if not pages:
        raise ValueError('Nessuna Pagina accessibile. Controlla ruoli e pages_show_list.')
    page = pages[int(input('Indice della Pagina BC TECH Studio: '))]
    page_graph = Graph(version, page['access_token'])
    details = page_graph.get(page['id'], fields='id,name,instagram_business_account')
    ig_id = details.get('instagram_business_account', {}).get('id')
    if not ig_id:
        raise ValueError('Collega il profilo Instagram professionale alla Pagina e ripeti')
    ig = page_graph.get(ig_id, fields='id,username')
    if ig.get('username') != config['instagram_username'] or page['name'] != config['page_name']:
        raise ValueError('Destinazione diversa da BC TECH Studio / bc.tech.digital')
    vault_path(root).parent.mkdir(parents=True, exist_ok=True)
    vault_path(root).write_bytes(dpapi(json.dumps({'page_token': page['access_token']}).encode()))
    config.update(graph_version=version, page_id=page['id'], ig_id=ig_id,
                  granted_permissions=sorted(granted), connected_at=stamp())
    write_json(root / 'config.json', config)
    print('Token cifrato DPAPI per questo utente Windows. Esegui: python engine.py run')


def attempt(graph, path, **params):
    try:
        return {'status': 'ok', 'data': graph.edge(path, **params), 'captured_at': stamp()}
    except MetaError as error:
        return {'status': 'unavailable', 'data': None, 'error': str(error), 'captured_at': stamp()}


def insight(graph, path, **params):
    # Insight paging links move the requested time window, rather than adding
    # cursor pages. Keep only the explicitly requested window and its metadata.
    try:
        result = graph.get(path, **params)
        return {'status':'ok','data':result.get('data',[]),'query':params,'captured_at':stamp()}
    except MetaError as error:
        return {'status':'unavailable','data':None,'query':params,'error':str(error),'captured_at':stamp()}


def check_credential_dates(secret):
    for field in ('expires_at','data_access_expires_at'):
        expiry=secret.get(field)
        if expiry and expiry <= now().timestamp():
            raise ValueError('Credenziale Meta scaduta: '+field)


def reconcile_queues(snapshot, cfg, root):
    proof=read_json(Path(root)/'evidence/native-queue-reconciliation.json',{})
    identity_valid=proof.get('page_id')==cfg.get('page_id') and proof.get('ig_id')==cfg.get('ig_id')
    def verified(browser):
        captured=browser.get('verified_at',proof.get('verified_at'))
        return bool(identity_valid and captured and timedelta(0)<=now()-dt(captured)<=timedelta(hours=24))
    if cfg.get('facebook_scheduled_api_incomplete'):
        entry=snapshot['facebook_scheduled']
        api_data=entry.get('api_data',entry.get('data'))
        entry.update(api_status=entry.get('api_status',entry['status']),api_data=api_data,
                     reason='API non completa: Business Suite mostra contenuti assenti dalla risposta API')
        browser=proof.get('platforms',{}).get('facebook',{})
        if verified(browser) and browser.get('complete') is True:
            combined=list(api_data or [])
            for item in browser.get('items',[]):
                if not item.get('id') or not any(p.get('id')==item['id'] for p in combined):
                    combined.append(dict(item,source='official_ui'))
            entry.update(status='ok',data=combined,reconciled_at=browser.get('verified_at',proof.get('verified_at')),
                         source='Meta API + verifica ufficiale Business Suite')
        else:
            entry.update(status='unavailable',data=None,source='API parziale; riconciliazione UI mancante/scaduta')
    browser=proof.get('platforms',{}).get('instagram',{})
    coverage_valid=bool(browser.get('coverage_start') and browser.get('coverage_end') and
        dt(browser['coverage_start'])<=now()<dt(browser['coverage_end']))
    if verified(browser) and coverage_valid and browser.get('complete') is True and browser.get('source')=='official_instagram_ui':
        snapshot['instagram_native_schedule']={'status':'ok','data':browser.get('items',[]),
            'source':'Coda diretta Instagram verificata; Business Suite non completa',
            'reconciled_at':browser.get('verified_at',proof.get('verified_at')),'coverage_start':browser.get('coverage_start'),
            'coverage_end':browser.get('coverage_end'),'api_enumeration':False}
    elif 'instagram_native_schedule' in snapshot:
        snapshot['instagram_native_schedule']={'status':'unavailable','data':None,
            'reason':'Coda nativa Instagram non enumerabile da questo adattatore; prova UI mancante/scaduta'}
    return snapshot


def collect(graph, cfg):
    since = int((now() - timedelta(days=7)).timestamp())
    page, ig = cfg['page_id'], cfg['ig_id']
    data = {
        'facebook_published': attempt(graph, page+'/published_posts', fields='id,message,created_time,permalink_url', since=since, limit=100),
        'facebook_scheduled': attempt(graph, page+'/scheduled_posts', fields='id,message,scheduled_publish_time', limit=100),
        'instagram_published': attempt(graph, ig+'/media', fields='id,caption,timestamp,permalink,media_type,media_product_type', limit=100),
        'instagram_native_schedule': {'status': 'unavailable', 'data': None,
            'reason': 'Nessuna enumerazione API della coda Business Suite implementata; la coda locale non prova quella nativa.'},
        'account_insights': {}, 'post_insights': [], 'comments': [], 'competitors': {},
        'window': {'since': since, 'until': int(now().timestamp()), 'period': 'day'},
    }
    # Metrics are configurable per API version. Unsupported is null, never 0.
    for platform, account in [('facebook', page), ('instagram', ig)]:
        data['account_insights'][platform] = {}
        for metric in cfg.get('account_metrics', {}).get(platform, []):
            options=cfg.get('account_metric_options',{}).get(platform,{}).get(metric,{})
            data['account_insights'][platform][metric] = insight(graph, account+'/insights',
                metric=metric, period='day', since=since, until=data['window']['until'], **options)
        for post in (data[platform+'_published'].get('data') or []):
            published = post.get('timestamp', post.get('created_time'))
            if published and dt(published) < now()-timedelta(days=7):
                continue
            for metric in cfg.get('post_metrics', {}).get(platform, []):
                value = insight(graph, post['id']+'/insights', metric=metric)
                data['post_insights'].append({'platform': platform, 'post_id': post['id'],
                    'published_at': published, 'metric': metric, **value})
            # Counts only: no author names, IDs or text stored.
            try:
                summary = graph.get(post['id']+'/comments', fields='id', limit=1, summary='true').get('summary', {})
                data['comments'].append({'platform': platform, 'post_id': post['id'],
                                        'total_count': summary.get('total_count')})
            except MetaError:
                data['comments'].append({'platform': platform, 'post_id': post['id'], 'total_count': None})
    for username in cfg.get('competitor_instagram_usernames', [])[:5]:
        if not re.fullmatch(r'[A-Za-z0-9_.]+', username):
            continue
        try:
            item = graph.get(ig, fields='business_discovery.username('+username+')'
                '{username,media.limit(5){caption,media_type,permalink,timestamp,like_count,comments_count}}')
            data['competitors'][username] = {'status': 'ok', 'data': item, 'captured_at': stamp(),
                'limit': 'Solo dati pubblici autorizzati; nessuna reach o conversione concorrenti'}
        except MetaError as error:
            data['competitors'][username] = {'status': 'unavailable', 'data': None, 'error': str(error)}
    return data


def checkpoint(published, captured, hours):
    age = (dt(captured)-dt(published)).total_seconds()/3600
    if hours not in (24,72,168) or age < hours:
        raise ValueError('Checkpoint non maturato')
    return {'age_hours': age, 'delay_minutes': (age-hours)*60, 'target_hours': hours}


def capture_checkpoints(store, snapshot):
    legacy = read_json(store.root/'evidence/legacy-monitoraggio.json', {'posts':[]})
    for platform in ('facebook','instagram'):
        for post in snapshot[platform+'_published'].get('data') or []:
            pub = post.get('timestamp', post.get('created_time'))
            if not pub:
                continue
            metrics = [x for x in snapshot['post_insights'] if x['platform']==platform and x['post_id']==post['id']]
            if not metrics:
                continue
            # Use actual observation timestamp, never a synthetic due time.
            captured = max((x['captured_at'] for x in metrics), default=snapshot[platform+'_published']['captured_at'])
            incoming_url = post.get('permalink',post.get('permalink_url',''))
            for old in legacy['posts']:
                old_url = old.get('url') or ''
                same_url = old_url and incoming_url and (old_url.rstrip('/') == incoming_url.rstrip('/') or
                    (platform=='instagram' and re.search(r'/p/([^/]+)',old_url) and
                     re.search(r'/p/([^/]+)',incoming_url) and
                     re.search(r'/p/([^/]+)',old_url).group(1)==re.search(r'/p/([^/]+)',incoming_url).group(1)))
                if same_url:
                    for old_cp in old.get('checkpoints',[]):
                        if old_cp.get('capturedAt'):
                            store.observe(f'checkpoint:{platform}:{post["id"]}:{old_cp["targetHours"]}',
                                {'origin':'legacy','source':str(store.root/'evidence/legacy-monitoraggio.json'),**old_cp})
            for hours in (24,72,168):
                try:
                    timing = checkpoint(pub, captured, hours)
                except ValueError:
                    continue
                store.observe(f'checkpoint:{platform}:{post["id"]}:{hours}',
                    {'platform': platform, 'post_id': post['id'], 'published_at': pub,
                     'captured_at': captured, 'metrics_original': metrics, **timing})


def refresh_due_checkpoints(store, graph, cfg, snapshot):
    # A cached morning snapshot cannot measure a threshold crossed later that
    # day. Read only the due post's metrics, retaining the original daily data.
    current={'post_insights':[]}
    for platform in ('facebook','instagram'):
        current[platform+'_published']={'data':[],'captured_at':stamp()}
        metrics=cfg.get('post_metrics',{}).get(platform,[])
        if not metrics:
            continue
        for post in snapshot[platform+'_published'].get('data') or []:
            pub=post.get('timestamp',post.get('created_time'))
            if not pub or not timedelta(0)<=now()-dt(pub)<=timedelta(days=8):
                continue
            due=[h for h in (24,72,168) if now()-dt(pub)>=timedelta(hours=h) and not store.db.execute(
                'SELECT 1 FROM observations WHERE key=?',(f'checkpoint:{platform}:{post["id"]}:{h}',)).fetchone()]
            if not due:
                continue
            current[platform+'_published']['data'].append(post)
            for metric in metrics:
                current['post_insights'].append({'platform':platform,'post_id':post['id'],'published_at':pub,
                    'metric':metric,**insight(graph,post['id']+'/insights',metric=metric)})
    capture_checkpoints(store,current)


def inventory_duplicate(post, snapshot):
    text = ' '.join(post['caption'].split()).casefold()
    names = ('facebook_published','facebook_scheduled') if post['platform']=='facebook' else ('instagram_published','instagram_native_schedule')
    for name in names:
        for item in snapshot.get(name,{}).get('data') or []:
            existing = ' '.join(item.get('caption',item.get('message','')).split()).casefold()
            if existing and (existing == text or (len(text)>100 and existing.startswith(text[:160]))):
                return True
    return False


def inventory_ready(post, snapshot):
    names=('facebook_published','facebook_scheduled') if post['platform']=='facebook' else ('instagram_published','instagram_native_schedule')
    if not all(snapshot.get(name,{}).get('status')=='ok' for name in names):
        return False
    if post['platform']=='instagram':
        queue=snapshot['instagram_native_schedule']
        if not queue.get('coverage_start') or not queue.get('coverage_end'):
            return False
        return dt(queue['coverage_start'])<=dt(post['planned_at'])<dt(queue['coverage_end'])
    return True


def ready(post, cfg):
    required = {'pages_manage_posts','pages_read_engagement'} if post['platform']=='facebook' else {'instagram_basic','instagram_content_publish'}
    if not required <= set(cfg.get('granted_permissions', [])):
        raise ValueError('Permessi di pubblicazione non verificati')
    if not post.get('qa', {}).get('visual_checked') or not post.get('evidence'):
        raise ValueError('QA visiva e prova fattuale richieste')
    if post.get('native_queue_unknown', True):
        raise ValueError('Coda nativa non riconciliata: pubblicazione sospesa')
    if not post.get('queue_checked_at') or now()-dt(post['queue_checked_at']) > timedelta(hours=24):
        raise ValueError('La verifica della coda deve avere meno di 24 ore')
    if not post.get('approval_hash') == fingerprint(post):
        raise ValueError('Versione del contenuto diversa da quella verificata')
    url = urlparse(post['destination'])
    if url.scheme != 'https' or url.hostname not in ('www.bctechstudio.com', 'bctechstudio.com'):
        raise ValueError('Destinazione BC HTTPS richiesta')
    if post['platform']=='facebook' and post.get('media'):
        if len(post['media'])>10 or post['destination'] not in post['caption']:
            raise ValueError('Album Facebook: massimo 10 immagini e link BC esplicito nel copy')
        for asset in post['media']:
            path=Path(asset.get('local_path',''))
            if asset.get('type')!='IMAGE' or path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]!='assets':
                raise ValueError('Album Facebook: solo immagini locali approvate nella cartella assets')
            if path.suffix.lower() not in ('.png','.jpg','.jpeg') or asset.get('sha256') not in post.get('asset_hashes',[]):
                raise ValueError('Immagine Facebook o hash non valido')
    if post['platform'] == 'instagram':
        media = post.get('media', [])
        if not media or len(media) > 10:
            raise ValueError('Servono 1–10 media per il formato implementato')
        for asset in media:
            parsed = urlparse(asset['url'])
            if parsed.scheme != 'https' or parsed.hostname not in cfg.get('media_hosts', []):
                raise ValueError('Media HTTPS su host autorizzato richiesto')
            if asset.get('type') not in ('IMAGE', 'REELS', 'STORIES'):
                raise ValueError('Formato non implementato')
            if asset.get('type')=='STORIES':
                raise ValueError('Stories mantenute in bozza fino al collaudo del relativo adattatore')
            if not asset.get('sha256') or asset['sha256'] not in post.get('asset_hashes', []):
                raise ValueError('Hash media non associato al contenuto')
            if asset['type'] == 'IMAGE' and not parsed.path.lower().endswith(('.jpg', '.jpeg')):
                raise ValueError('Le immagini Instagram devono essere JPEG')


def verify_media(post, cfg, root=ROOT):
    for asset in post.get('media', []):
        if post['platform']=='facebook' and asset.get('local_path'):
            path=(Path(root)/asset['local_path']).resolve()
            if not path.is_relative_to((Path(root)/'assets').resolve()):
                raise ValueError('Media al di fuori della cartella assets')
            data=path.read_bytes()
            if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=asset['sha256']:
                raise ValueError('Immagine Facebook modificata o troppo grande')
            continue
        # Hash the bytes Meta will fetch, not just a local filename.
        req = Request(asset['url'], headers={'User-Agent':'BCOrganic/1.0'})
        try:
            with build_opener(NoRedirect()).open(req, timeout=25) as response:
                data = response.read(20*1024*1024+1)
        except (HTTPError, URLError, OSError):
            raise ValueError('Media pubblico non recuperabile senza login') from None
        if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=asset['sha256']:
            raise ValueError('Hash media pubblico diverso dalla versione verificata, o dimensione non supportata')


def stage(store, cfg, post):
    ready(post, cfg)
    verify_media(post, cfg, store.root)
    post = dict(post, status='ready')
    existing = store.db.execute('SELECT * FROM jobs WHERE id=?',(post['id'],)).fetchone()
    if existing:
        if existing['status']!='draft':
            raise ValueError('Solo bozze modificabili; conserva gli stati di pubblicazione')
        store.event('draft_version_archived',post['id'],previous=json.loads(existing['payload']))
        with store.db:
            store.db.execute('UPDATE jobs SET payload=?,fingerprint=?,status=?,updated=? WHERE id=?',
                (json.dumps(post,ensure_ascii=False),fingerprint(post),'ready',stamp(),post['id']))
    elif not store.add(post):
        raise ValueError('Contenuto già presente: duplicazione bloccata')
    store.event('staged', post['id'], content_hash=fingerprint(post))
    store.export()


def container_finished(graph, container):
    for _ in range(6):
        state = graph.get(container, fields='status_code').get('status_code')
        if state == 'FINISHED':
            return True
        if state in ('ERROR', 'EXPIRED'):
            raise MetaError('container-' + state)
        time.sleep(5)
    return False


def publish_one(store, graph, cfg, row):
    post = json.loads(row['payload'])
    ready(post, cfg)
    verify_media(post, cfg, store.root)
    if not store.claim(post['id']):
        return
    try:
        if post['platform'] == 'facebook':
            params = {'message': post['caption']}
            if post.get('media'):
                attached=[]
                for asset in post['media']:
                    photo=graph.upload_photo(cfg['page_id'],store.root/asset['local_path'],asset.get('alt',post.get('alt','')))['id']
                    attached.append({'media_fbid':photo})
                    store.event('facebook_photo_uploaded',post['id'],photo_id=photo,sha256=asset['sha256'],published=False)
                params['attached_media']=json.dumps(attached)
            else:
                params['link']=post['destination']
            future = (dt(post['planned_at'])-now()).total_seconds()
            scheduled = future >= 1200
            if scheduled:
                params.update(published='false', scheduled_publish_time=int(dt(post['planned_at']).timestamp()))
            result = graph.post(cfg['page_id']+'/feed', **params)
            native = result['id']
            if scheduled:
                proof = graph.get(native, fields='id,scheduled_publish_time,is_published')
                if proof.get('is_published') is not False or abs(int(proof.get('scheduled_publish_time',0))-int(dt(post['planned_at']).timestamp()))>60:
                    raise MetaError('schedule-verification-failed')
                store.state(post['id'], 'scheduled', native=native)
                store.event('native_schedule_verified', post['id'], proof=proof)
                return
        else:
            media = post['media']
            children = []
            for asset in media:
                params = {'alt_text': asset.get('alt',post.get('alt', ''))} if asset['type'] == 'IMAGE' else {}
                params['image_url' if asset['type'] == 'IMAGE' else 'video_url'] = asset['url']
                if asset['type'] != 'IMAGE':
                    params['media_type'] = asset['type']
                if len(media) > 1:
                    if asset['type'] != 'IMAGE':
                        raise ValueError('Caroselli implementati solo con immagini')
                    params['is_carousel_item'] = 'true'
                else:
                    params['caption'] = post['caption']
                child = graph.post(cfg['ig_id']+'/media', **params)['id']
                store.event('container_created', post['id'], container_id=child)
                children.append(child)
            if len(children) > 1:
                container = graph.post(cfg['ig_id']+'/media', media_type='CAROUSEL',
                    children=','.join(children), caption=post['caption'])['id']
            else:
                container = children[0]
            store.state(post['id'], 'inflight', container=container)
            if not container_finished(graph, container):
                store.state(post['id'], 'blocked', container=container)
                return
            native = graph.post(cfg['ig_id']+'/media_publish', creation_id=container)['id']
        store.state(post['id'], 'submitted', native=native)
        fields = 'id,permalink,timestamp' if post['platform']=='instagram' else 'id,permalink_url,created_time'
        verified = graph.get(native, fields=fields)
        if not (verified.get('permalink') or verified.get('permalink_url')):
            raise MetaError('permalink-unavailable')
        store.state(post['id'], 'published', native=native)
        store.event('publication_verified', post['id'], proof=verified)
    except (MetaError, ValueError, KeyError) as error:
        # Includes rejected writes: deliberate manual review, never blind retries.
        store.state(post['id'], 'uncertain' if isinstance(error, (MetaError, KeyError)) else 'blocked')
        store.event('publication_needs_review', post['id'], error=str(error) if not isinstance(error, KeyError) else 'Missing response id')


def reconcile_scheduled(store, graph):
    for row in store.rows():
        if row['status']!='scheduled' or row['platform']!='facebook' or not row['native_id']:
            continue
        post=json.loads(row['payload'])
        if dt(post['planned_at'])>now():
            continue
        try:
            proof=graph.get(row['native_id'],fields='id,is_published,permalink_url,created_time')
            if proof.get('is_published') is True and proof.get('permalink_url'):
                store.state(row['id'],'published',native=row['native_id'])
                store.event('publication_verified',row['id'],proof=proof)
        except MetaError as error:
            key='alert:schedule-verification:'+row['native_id']
            if store.observe(key,{'at':stamp(),'error':str(error)}):
                store.event('schedule_verification_unavailable',row['id'],error=str(error))


def reconcile_external_scheduled(store, graph, cfg):
    due={platform:[] for platform in ('facebook','instagram')}
    for row in store.rows():
        if row['status']=='external_scheduled' and dt(json.loads(row['payload'])['planned_at'])<=now():
            due[row['platform']].append(row)
    for platform, rows in due.items():
        if not rows:
            continue
        identity=cfg['ig_id'] if platform=='instagram' else cfg['page_id']
        fields='id,caption,timestamp,permalink' if platform=='instagram' else 'id,message,created_time,permalink_url'
        inventory=attempt(graph,identity+('/media' if platform=='instagram' else '/published_posts'),fields=fields,limit=100)
        if inventory['status']!='ok':
            store.event('external_schedule_verification_unavailable',reason=inventory.get('error'))
            continue
        for row in rows:
            post=json.loads(row['payload']);planned=dt(post['planned_at'])
            caption=' '.join(post['caption'].split()).casefold()
            matches=[p for p in inventory['data'] if ' '.join(p.get('caption',p.get('message','')).split()).casefold()==caption
                and p.get('timestamp',p.get('created_time')) and
                timedelta(minutes=-2)<=dt(p.get('timestamp',p.get('created_time')))-planned<=timedelta(hours=24)
                and (p.get('permalink') or p.get('permalink_url'))]
            if len(matches)==1:
                store.state(row['id'],'published',native=matches[0]['id'])
                store.event('publication_verified',row['id'],proof=matches[0],origin='external_native_schedule')
            elif len(matches)>1:
                store.event('external_schedule_ambiguous',row['id'],matches=[p['id'] for p in matches])


def funnel(events):
    # Count unique opportunities, and unique payment events, across all channels.
    if not events:
        return {k: None for k in ('messages', 'qualified', 'quote_requests', 'quotes_sent', 'clients', 'paid_eur')}
    stages = ('message', 'qualified', 'quote_request', 'quote_sent', 'client')
    result = {k: len({e['opportunity_id'] for e in events if e.get('verified') is True and e['stage']==s}) or None
              for k, s in zip(('messages','qualified','quote_requests','quotes_sent','clients'), stages)}
    payments = {e['payment_id']: e['amount_eur'] for e in events
                if e.get('verified') is True and e['stage']=='payment' and e.get('payment_id')}
    result['paid_eur'] = sum(payments.values()) if payments else None
    return result


def lead_add(store, event):
    if not re.fullmatch(r'BC-[A-Z0-9-]{4,32}', event.get('opportunity_id', '')):
        raise ValueError('ID anonimo BC-... richiesto')
    if event.get('verified') is not True or not event.get('evidence_ref'):
        raise ValueError('Evidenza verificata richiesta; nessun testo delle chat')
    if event['stage'] not in ('click', 'message', 'qualified', 'quote_request', 'quote_sent', 'client', 'invoice', 'payment', 'lost'):
        raise ValueError('Fase non valida')
    if event['stage'] == 'qualified' and not all(event.get(k) is True for k in ('relevant_business','concrete_need','next_step')):
        raise ValueError('Qualifica incompleta')
    if event['stage'] in ('payment','invoice') and (not isinstance(event.get('amount_eur'), (int,float)) or event['amount_eur']<=0):
        raise ValueError('Importo reale positivo richiesto')
    if event['stage']=='payment' and not event.get('payment_id'):
        raise ValueError('ID univoco incasso richiesto')
    dt(event['occurred_at'])
    key = 'lead:' + digest([event['opportunity_id'], event['stage'], event.get('payment_id', event.get('event_id'))])
    store.observe(key, event)


def report(store, cfg, snapshot, status):
    date = now().astimezone(ROME).date().isoformat()
    rows = store.rows()
    counts = {s: sum(r['status']==s for r in rows) for s in sorted(STATES)}
    leads = [json.loads(r['data']) for r in store.db.execute("SELECT data FROM observations WHERE key LIKE 'lead:%'")]
    summary = funnel(leads)
    records = [dict(r, data=json.loads(r['data'])) for r in store.db.execute("SELECT * FROM observations WHERE key LIKE 'daily:%' ORDER BY at DESC LIMIT 1")]
    lines = [f'# BC TECH Studio — {date}', '', f'Generato: {stamp()} (UTC); fuso operativo Europe/Rome.', '',
        f'Stato ciclo: **{status}**. Budget pubblicitario: **0 €**.', '',
        '## Pubblicazioni e calendario', '',
        'Stati cumulativi del registro, incluse le pubblicazioni storiche importate: '+', '.join(f'{k}={v}' for k,v in counts.items())+'.',
        '“ready” è coda locale; “scheduled” richiede ID nativo; “external_scheduled” è una programmazione verificata nella UI ufficiale con ID non esposto. Il calendario proposto non è una programmazione Meta.', '']
    today_events = store.db.execute('SELECT * FROM events WHERE at>=? ORDER BY seq',
                                  (datetime.combine(now().astimezone(ROME).date(), datetime.min.time(), ROME).astimezone(UTC).isoformat(),)).fetchall()
    published_today = [r for r in today_events if r['kind']=='publication_verified']
    scheduled_today = [r for r in today_events if r['kind']=='native_schedule_verified']
    lines += [f'Nuove pubblicazioni API verificate da questo motore oggi: {len(published_today)}. '
              f'Nuove programmazioni native API verificate: {len(scheduled_today)}. '
              'Questi conteggi non descrivono eventuali uscite da altre code.', '']
    for event in today_events:
        if event['kind']=='draft_version_archived':
            continue
        detail = json.loads(event['data'])
        readable = detail.get('reason') or detail.get('error') or detail.get('proof',{}).get('permalink') or detail.get('proof',{}).get('permalink_url') or ''
        lines.append(f'- {event["at"]}: {event["kind"]} / {event["item"] or "sistema"} — {readable}')
    lines += ['', '## Dati ufficiali disponibili', '']
    if snapshot:
        for name in ('facebook_published', 'facebook_scheduled','instagram_published','instagram_native_schedule'):
            entry = snapshot[name]
            count = len(entry['data']) if entry.get('data') is not None else 'non rilevato'
            lines.append(f'- {name}: {entry["status"]}; elementi={count}.')
            if entry.get('source'):
                lines.append('  Fonte: '+entry['source']+'. '+entry.get('reason','')+'. Riconciliazione: '+entry.get('reconciled_at','non disponibile')+'.')
                if 'api_data' in entry:
                    lines.append(f'  Elementi restituiti dalla sola API: {len(entry["api_data"] or [])}; non equivalgono alla coda completa.')
        lines += ['', 'Metriche con definizioni originali e finestre in runtime/observations.json; non sommare reach dei canali.']
        unavailable = sum(x.get('status') != 'ok' for x in snapshot.get('post_insights', []))
        lines.append(f'Metriche post non accessibili/non supportate: {unavailable}.')
        for platform, metrics in snapshot.get('account_insights',{}).items():
            for metric, entry in metrics.items():
                if entry['status']!='ok':
                    lines.append(f'- Account {platform}, {metric}: non disponibile ({entry.get("error","non rilevato")}).')
                    continue
                for item in entry.get('data') or []:
                    value=item.get('total_value')
                    if value is not None:
                        lines.append(f'- Account {platform}, {metric}: {value.get("value")}; finestra richiesta {snapshot["window"]}; rilevato {entry["captured_at"]}.')
                    elif item.get('values'):
                        last=item['values'][-1]
                        lines.append(f'- Account {platform}, {metric}: ultimo valore giornaliero {last.get("value")}, fine intervallo {last.get("end_time")}; non sommare utenti unici fra giorni.')
        lines += ['', '| Contenuto | Pubblicato | Metrica cumulativa | Valore | Rilevato UTC |',
                  '|---|---|---|---:|---|']
        for entry in snapshot.get('post_insights',[]):
            value=None
            if entry['status']=='ok' and entry.get('data'):
                metric=entry['data'][0]
                value=metric.get('total_value',{}).get('value')
                if value is None and metric.get('values'):
                    value=metric['values'][-1].get('value')
            source=next((p for p in snapshot[entry['platform']+'_published'].get('data') or [] if p['id']==entry['post_id']),{})
            url=source.get('permalink',source.get('permalink_url'))
            label=f'[{entry["post_id"]}]({url})' if url else entry['post_id']
            readable = ('nessuna voce restituita; totale non rilevato' if value == {} else
                json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else
                value if value is not None else 'non rilevato')
            lines.append(f'| {label} | {entry.get("published_at")} | {entry["metric"]} | {readable} | {entry["captured_at"]} |')
    else:
        lines.append('Nessuna lettura Meta aggiornata: credenziali/app non configurate o ciclo fallito. Statistiche e coda nativa = non rilevate.')
        if records:
            lines.append('Ultimo snapshot conservato: '+records[0]['at']+'; non presentato come dato di oggi.')
    access = read_json(store.root/'evidence/access-state.json')
    if access:
        lines += ['', 'Accesso browser verificato: Facebook BC TECH Studio e Instagram @bc.tech.digital alle '
                  +access['verified_at']+'. Stato API verificato separatamente in evidence/meta-api-connection.json.']
        if access.get('facebook_instagram_linked'):
            lines.append('Collegamento Pagina Facebook → @bc.tech.digital completato e verificato nell’interfaccia ufficiale. Accesso ai messaggi disattivato.')
        if access.get('meta_developer_registered'):
            lines.append('Registrazione Meta for Developers completata. Stato app: '+access.get('app_status','non verificato')+'.')
    lines += ['', 'Facebook insights: '+('metriche configurate; verificare esiti API.' if cfg.get('post_metrics',{}).get('facebook') else
        'nessuna metrica selezionata finché non è verificato il contratto della versione Graph. Nessun valore stimato.'),
        '', '## Risultati commerciali documentati', '',
        json.dumps(summary, ensure_ascii=False), '',
        'Registro parziale di evidenze autorizzate, non totale dei contatti. Un clic WhatsApp non prova messaggio, lead o vendita.', '',
        '## Miglioramento e prossimo esperimento', '',
        'CTA con attività + comune + ostacolo concreto, quindi Growth Engine o richiesta di preventivo. IG: percorso dal link in bio; FB: link diretto con UTM.',
        'Finché mancano conversioni attribuite, mantenere il test senza dichiarare un formato vincente. Confrontare 24/72/168 ore e la stessa definizione.',
        'Priorità: riconciliare coda esistente, collaudare la pubblicazione e collegare evidenze commerciali anonime. Nessun nuovo strumento a pagamento necessario.', '',
        '## Limiti', '',
        'Instagram: coda Business Suite non enumerata da questo adattatore. Blocca nuove uscite se la coda non è stata verificata. Nessuna lettura inbox, DM o account personali.',
        'Competitor: solo contenuti pubblici o Business Discovery autorizzato; insight e vendite dei concorrenti restano non accessibili.',
        'Il primo collegamento deve collaudare permessi, versione Graph, media pubblici e una pubblicazione reale verificata. I test simulati non sostituiscono il collaudo live.',
        'Esecuzione locale: PC acceso e Codex aperto alle scadenze. Nessun hosting continuo è stato attivato.', '']
    frequency=read_json(store.root/'frequency-decision.json')
    if frequency:
        lines += ['## Frequenza editoriale', '', frequency['decision'],
                  'Valutazione: '+frequency['evaluated_at']+'. '+frequency['reason'],
                  'Prossima revisione: '+frequency['next_review']+'. Vedi valutazione-frequenza.md.', '']
    connection=read_json(store.root/'evidence/meta-api-connection.json',{})
    if connection.get('facebook_schedule_test'):
        test=connection['facebook_schedule_test']
        lines += ['## Collaudo di programmazione API', '',
                  'Album Xenìa Facebook, tre immagini: ID '+test['native_id']+', programmato '+test['planned_at']+
                  '; orario e is_published=false verificati via API e presenza confermata in Business Suite. È una programmazione, non una pubblicazione già avvenuta.',
                  'Instagram: letture e insight verificati; la riconciliazione UI ha scadenza e copertura esplicite. I nuovi media richiedono URL HTTPS pubblici e un collaudo di pubblicazione prima dell’abilitazione.', '']
    folder = store.root / 'reports'
    folder.mkdir(exist_ok=True)
    revision = stamp().replace(':','-')
    output = folder / f'{date}-{revision}.md'
    output.write_text('\n'.join(lines), encoding='utf-8')
    (folder / f'{date}.md').write_text('\n'.join(lines), encoding='utf-8')
    return output


def run(root=ROOT, graph=None, *, allow_publishing=True):
    root = Path(root)
    store, cfg = Store(root), read_json(root / 'config.json')
    store.lease()
    snapshot, status = None, 'blocked_missing_meta'
    try:
        from planner import refresh
        refresh(store, cfg)
        secret = credentials(root) if graph is None else None
        if graph is None and secret and cfg.get('graph_version'):
            check_credential_dates(secret)
            graph = Graph(cfg['graph_version'], secret['page_token'])
            # Validate access even when the daily content snapshot is cached.
            identity=graph.get(cfg['page_id'],fields='id,name,instagram_business_account')
            if identity.get('id')!=cfg['page_id'] or identity.get('name')!=cfg['page_name'] or identity.get('instagram_business_account',{}).get('id')!=cfg['ig_id']:
                raise ValueError('Identità Meta non corrispondente alla configurazione verificata')
        day = now().astimezone(ROME).date().isoformat()
        if graph:
            reconcile_scheduled(store,graph)
            reconcile_external_scheduled(store,graph,cfg)
            # Keep previous snapshots immutable when the metric contract changes.
            contract = {name: cfg.get(name) for name in (
                'graph_version','page_id','ig_id','account_metrics',
                'account_metric_options','post_metrics','competitor_instagram_usernames','runtime_backend')}
            key = 'daily:' + day + ':' + digest(contract)[:16]
            previous = store.db.execute('SELECT data FROM observations WHERE key=?', (key,)).fetchone()
            if previous:
                snapshot = json.loads(previous['data'])
            else:
                snapshot = collect(graph, cfg)
                # Failed reads do not mark the whole day successful, so a later run may recover.
                if all(snapshot[p]['status']=='ok' for p in ('facebook_published','instagram_published')):
                    store.observe(key, snapshot)
            snapshot=reconcile_queues(snapshot,cfg,root)
            capture_checkpoints(store, snapshot)
            if previous:
                refresh_due_checkpoints(store,graph,cfg,snapshot)
            status = 'partial' if any(snapshot[p]['status']!='ok' for p in ('facebook_published','facebook_scheduled','instagram_published')) else 'read_complete_native_ig_unknown'
            if status!='partial' and snapshot['instagram_native_schedule']['status']=='ok':
                status='read_complete_reconciled_ui'
            pending=[]
            for candidate in store.rows():
                if candidate['status']!='ready' or candidate['platform'] not in cfg.get('publishing_platforms',['facebook','instagram']):
                    continue
                age=(now()-dt(json.loads(candidate['payload'])['planned_at'])).total_seconds()
                if 0<=age<=2700 or (candidate['platform']=='facebook' and -7*86400<=age<=-1200):
                    pending.append(candidate)
            if allow_publishing and cfg.get('publishing_enabled') and pending:
                identity = graph.get(cfg['page_id'], fields='id,name,instagram_business_account')
                if identity.get('id')!=cfg['page_id'] or identity.get('name')!=cfg['page_name'] or identity.get('instagram_business_account',{}).get('id')!=cfg['ig_id']:
                    raise ValueError('Identità Meta non corrispondente alla configurazione verificata')
                # Fresh reconciliation immediately before any write, independent of daily cache.
                fresh = reconcile_queues(collect(graph, cfg),cfg,root)
                if not any(inventory_ready(json.loads(row['payload']),fresh) for row in pending):
                    store.event('publishing_blocked', reason='Fresh published/scheduled inventory unavailable')
                else:
                    for row in pending:
                        post = json.loads(row['payload'])
                        if not inventory_ready(post,fresh):
                            store.event('publishing_blocked',row['id'],reason='Inventario del canale non completo o data fuori dalla copertura verificata')
                            continue
                        if post['platform'] not in cfg.get('publishing_platforms',['facebook','instagram']):
                            continue
                        age = (now()-dt(post['planned_at'])).total_seconds()
                        due = 0 <= age <= 2700
                        fb_schedule_window = post['platform']=='facebook' and -7*86400 <= age <= -1200
                        if row['status']=='ready' and (due or fb_schedule_window):
                            if inventory_duplicate(post, fresh):
                                store.state(post['id'], 'blocked')
                                store.event('duplicate_prevented', post['id'])
                                continue
                            try:
                                publish_one(store, graph, cfg, row)
                            except ValueError as error:
                                store.event('preflight_blocked', row['id'], reason=str(error))
        else:
            if store.observe('alert:missing_meta', {'reason':'App/token Meta non configurati','at':stamp()}):
                store.event('access_blocked', reason='App/token Meta non configurati; nessun dato inventato')
        store.export()
        result = report(store, cfg, snapshot, status)
        print(json.dumps({'status': status, 'report': str(result)}, ensure_ascii=True))
        return status
    except Exception as error:
        # Do not log raw network exceptions, requests, credentials or arbitrary API payloads.
        store.event('run_failed', error_type=type(error).__name__)
        report(store, cfg, snapshot, 'failed:'+type(error).__name__)
        raise
    finally:
        store.release()
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run','connect','status','lead','stage'])
    parser.add_argument('--event', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'connect':
            connect(ROOT)
        elif args.command == 'run':
            status = run()
            return 2 if status.startswith('blocked') else 0
        elif args.command in ('lead','stage'):
            if not args.event:
                parser.error('--event richiesto')
            store = Store()
            if args.command=='lead':
                lead_add(store, read_json(args.event))
            else:
                stage(store, read_json(ROOT/'config.json'), read_json(args.event))
            store.export()
        else:
            store = Store()
            print(json.dumps({'credentials_configured': vault_path(ROOT).exists(),
                'states': {s: sum(r['status']==s for r in store.rows()) for s in STATES}}, indent=2))
    except Exception as error:
        print('Operazione interrotta: '+type(error).__name__+'. Consulta README e report; nessun segreto stampato.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
