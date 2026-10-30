"""Run own offline tests with a synthetic model configuration; never open private project files."""
import argparse, json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch

parser=argparse.ArgumentParser()
parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--pattern',default='test_*.py')
args=parser.parse_args()
root=Path(args.root).resolve()
sys.dont_write_bytecode=True
sys.path[:0]=[str(root/'src'),str(root)]
os.chdir(root)
profile={
 'code':{'base_url':'http://code.fixture.invalid/v1','model':'GLM-5.3','thinking':False,'max_output_tokens':65536},
 'business':{'base_url':'https://business.fixture.invalid/v1','model':'DeepSeek-V4-Flash','thinking':False,
             'max_output_tokens':65536,'ca_bundle':'../certs/deepseek/ca.crt.txt',
             'client_cert':'../certs/deepseek/client.crt.txt','client_key':'../certs/deepseek/client.key.txt'}}
fixture=json.dumps(profile)
private_names={'config','certs','inputs','materials','references','runs'}
private_roots=[root/p for p in private_names]
original=Path.read_text
redirects=0
def read_text(path,*a,**kw):
    global redirects
    if path.absolute()==root/'config/model.corporate.json':
        redirects+=1
        return fixture
    return original(path,*a,**kw)
def guard(event,arguments):
    if event=='open' and arguments and isinstance(arguments[0],(str,bytes,os.PathLike)):
        name=Path(os.fsdecode(arguments[0])).absolute()
        if any(name==p or p in name.parents for p in private_roots):
            raise PermissionError('Offline tests must not read private project data')
sys.addaudithook(guard)
with patch.object(Path,'read_text',read_text):
    suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern=args.pattern)
    result=unittest.TextTestRunner(verbosity=1).run(suite)
print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
                  'skipped':len(result.skipped),'configuration':'synthetic fixture; private corporate config/TLS not tested',
                  'config_reads_redirected':redirects}))
raise SystemExit(0 if result.wasSuccessful() else 1)
