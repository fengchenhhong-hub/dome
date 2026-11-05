"""Import only hash-verified, already-public portfolio assets once."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--destination', default='public')
parser.add_argument('--verify-only', action='store_true')
args = parser.parse_args()
root = Path(args.destination).resolve()
manifest = json.loads(Path('assets-manifest.json').read_text())

def valid(path, item):
    return path.is_file() and path.stat().st_size == item['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']

def import_one(item):
    relative = PurePosixPath(item['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid relative asset path')
    target = root.joinpath(*relative.parts)
    if valid(target, item):
        return
    if args.verify_only:
        raise ValueError('Missing or changed: ' + str(relative))
    request = Request(urljoin(manifest['source'], quote(str(relative))), headers={'User-Agent': 'Portfolio-Asset-Import/1.0'})
    with urlopen(request, timeout=90) as response:
        data = response.read(item['bytes'] + 1)
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Asset verification failed: ' + str(relative))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(import_one, manifest['files']))
print(f"Verified {len(manifest['files'])} files; {sum(f['bytes'] for f in manifest['files'])} bytes")
