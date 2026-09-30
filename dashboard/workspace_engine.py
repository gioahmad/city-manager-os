"""Deterministic relationship evidence and calendar rules. No external calls."""
import calendar
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta

FAMILY = {'MOTHER_OF','FATHER_OF','PARENT_OF','DAUGHTER_OF','SON_OF','CHILD_OF',
          'SISTER_OF','BROTHER_OF','SIBLING_OF','SPOUSE_OF','AUNT_OF','UNCLE_OF','GUARDIAN_OF'}
RELATIONS = FAMILY | {'WORKS_FOR','CONTACT_FOR','LIVES_AT','OWNS','MANAGES','ON_STREET',
                      'AFFECTS','MENTIONS','LINKED_TO'}


def normalize(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


def address_key(value):
    value = normalize(value)
    # Preserve unit information; do not silently collapse households.
    for short, full in [('st','street'),('ave','avenue'),('rd','road'),('blvd','boulevard')]:
        value = re.sub(r'\b'+short+r'\.?\b', full, value)
    return re.sub(r'[^\w\s#-]', '', value)


def derive_relationships(facts):
    """Return named derivations and their complete supporting fact IDs.

    Only declared facts participate: uncertain suggestions never feed inference.
    Recompute from currently permitted facts so retraction and access changes apply.
    """
    parents, siblings = [], []
    for f in facts:
        a, b, kind = str(f['source_id']), str(f['target_id']), f['relation']
        proof = [str(f['id'])]
        if kind in {'MOTHER_OF','FATHER_OF','PARENT_OF'}:
            parents.append((a,b,proof))
        elif kind in {'DAUGHTER_OF','SON_OF','CHILD_OF'}:
            parents.append((b,a,proof))
        elif kind in {'SISTER_OF','BROTHER_OF','SIBLING_OF'}:
            siblings += [(a,b,kind,proof),(b,a,'SIBLING_OF',proof)]
    result = {}
    declared = {(str(f['source_id']),str(f['target_id']),f['relation']) for f in facts}

    def add(a,b,kind,proof):
        if a==b or (a,b,kind) in declared:
            return
        key = (a,b,kind)
        if key not in result:
            result[key] = {'id':'derived:'+':'.join(key), 'source_id':a,'target_id':b,
                           'relation':kind,'derived':True,'proof':sorted(set(proof))}

    children = defaultdict(list)
    for a,b,proof in parents:
        children[a].append((b,proof))
    for a,b,proof in parents:
        for c,other in children[b]:
            add(a,c,'GRANDPARENT_OF',proof+other)
    for a,b,kind,proof in siblings:
        for child,other in children[b]:
            label = {'SISTER_OF':'AUNT_OF','BROTHER_OF':'UNCLE_OF'}.get(kind,'PARENTS_SIBLING_OF')
            add(a,child,label,proof+other)
        for child,left in children[a]:
            for cousin,right in children[b]:
                add(child,cousin,'COUSIN_OF',proof+left+right)
    return list(result.values())


def suggestions(entities, dismissed=()):
    """Bounded candidate matching; evidence ranks signals, never probabilities."""
    buckets = defaultdict(set)
    for e in entities:
        if e['kind']!='PERSON':
            continue
        attrs=e.get('attributes') or {}
        last=normalize(e['name']).split()[-1:]
        signals=[('surname',last[0] if last else ''),('address',address_key(attrs.get('address'))),
                 ('organization',normalize(attrs.get('organization')))]
        signals += [('email',normalize(x)) for x in attrs.get('emails',[])]
        signals += [('phone',re.sub(r'\D','',x)) for x in attrs.get('phones',[])]
        signals += [('tag',normalize(x)) for x in attrs.get('tags',[])]
        for kind,value in signals:
            if value:
                buckets[(kind,value)].add(str(e['id']))
    pairs=defaultdict(set)
    # ponytail: bounded visible directory; indexed candidate search replaces this at >500 records.
    for (kind,_), ids in buckets.items():
        ordered=sorted(ids)
        for i,a in enumerate(ordered):
            for b in ordered[i+1:]:
                pairs[(a,b)].add(kind)
    result=[]
    for (a,b),signals in pairs.items():
        key=hashlib.sha256(json.dumps([a,b,sorted(signals)],separators=(',',':')).encode()).hexdigest()
        if key in dismissed:
            continue
        reasons={'surname':'Same surname; family unknown','address':'Same address; household unverified',
                 'organization':'Same organization','email':'Shared email; identity needs review',
                 'phone':'Shared phone; identity needs review','tag':'Shared tag'}
        result.append({'source_id':a,'target_id':b,'fingerprint':key,
                       'reasons':[reasons[s] for s in sorted(signals)],
                       'strength':'Strong identity signal' if signals & {'email','phone'} else 'Possible connection'})
    return sorted(result,key=lambda x:(x['strength']!='Strong identity signal',-len(x['reasons']),x['fingerprint']))[:100]


def occurrence(row, today):
    d=row['event_date']
    if isinstance(d,str):
        d=date.fromisoformat(d)
    if not row['annual']:
        return d
    year=max(today.year,d.year)
    current=date(year,d.month,min(d.day,calendar.monthrange(year,d.month)[1]))
    if current<today:
        year+=1
        current=date(year,d.month,min(d.day,calendar.monthrange(year,d.month)[1]))
    return current


def briefing_dates(rows, today, days):
    result=[]
    for row in rows:
        event=occurrence(row,today)
        reminder=event-timedelta(days=row['lead_days'])
        handled=row.get('handled_occurrence')
        if isinstance(handled,str):
            handled=date.fromisoformat(handled)
        # One-time overdue dates remain until handled; annual dates advance after occurrence.
        if reminder<=today+timedelta(days=days) and handled!=event:
            result.append({**row,'occurrence':event,'remind_on':reminder,'overdue':event<today})
    return sorted(result,key=lambda r:(r['occurrence'],r['label']))


def text_draft(row):
    first=row['name'].split()[0]
    return {'ANNIVERSARY':f'Happy anniversary, {first}! Wishing you both a wonderful day.',
            'BIRTHDAY':f'Happy birthday, {first}! Hope you have a great day.',
            'REMEMBRANCE':f"Thinking of you today, {first}. Just wanted you to know you're in my thoughts.",
            'FOLLOW_UP':f'Hi {first}, just checking in. Let me know how things are going.'}.get(
                row['occasion'],f'Hi {first}, thinking of you and wanted to check in.')
