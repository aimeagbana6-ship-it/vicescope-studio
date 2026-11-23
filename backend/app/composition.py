"""Geometry and speech captions for the vertical editor."""
import os
from functools import lru_cache
import subprocess
from .engine import ass_escape, ass_timestamp, probe

POSITIONS = {'top': 17, 'middle': 50, 'lower': 70, 'bottom': 85}

def settings(layout, share, style, position, y=None, camera=None):
    if layout not in ('standard','streamer_top','streamer_bottom') or style not in ('off','classic','bold','box','spectacular'):
        raise ValueError('Disposition ou style invalide')
    if position not in POSITIONS or not 20 <= share <= 80:
        raise ValueError('Position ou proportion invalide')
    if y is None:
        y = POSITIONS[position]
    if not 5 <= y <= 95:
        raise ValueError('Position du texte hors cadre')
    if camera is not None:
        x,t,w,h = camera
        if not (0 <= x < 1 and 0 <= t < 1 and .03 <= w <= 1 and .03 <= h <= 1 and x+w <= 1.000001 and t+h <= 1.000001):
            raise ValueError('Cadre caméra invalide')
    return dict(layout=layout,share=share,style=style,position=position,y=y,camera=camera)

def detect(source, start, end):
    import cv2
    classifier = cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    cap = cv2.VideoCapture(str(source))
    faces = []
    try:
        for frac in (.05,.17,.32,.49,.64,.81,.95):
            at = start + (end-start)*frac
            cap.set(cv2.CAP_PROP_POS_MSEC, at*1000)
            ok,img = cap.read()
            if not ok: continue
            h,w = img.shape[:2]
            scaled = cv2.resize(img,(min(w,960),max(1,round(h*min(w,960)/w))))
            sh,sw = scaled.shape[:2]
            for x,y,fw,fh in classifier.detectMultiScale(cv2.cvtColor(scaled,cv2.COLOR_BGR2GRAY),1.1,5):
                cx,cy=(x+fw/2)/sw,(y+fh/2)/sh
                if fw/sw < .35:
                    faces.append((cx,cy,fw/sw,fh/sh,at))
    finally:
        cap.release()
    if not faces: return None,start
    def score(face):
        x,y,*_=face
        hits={f[4] for f in faces if abs(f[0]-x)<.05 and abs(f[1]-y)<.075}
        return len(hits),abs(x-.5)+abs(y-.5)
    x,y,fw,fh,at=max(faces,key=score)
    w,h=min(1,fw*3.2),min(1,fh*2.9)
    return [max(0,min(1-w,x-w/2)),max(0,min(1-h,y-fh*.95)),w,h],at

def geometry(w,h,box):
    def even(v): return max(0,2*int(v/2))
    x,y,cw,ch=[even(v*m) for v,m in zip(box,(w,h,w,h))]
    choices=[(0,0,x,h),(x+cw,0,w-x-cw,h),(0,0,w,y),(0,y+ch,w,h-y-ch)]
    game=max(choices,key=lambda r:r[2]*r[3])
    if min(game[2:])<2 or min(cw,ch)<2: raise ValueError('Réduis le cadre caméra pour laisser le jeu visible')
    return (x,y,cw,ch),game

def graph(source,opts):
    layout=opts['layout']
    if layout=='standard':
        return '[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[base]'
    stream=next(s for s in probe(source)['streams'] if s['codec_type']=='video')
    camera,game=geometry(stream['width'],stream['height'],opts['camera'])
    camh=round(1920*opts['share']/200)*2
    parts=['[0:v]split=2[c][g]']
    for label,rect,height in [('c',camera,camh),('g',game,1920-camh)]:
        x,y,w,h=rect
        # Fit the entire selected camera rectangle, including hands near the edges.
        parts.append(f'[{label}]crop={w}:{h}:{x}:{y},scale=1080:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1080:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[{label}out]')
    order='[cout][gout]' if layout=='streamer_top' else '[gout][cout]'
    return ';'.join(parts+[order+'vstack[base]'])

@lru_cache(maxsize=1)
def speech_model():
    from faster_whisper import WhisperModel
    return WhisperModel(os.getenv('STUDIO_WHISPER_MODEL','tiny'),device='cpu',compute_type='int8',cpu_threads=2)

def words(source,start,end,language):
    if not any(s['codec_type']=='audio' for s in probe(source)['streams']): return []
    import numpy as np
    audio=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-ss',str(start),'-i',str(source),
        '-t',str(end-start),'-vn','-ac','1','-ar','16000','-f','f32le','pipe:1'])
    segments,_=speech_model().transcribe(np.frombuffer(audio,dtype='<f4').copy(),
        language=None if language=='auto' else language,word_timestamps=True,beam_size=1)
    return [(w.start,w.end,ass_escape(w.word.strip())) for seg in segments for w in (seg.words or []) if w.word.strip()]


def captions(source,clip,opts,language,path):
    from .engine import subtitles_for_clip
    subtitles_for_clip([],clip,path)
    data=words(source,clip['start'],clip['end'],language)
    style=opts['style']
    def row(start,end,line):
        if end<=start:return ''
        tags=r'{\an5\pos(540,'+str(round(opts['y']*19.2))+r')\bord5}'
        return f'Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Main,,0,0,0,,{tags}{line}\n'
    with path.open('a',encoding='utf8') as out:
        for i in range(0,len(data),4):
            group=data[i:i+4]
            strings=[v[2] for v in group]
            if style=='spectacular':
                for n,(a,b,_) in enumerate(group):
                    line=' '.join(r'{\c&H39DFFF&}'+word+r'{\c&HFFFFFF&}' if k==n else word for k,word in enumerate(strings))
                    out.write(row(a,b,line))
            else:
                line=' '.join(strings)
                if style=='bold':line=r'{\c&H39DFFF&}'+line.upper()
                if style=='box':line=r'{\bord8\shad0\3c&H191919&}'+line
                out.write(row(group[0][0],group[-1][1],line))
