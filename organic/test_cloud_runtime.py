import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import engine as e
from cloud_runtime import GitHubState, DurableGraph, DurabilityError, seed_document


class MemoryGitHub:
    def __init__(self, document):
        self.document=document
        self.sha='initial'
        self.writes=0

    def request(self, method, body=None):
        if method=='GET':
            return {'sha':self.sha,'content':base64.b64encode(json.dumps(self.document).encode()).decode()}
        if body['sha']!=self.sha:
            raise DurabilityError('CAS conflict')
        self.document=json.loads(base64.b64decode(body['content']))
        self.writes+=1
        self.sha=hashlib.sha256(body['content'].encode()).hexdigest()
        return {'content':{'sha':self.sha}}


class CloudTests(unittest.TestCase):
    def test_public_repository_requires_both_consent_and_encryption(self):
        import io
        response=Mock();response.__enter__=Mock(return_value=io.BytesIO(b'{"full_name":"owner/public","private":false}'))
        response.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=response
        state=GitHubState('owner/public','synthetic',self.root,opener=opener)
        with self.assertRaises(DurabilityError):state.assert_repository(public_approved=True)
        state.state_key=bytes(range(32))
        response.__enter__.return_value=io.BytesIO(b'{"full_name":"owner/public","private":false}')
        with self.assertRaises(DurabilityError):state.assert_repository(public_approved=False)
        response.__enter__.return_value=io.BytesIO(b'{"full_name":"owner/public","private":false}')
        state.assert_repository(public_approved=True)

    def test_read_only_cycle_preserves_ready_job(self):
        from test_engine import config,post,FakeGraph
        cfg=dict(config(),publishing_enabled=True)
        e.write_json(self.root/'config.json',cfg)
        e.write_json(self.root/'topic-catalog.json',[
            ['sample','Sample hook','Factual sample','growth-engine','Local business','Verified visual']])
        store=e.Store(self.root);store.add(post());store.db.close()
        graph=FakeGraph()
        e.run(self.root,graph=graph,allow_publishing=False)
        store=e.Store(self.root)
        self.assertEqual(store.db.execute("SELECT status FROM jobs WHERE id='test'").fetchone()['status'],'ready')
        self.assertEqual(graph.calls,[])
        store.db.close()

    def test_public_state_hides_calendar_and_metrics_and_rejects_tampering(self):
        key=bytes(range(32));state=GitHubState('owner/public','synthetic',self.root,state_key=key)
        document={'schema':1,'repo':'owner/public','private_caption':'SECRET-CAMPAIGN-DRAFT','private_metric':9182}
        encoded=state.encode(document)
        self.assertNotIn(b'SECRET-CAMPAIGN-DRAFT',encoded)
        self.assertNotIn(b'private_metric',encoded)
        self.assertEqual(state.decode(encoded),document)
        envelope=json.loads(encoded);payload=bytearray(base64.b64decode(envelope['ciphertext']));payload[0]^=1
        envelope['ciphertext']=base64.b64encode(payload).decode()
        with self.assertRaises(DurabilityError):state.decode(json.dumps(envelope).encode())
        other=GitHubState('owner/other','synthetic',self.root,state_key=key)
        with self.assertRaises(DurabilityError):other.decode(encoded)
        with self.assertRaises(DurabilityError):state.decode(json.dumps(document).encode())

    def test_configuration_and_reports_are_checkpointed_without_runtime_secrets(self):
        self.state.acquire()
        e.write_json(self.root/'config.json',{'page_id':'10'})
        (self.root/'runtime/private-token').write_text('NEVER-COPY',encoding='utf8')
        (self.root/'reports').mkdir();(self.root/'reports/2026-10-20.md').write_text('Private daily report',encoding='utf8')
        self.state.checkpoint('complete',release=True)
        files=self.remote.document['files']
        self.assertIn('config.json',files)
        self.assertIn('reports/2026-10-20.md',files)
        self.assertNotIn('runtime/private-token',files)
        self.assertNotIn('NEVER-COPY',json.dumps(self.remote.document))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        store=e.Store(self.root);store.db.close()
        self.database=(self.root/'runtime/state.sqlite').read_bytes()
        self.remote=MemoryGitHub(seed_document('owner/private',self.database))
        self.state=GitHubState('owner/private','synthetic',self.root,run_id='one')
        self.state.request=self.remote.request

    def tearDown(self):
        self.temp.cleanup()

    def test_compressed_state_roundtrip_and_hash_check(self):
        self.assertEqual(self.state.load(),self.database)
        self.remote.document['database_sha256']='0'*64
        with self.assertRaises(DurabilityError):self.state.load()

    def test_live_lease_prevents_second_worker(self):
        self.state.acquire()
        second=GitHubState('owner/private','synthetic',self.root,run_id='two')
        second.request=self.remote.request
        with self.assertRaises(DurabilityError):second.acquire()

    def test_stale_writer_loses_cas_before_publication(self):
        self.state.acquire()
        self.remote.sha='another-writer-won'
        with self.assertRaises(DurabilityError):self.state.checkpoint('before_meta_write')

    def test_database_intent_survives_worker_crash(self):
        self.state.acquire()
        store=e.Store(self.root)
        p={'id':'job','platform':'facebook','caption':'Proof','planned_at':e.stamp(),'status':'ready'}
        store.add(p);store.claim('job');store.db.close()
        self.state.checkpoint('before_meta_write')
        self.remote.document['lease']['until']=time.time()-1
        other_root=self.root/'other';other_root.mkdir()
        next_state=GitHubState('owner/private','synthetic',other_root,run_id='two');next_state.request=self.remote.request
        next_state.acquire()
        restored=e.Store(other_root)
        self.assertEqual(restored.rows()[0]['status'],'inflight')
        self.assertFalse(restored.claim('job'))
        restored.db.close()

    def test_missing_durable_ack_prevents_meta_transport(self):
        state=Mock();state.checkpoint.side_effect=DurabilityError('No ack')
        graph=DurableGraph('v26.0','synthetic',state)
        with patch.object(e.Graph,'request') as transport:
            with self.assertRaises(DurabilityError):graph.post('10/feed',message='Proof')
            transport.assert_not_called()
        with patch.object(e.Graph,'upload_photo') as upload:
            with self.assertRaises(DurabilityError):graph.upload_photo('10',self.root/'image.jpg')
            upload.assert_not_called()

    def test_receipt_acknowledged_before_result_is_returned(self):
        order=[];state=Mock();state.checkpoint.side_effect=lambda phase,**kw:order.append(phase)
        graph=DurableGraph('v26.0','synthetic',state)
        with patch.object(e.Graph,'request',side_effect=lambda *a,**kw:order.append('transport') or {'id':'native'}):
            self.assertEqual(graph.post('10/feed')['id'],'native')
        self.assertEqual(order,['before_meta_write','transport','after_meta_write'])

    def test_read_only_does_not_send_or_claim_mutations(self):
        state=Mock();graph=DurableGraph('v26.0','synthetic',state,read_only=True)
        with patch.object(e.Graph,'request') as transport:
            with self.assertRaises(ValueError):graph.post('10/feed')
            transport.assert_not_called()
            state.checkpoint.assert_not_called()

    def test_capacity_limit_fails_before_remote_write(self):
        self.state.load()
        before=self.remote.writes
        with self.assertRaises(DurabilityError):self.state.save_document({'oversized':'x'*900001})
        self.assertEqual(self.remote.writes,before)


if __name__=='__main__':unittest.main()
