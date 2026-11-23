"""Prototype of a private VICEscope service; deploy only behind authentication on a persistent host."""
from .composition import settings, detect
from .engine import frame
from starlette.background import BackgroundTask
import zipfile
import tempfile
from .highlights import video_id, select
import subprocess
import sys
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import ipaddress
import socket

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .engine import duration, export_clip, seconds, select_clips, transcribe, valid_range

BASE = Path(os.environ.get('STUDIO_DATA_DIR','./media')).resolve()
BASE.mkdir(parents=True,exist_ok=True)
DB=BASE/'vicescope.sqlite3'
ROOT=Path(__file__).resolve().parent
PASS=os.environ.get('STUDIO_PASSWORD','')
KEY_HASH=os.environ.get('STUDIO_KEY_SHA256','')
if os.environ.get('STUDIO_ENV')=='production' and len(PASS)<20 and len(KEY_HASH)!=64:
    raise RuntimeError('STUDIO_PASSWORD >= 20 caractères obligatoire en production')
app=FastAPI(title='VICEscope Studio',docs_url=None,redoc_url=None,openapi_url=None)
_mutex=threading.RLock()
_pending=set()


def connect():
    con=sqlite3.connect(DB,timeout=30,check_same_thread=False)
    con.row_factory=sqlite3.Row
    return con


def sql(statement,params=(),fetch=False):
    with _mutex,connect() as c:
        cur=c.execute(statement,params)
        return [dict(r) for r in cur.fetchall()] if fetch else cur.lastrowid


def initialize():
    sql('''CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, source_kind TEXT, source_label TEXT, stage TEXT NOT NULL,
          progress REAL DEFAULT 0, message TEXT DEFAULT '', start REAL, finish REAL,
          lang TEXT, maximum INTEGER, instructions TEXT, source_path TEXT,
          created_at REAL, updated_at REAL, allow_cost INTEGER DEFAULT 0,
          error TEXT DEFAULT '', analysis_model TEXT, processing_seconds REAL DEFAULT 0)''')
    sql('''CREATE TABLE IF NOT EXISTS clips(
          id TEXT PRIMARY KEY, job_id TEXT, ordinal INTEGER, start REAL, finish REAL,
          title TEXT, why TEXT, path TEXT, duration REAL DEFAULT 0,
          state TEXT DEFAULT 'waiting', error TEXT DEFAULT '')''')
    columns={r['name'] for r in sql('PRAGMA table_info(jobs)',fetch=True)}
    for name,kind in [('options','TEXT'),('preview','INTEGER DEFAULT 0')]:
        if name not in columns: sql(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
initialize()


def job(job_id):
    rows=sql('SELECT * FROM jobs WHERE id=?',(job_id,),True)
    if not rows: raise HTTPException(404,'Tâche introuvable')
    return rows[0]


def check_request(req:Request):
    if KEY_HASH:
        candidate = hashlib.sha256(req.headers.get('x-studio-key','').encode()).hexdigest()
        if hmac.compare_digest(candidate, KEY_HASH):return
        raise HTTPException(401,'Connexion nécessaire')
    if PASS and hmac.compare_digest(req.headers.get('x-studio-key',''),PASS):return
    if not PASS:raise HTTPException(503,'Accès serveur non configuré')
    stamp=req.cookies.get('vicescope_session','')
    expect=hmac.new(PASS.encode(),b'VICEscope session v1',hashlib.sha256).hexdigest()
    if not hmac.compare_digest(stamp,expect):raise HTTPException(401,'Connexion nécessaire')


def update(job_id,stage,progress,message,error=''):
    sql('UPDATE jobs SET stage=?, progress=?,message=?,error=?,updated_at=? WHERE id=?',
        (stage,float(progress),message,error,time.time(),job_id))


def worker(job_id):
    started=time.monotonic()
    try:
        j=job(job_id)
        source=Path(j['source_path'])
        if not source.is_file() and j['source_kind']=='youtube':
            update(job_id,'analyse',.03,'Import YouTube')
            url='https://www.youtube.com/watch?v='+j['source_label'].split()[-1]
            subprocess.run([sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--js-runtimes','node','--match-filter','!is_live & duration <= 10800','--max-filesize','500M','-f','b[ext=mp4]/b','-o',str(source),url],check=True,timeout=1800)
        if not source.is_file():raise RuntimeError('Import impossible ou source hors limites')
        total=duration(source)
        a=seconds(str(j['start']) if j['start'] is not None else '',0)
        b=seconds(str(j['finish']) if j['finish'] is not None else '',total)
        valid_range(a,b,total)
        sql('UPDATE jobs SET start=?,finish=? WHERE id=?',(a,b,job_id))
        d=BASE/job_id
        d.mkdir(exist_ok=True)
        def report(stage,pc,message):update(job_id,stage,pc,message)
        options=json.loads(j['options']) if j['options'] else None
        if options and (j['preview'] or (options['layout']!='standard' and not options['camera'])):
            at=a+(b-a)*.05
            if options['layout']!='standard':
                options['camera'],at=detect(source,a,b)
                sql('UPDATE jobs SET options=? WHERE id=?',(json.dumps(options),job_id))
            if j['preview'] or not options['camera'] and options['layout']!='standard':
                frame(source,d/'preview.jpg',at)
                sql("UPDATE jobs SET options=?,stage='preview_ready',message=? WHERE id=?",
                    (json.dumps(options),'Aperçu prêt à valider',job_id))
                return
        report('analyse',.15,'Recherche de passages par variations visuelles')
        transcripts=[]
        candidates=select(source,a,b,j['maximum'])
        if not candidates:
            update(job_id,'done',1,'Aucun passage suffisamment intéressant détecté')
            return
        outputs=d/'exports';outputs.mkdir(exist_ok=True)
        for i,c in enumerate(candidates):
            cid=hashlib.sha256(f'{job_id}:{i}'.encode()).hexdigest()[:24]
            existing=sql('SELECT id,state FROM clips WHERE id=?',(cid,),True)
            if not existing:
                sql('INSERT INTO clips(id,job_id,ordinal,start,finish,title,why,state) VALUES(?,?,?,?,?,?,?,?)',
                  (cid,job_id,i,c['start'],c['end'],c['title'],c['why'],'waiting'))
            if existing and existing[0]['state']=='done':continue
            sql('UPDATE clips SET state=? WHERE id=?',('rendering',cid))
            report('montage',.76+.19*i/len(candidates),f'Export {i+1}/{len(candidates)}')
            output=outputs/f'{cid}.mp4'
            x=export_clip(source,output,c,transcripts,options,j['lang'])
            sql('UPDATE clips SET path=?,duration=?,state=?,error=? WHERE id=?',
                (str(output),x['duration'],'done','',cid))
        report('export',.98,'Vérification des fichiers')
        update(job_id,'done',1,f'{len(candidates)} clip(s) prêts')
    except Exception as exc:
        update(job_id,'failed',0,'Traitement interrompu',str(exc)[:1200])
    finally:
        sql('UPDATE jobs SET processing_seconds=processing_seconds+? WHERE id=?',(time.monotonic()-started,job_id))
        with _mutex:_pending.discard(job_id)


def start_job(job_id):
    with _mutex:
        if job_id in _pending:return
        _pending.add(job_id)
    threading.Thread(target=worker,args=(job_id,),daemon=True,name=f'vicescope-{job_id[:8]}').start()


@app.on_event('startup')
def resume_work():
    # Imported sources are immutable. Keep each job path and share identical bytes.
    groups={}
    for rec in sql("SELECT source_path FROM jobs WHERE source_kind='youtube'",fetch=True):
        source=Path(rec['source_path'])
        if not source.is_file() or not source.resolve().is_relative_to(BASE): continue
        groups.setdefault(source.stat().st_size,[]).append(source)
    for paths in groups.values():
        if len(paths)<2: continue
        by_hash={}
        for path in paths:
            with path.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').digest()
            original=by_hash.setdefault(digest,path)
            if original.samefile(path): continue
            temp=path.with_name('source-'+secrets.token_hex(8)+'.link')
            try:
                os.link(original,temp)
                os.replace(temp,path)
            except OSError as exc:
                print('Source deduplication skipped:',exc.errno,flush=True)
            finally:
                temp.unlink(missing_ok=True)
    print('Studio storage available (MiB):',shutil.disk_usage(BASE).free//(1024**2),flush=True)
    for j in sql("SELECT id FROM jobs WHERE stage IN ('queued','analyse','montage','export')",fetch=True):
        start_job(j['id'])


@app.post('/api/login')
async def login(request:Request):
    obj=await request.json()
    if not PASS or not hmac.compare_digest(str(obj.get('password','')),PASS):
        raise HTTPException(401,'Mot de passe incorrect')
    res=JSONResponse({'ok':True})
    digest=hmac.new(PASS.encode(),b'VICEscope session v1',hashlib.sha256).hexdigest()
    res.set_cookie('vicescope_session',digest,httponly=True,samesite='strict',secure=(os.getenv('STUDIO_ENV')=='production'),max_age=86400)
    return res


@app.get('/api/session')
async def session(request:Request):
    check_request(request)
    return {'ok':True,'composition_version':2}


@app.get('/api/jobs')
async def list_jobs(request:Request):
    check_request(request)
    return {'jobs':sql('SELECT id,source_kind,source_label,stage,progress,message,created_at FROM jobs ORDER BY created_at DESC LIMIT 40',fetch=True)}


@app.get('/api/jobs/{job_id}')
async def get_job(job_id:str,request:Request):
    check_request(request)
    j=job(job_id)
    j.pop('source_path',None)
    return {'job':j,'clips':sql('SELECT id,ordinal,start,finish,title,why,duration,state,error FROM clips WHERE job_id=? ORDER BY ordinal',(job_id,),True)}


def youtube(url):
    p=urlparse(url)
    host=(p.hostname or '').lower()
    return p.scheme=='https' and host in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be','www.youtu.be','youtube-nocookie.com','www.youtube-nocookie.com')


def safe_direct(url:str):
    p=urlparse(url)
    hosts={x.strip().lower() for x in os.getenv('STUDIO_DIRECT_HOSTS','').split(',') if x.strip()}
    if p.scheme!='https' or not p.hostname or p.port not in (None,443) or p.username or p.password or p.hostname.lower() not in hosts:
        raise HTTPException(400,'Hôte direct non autorisé. Ajoutez-le à STUDIO_DIRECT_HOSTS côté serveur.')
    try:
        addrs=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        if not addrs: raise ValueError('DNS vide')
        for adr in addrs:
            ip=ipaddress.ip_address(adr[4][0])
            if not ip.is_global:raise ValueError('Adresse non publique')
    except Exception:
        raise HTTPException(400,'Adresse distante non autorisée')


async def download_direct(url: str, output:Path):
    safe_direct(url)
    limit=int(os.getenv('STUDIO_MAX_UPLOAD_BYTES',str(8*1024**3)))
    async with httpx.AsyncClient(follow_redirects=False,timeout=httpx.Timeout(30,read=180)) as client:
        async with client.stream('GET',url) as res:
            if res.status_code!=200:raise HTTPException(400,f'Serveur distant a répondu {res.status_code}')
            total=0
            with output.open('wb') as f:
                async for chunk in res.aiter_bytes(1024*1024):
                    total+=len(chunk)
                    if total>limit:raise HTTPException(413,'Taille vidéo trop élevée')
                    f.write(chunk)


@app.post('/api/jobs')
async def create_job(request:Request,
    link:str=Form(''),file:UploadFile|None=File(None),start:str=Form(''),end:str=Form(''),
    max_clips:int=Form(10),language:str=Form('fr'),instructions:str=Form(''),
    authorized:bool=Form(False),layout:str=Form('standard'),share:int=Form(35),
    caption_style:str=Form('off'),caption_position:str=Form('lower'),caption_y:float|None=Form(None),
    preview:bool=Form(False)):
    check_request(request)
    if shutil.disk_usage(BASE).free < 2*1024**2:
        raise HTTPException(507,'Le stockage du serveur est plein. Libère de la place avant de lancer un montage.')
    if not authorized:raise HTTPException(400,'Confirmez que vous êtes autorisé à traiter cette vidéo')
    if not 1 <= max_clips <= 30:raise HTTPException(400,'Maximum non valide')
    if language not in ('fr','en','auto'):raise HTTPException(400,'Langue non valide')
    if not file and not link.strip():raise HTTPException(400,'Fournissez un MP4 autorisé ou une URL directe autorisée')
    key=video_id(link) if link else None
    if link and (file or not key):raise HTTPException(422,'Choisis un fichier ou un lien vidéo YouTube valide')
    try: options=settings(layout,share,caption_style,caption_position,caption_y)
    except ValueError as e: raise HTTPException(422,str(e))
    jid=secrets.token_hex(12)
    d=BASE/jid;d.mkdir(mode=0o700,exist_ok=True)
    target=d/'source.mp4'
    if file:
        limit=int(os.getenv('STUDIO_MAX_UPLOAD_BYTES',str(8*1024**3)))
        total=0
        with target.open('wb') as f:
            while data:=await file.read(1024*1024):
                total+=len(data)
                if total>limit:
                    target.unlink(missing_ok=True)
                    raise HTTPException(413,'Fichier trop volumineux')
                f.write(data)
        label='Fichier MP4 fourni'
        kind='upload'
    else:
        label='YouTube '+key
        kind='youtube'
    try:
        length=duration(target) if file else None
        a=seconds(start,0)
        b=seconds(end,length)
        if length is not None:valid_range(a,b,length)
        elif b is not None:valid_range(a,b,b)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400,'Fichier ou plage invalide')
    sql('''INSERT INTO jobs(id,source_kind,source_label,stage,progress,message,start,finish,lang,maximum,instructions,
      source_path,created_at,updated_at,allow_cost,analysis_model,options,preview)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (jid,kind,label,'queued',0,'En attente',a,b,language,max_clips,instructions[:3000],str(target),time.time(),time.time(),0,'variation-visuelle',json.dumps(options),int(preview)))
    start_job(jid)
    return {'job_id':jid,'stage':'queued','source_duration':length}


@app.patch('/api/clips/{cid}')
async def rename(cid:str,request:Request):
    check_request(request)
    obj=await request.json()
    title=str(obj.get('title','')).strip()[:160]
    if not title:raise HTTPException(400,'Titre vide')
    if not sql('SELECT id FROM clips WHERE id=?',(cid,),True):raise HTTPException(404,'Clip introuvable')
    sql('UPDATE clips SET title=? WHERE id=?',(title,cid))
    return {'ok':True}


@app.post('/api/clips/{cid}/redo')
async def redo(cid:str,request:Request):
    check_request(request)
    obj=await request.json()
    instruction=str(obj.get('instruction','')).lower().strip()
    rec=sql('SELECT * FROM clips WHERE id=?',(cid,),True)
    if not rec: raise HTTPException(404,'Clip introuvable')
    c=rec[0];j=job(c['job_id'])
    # Only implement deterministic time shift. Other reframing instructions should not be falsely marked as done.
    shift=0
    if 'deux secondes plus tôt' in instruction or '2 secondes plus tôt' in instruction:shift=-2
    elif 'deux secondes plus tard' in instruction or '2 secondes plus tard' in instruction:shift=2
    else:raise HTTPException(422,"Correction non prise en charge automatiquement : essayez « commence deux secondes plus tôt ».")
    a=max(float(j['start']),float(c['start'])+shift)
    b=float(c['finish'])
    if b-a<5:raise HTTPException(422,'Clip trop court après correction')
    sql('UPDATE clips SET state=?,start=? WHERE id=?',('rendering',a,cid))
    def task():
        try:
            path=Path(c['path'])
            saved=BASE/c['job_id']/'redo_transcript_cache.json'
            segs=[]
            for p in sorted((BASE/c['job_id']/'chunks').glob('transcript_*.json')):
                segs.extend(json.loads(p.read_text(encoding='utf-8')))
            tmp=path.with_suffix('.new.mp4')
            result=export_clip(Path(j['source_path']),tmp,{'start':a,'end':b},segs,json.loads(j['options']) if j['options'] else None,j['lang'])
            os.replace(tmp,path)
            subs=tmp.with_suffix('.ass');subs.unlink(missing_ok=True)
            sql('UPDATE clips SET state=?,duration=?,error=? WHERE id=?',('done',result['duration'],'',cid))
        except Exception as e:
            sql('UPDATE clips SET state=?,error=? WHERE id=?',('failed',str(e)[:500],cid))
    threading.Thread(target=task,daemon=True).start()
    return {'ok':True,'stage':'rendering'}


@app.get('/api/clips/{cid}/video')
async def clip_file(cid:str,request:Request):
    check_request(request)
    rec=sql('SELECT path FROM clips WHERE id=? AND state=?',(cid,'done'),True)
    if not rec or not rec[0]['path']:raise HTTPException(404,'Export indisponible')
    path=Path(rec[0]['path'])
    if not path.exists() or not path.is_relative_to(BASE):raise HTTPException(404,'Export absent')
    return FileResponse(path,media_type='video/mp4',filename=f'VICEscope-{cid}.mp4')


@app.get('/')
async def home():
    return FileResponse(ROOT/'static'/'index.html')


@app.get('/api/jobs/{job_id}/preview')
def preview_frame(job_id:str,request:Request):
    check_request(request)
    j=job(job_id)
    path=Path(j['source_path']).parent/'preview.jpg'
    if not path.exists():raise HTTPException(404,'Image indisponible')
    return FileResponse(path,media_type='image/jpeg')

@app.post('/api/jobs/{job_id}/apply')
async def apply_layout(job_id:str,request:Request):
    check_request(request)
    obj=await request.json()
    try:
        opts=settings(obj['layout'],int(obj['share']),obj['style'],obj['position'],float(obj['y']),obj.get('camera'))
        if opts['layout']!='standard' and not opts['camera']:raise ValueError('Trace le cadre caméra sur la source')
    except (ValueError,KeyError,TypeError) as e: raise HTTPException(422,str(e))
    with _mutex,connect() as con:
        if job_id in _pending: raise HTTPException(409,'Préparation en cours, réessaie dans un instant')
        changed=con.execute("UPDATE jobs SET options=?,preview=0,stage='queued' WHERE id=? AND stage='preview_ready'",(json.dumps(opts),job_id)).rowcount
        if not changed:raise HTTPException(409,'Cette tâche ne peut plus être modifiée')
    start_job(job_id)
    return {'ok':True}

@app.get('/api/jobs/{job_id}/archive')
def download_series(job_id:str,request:Request):
    check_request(request)
    j=job(job_id)
    clips=sql('SELECT ordinal,path,state FROM clips WHERE job_id=? ORDER BY ordinal',(job_id,),True)
    if j['stage']!='done' or not clips or any(c['state']!='done' for c in clips):
        raise HTTPException(409,'Les rendus sont encore en cours')
    with tempfile.NamedTemporaryFile(suffix='.zip',delete=False) as tmp:
        path=Path(tmp.name)
    try:
        with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as archive:
            for c in clips:
                media=Path(c['path']).resolve()
                if not media.is_file() or not media.is_relative_to(BASE):raise HTTPException(404,'Export absent')
                archive.write(media,f"Short-{c['ordinal']+1:02d}.mp4")
        return FileResponse(path,media_type='application/zip',filename=f'VICEscope-{job_id}.zip',background=BackgroundTask(path.unlink,missing_ok=True))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
