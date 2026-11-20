from datetime import date
from decimal import Decimal,ROUND_HALF_UP
from collections import defaultdict

def price(segments,holidays):
    normal_used=defaultdict(int);out=[]
    for s in sorted(segments,key=lambda r:(r['start'],r['employee'])):
        d=date.fromisoformat(s['calendar_day']);key=(s['employee'],s['calendar_day']);minutes=s['minutes'];rate=Decimal(str(s['rate']))
        if s['calendar_day'] in holidays:parts=[(minutes,Decimal(3),'HOLIDAY')]
        elif d.weekday()>=5:parts=[(minutes,Decimal(2),'REST')]
        else:
            regular=min(minutes,max(0,480-normal_used[key]));normal_used[key]+=minutes
            parts=[(regular,Decimal(1),'REGULAR'),(minutes-regular,Decimal('1.5'),'OVERTIME')]
        for count,multiplier,code in parts:
            if count:
                item=dict(s);item.update(minutes=count,multiplier=str(multiplier),code=code,amount=str((rate*count*multiplier/60).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)));out.append(item)
    return out

def apply(state):
    holidays=set(state['parameters'].get('holidays',['2026-10-01']))
    calculations=price(state['segments'],holidays)
    total=sum((Decimal(r['amount']) for r in calculations),Decimal(0));paid=sum((Decimal(str(r['paid'])) for r in state['rows']),Decimal(0))
    state['calculations']=calculations;state['business']={'total':str(total),'paid':str(paid),'difference':str(total-paid),'minutes':sum(r['minutes'] for r in calculations),'unit':'CNY'};return state
