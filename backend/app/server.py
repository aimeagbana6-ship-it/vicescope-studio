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


CACHE = Path(tempfile.gettempdir())/'vicescope-youtube'

def youtube_cache(jid):
    return CACHE/jid

def clean_finished_job(jid):
    # A failed import with exported clips can still be reopened for editing.
    if len(jid)!=24 or any(ch not in '0123456789abcdef' for ch in jid):
        return
    rec=sql("SELECT stage FROM jobs WHERE id=?",(jid,),True)
    if not rec or rec[0]['stage'] not in ('failed','downloaded'):
        return
    if sql("SELECT 1 FROM clips WHERE job_id=? AND state='done'",(jid,),True):
        return
    for path in (BASE/jid,youtube_cache(jid)):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)

def clean_finished_jobs():
    for row in sql("SELECT id FROM jobs WHERE stage IN ('failed','downloaded')",fetch=True):
        clean_finished_job(row['id'])

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
    clean_finished_jobs()
    columns={r['name'] for r in sql('PRAGMA table_info(jobs)',fetch=True)}
    for name,kind in [('options','TEXT'),('preview','INTEGER DEFAULT 0'),('replay_task','TEXT'),('replay_redo','TEXT')]:
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


def local_source(j):
    source=Path(j['source_path'])
    if not source.is_file() and j['source_kind']=='youtube':
        source=youtube_cache(j['id'])/'source.mp4'
        source.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if source.is_file():return source
        url='https://www.youtube.com/watch?v='+j['source_label'].split()[-1]
        output=source.with_name('import.mp4')
        command=[sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist',
                 '--no-progress','--js-runtimes','node','--match-filter','!is_live',
                 '--concurrent-fragments','4','--abort-on-unavailable-fragments',
                 '-f','b[ext=mp4][height<=480]/bv[height<=480]+ba/b[height<=480]',
                 '--merge-output-format','mp4','-o',str(output),url]
        result=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=10800)
        if result.returncode:
            errors=[line for line in result.stderr.splitlines() if 'ERROR:' in line]
            detail=errors[-1].split('ERROR:',1)[1].strip() if errors else 'Téléchargement interrompu'
            import re
            raise RuntimeError('Import YouTube : '+re.sub(r'https?://\S+', '[URL]', detail)[:400])
        if not output.is_file():
            raise RuntimeError('YouTube ne fournit aucun fichier pour ce replay. Vérifie que le direct est terminé.')
        duration(output)
        os.replace(output,source)
    return source


def worker(job_id):
    started=time.monotonic()
    try:
        j=job(job_id)
        source=local_source(j)
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
        try:
            if not sql("SELECT 1 FROM clips WHERE job_id=? AND state='done'",(job_id,),True):
                shutil.rmtree(youtube_cache(job_id),ignore_errors=True)
        finally:
            update(job_id,'failed',0,'Traitement interrompu',str(exc)[:1200])
        clean_finished_job(job_id)
    finally:
        sql('UPDATE jobs SET processing_seconds=processing_seconds+? WHERE id=?',(time.monotonic()-started,job_id))
        with _mutex:_pending.discard(job_id)


def dispatch_replay(jid, redo=None):
    task=secrets.token_hex(16)
    sql("UPDATE jobs SET replay_task=?, replay_redo=?, stage='analyse', message=?, updated_at=? WHERE id=?",
        (task,json.dumps(redo or []),'En attente de GitHub Actions',time.time(),jid))
    def dispatch():
        try:
            response=httpx.post('https://api.github.com/repos/aimeagbana6-ship-it/vicescope-studio/actions/workflows/studio-replay.yml/dispatches',
                headers={'Authorization':'Bearer '+os.environ['STUDIO_ACTIONS_TOKEN']},
                json={'ref':'main','inputs':{'task':task}},timeout=30)
            response.raise_for_status()
        except (httpx.HTTPError,KeyError):
            sql("UPDATE jobs SET stage='failed',message=? WHERE replay_task=?",
                ('GitHub Actions indisponible. Vérifie la configuration du Studio.',task))
    threading.Thread(target=dispatch,daemon=True).start()


def replay_request(task,request):
    secret=os.getenv('STUDIO_REPLAY_KEY','')
    if not secret or not hmac.compare_digest(request.headers.get('x-replay-key',''),secret):
        raise HTTPException(401,'Connexion nécessaire')
    rows=sql("SELECT * FROM jobs WHERE replay_task=? AND stage IN ('analyse','montage')",(task,),True)
    if not rows:raise HTTPException(409,'Tâche expirée')
    return rows[0]


@app.get('/api/replay/{task}/')
def replay_config(task:str,request:Request):
    j=replay_request(task,request)
    return dict(key=j['source_label'].split()[-1],options=json.loads(j['options']),
        redo=json.loads(j['replay_redo']), **{k:j[k] for k in ('start','finish','maximum','preview','lang')})


@app.put('/api/replay/{task}/{name}')
async def receive_render(task:str,name:str,request:Request):
    j=replay_request(task,request)
    if name!='preview' and name not in map(str,range(j['maximum'])):
        raise HTTPException(422,'Export inconnu')
    directory=BASE/j['id']/task
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory,delete=False) as stream:
        tmp=Path(stream.name)
        try:
            async for block in request.stream():
                stream.write(block)
            stream.close()
            os.replace(tmp,directory/name)
        finally:
            tmp.unlink(missing_ok=True)
    return {'ok':True}


@app.post('/api/replay/{task}/')
async def replay_event(task:str,request:Request):
    data=await request.json()
    with _mutex:
        j=replay_request(task,request)
        jid=j['id']
        directory=BASE/jid/task
        kind=data['kind']
        if kind=='progress':
            update(jid,'analyse',data['progress'],data['message'])
        elif kind=='failed':
            update(jid,'failed',0,'Traitement interrompu',data['message'])
        elif kind=='preview':
            os.replace(directory/'preview',BASE/jid/'preview.jpg')
            sql("UPDATE jobs SET options=?,stage='preview_ready',message=? WHERE id=?",
                (json.dumps(data['options']),'Aperçu prêt à valider',jid))
        elif kind=='done':
            clips=data['clips']
            with connect() as con:
                for i,c in enumerate(clips):
                    ordinal=c.get('ordinal',i)
                    path=directory/str(ordinal)
                    if not path.is_file():raise HTTPException(409,'Export incomplet')
                    cid=hashlib.sha256(f'{jid}:{ordinal}'.encode()).hexdigest()[:24]
                    con.execute("""INSERT INTO clips(id,job_id,ordinal,start,finish,title,why,path,duration,state)
                        VALUES(?,?,?,?,?,?,?,?,?,'done') ON CONFLICT(id) DO UPDATE SET
                        start=excluded.start,finish=excluded.finish,path=excluded.path,duration=excluded.duration,state='done',error=''""",
                        (cid,jid,ordinal,c['start'],c['end'],c['title'],c['why'],str(path),c['duration']))
                con.execute("UPDATE jobs SET stage='done',progress=1,finish=?,message=?,updated_at=? WHERE id=?",
                    (data['finish'],'Montage terminé' if clips else 'Aucun passage détecté',time.time(),jid))
        else:
            raise HTTPException(422,'Événement inconnu')
    return {'ok':True}


def start_job(job_id):
    j=job(job_id)
    if j["source_kind"]=="youtube" and os.getenv("STUDIO_REPLAY_KEY"):
        dispatch_replay(job_id)
        return
    with _mutex:
        if job_id in _pending:return
        _pending.add(job_id)
    threading.Thread(target=worker,args=(job_id,),daemon=True,name=f'vicescope-{job_id[:8]}').start()


@app.on_event('startup')
def resume_work():
    # Retry jobs skipped by the retired three-hour / 500 MB import filters once.
    sql("""UPDATE jobs SET stage='queued', error='', message='Nouvel import du replay'
           WHERE stage='failed' AND source_kind='youtube' AND error LIKE ?
           AND NOT EXISTS (SELECT 1 FROM clips WHERE clips.job_id=jobs.id)""",
        ("%No such file or directory:%/import.mp4%' -> '%/source.mp4'%",))
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
    for j in sql("SELECT id FROM jobs WHERE replay_task IS NULL AND stage IN ('queued','analyse','montage','export')",fetch=True):
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
    return {'ok':True,'composition_version':2,'storage_free_mib':shutil.disk_usage(BASE).free//(1024**2)}


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
    if shutil.disk_usage(BASE).free < 16*1024**2:
        clean_finished_jobs()
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
        elif b is not None:
            if os.getenv("STUDIO_REPLAY_KEY"):
                if not 0 <= a < b:raise ValueError("Plage invalide")
            else:valid_range(a,b,b)
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
    if j['replay_task']:
        with _mutex:
            if job(j['id'])['stage']!='done':raise HTTPException(409,'Montage en cours')
            dispatch_replay(j['id'],[dict(ordinal=c['ordinal'],start=a,end=b,title=c['title'],why=c['why'])])
        return {'ok':True,'stage':'rendering'}
    sql('UPDATE clips SET state=?,start=? WHERE id=?',('rendering',a,cid))
    def task():
        try:
            path=Path(c['path'])
            saved=BASE/c['job_id']/'redo_transcript_cache.json'
            segs=[]
            for p in sorted((BASE/c['job_id']/'chunks').glob('transcript_*.json')):
                segs.extend(json.loads(p.read_text(encoding='utf-8')))
            tmp=path.with_suffix('.new.mp4')
            result=export_clip(local_source(j),tmp,{'start':a,'end':b},segs,json.loads(j['options']) if j['options'] else None,j['lang'])
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
    if obj.get('action')=='confirm_download':
        with _mutex:
            j=job(job_id)
            if j['stage']=='downloaded':return {'ok':True}
            if j['stage'] not in ('done','purging') or job_id in _pending:
                raise HTTPException(409,'Le montage doit être terminé')
            if sql("SELECT id FROM clips WHERE job_id=? AND state!='done'",(job_id,),True):
                raise HTTPException(409,'Un rendu est encore en cours')
            directory=BASE/job_id
            if directory.parent!=BASE or directory.is_symlink() or len(job_id)!=24:
                raise HTTPException(400,'Identifiant invalide')
            # Reclaim disk before writing SQLite, which may be on a full volume.
            if directory.is_dir():shutil.rmtree(directory)
            shutil.rmtree(youtube_cache(job_id),ignore_errors=True)
            with connect() as con:
                con.execute('DELETE FROM clips WHERE job_id=?',(job_id,))
                con.execute("UPDATE jobs SET stage='downloaded',message='Téléchargement confirmé, fichiers effacés' WHERE id=?",(job_id,))
        return {'ok':True}
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
