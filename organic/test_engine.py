import json
import io
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, Mock
from urllib.error import HTTPError, URLError

import engine as e


def config():
    return {'page_id':'10','page_name':'BC TECH Studio','ig_id':'20','instagram_username':'bc.tech.digital',
        'publishing_enabled':False,'legacy_reserved':[], 'account_metrics':{},'post_metrics':{},
        'granted_permissions':['pages_manage_posts','pages_read_engagement','instagram_basic','instagram_content_publish'],
        'media_hosts':['www.bctechstudio.com']}


def post():
    p = {'id':'test','platform':'facebook','caption':'Un contenuto concreto con CTA',
         'planned_at':e.stamp(),'destination':'https://www.bctechstudio.com/contatti',
         'status':'ready','evidence':['source'], 'qa':{'visual_checked':True},
         'native_queue_unknown':False,'queue_checked_at':e.stamp(),'asset_hashes':[], 'media':[]}
    p['approval_hash'] = e.fingerprint(p)
    return p


class FakeGraph:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
    def edge(self,path,**params):
        return []
    def get(self,path,**params):
        return {'id':'p1','permalink_url':'https://facebook.com/p1','created_time':e.stamp()}
    def post(self,path,**params):
        self.calls.append((path,params))
        if self.fail:
            raise e.MetaError('timeout',ambiguous=True)
        return {'id':'p1'}


class EngineTests(unittest.TestCase):
    def test_meta_photo_resolves_fresh_url_and_preserves_original_job(self):
        import hashlib
        data=b'\xff\xd8\xffJPEG-SYNTHETIC';sha=hashlib.sha256(data).hexdigest()
        cfg=config();cfg.update(meta_photo_staging_enabled=True,meta_photo_assets={
            'photo':{'page_id':'10','sha256':sha,'visual_checked':True,'dimensions':[1080,1350]}})
        p=dict(post(),platform='instagram',asset_hashes=[sha],media=[{'type':'IMAGE','meta_photo_id':'photo','sha256':sha}])
        p['approval_hash']=e.fingerprint(p);e.ready(p,cfg)
        graph=Mock();graph.get.return_value={'from':{'id':'10'},'images':[
            {'width':1080,'height':1350,'source':'https://scontent.example.fbcdn.net/media.jpg?fresh=1'}]}
        response=Mock();response.__enter__=Mock(return_value=io.BytesIO(data));response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        with patch.object(e,'build_opener',return_value=opener):
            resolved=e.verify_media(p,cfg,self.root,graph=graph)
        self.assertIn('fresh=1',resolved[0]['url'])
        self.assertNotIn('url',p['media'][0])
        graph.get.return_value['from']['id']='other'
        with self.assertRaises(ValueError):e.verify_media(p,cfg,self.root,graph=graph)

    def test_meta_photo_rejects_unregistered_and_untrusted_media(self):
        p=dict(post(),platform='instagram',asset_hashes=['a'*64],media=[{'type':'IMAGE','meta_photo_id':'photo','sha256':'a'*64}])
        p['approval_hash']=e.fingerprint(p)
        with self.assertRaises(ValueError):e.ready(p,config())
        cfg=dict(config(),meta_photo_staging_enabled=True,meta_photo_assets={
            'photo':{'page_id':'10','sha256':'a'*64,'visual_checked':True,'dimensions':[1080,1350]}})
        graph=Mock();graph.get.return_value={'from':{'id':'10'},'images':[
            {'width':1080,'height':1350,'source':'https://fbcdn.net.attacker.test/media.jpg'}]}
        with self.assertRaises(ValueError):e.verify_media(p,cfg,self.root,graph=graph)

    def test_changed_metric_contract_refreshes_without_overwriting_old_snapshot(self):
        cfg=config();e.write_json(self.root/'config.json',cfg)
        graph=FakeGraph()
        with patch.object(e,'collect',wraps=e.collect) as collect:
            e.run(self.root,graph=graph,allow_publishing=False)
            e.run(self.root,graph=graph,allow_publishing=False)
            self.assertEqual(collect.call_count,1)
            cfg['post_metrics']={'facebook':['post_media_view']}
            e.write_json(self.root/'config.json',cfg)
            e.run(self.root,graph=graph,allow_publishing=False)
            self.assertEqual(collect.call_count,2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations WHERE key LIKE 'daily:%'").fetchone()[0],2)

    def test_fresh_instagram_proof_does_not_refresh_old_facebook_proof(self):
        cfg=dict(config(),facebook_scheduled_api_incomplete=True)
        proof={'verified_at':e.stamp(),'page_id':'10','ig_id':'20','platforms':{
            'facebook':{'complete':True,'verified_at':(e.now()-timedelta(hours=25)).isoformat(),'items':[]},
            'instagram':{'complete':True,'source':'official_instagram_ui','verified_at':e.stamp(),
                'coverage_start':(e.now()-timedelta(days=1)).isoformat(),'coverage_end':(e.now()+timedelta(days=30)).isoformat(),'items':[]}}}
        e.write_json(self.root/'evidence/native-queue-reconciliation.json',proof)
        snapshot={'facebook_scheduled':{'status':'ok','data':[]},'instagram_native_schedule':{'status':'unavailable','data':None}}
        result=e.reconcile_queues(snapshot,cfg,self.root)
        self.assertEqual(result['facebook_scheduled']['status'],'unavailable')
        self.assertEqual(result['instagram_native_schedule']['status'],'ok')

    def test_direct_instagram_queue_preserved_even_when_business_suite_is_empty(self):
        proof={'verified_at':e.stamp(),'page_id':'10','ig_id':'20','platforms':{'instagram':{
            'complete':True,'source':'official_instagram_ui',
            'coverage_start':(e.now()-timedelta(days=1)).isoformat(),
            'coverage_end':(e.now()+timedelta(days=30)).isoformat(),
            'items':[{'caption':'Un contenuto concreto con CTA','scheduled_at':e.stamp(),'id':None}]}}}
        e.write_json(self.root/'evidence/native-queue-reconciliation.json',proof)
        snapshot={'instagram_published':{'status':'ok','data':[]},'instagram_native_schedule':{'status':'unavailable','data':None}}
        result=e.reconcile_queues(snapshot,config(),self.root)
        p=dict(post(),platform='instagram')
        self.assertTrue(e.inventory_ready(p,result))
        self.assertTrue(e.inventory_duplicate(p,result))
        self.assertFalse(result['instagram_native_schedule']['api_enumeration'])
        p['planned_at']=(e.now()+timedelta(days=31)).isoformat()
        self.assertFalse(e.inventory_ready(p,result))
        proof['verified_at']=(e.now()-timedelta(hours=25)).isoformat()
        e.write_json(self.root/'evidence/native-queue-reconciliation.json',proof)
        self.assertEqual(e.reconcile_queues(snapshot,config(),self.root)['instagram_native_schedule']['status'],'unavailable')

    def test_external_schedule_gets_native_id_only_after_matching_publication(self):
        p=dict(post(),platform='instagram',status='external_scheduled',planned_at=(e.now()-timedelta(minutes=10)).isoformat())
        self.store.add(p)
        graph=Mock();graph.edge.return_value=[]
        e.reconcile_external_scheduled(self.store,graph,config())
        self.assertEqual(self.store.rows()[0]['status'],'external_scheduled')
        graph.edge.return_value=[{'id':'native1','caption':p['caption'],'timestamp':e.stamp(),'permalink':'https://instagram.com/p/verified'}]
        e.reconcile_external_scheduled(self.store,graph,config())
        self.assertEqual(self.store.rows()[0]['native_id'],'native1')
        self.assertEqual(self.store.rows()[0]['status'],'published')

    def test_external_schedule_with_two_matches_stays_unresolved(self):
        p=dict(post(),platform='instagram',status='external_scheduled',planned_at=(e.now()-timedelta(minutes=10)).isoformat())
        self.store.add(p)
        graph=Mock();graph.edge.return_value=[{'id':x,'caption':p['caption'],'timestamp':e.stamp(),'permalink':'https://instagram.com/p/'+x} for x in ['a','b']]
        e.reconcile_external_scheduled(self.store,graph,config())
        self.assertEqual(self.store.rows()[0]['status'],'external_scheduled')
        self.assertIsNone(self.store.rows()[0]['native_id'])

    def test_native_schedule_becomes_published_only_after_verified_permalink(self):
        p=post();p['status']='scheduled';p['planned_at']=(e.now()-timedelta(minutes=5)).isoformat()
        self.store.add(p);self.store.state(p['id'],'scheduled',native='10_1')
        graph=Mock();graph.get.return_value={'id':'10_1','is_published':False}
        e.reconcile_scheduled(self.store,graph)
        self.assertEqual(self.store.rows()[0]['status'],'scheduled')
        graph.get.return_value={'id':'10_1','is_published':True,'permalink_url':'https://facebook.com/10_1','created_time':e.stamp()}
        e.reconcile_scheduled(self.store,graph)
        self.assertEqual(self.store.rows()[0]['status'],'published')
        before=graph.get.call_count;e.reconcile_scheduled(self.store,graph)
        self.assertEqual(graph.get.call_count,before)

    def test_checkpoint_crossed_after_cached_daily_snapshot_gets_fresh_metrics(self):
        published=(e.now()-timedelta(hours=25)).isoformat()
        snap={'facebook_published':{'data':[]},'instagram_published':{'data':[{'id':'i1','timestamp':published}]},'post_insights':[]}
        graph=Mock();graph.get.return_value={'data':[{'name':'reach','values':[{'value':12}]}]}
        cfg=config();cfg['post_metrics']={'instagram':['reach']}
        e.refresh_due_checkpoints(self.store,graph,cfg,snap)
        record=self.store.db.execute("SELECT data FROM observations WHERE key='checkpoint:instagram:i1:24'").fetchone()
        self.assertIsNotNone(record)
        self.assertEqual(json.loads(record['data'])['metrics_original'][0]['data'][0]['values'][0]['value'],12)
        e.refresh_due_checkpoints(self.store,graph,cfg,snap)
        graph.get.assert_called_once()

    def test_photo_transport_does_not_retry_unknown_upload(self):
        path=self.root/'asset.png';path.write_bytes(b'image')
        graph=e.Graph('v26.0','synthetic-token')
        graph.opener=Mock();graph.opener.open.side_effect=URLError('timeout')
        with self.assertRaises(e.MetaError):graph.upload_photo('10',path)
        self.assertEqual(graph.opener.open.call_count,1)

    def test_facebook_album_keeps_images_and_verifies_native_schedule(self):
        import hashlib
        p=post();p['caption']+=' '+p['destination'];p['planned_at']=(e.now()+timedelta(days=2)).isoformat()
        (self.root/'assets').mkdir()
        raw=b'approved-image-bytes'
        (self.root/'assets/test.png').write_bytes(raw)
        sha=hashlib.sha256(raw).hexdigest();p['asset_hashes']=[sha]
        p['media']=[{'type':'IMAGE','local_path':'assets/test.png','sha256':sha,'alt':'Prova reale'}]
        p['approval_hash']=e.fingerprint(p);self.store.add(p)
        class Album(FakeGraph):
            def upload_photo(self,page_id,path,alt):
                self.calls.append(('photo',{'published':False,'alt':alt}))
                return {'id':'photo1'}
            def get(self,path,**params):
                return {'id':'p1','is_published':False,'scheduled_publish_time':int(e.dt(p['planned_at']).timestamp())}
        g=Album();e.publish_one(self.store,g,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'scheduled')
        self.assertEqual(json.loads(g.calls[1][1]['attached_media']),[{'media_fbid':'photo1'}])
        self.assertNotIn('link',g.calls[1][1])
        self.assertTrue(self.store.db.execute("SELECT 1 FROM events WHERE kind='facebook_photo_uploaded'").fetchone())

    def test_facebook_album_cannot_upload_outside_assets(self):
        p=post();p['caption']+=' '+p['destination'];p['asset_hashes']=['sha']
        p['media']=[{'type':'IMAGE','local_path':'assets/../../private.txt','sha256':'sha'}]
        p['approval_hash']=e.fingerprint(p)
        with self.assertRaises(ValueError):e.ready(p,config())

    def test_empty_api_queue_requires_fresh_official_reconciliation(self):
        cfg=dict(config(),facebook_scheduled_api_incomplete=True)
        snap={'facebook_scheduled':{'status':'ok','data':[]}}
        self.assertEqual(e.reconcile_queues(snap,cfg,self.root)['facebook_scheduled']['status'],'unavailable')
        proof={'verified_at':e.stamp(),'page_id':'10','ig_id':'20','platforms':{'facebook':{'complete':True,'items':[{'message':'existing scheduled caption'}]}}}
        e.write_json(self.root/'evidence/native-queue-reconciliation.json',proof)
        snap={'facebook_scheduled':{'status':'ok','data':[]}}
        result=e.reconcile_queues(snap,cfg,self.root)['facebook_scheduled']
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data'][0]['message'],'existing scheduled caption')
        self.assertEqual(result['api_data'],[])
        proof['verified_at']=(e.now()-timedelta(hours=25)).isoformat()
        e.write_json(self.root/'evidence/native-queue-reconciliation.json',proof)
        self.assertEqual(e.reconcile_queues(snap,cfg,self.root)['facebook_scheduled']['status'],'unavailable')

    def test_insight_time_window_does_not_follow_paging_links(self):
        graph=Mock()
        graph.get.return_value={'data':[{'values':[{'value':7}]}], 'paging':{'next':'https://graph.facebook.com/other-window'}}
        result=e.insight(graph,'20/insights',metric='reach',since=1,until=2)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['query']['until'],2)
        graph.get.assert_called_once()
        graph.edge.assert_not_called()

    def test_expired_data_access_blocks_even_without_token_expiry(self):
        with self.assertRaises(ValueError):
            e.check_credential_dates({'expires_at':0,'data_access_expires_at':e.now().timestamp()-1})
        e.check_credential_dates({'expires_at':0,'data_access_expires_at':e.now().timestamp()+86400})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = e.Store(self.root)
        e.write_json(self.root/'topic-catalog.json',[
            ['sample','Hook campione','Prova fattuale campione','growth-engine','Attività locali','Visual verificato']])
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()
    def test_no_token_creates_honest_report(self):
        e.write_json(self.root/'config.json',config())
        with patch.object(e,'credentials',return_value=None):
            result = e.run(self.root)
        self.assertEqual(result,'blocked_missing_meta')
        text = next((self.root/'reports').glob('*.md')).read_text(encoding='utf8')
        self.assertIn('non rilevate',text)
        self.assertIn('"qualified": null',text)
    def test_duplicate_id_and_body_even_changed_date(self):
        p=post()
        self.assertTrue(self.store.add(p))
        p.update(id='other',planned_at=(e.now()+timedelta(days=2)).isoformat())
        self.assertFalse(self.store.add(p))
    def test_atomic_claim(self):
        self.store.add(post())
        self.assertTrue(self.store.claim('test'))
        self.assertFalse(self.store.claim('test'))
    def test_process_lease(self):
        self.store.lease()
        other=e.Store(self.root)
        with self.assertRaises(ValueError):
            other.lease()
        other.db.close()
        self.store.release()
    def test_unknown_write_never_retried(self):
        self.store.add(post())
        graph=FakeGraph(True)
        e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'uncertain')
        e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(len(graph.calls),1)
    def test_publish_verified_by_permalink(self):
        self.store.add(post())
        graph=FakeGraph()
        e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'published')
        self.assertEqual(len(graph.calls),1)
    def test_schedule_native_verified(self):
        p=post(); p['planned_at']=(e.now()+timedelta(days=2)).isoformat()
        self.store.add(p)
        class Scheduling(FakeGraph):
            def get(self,path,**params):
                return {'id':'p1','is_published':False,'scheduled_publish_time':int(e.dt(p['planned_at']).timestamp())}
        graph=Scheduling()
        e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'scheduled')
        self.assertEqual(graph.calls[0][1]['published'],'false')
    def test_missing_permalink_is_not_published(self):
        self.store.add(post()); graph=FakeGraph()
        graph.get=lambda *a,**kw:{'id':'p1'}
        e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'uncertain')
        self.assertEqual(self.store.rows()[0]['native_id'],'p1')
    def test_unknown_native_queue_blocks(self):
        p=post();p['native_queue_unknown']=True
        with self.assertRaises(ValueError):e.ready(p,config())
    def test_changed_caption_invalidates_qa(self):
        p=post();p['caption']='changed'
        with self.assertRaises(ValueError):e.ready(p,config())
    def test_stale_queue_blocks(self):
        p=post();p['queue_checked_at']=(e.now()-timedelta(days=2)).isoformat()
        with self.assertRaises(ValueError):e.ready(p,config())
    def test_missing_permission_blocks(self):
        cfg=config();cfg['granted_permissions']=[]
        with self.assertRaises(ValueError):e.ready(post(),cfg)
    def test_inventory_duplicate(self):
        p=post();snapshot={'facebook_published':{'data':[{'message':p['caption']}]},'facebook_scheduled':{'data':[]}}
        self.assertTrue(e.inventory_duplicate(p,snapshot))
    def test_missing_metrics_are_null(self):
        self.assertIsNone(e.funnel([])['qualified'])
        data=e.collect(FakeGraph(),config())
        self.assertIsNone(data['instagram_native_schedule']['data'])
    def test_failed_edge_is_null(self):
        class Denied(FakeGraph):
            def edge(self,*a,**kw):raise e.MetaError(190,401)
        data=e.collect(Denied(),config())
        self.assertIsNone(data['facebook_scheduled']['data'])
        self.assertEqual(data['facebook_scheduled']['status'],'unavailable')
    def test_checkpoint_no_early_or_duplicate(self):
        with self.assertRaises(ValueError):e.checkpoint(e.stamp(),e.stamp(),24)
        self.assertTrue(self.store.observe('key',{'value':None}))
        self.assertFalse(self.store.observe('key',{'value':99}))
    def test_dst_checkpoint_is_elapsed_utc(self):
        cp=e.checkpoint('2026-10-24T18:30:00+02:00','2026-10-25T17:30:00+01:00',24)
        self.assertEqual(cp['age_hours'],24)
    def test_qualified_needs_all_conditions(self):
        event={'opportunity_id':'BC-TEST','stage':'qualified','verified':True,'evidence_ref':'local','occurred_at':e.stamp()}
        with self.assertRaises(ValueError):e.lead_add(self.store,event)
    def test_payment_dedup_across_channels(self):
        events=[{'opportunity_id':'BC-TEST','stage':'payment','verified':True,'payment_id':'p1','amount_eur':300,'channel':c} for c in ('facebook','instagram')]
        self.assertEqual(e.funnel(events)['paid_eur'],300)
    def test_click_is_not_message(self):
        result=e.funnel([{'opportunity_id':'BC-TEST','stage':'click','verified':True}])
        self.assertIsNone(result['messages'])
        self.assertIsNone(result['clients'])
        self.assertIsNone(result['paid_eur'])
    def test_calendar_idempotent_30_days_dst(self):
        from planner import refresh
        cfg=config()
        refresh(self.store,cfg);count=len(self.store.rows())
        refresh(self.store,cfg)
        self.assertEqual(len(self.store.rows()),count)
        self.assertEqual(len(e.read_json(self.root/'stories-30-giorni.json')),30)
        self.assertTrue(all(e.dt(json.loads(r['payload'])['planned_at']) for r in self.store.rows()))
    @unittest.skipUnless(e.os.name=='nt','Windows only')
    def test_windows_vault_roundtrip(self):
        blob=e.dpapi(b'FAKE_TEST_SECRET')
        self.assertNotIn(b'FAKE_TEST_SECRET',blob)
        self.assertEqual(e.dpapi(blob,decrypt=True),b'FAKE_TEST_SECRET')
    def test_graph_disallows_external_and_traversal_paths(self):
        g=e.Graph('v26.0','TEST')
        for path in ('https://example.org','../me'):
            with self.assertRaises(ValueError):g.get(path)
    def test_read_retries_but_mutation_does_not(self):
        graph=e.Graph('v26.0','FAKE_SECRET')
        graph.opener=Mock()
        graph.opener.open.side_effect=URLError('timed out')
        with patch.object(e.time,'sleep'):
            with self.assertRaises(e.MetaError):graph.get('me')
            self.assertEqual(graph.opener.open.call_count,3)
            graph.opener.open.reset_mock()
            with self.assertRaises(e.MetaError) as failure:graph.post('10/feed',message='test')
            self.assertEqual(graph.opener.open.call_count,1)
            self.assertTrue(failure.exception.ambiguous)
    def test_api_message_and_token_do_not_leak_in_error(self):
        graph=e.Graph('v26.0','FAKE_SECRET');graph.opener=Mock()
        graph.opener.open.side_effect=HTTPError('https://graph.facebook.com',400,'bad',{},
            io.BytesIO(b'{"error":{"code":190,"message":"FAKE_SECRET"}}'))
        with self.assertRaises(e.MetaError) as failure:graph.get('me')
        self.assertNotIn('FAKE_SECRET',str(failure.exception))
        request=graph.opener.open.call_args.args[0]
        self.assertNotIn('FAKE_SECRET',request.full_url)
        self.assertEqual(request.get_header('Authorization'),'Bearer FAKE_SECRET')
    def test_staging_preserves_draft_version(self):
        p=post();p['status']='draft';self.store.add(p)
        e.stage(self.store,config(),p)
        self.assertEqual(self.store.rows()[0]['status'],'ready')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='draft_version_archived'").fetchone()[0],1)
    def test_public_media_changed_hash_stops_before_claim(self):
        p=post();p['platform']='instagram';p['asset_hashes']=['a'*64]
        p['media']=[{'url':'https://www.bctechstudio.com/a.jpg','type':'IMAGE','sha256':'a'*64}]
        p['approval_hash']=e.fingerprint(p);self.store.add(p)
        with patch.object(e,'verify_media',side_effect=ValueError('hash mismatch')):
            with self.assertRaises(ValueError):e.publish_one(self.store,FakeGraph(),config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'ready')
    def test_ig_container_publish_and_verification(self):
        p=post();p['platform']='instagram';p['asset_hashes']=['a'*64]
        p['media']=[{'url':'https://www.bctechstudio.com/a.jpg','type':'IMAGE','sha256':'a'*64}]
        p['approval_hash']=e.fingerprint(p);self.store.add(p)
        class Instagram(FakeGraph):
            def post(self,path,**params):
                self.calls.append((path,params))
                return {'id':'container' if path.endswith('/media') else 'published1'}
            def get(self,path,**params):
                if path=='container':return {'status_code':'FINISHED'}
                return {'id':'published1','permalink':'https://www.instagram.com/p/test','timestamp':e.stamp()}
        graph=Instagram()
        with patch.object(e,'verify_media',return_value=p['media']):
            e.publish_one(self.store,graph,config(),self.store.rows()[0])
        self.assertEqual(self.store.rows()[0]['status'],'published')
        self.assertEqual([c[0] for c in graph.calls],['20/media','20/media_publish'])
    def test_portable_restore_preserves_uncertain_state(self):
        self.store.add(post());self.store.state('test','uncertain',native='p1');self.store.export()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            e.write_json(root/'calendar.json',e.read_json(self.root/'calendar.json'))
            restored=e.Store(root)
            self.assertEqual(restored.rows()[0]['status'],'uncertain')
            self.assertEqual(restored.rows()[0]['native_id'],'p1')
            self.assertFalse(restored.claim('test'))
            restored.db.close()


if __name__=='__main__':unittest.main(verbosity=2)
