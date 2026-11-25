"""Download every section before selecting or rendering any Shorts."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import math
import os
import subprocess
import sys
import tempfile

import httpx
from backend.app.composition import detect
from backend.app.engine import duration, export_clip, frame, probe
from backend.app.highlights import select

SECTION = 1800


def sections(total):
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Durée de la vidéo indisponible")
    return [(start, min(start + SECTION, total)) for start in range(0, math.ceil(total), SECTION)]


def download(url, directory, progress):
    base = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--js-runtimes', 'node', '-f', 'bv[height<=480][vcodec^=avc1]+ba[ext=m4a]/b[height<=480][ext=mp4]']
    info = json.loads(subprocess.check_output(base + ['-J', '--', url]))
    if info.get('is_live'):
        raise ValueError("Le direct doit être terminé")
    total = float(info.get('duration') or 0)
    ranges = sections(total)

    def fetch(i):
        a,b = ranges[i]
        path = directory / f'part-{i:04}.mp4'
        for attempt in range(3):
            try:
                subprocess.run(base + ['--quiet','--no-progress','--abort-on-unavailable-fragments',
                    '--download-sections',f'*{a}-{b}','--force-keyframes-at-cuts',
                    '--merge-output-format','mp4','--force-overwrites','-o',str(path),'--',url],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                streams = {s['codec_type'] for s in probe(path)['streams']}
                if {'audio','video'} <= streams and abs(duration(path)-(b-a)) <= 1:
                    return path
                raise ValueError('Tranche incomplète')
            except (ValueError, RuntimeError, subprocess.SubprocessError):
                path.unlink(missing_ok=True)
                if attempt == 2:
                    raise RuntimeError(f'Échec du téléchargement de la tranche {i+1}/{len(ranges)}') from None

    with ThreadPoolExecutor(max_workers=min(len(ranges),16)) as pool:
        futures = [pool.submit(fetch,i) for i in range(len(ranges))]
        for count, future in enumerate(as_completed(futures),1):
            future.result()
            progress(count,len(futures))
    paths = [f.result() for f in futures]
    if len(paths)==1:
        return paths[0]
    listing = directory / 'parts.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in paths))
    source = directory / 'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','concat','-safe','1','-i',str(listing),
                    '-c','copy',str(source)],check=True)
    if abs(duration(source)-total)>2:
        raise RuntimeError('Durée du replay assemblé incorrecte')
    for path in paths:
        path.unlink()
    return source


def run(api, directory):
    j = api.get('').raise_for_status().json()
    def event(kind, **data):
        api.post('',json=dict(kind=kind,**data)).raise_for_status()
    def upload(name,path):
        with path.open('rb') as stream:
            api.put(name,content=stream).raise_for_status()
    try:
        source = download('https://www.youtube.com/watch?v='+j['key'],directory,
            lambda n,total:event('progress',message=f'Téléchargement {n}/{total} tranches',progress=.4*n/total))
        total = duration(source)
        a = j['start'] or 0
        b = total if j['finish'] is None else min(j['finish'],total)
        if not 0 <= a < b or (j['finish'] or 0)>total+.1:
            raise ValueError('Plage en dehors de la vidéo')
        opts = j['options']
        at = a+(b-a)*.05
        if opts['layout']!='standard' and not opts['camera']:
            opts['camera'],at = detect(source,a,b)
        if j['preview'] or (opts['layout']!='standard' and not opts['camera']):
            preview = directory/'preview.jpg'
            frame(source,preview,at)
            upload('preview',preview)
            event('preview',options=opts)
            return
        event('progress',progress=.45,message='Vidéo complète, recherche des passages')
        clips = j['redo'] or select(source,a,b,j['maximum'])
        for i,c in enumerate(clips):
            out = directory/'render.mp4'
            event('progress',progress=.5+.48*i/len(clips),message=f'Montage {i+1}/{len(clips)}')
            result = export_clip(source,out,c,[],opts,j['lang'])
            upload(str(c.get('ordinal',i)),out)
            c['duration'] = result['duration']
        event('done',clips=clips,finish=b)
    except Exception as exc:
        event('failed',message=str(exc)[:350] if isinstance(exc,(RuntimeError,ValueError)) else 'Échec du traitement sur GitHub Actions')
        raise


if __name__=='__main__':
    url = os.environ['STUDIO_URL'].rstrip('/')+'/api/replay/'+os.environ['TASK_ID']+'/'
    with tempfile.TemporaryDirectory() as tmp, httpx.Client(base_url=url,timeout=180,
            headers={'X-Replay-Key':os.environ['REPLAY_KEY']}) as api:
        run(api,Path(tmp))
