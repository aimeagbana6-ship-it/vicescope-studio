"""VICEscope: video decision and FFmpeg pipeline, no redistribution of source media."""
import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable


def command(argv: list[str], capture=True) -> str:
    p = subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE)
    return p.stdout if capture else ""


def probe(path: Path) -> dict:
    return json.loads(command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))


def duration(path: Path) -> float:
    obj = probe(path)
    if not any(s.get("codec_type") == "video" for s in obj["streams"]):
        raise ValueError("Le fichier ne contient aucune piste vidéo")
    return float(obj["format"]["duration"])


def seconds(s: str | None, default: float) -> float:
    if not s:
        return default
    s = str(s).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if len(parts) not in (2, 3) or not all(re.fullmatch(r"\d+(?:\.\d+)?", x) for x in parts):
        raise ValueError("Timecode attendu : secondes, MM:SS ou HH:MM:SS")
    return sum(float(v)*60**idx for idx,v in enumerate(reversed(parts)))


def valid_range(start: float, end: float, total: float) -> None:
    if not (0 <= start < end <= total + .1):
        raise ValueError(f"Plage invalide : {start:.1f}–{end:.1f}s pour {total:.1f}s")
    if end-start > 3*3600:
        raise ValueError("La plage excède la limite de trois heures")


def cut_audio(source: Path, out: Path, start: float, length: float) -> None:
    command(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", str(start), "-i", str(source), "-t", str(length),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(out)])


def frame(source: Path, out: Path, at: float) -> None:
    command(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(source), "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5", str(out)])


def transcribe(source: Path, jobdir: Path, start: float, end: float, lang: str, progress: Callable) -> list[dict]:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    chunks = jobdir / "chunks"; chunks.mkdir(exist_ok=True)
    all_segments: list[dict] = []
    block = int(os.getenv("STUDIO_AUDIO_CHUNK_SECONDS", "600"))
    n = max(1, int((end-start+block-1)//block))
    for i in range(n):
        offset = start + i*block
        length = min(end-offset, block)
        cached = chunks / f"transcript_{i:03}.json"
        if cached.exists():
            segments = json.loads(cached.read_text(encoding="utf-8"))
        else:
            clip = chunks / f"audio_{i:03}.mp3"
            cut_audio(source, clip, offset, length)
            with clip.open("rb") as f:
                result = client.audio.transcriptions.create(model=os.getenv('OPENAI_TRANSCRIBE_MODEL','whisper-1'),
                         file=f, response_format="verbose_json", timestamp_granularities=["segment"],
                         language=lang if lang in ("fr","en") else None)
            segments=[]
            for s in getattr(result,"segments",[]) or []:
                sg = s.model_dump() if hasattr(s,"model_dump") else dict(s)
                segments.append({"start":round(offset+float(sg["start"]),2),"end":round(offset+float(sg["end"]),2),"text":sg.get("text","").strip()})
            cached.write_text(json.dumps(segments,ensure_ascii=False),encoding="utf-8")
            clip.unlink(missing_ok=True)
        all_segments.extend(segments)
        progress("analyse",.12 + .40*(i+1)/n, f"Transcription bloc {i+1}/{n}")
    return all_segments


def select_clips(source: Path, jobdir: Path, start: float, end: float, max_clips: int, segments: list[dict], instructions: str, lang: str, progress: Callable) -> list[dict]:
    """Sample a baseline frame every minute, a few denser frames around lively speech, then ask model for bounded JSON."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    imagesdir = jobdir / "frames"; imagesdir.mkdir(exist_ok=True)
    # Bounded frame count per chunk to constrain token consumption.
    image_points = [start + i*60 for i in range(int((end-start)//60)+1)]
    # Speech-dense candidates get additional inspection before ranking.
    density = sorted(segments,key=lambda s: len(s.get('text',''))/max(1,float(s['end'])-float(s['start'])),reverse=True)
    for sg in density[:min(15,max_clips*2)]:
        m=(float(sg['start'])+float(sg['end']))/2
        image_points += [m-3,m,m+3]
    image_points = sorted({int(min(end-1,max(start,p))) for p in image_points if end-start>=1})
    # Process 5-minute windows, caching IA decisions and frames so retries don't redo successful blocks.
    results=[]
    window=300
    windows = max(1,int((end-start+window-1)//window))
    for wi in range(windows):
        a=start+wi*window; b=min(end,a+window)
        cached=jobdir / f"selection_{wi:03}.json"
        if cached.exists():
            proposed=json.loads(cached.read_text(encoding="utf-8"))
        else:
            nearby=[s for s in segments if float(s['end'])>a and float(s['start'])<b]
            timeline='\n'.join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in nearby)
            content=[{"type":"input_text","text":f"Fenêtre {a:.1f}-{b:.1f}s de source. Transcription horodatée :\n{timeline[:28000]}\nConsignes : {instructions[:3000]}\nTrouve 0 à 3 extraits réellement intéressants, sans inventer d’événement ; donne start, end, title, why. Évite les doublons. Durée habituelle 15–60s. Si aucun bon moment, renvoie []."}]
            pts=[p for p in image_points if a<=p<b]
            for j,p in enumerate(pts[:10]):
                path=imagesdir/f"img_{p:06}.jpg"
                if not path.exists(): frame(source,path,p)
                image64=base64.b64encode(path.read_bytes()).decode()
                content.append({"type":"input_text","text":f"Image source à t={p}s"})
                content.append({"type":"input_image","image_url":f"data:image/jpeg;base64,{image64}","detail":"low"})
            schema={"type":"object","properties":{"clips":{"type":"array","items":{"type":"object","properties":{"start":{"type":"number"},"end":{"type":"number"},"title":{"type":"string"},"why":{"type":"string"}},"required":["start","end","title","why"],"additionalProperties":False}}},"required":["clips"],"additionalProperties":False}
            r=client.responses.create(model=os.getenv('OPENAI_ANALYSIS_MODEL','gpt-4.1-mini'),
                     input=[{"role":"system","content":"Tu es un monteur vidéo professionnel. Respecte strictement les timecodes. Ne crée pas de clip si le passage n'est pas intéressant. Aucun commentaire inventé."},{"role":"user","content":content}],
                     text={"format":{"type":"json_schema","name":"vicescope_clips","schema":schema,"strict":True}})
            proposed=json.loads(r.output_text).get('clips',[])
            cached.write_text(json.dumps(proposed,ensure_ascii=False),encoding="utf-8")
        for c in proposed:
            ca,cb=float(c.get('start',-1)),float(c.get('end',-1))
            if a <= ca < cb <= b and 8 <= cb-ca <= 90:
                results.append({"start":ca,"end":cb,"title":str(c.get('title','Moment fort'))[:160],"why":str(c.get('why',''))[:600]})
        progress("analyse", .55+.20*(wi+1)/windows, f"Analyse visuelle bloc {wi+1}/{windows}")
    results.sort(key=lambda x:x['start'])
    distinct=[]
    for c in results:
        overlap=any(max(0,min(c['end'],d['end'])-max(c['start'],d['start']))>min(c['end']-c['start'],d['end']-d['start'])*.3 for d in distinct)
        if not overlap: distinct.append(c)
        if len(distinct)>=max_clips: break
    return distinct


def ass_timestamp(sec: float) -> str:
    cs=round(sec*100)
    return f"{cs//360000}:{(cs//6000)%60:02}:{(cs//100)%60:02}.{cs%100:02}"


def ass_escape(value: str) -> str:
    return value.replace('\\','').replace('{','').replace('}','').replace('\n',' ')


def subtitles_for_clip(segments: list[dict], clip: dict, out: Path) -> None:
    a=float(clip['start']); b=float(clip['end'])
    head='''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Arial,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H72000000,-1,0,0,0,100,100,0,0,1,5,1,2,70,70,370,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    rows=[]
    for s in segments:
        sa=max(a,float(s['start']));sb=min(b,float(s['end']))
        if sb<=sa or not s['text'].strip():continue
        # Text grows gently at entrance; timing is attached to original transcript segments.
        txt=ass_escape(s['text'].strip()[:95])
        rows.append(f"Dialogue: 0,{ass_timestamp(sa-a)},{ass_timestamp(sb-a)},Main,,0,0,0,,{{\\fad(100,80)}}{txt}")
    out.write_text(head+'\n'.join(rows)+'\n',encoding='utf-8')


def export_clip(source: Path, output: Path, clip: dict, segments: list[dict]) -> dict:
    start=float(clip['start']); end=float(clip['end']);valid_range(start,end,duration(source))
    subs=output.with_suffix('.ass');subtitles_for_clip(segments,clip,subs)
    # Keep the entire game image and webcam visible: no permanent zoom/crop of the source.
    # Blurred background fills 9:16, original image remains completely visible in foreground.
    graph=("[0:v]split=2[bg][fg];"
           "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:10[blur];"
           "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];"
           "[blur][front]overlay=(W-w)/2:(H-h)/2,subtitles='"+str(subs).replace("'","\\'").replace(":","\\:")+"'[v]")
    command(["ffmpeg","-hide_banner","-nostdin","-loglevel","error","-y","-ss",str(start),"-i",str(source),"-t",str(end-start),
             "-filter_complex_threads","2","-filter_complex",graph,"-map","[v]","-map","0:a:0?","-c:v","libx264","-threads","2","-preset","veryfast","-crf","24","-pix_fmt","yuv420p","-r","30",
             "-c:a","aac","-b:a","128k","-movflags","+faststart","-s","1080x1920",str(output)])
    info=probe(output)
    stream=next(s for s in info['streams'] if s['codec_type']=='video')
    if int(stream['width'])!=1080 or int(stream['height'])!=1920:
        raise RuntimeError('Export non conforme')
    return {"file":str(output),"duration":float(info['format']['duration']),"size_bytes":output.stat().st_size}
