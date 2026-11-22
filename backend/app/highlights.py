"""Rank non-overlapping windows by changes between downsampled frames."""
import subprocess
from urllib.parse import urlsplit, parse_qs
import re

def video_id(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.port or p.username or p.password:
            return None
        if p.hostname == 'youtu.be':
            key = p.path.strip('/')
        elif p.hostname in ('youtube.com','www.youtube.com','m.youtube.com'):
            if p.path == '/watch':
                key = parse_qs(p.query).get('v',[''])[0]
            elif p.path.startswith(('/shorts/','/live/')):
                key = p.path.split('/')[-1]
            else:
                return None
        else:
            return None
        return key if re.fullmatch(r'[\w-]{11}',key,re.ASCII) else None
    except ValueError:
        return None

def select(source, begin, end, maximum):
    length = min(30, end-begin)
    args = ['ffmpeg','-nostdin','-v','error','-ss',str(begin),'-i',str(source),'-t',str(end-begin),
            '-vf','fps=1,scale=32:18,format=gray','-an','-f','rawvideo','-threads','1','pipe:1']
    frames = subprocess.check_output(args)
    last = None
    scores = []
    for pos in range(0,len(frames),576):
        frame = frames[pos:pos+576]
        if last is not None:
            scores.append(sum(abs(a-b) for a,b in zip(frame,last))/576)
        last = frame
    span = max(1,int(length))
    starts = list(range(0,max(1,int(end-begin-length)+1),5))
    chosen = []
    for start in sorted(starts,key=lambda s: -sum(scores[s:s+span])):
        if all(abs(start-old) >= length for old in chosen):
            chosen.append(start)
        if len(chosen) == maximum:
            break
    return [dict(start=begin+s,end=begin+s+length,title=f'Passage {i+1}',why='Sélection selon les variations visuelles.') for i,s in enumerate(sorted(chosen))]
