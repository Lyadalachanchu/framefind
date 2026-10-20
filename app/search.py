"""Time-aware snippet assembly, independent of HTTP."""
import json
import math
import re

MIN_MATCH = 0.95


def rank_frames(frames, profile, labels):
    """Soft conjunction: several requested concepts should coexist in a frame."""
    weights = {key: max(0.0,float(value)-0.35) for key,value in profile.items() if value>0.35}
    if not weights:
        weights = dict(sorted(profile.items(),key=lambda x:x[1],reverse=True)[:3])
    total = sum(weights.values()) or 1.0
    ranked=[]
    for frame in frames:
        probs = json.loads(frame['probabilities'] or '{}')
        log_score = sum(weight*math.log(max(0.02,float(probs.get(key,0)))) for key,weight in weights.items())/total
        ranked.append({**frame,'index_score':math.exp(log_score)})
    return sorted(ranked,key=lambda f:f['index_score'],reverse=True)


def diverse_candidates(ranked, limit=48):
    # First explore distinct two-second windows, then fill remaining slots.
    selected, seen, ids = [], set(), set()
    for frame in ranked:
        bucket=(frame['video_id'],int(frame['timestamp']//2))
        if bucket not in seen:
            seen.add(bucket); selected.append(frame); ids.add(frame['id'])
            if len(selected)>=limit:
                return selected
    selected.extend(frame for frame in ranked if frame['id'] not in ids)
    return selected[:limit]


def top_labels(probabilities, labels, limit=5):
    probabilities = probabilities or {}
    if isinstance(probabilities,str):
        probabilities=json.loads(probabilities or '{}')
    names={label['id']:label['label'] for label in labels}
    return [{'label':names.get(key,key),'score':round(float(score),3)}
            for key,score in sorted(probabilities.items(),key=lambda item:item[1],reverse=True)
            if score>=0.55][:limit]


def build_snippets(matches, videos, labels, threshold=MIN_MATCH):
    """Merge adjacent hits, never bridge missing evidence or unrelated videos."""
    groups=[]
    for frame in sorted((f for f in matches if f['score']>=threshold),key=lambda f:(f['video_id'],f['timestamp'])):
        video=videos[frame['video_id']]
        gap=max(0.75,min(1.5,20/max(1,video['fps'])))
        if groups and groups[-1][-1]['video_id']==frame['video_id'] and frame['timestamp']-groups[-1][-1]['timestamp']<=gap and frame['timestamp']-groups[-1][0]['timestamp']<20:
            groups[-1].append(frame)
        else:
            groups.append([frame])
    results=[]
    for group in groups:
        peak=max(group,key=lambda f:f['score'])
        video=videos[peak['video_id']]
        start=max(0.0,group[0]['timestamp']-1.5)
        end=min(video['duration'],max(start+3,group[-1]['timestamp']+2))
        results.append(dict(video_id=video['id'],video_name=video['name'],start=round(start,3),end=round(end,3),
            peak_time=peak['timestamp'],score=round(peak['score'],4),thumbnail_url=f"/api/frames/{peak['id']}/image",
            media_url=f"/api/videos/{video['id']}/media",labels=[x['label'] for x in top_labels(peak['probabilities'],labels)],
            evidence_count=len(group),verified=True))
    return sorted(results,key=lambda result:result['score'],reverse=True)[:60]


def normalized_query(query):
    return re.sub(r'\s+',' ',query.strip()).casefold()
