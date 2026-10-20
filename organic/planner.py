"""Rolling 30 day proposals. New concepts never overwrite publication history."""
import json
from datetime import datetime, timedelta, time
from urllib.parse import urlencode

TOPICS = []  # Private catalog is restored only from authenticated encrypted state.


def refresh(store, cfg):
    from engine import ROME, now, write_json, read_json
    topics=read_json(store.root/'topic-catalog.json',TOPICS)
    if not topics:
        raise ValueError('Catalogo editoriale verificato mancante')
    start = now().astimezone(ROME).date()
    existing = {(json.loads(r['payload'])['planned_at'][:10], r['platform']) for r in store.rows()}
    history = cfg.get('legacy_reserved', [])
    reserved = {(x['date'], x['platform']) for x in history}
    cities = ['Cinisi','Terrasini','Carini','Capaci','Palermo']
    for offset in range(30):
        date = start+timedelta(days=offset)
        for platform, weekdays, hour in [('instagram',(0,2,4),19),('facebook',(1,3),15)]:
            if date.weekday() not in weekdays or (str(date),platform) in existing or (str(date),platform) in reserved:
                continue
            # Absolute week keeps topics stable across daily runs and across month boundaries.
            index = (date.toordinal()//7*3 + date.weekday()) % len(topics)
            slug, hook, body, path, audience, visual = topics[index]
            city = cities[date.toordinal()%len(cities)]
            content_id = platform[:2]+'-'+date.strftime('%Y%m%d')+'-'+slug
            params = urlencode(dict(utm_source=platform,utm_medium='organic_social',
                utm_campaign='local_2026_10',utm_content=content_id))
            link = 'https://www.bctechstudio.com/'+path+'?'+params
            cta = ('Dal link in bio apri '+('Progetti' if path.startswith('progetti') else 'BC Growth Engine' if path=='growth-engine' else 'Contatti')+
                   '. Raccontami attività, comune e passaggio che vuoi migliorare.') if platform=='instagram' else (
                   'Gestisci un’attività a '+city+' o nell’area di Palermo? Parti dal caso o dallo strumento, poi raccontami attività e bisogno concreto.\n\n'+link)
            post = dict(id=content_id,platform=platform,planned_at=datetime.combine(date,time(hour,10),ROME).isoformat(),
                status='draft',format='carosello' if platform=='instagram' else 'link_post',hook=hook,
                caption=hook+'\n\n'+body+'\n\n'+cta, destination=link, audience=audience,city=city,
                visual=visual,alt=hook+' '+visual,media=[],asset_hashes=[],
                hypothesis='Richieste con attività pertinente, bisogno concreto e prossimo passo; validare su evidenze reali.',
                evidence=['BC: fonti verificate in evidence/legacy-sources.json'],
                native_queue_unknown=True,qa={'visual_checked':False},version=1)
            store.add(post)
    # Daily Stories are proposals, never silently sent as feed content.
    stories = []
    for offset in range(30):
        date = start+timedelta(days=offset)
        topic = topics[date.toordinal()%len(topics)]
        stories.append({'date':str(date),'status':'concept_non_programmato','platforms':['instagram','facebook'],
            'hook':topic[1], 'frames':[topic[1],topic[2],
            'Raccontami attività, comune e ostacolo concreto. Growth Engine e contatti su bctechstudio.com.'],
            'proof':topic[3], 'visual':topic[5], 'sticker_link':'Solo se supportato e verificato; non simulare uno sticker nel video.'})
    write_json(store.root/'stories-30-giorni.json',stories)
    store.export()
    plan = ['# Calendario dinamico — 30 giorni', '',
        'Bozze locali. Le date riservate dallo storico non provano la coda aggiornata Meta. Nessun contenuto viene pubblicato da questo file.', '',
        '| Data Europe/Rome | Canale | Stato | Hook |', '|---|---|---|---|']
    for item in history:
        if str(start) <= item['date'] < str(start+timedelta(days=30)) and not any(r['id']=='legacy-'+item['id'] for r in store.rows()):
            plan.append(f'| {item["date"]} | {item["platform"]} | riservato da storico; da riconfermare | {item["id"]} |')
    for row in store.rows():
        p = json.loads(row['payload'])
        if str(start) <= p['planned_at'][:10] < str(start+timedelta(days=30)):
            status='storico: programmato Meta; da riconfermare' if row['status']=='blocked' and 'nativamente' in p.get('legacy_status','') else row['status']
            plan.append(f'| {p["planned_at"]} | {p["platform"]} | {status} | {p.get("hook", "")} |')
    plan += ['', 'Ogni giorno: sequenza Stories proposta in stories-30-giorni.json. Reel: sviluppare una dimostrazione reale ogni settimana quando esiste materiale verificato. Non convertire automaticamente un’immagine in una falsa dimostrazione video.',
             '', 'Le bozze conservano ID e versione. Aggiornamenti: salvare la versione precedente e motivazione prima di intervenire. A 7/14/30 giorni decidere su dati comparabili e risultati commerciali, non soltanto like.']
    (store.root/'calendario-30-giorni.md').write_text('\n'.join(plan),encoding='utf-8')
