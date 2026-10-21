"""Evidence-based daily editorial review; descriptive comparisons, never sales estimates."""
import json
from collections import Counter,defaultdict
from datetime import timedelta

import engine


def metric_value(entry):
    if entry.get('status')!='ok' or not entry.get('data'):return None
    metric=entry['data'][0]
    if 'total_value' in metric:return metric['total_value'].get('value')
    values=metric.get('values') or []
    return values[-1].get('value') if values else None


def analyze(store,cfg,snapshot):
    current=engine.now();day=current.astimezone(engine.ROME).date().isoformat()
    own=[]
    for platform in ('facebook','instagram'):
        for post in snapshot.get(platform+'_published',{}).get('data') or []:
            published=post.get('timestamp',post.get('created_time'))
            if not published or engine.dt(published)<current-timedelta(days=7):continue
            own.append({'platform':platform,'post_id':post['id'],'published_at':published,
                'age_hours':round((current-engine.dt(published)).total_seconds()/3600,2),
                'format':post.get('media_type','non_rilevato'),
                'metrics':{m['metric']:metric_value(m) for m in snapshot.get('post_insights',[])
                    if m['platform']==platform and m['post_id']==post['id']}})
    groups=defaultdict(dict);excluded=0
    for row in store.db.execute("SELECT data FROM observations WHERE key LIKE 'checkpoint:%'"):
        point=json.loads(row['data'])
        # Imported legacy screenshots without a compatible metric contract
        # cannot silently enter a cohort of official API observations.
        if (point.get('origin')=='legacy' or not 0<=point.get('delay_minutes',-1)<=60
            or not point.get('published_at') or engine.dt(point['published_at'])<current-timedelta(days=30)):
            excluded+=1;continue
        for entry in point.get('metrics_original',[]):
            value=metric_value(entry)
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                key=(point['platform'],point['target_hours'],entry['metric'])
                groups[key][point['post_id']]=value
    comparisons=[]
    for (platform,hours,metric),values in groups.items():
        if len(values)<3:continue
        maximum=max(values.values())
        comparisons.append({'platform':platform,'target_hours':hours,'metric':metric,
            'post_count':len(values),'highest_observed_value':maximum,
            'highest_observed_posts':[post for post,value in values.items() if value==maximum],
            'scope':'Confronto descrittivo di post alla stessa età ±60 minuti; non prova un formato vincente o vendite.'})
    competitors=[]
    for username,entry in snapshot.get('competitors',{}).items():
        profile=(entry.get('data') or {}).get('business_discovery',{})
        media=profile.get('media',{}).get('data',[])
        valid=[m for m in media if m.get('timestamp')]
        newest=max((engine.dt(m['timestamp']) for m in valid),default=None)
        competitors.append({'username':username,'status':entry['status'],'sample_count':len(media),
            'formats':dict(Counter(m.get('media_type','non_rilevato') for m in media)),
            'newest_published_at':newest.isoformat() if newest else None,
            'recent_sample_count':sum(engine.dt(m['timestamp'])>=current-timedelta(days=30) for m in valid),
            'usable_as_current_benchmark':bool(newest and newest>=current-timedelta(days=30)),
            'limit':'Campione pubblico API; nessuna reach, conversione o vendita concorrente accessibile.'})
    leads=[json.loads(row['data']) for row in store.db.execute("SELECT data FROM observations WHERE key LIKE 'lead:%'")]
    funnel=engine.funnel(leads)
    pending=[]
    for row in store.rows():
        p=json.loads(row['payload'])
        if row['status']=='draft' and current<=engine.dt(p['planned_at'])<current+timedelta(days=7):
            missing=[]
            if not p.get('qa',{}).get('visual_checked'):missing.append('QA visiva')
            if p.get('native_queue_unknown',True):missing.append('riconciliazione coda')
            if p['platform']=='instagram' and not p.get('media'):missing.append('media finiti')
            pending.append({'id':p['id'],'platform':p['platform'],'planned_at':p['planned_at'],'missing':missing})
    reasons=[str(len(own))+' post recenti letti; '+str(len(comparisons))+' confronti con almeno tre post alla stessa età disponibili.']
    reasons.append('Esiti commerciali attribuiti non documentati.' if all(v is None for v in funnel.values()) else
        'Registro commerciale parziale: valutare le evidenze per opportunità, senza sommare le fasi del funnel.')
    if pending:reasons.append(str(len(pending))+' bozze nei prossimi sette giorni richiedono ancora controlli; nessun riempimento automatico.')
    baseline=engine.dt(cfg.get('connected_at',current.isoformat())).astimezone(engine.ROME).date()
    reviews=[baseline+timedelta(days=n) for n in (7,14,30)]
    next_review=next((d for d in reviews if d>=current.astimezone(engine.ROME).date()),None)
    if next_review is None:next_review=current.astimezone(engine.ROME).date()+timedelta(days=7)
    decision={'evaluated_at':engine.stamp(),'decision':'Mantenere il piano di 2 post Facebook e 3 Instagram a settimana; nessun aumento automatico.',
        'reason':' '.join(reasons),'next_review':str(next_review),
        'facebook_weekly_target':2,'instagram_weekly_target':3,'scheduled_is_distinct_from_target':True}
    result={'date':day,'generated_at':engine.stamp(),'own_recent':own,'comparable_cohorts':comparisons,
        'excluded_checkpoints':excluded,'competitors':competitors,'funnel':funnel,
        'pending_next_seven_days':pending,'frequency':decision,
        'experiment':{'status':'hypothesis_pending_validation','change':'Una CTA con attività, comune e ostacolo concreto; mostrare un passaggio reale del progetto.',
            'success_metric':'Messaggio ricevuto e richiesta qualificata attribuita; i clic restano una fase distinta.',
            'guard':'Nessuna causalità dedotta dai soli like o da post di età diverse.'},
        'visual_analysis':'I formati provengono dai metadati; qualità visiva e nuove creatività richiedono QA separata.'}
    store.observe('editorial:'+day+':'+engine.digest(result),result)
    engine.write_json(store.root/'editorial-analysis.json',result)
    engine.write_json(store.root/'frequency-decision.json',decision)
    return result
