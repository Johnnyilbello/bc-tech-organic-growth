import unittest
from unittest.mock import Mock
from urllib.error import URLError
from cloud_notify import GitHubNotifications,NotificationError


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.client=GitHubNotifications('owner/repo','SYNTHETIC-SECRET','123')
        self.client.request=Mock()

    def test_failure_notifies_only_owner_without_private_payload(self):
        self.client.request.side_effect=[[],{'number':7}]
        self.assertEqual(self.client.deliver('failure'),{'action':'created','number':7})
        body=self.client.request.call_args.args[2]
        self.assertEqual(body['assignees'],['owner'])
        self.assertIn('/owner/repo/actions/runs/123',body['body'])
        self.assertNotIn('SYNTHETIC-SECRET',str(body))

    def test_repeated_failure_does_not_send_another_notification(self):
        self.client.request.return_value=[{'title':self.client.ALERT,'number':7,'user':{'login':'github-actions[bot]'}}]
        self.assertEqual(self.client.deliver('failure')['action'],'unchanged')
        self.client.request.assert_called_once_with('GET','issues?state=open&per_page=100')

    def test_recovery_closes_only_the_automated_alert(self):
        self.client.request.side_effect=[[
            {'title':self.client.ALERT,'number':9,'user':{'login':'human'}},
            {'title':self.client.ALERT,'number':7,'user':{'login':'github-actions[bot]'}}],{}]
        self.assertEqual(self.client.deliver('success')['action'],'recovery_closed')
        self.assertEqual(self.client.request.call_args.args[:2],('PATCH','issues/7'))

    def test_success_and_cancellation_stay_quiet(self):
        self.client.request.return_value=[]
        self.assertEqual(self.client.deliver('success')['action'],'none')
        self.client.request.reset_mock()
        self.assertEqual(self.client.deliver('cancelled')['action'],'none')
        self.client.request.assert_not_called()

    def test_notification_test_has_a_separate_issue(self):
        self.client.request.side_effect=[[],{'number':8}]
        self.client.deliver('success',test=True)
        self.assertEqual(self.client.request.call_args.args[2]['title'],self.client.TEST)

    def test_unknown_mutation_outcome_is_not_retried_or_exposed(self):
        opener=Mock();opener.open.side_effect=URLError('SYNTHETIC-SECRET')
        client=GitHubNotifications('owner/repo','SYNTHETIC-SECRET','123',opener=opener)
        with self.assertRaises(NotificationError) as failure:client.request('POST','issues',{'title':'test'})
        self.assertNotIn('SYNTHETIC-SECRET',str(failure.exception))
        self.assertEqual(opener.open.call_count,1)


if __name__=='__main__':unittest.main()
