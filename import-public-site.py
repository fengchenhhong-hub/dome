"""Import only hash-verified, already-public portfolio assets once."""
import argparse
import concurrent.futures
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--destination', default='public')
parser.add_argument('--verify-only', action='store_true')
parser.add_argument('--ffmpeg', default='ffmpeg')
parser.add_argument('--ffprobe', default='ffprobe')
args = parser.parse_args()
root = Path(args.destination).resolve()
manifest = json.loads(Path('assets-manifest.json').read_text())

def valid(path, item):
    return path.is_file() and path.stat().st_size == item['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']

def relative_path(item):
    relative = PurePosixPath(item['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid relative asset path')
    return relative

def download(item):
    relative = relative_path(item)
    request = Request(urljoin(manifest['source'], quote(str(relative))), headers={'User-Agent': 'Portfolio-Asset-Import/1.0'})
    with urlopen(request, timeout=90) as response:
        data = response.read(item['bytes'] + 1)
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Asset verification failed: ' + str(relative))
    return data

def import_one(item):
    relative = relative_path(item)
    target = root.joinpath(*relative.parts)
    if valid(target, item):
        return
    if args.verify_only:
        raise ValueError('Missing or changed: ' + str(relative))
    data = download(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(import_one, manifest['files']))

state_path = root / '.media-build.json'
state = json.loads(state_path.read_text()) if state_path.is_file() else {}

def probe_video(path, expected):
    result = subprocess.run([
        args.ffprobe, '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,codec_name,width,height,duration',
        '-of', 'json', str(path)
    ], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    audios = [s for s in data['streams'] if s['codec_type'] == 'audio']
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError('Derived MP4 must contain one video and one audio stream')
    video, audio = videos[0], audios[0]
    if (video['codec_name'], video['width'], video['height'], audio['codec_name']) != (
        expected['video_codec'], expected['width'], expected['height'], expected['audio_codec']
    ):
        raise ValueError('Derived MP4 codec or dimensions do not match')
    duration = float(data['format']['duration'])
    if abs(duration - expected['duration_s']) > expected['duration_tolerance_s']:
        raise ValueError('Derived MP4 duration does not match')
    if abs(float(video['duration']) - float(audio['duration'])) > .15:
        raise ValueError('Derived MP4 audio/video duration mismatch')
    return {'duration_s': duration, 'width': video['width'], 'height': video['height'],
            'video_codec': video['codec_name'], 'audio_codec': audio['codec_name']}

for item in manifest.get('derived', []):
    target = root.joinpath(*relative_path(item).parts)
    playlist_relative = relative_path({'path': item['source_playlist']})
    prefix = str(playlist_relative.parent) + '/'
    inputs = sorted((f for f in manifest['files'] if f['path'].startswith(prefix)), key=lambda f: f['path'])
    if not inputs or not any(f['path'] == item['source_playlist'] for f in inputs):
        raise ValueError('Derived MP4 source playlist is not in the verified manifest')
    fingerprint = hashlib.sha256(json.dumps({
        'method': 'ffmpeg-copy-faststart-v1', 'inputs': inputs,
        'source_playlist': item['source_playlist'], 'expected': item['expected']
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    cached = state.get(item['path'], {})
    reusable = cached.get('input_fingerprint') == fingerprint and valid(target, {
        'bytes': cached.get('bytes', -1), 'sha256': cached.get('sha256', '')
    })
    if reusable:
        probe_video(target, item['expected'])
        print('Reusing verified MP4: ' + item['path'])
        continue
    if args.verify_only:
        raise ValueError('Missing, stale or changed derived MP4: ' + item['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.remuxing.mp4')
    try:
        subprocess.run([
            args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
            '-protocol_whitelist', 'file,crypto,data', '-i', str(root.joinpath(*playlist_relative.parts)),
            '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy',
            '-movflags', '+faststart', '-f', 'mp4', str(temporary)
        ], check=True)
        metadata = probe_video(temporary, item['expected'])
        state[item['path']] = {
            'input_fingerprint': fingerprint, 'bytes': temporary.stat().st_size,
            'sha256': hashlib.sha256(temporary.read_bytes()).hexdigest(), 'media': metadata
        }
        temporary.replace(target)
        print('Generated verified MP4: ' + item['path'])
    finally:
        temporary.unlink(missing_ok=True)

if not args.verify_only and manifest.get('derived'):
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
print(f"Verified {len(manifest['files'])} assets and {len(manifest.get('derived', []))} derived videos")
