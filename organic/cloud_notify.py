"""Technical GitHub notifications to the repository owner, without private data."""
import argparse
import json
import os
import re
from urllib.error import HTTPError,URLError
from urllib.request import Request,build_opener

from engine import NoRedirect


class NotificationError(RuntimeError):
    pass


class GitHubNotifications:
    ALERT='[BC organic] Runtime requires attention'
    TEST='[BC organic] Notification delivery test'

    def __init__(self,repo,token,run_id,opener=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not str(run_id).isdigit():
            raise ValueError('Destinazione notifica non valida')
        self.repo,self.token,self.run_id=repo,token,str(run_id)
        self.owner=repo.split('/')[0]
        self.opener=opener or build_opener(NoRedirect())

    def request(self,method,path,body=None):
        if not re.fullmatch(r'issues(?:/\d+)?(?:\?state=open&per_page=100)?',path):
            raise ValueError('Endpoint notifica non consentito')
        request=Request('https://api.github.com/repos/'+self.repo+'/'+path,method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
        # No mutation retry: a lost acknowledgement is reconciled by listing
        # our existing issue at the next run.
        try:
            with self.opener.open(request,timeout=20) as response:return json.load(response)
        except (HTTPError,URLError,OSError,TimeoutError,json.JSONDecodeError):
            raise NotificationError('Notifica GitHub non confermata') from None

    def deliver(self,outcome,test=False):
        if outcome not in ('success','failure','cancelled','skipped'):
            raise ValueError('Esito non riconosciuto')
        if outcome in ('cancelled','skipped') and not test:return {'action':'none'}
        issues=self.request('GET','issues?state=open&per_page=100')
        title=self.TEST if test else self.ALERT
        matches=[i for i in issues if i.get('title')==title and not i.get('pull_request')
            and i.get('user',{}).get('login')=='github-actions[bot]']
        if len(matches)>1:raise NotificationError('Più segnalazioni aperte: riconciliazione richiesta')
        if matches:
            issue=matches[0]
            if outcome=='success' and not test:
                self.request('PATCH','issues/'+str(issue['number']),{'state':'closed','state_reason':'completed'})
                return {'action':'recovery_closed','number':issue['number']}
            return {'action':'unchanged','number':issue['number']}
        if outcome=='success' and not test:return {'action':'none'}
        body=('Notifica tecnica di prova per il titolare.' if test else
            'Il ciclo automatico richiede un controllo. Le pubblicazioni devono restare bloccate finché l’esito non è verificato.')
        body+='\n\nEsecuzione: https://github.com/'+self.repo+'/actions/runs/'+self.run_id
        body+='\n\nMetriche, calendario, credenziali e report completi restano nello stato cifrato. Nessun dato commerciale è incluso in questa segnalazione.'
        result=self.request('POST','issues',{'title':title,'body':body,'assignees':[self.owner]})
        if not result.get('number'):raise NotificationError('Segnalazione senza ID verificabile')
        return {'action':'created','number':result['number']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outcome',choices=['success','failure','cancelled','skipped'],required=True)
    parser.add_argument('--test',action='store_true');args=parser.parse_args()
    client=GitHubNotifications(os.environ['GITHUB_REPOSITORY'],os.environ['GITHUB_TOKEN'],os.environ['GITHUB_RUN_ID'])
    print(json.dumps(client.deliver(args.outcome,test=args.test)))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Notifica non confermata: '+type(error).__name__)
        raise SystemExit(1)
