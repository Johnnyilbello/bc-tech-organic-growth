import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import engine
import editorial


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=engine.Store(self.root)
        self.snapshot={'facebook_published':{'data':[]},'instagram_published':{'data':[]},
            'post_insights':[],'competitors':{}}

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def point(self,post,value,delay=30):
        self.store.observe('checkpoint:instagram:'+post+':24',{'platform':'instagram','post_id':post,
            'target_hours':24,'delay_minutes':delay,'published_at':(engine.now()-timedelta(hours=24,minutes=delay)).isoformat(),
            'metrics_original':[{'status':'ok','metric':'reach','data':[{'values':[{'value':value}]}]}]})

    def test_missing_metric_and_empty_breakdown_are_not_assumed_zero(self):
        self.assertIsNone(editorial.metric_value({'status':'unavailable','data':None}))
        self.assertEqual(editorial.metric_value({'status':'ok','data':[{'values':[{'value':{}}]}]}),{})
        result=editorial.analyze(self.store,{},self.snapshot)
        self.assertIsNone(result['funnel']['qualified'])
        self.assertEqual(result['frequency']['instagram_weekly_target'],3)

    def test_different_checkpoint_age_does_not_produce_a_winner(self):
        self.point('one',5);self.point('two',8);self.point('late',20000,delay=90)
        result=editorial.analyze(self.store,{},self.snapshot)
        self.assertEqual(result['comparable_cohorts'],[])
        self.assertEqual(result['excluded_checkpoints'],1)

    def test_three_same_age_posts_allow_only_a_descriptive_comparison(self):
        for name,value in [('one',0),('two',8),('three',5)]:self.point(name,value)
        result=editorial.analyze(self.store,{},self.snapshot)
        comparison=result['comparable_cohorts'][0]
        self.assertEqual(comparison['post_count'],3)
        self.assertEqual(comparison['highest_observed_posts'],['two'])
        self.assertEqual(result['frequency']['facebook_weekly_target'],2)
        self.assertIsNone(result['funnel']['clients'])

    def test_old_competitor_content_is_not_a_current_benchmark(self):
        self.snapshot['competitors']={'verified_profile':{'status':'ok','data':{'business_discovery':{
            'media':{'data':[{'timestamp':(engine.now()-timedelta(days=500)).isoformat(),'media_type':'VIDEO'}]}}}}}
        result=editorial.analyze(self.store,{},self.snapshot)
        competitor=result['competitors'][0]
        self.assertFalse(competitor['usable_as_current_benchmark'])
        self.assertEqual(competitor['recent_sample_count'],0)
        self.assertNotIn('reach',competitor)


if __name__=='__main__':unittest.main()
