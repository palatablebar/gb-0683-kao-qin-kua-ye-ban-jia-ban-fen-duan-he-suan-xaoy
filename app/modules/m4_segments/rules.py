from datetime import datetime,timedelta
from app.modules.m3_versions.rules import schedule,ZONE
from app.errors import DomainError

def split_shift(shift):
    if shift['in'] is None or shift['out'] is None:raise DomainError('MISSING','班次缺少进出打卡')
    a=shift['in'];b=shift['out'];start=datetime.fromisoformat(a['at']).astimezone(ZONE);end=datetime.fromisoformat(b['at']).astimezone(ZONE)
    planned_start,planned_end,bs,be=schedule(a)
    if start>=end or end-start>timedelta(hours=18):raise DomainError('RANGE','打卡时长超出18小时')
    if start<planned_start-timedelta(hours=6) or end>planned_end+timedelta(hours=6):raise DomainError('RANGE','打卡超出班次范围')
    if start.second or end.second or start.microsecond or end.microsecond:raise DomainError('RANGE','打卡精度必须为整分钟')
    boundaries={start,end}
    for d in (bs,be):
        if start<d<end:boundaries.add(d)
    midnight=start.replace(hour=0,minute=0,second=0)+timedelta(days=1)
    while midnight<end:boundaries.add(midnight);midnight+=timedelta(days=1)
    points=sorted(boundaries);segments=[]
    for left,right in zip(points,points[1:]):
        if bs<=left<be:continue
        segments.append({'id':shift['id'],'employee':shift['employee'],'shift_day':shift['day'],'calendar_day':left.date().isoformat(),'start':left.isoformat(),'end':right.isoformat(),'minutes':int((right-left).total_seconds()//60),'rate':str(a['hourly_rate']),'line':a['_line']})
    return segments

def apply(state):
    out=[];bad=set()
    for shift in state['shifts']:
        try:out.extend(split_shift(shift))
        except DomainError as e:
            for r in shift['sources']:bad.add(r['_line']);state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':e.code,'raw':r['_raw']})
    state['rows']=[r for r in state['rows'] if r['_line'] not in bad];state['segments']=out;return state
