"""Bounded server-owned schedule choices for native Assist follow-up requests.

A token proves a later request from the same device/user, not the semantics of
the person's words. Candidate revisions and the requested edit cannot change.
"""
from collections import OrderedDict
from copy import deepcopy
import secrets


class ScheduleChoices:
    def __init__(self, clock, ttl=120, maximum=128):
        self.clock, self.ttl, self.maximum = clock, ttl, maximum
        self.pending = OrderedDict()

    @staticmethod
    def source(context):
        device=getattr(context,'device_id',None)
        request=getattr(getattr(context,'context',None),'id',None)
        user=getattr(getattr(context,'context',None),'user_id',None)
        return (device,user,request) if isinstance(device,str) and device and isinstance(request,str) and request else None

    def prune(self):
        for key,row in list(self.pending.items()):
            if row['expires']<=self.clock(): self.pending.pop(key,None)

    def issue(self, context, args, candidates):
        self.prune()
        source=self.source(context)
        if source is None or not candidates or len(candidates)>10 or len(self.pending)>=self.maximum:
            return None
        token=secrets.token_urlsafe(32)
        self.pending[token]={'source':source,'args':deepcopy(args),
            'candidates':{row['id']:row['revision'] for row in candidates},'expires':self.clock()+self.ttl}
        return token

    def redeem(self, token, context, args, current):
        self.prune()
        source=self.source(context)
        row=self.pending.get(token) if isinstance(token,str) else None
        if row is None or source is None or source[:2]!=row['source'][:2] or source[2]==row['source'][2]:
            raise ValueError('Schedule choice needs a later request from the same device and user')
        identifier=args.get('schedule_id')
        if identifier not in row['candidates']:
            raise ValueError('Schedule is not a candidate for this choice')
        expected=dict(row['args'])
        supplied={k:v for k,v in args.items() if k not in ('choice_token','schedule_id')}
        # Follow-up must preserve every original operation argument; selectors
        # may not be used to change the candidate set or requested mutation.
        if supplied!=expected:
            raise ValueError('Schedule choice arguments changed')
        revisions={item['id']:item['revision'] for item in current}
        if any(revisions.get(key)!=revision for key,revision in row['candidates'].items()):
            raise ValueError('Schedule candidates changed')
        self.pending.pop(token)
        return dict(expected,schedule_id=identifier)
