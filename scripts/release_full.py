#!/usr/bin/env python3
"""Self-contained source release. Reads only own allowlisted files, never a baseline or private payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

PREFIX = 'process-observability/'
PROTOCOL = 'process-observability-full/1'
FIXED = {'run.py', 'pyproject.toml', 'README.md', 'AGENT-START.md', 'examples/decisions.json', 'tests/__init__.py',
         'src/process_observability/map_presentation/PROVENANCE.md',
         'src/process_observability/map_presentation/assets/dpss/PROVENANCE.md',
         'src/process_observability/map_presentation/assets/horizon/tokens.css'}
PATTERN = re.compile(r'src/process_observability/[A-Za-z0-9_]+\.py|tests/test_[A-Za-z0-9_]+\.py|'
                     r'src/process_observability/map_presentation/[A-Za-z0-9_]+\.py|'
                     r'src/process_observability/map_presentation/assets/dpss/SBSansDisplay-(?:400|600|700)\.woff2|'
                     r'(?:docs|plans)/[A-Za-z0-9_-]+\.md|scripts/(?:release_full|release_patch|test_offline)\.py|'
                     r'guidance/process-observability/(?:SKILL\.md|sources\.json|references/[A-Za-z0-9_-]+\.md)|'
                     r'(?:examples|tests/fixtures)/synthetic/[A-Za-z0-9_.-]+')
MANDATORY = {'run.py', 'pyproject.toml', 'README.md', 'AGENT-START.md', 'src/process_observability/__init__.py'}
GENERATED = {'config/model.example.json', 'RELEASE-MANIFEST.json', 'SHA256SUMS'}
SCAN = ('src/process_observability', 'tests', 'docs', 'plans', 'scripts', 'guidance/process-observability',
        'examples/synthetic', 'examples')


class ReleaseError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def allowed(path):
    return type(path) is str and (path in FIXED or PATTERN.fullmatch(path) is not None)


def regular(path):
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ReleaseError('Expected a regular file without symbolic links: ' + str(path))


def template():
    return {role: {'base_url': 'https://' + role + '.example.invalid/v1', 'model': 'SET_MODEL_ID',
                   'thinking': False, 'max_output_tokens': 65536, 'api_key_env': 'LLM_API_KEY'}
            for role in ('code', 'business')}


def payload(root):
    root=Path(root).absolute()
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise ReleaseError('Source directory must not traverse symbolic links')
    names=set(FIXED)
    for folder in SCAN:
        start=root/folder
        if start.is_symlink():
            raise ReleaseError('Selected source directory is a symbolic link')
        if not start.is_dir():
            continue
        for current, dirs, files in os.walk(start, followlinks=False):
            # Only descend through own selected source trees, never hidden/runtime directories.
            dirs[:]=[name for name in dirs if not name.startswith('.') and name!='__pycache__']
            for name in dirs:
                if (Path(current)/name).is_symlink():
                    raise ReleaseError('Symbolic link in selected source tree')
            for name in files:
                rel=(Path(current)/name).relative_to(root).as_posix()
                if allowed(rel): names.add(rel)
    result={}
    for name in sorted(names):
        path=root/name
        if path.exists() or path.is_symlink():
            regular(path);result[name]=path.read_bytes()
    if MANDATORY-set(result):
        raise ReleaseError('Missing required runtime files: '+', '.join(sorted(MANDATORY-set(result))))
    result['config/model.example.json']=(json.dumps(template(),indent=2)+'\n').encode()
    return result


def build(root, out):
    out=Path(out).absolute()
    if out.exists() or out.is_symlink(): raise ReleaseError('Output already exists')
    if any(p.is_symlink() for p in out.parents): raise ReleaseError('Output traverses symbolic links')
    files=payload(root)
    manifest={'protocol':PROTOCOL,'kind':'standalone','baseline_required':False,
              'files':{name:sha(data) for name,data in sorted(files.items())},
              'validation_scope':'Offline host implementation; no live semantic or corporate TLS acceptance.'}
    files['RELEASE-MANIFEST.json']=(json.dumps(manifest,indent=2)+'\n').encode()
    files['SHA256SUMS']=''.join(sha(data)+'  '+name+'\n' for name,data in sorted(files.items())).encode()
    out.parent.mkdir(parents=True,exist_ok=True)
    stream=out.open('xb')  # An exclusive-open failure must never remove somebody else's file.
    try:
        with stream, zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,data in sorted(files.items()):
                item=zipfile.ZipInfo(PREFIX+name,date_time=(2026,10,27,0,0,0))
                item.compress_type=zipfile.ZIP_DEFLATED;item.external_attr=(stat.S_IFREG|0o644)<<16
                archive.writestr(item,data)
    except BaseException:
        # Never remove a preexisting output; exclusive open above owns this file only after successful creation.
        if out.exists(): out.unlink()
        raise
    return verify(out)


def verify(path):
    regular(Path(path).absolute())
    try:
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)):raise ReleaseError('Duplicate archive names')
            files={}
            for item in archive.infolist():
                name=item.filename
                rel=name[len(PREFIX):] if name.startswith(PREFIX) else ''
                if (not rel or PurePosixPath(rel).as_posix()!=rel or '..' in PurePosixPath(rel).parts
                        or '\\' in rel or rel.startswith('/') or not (allowed(rel) or rel in GENERATED)
                        or stat.S_IFMT(item.external_attr>>16)!=stat.S_IFREG
                        or item.file_size>64*1024*1024):
                    raise ReleaseError('Unexpected or unsafe archive entry: '+name)
                files[rel]=archive.read(item)
            if (MANDATORY|GENERATED)-set(files):raise ReleaseError('Incomplete standalone runtime')
            manifest=json.loads(files['RELEASE-MANIFEST.json'])
            expected={name:sha(data) for name,data in sorted(files.items()) if name not in {'RELEASE-MANIFEST.json','SHA256SUMS'}}
            if (manifest.get('protocol')!=PROTOCOL or manifest.get('kind')!='standalone'
                    or manifest.get('baseline_required') is not False or manifest.get('files')!=expected):
                raise ReleaseError('Manifest content mismatch')
            sums=''.join(sha(data)+'  '+name+'\n' for name,data in sorted(files.items()) if name!='SHA256SUMS').encode()
            if files['SHA256SUMS']!=sums:raise ReleaseError('Payload checksum mismatch')
            if json.loads(files['config/model.example.json'])!=template():raise ReleaseError('Unexpected generated model template')
    except (KeyError,ValueError,zipfile.BadZipFile,UnicodeError) as exc:
        raise ReleaseError(str(exc)) from None
    return {'kind':'standalone','protocol':PROTOCOL,'files':len(files),'sha256':sha(Path(path).read_bytes()),'baseline_required':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    build_parser=sub.add_parser('build');build_parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));build_parser.add_argument('--out',required=True)
    verify_parser=sub.add_parser('verify');verify_parser.add_argument('--zip',required=True)
    args=parser.parse_args()
    try:result=build(args.root,args.out) if args.command=='build' else verify(args.zip)
    except (ReleaseError,OSError) as exc:parser.exit(1,str(exc)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
