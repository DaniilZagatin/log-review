"""Long public JSON fields must not make credential masking rescan every suffix."""
from pathlib import Path
import subprocess,sys,unittest
from process_observability.redaction import redact_text

class LongTokenRedactionControls(unittest.TestCase):
 def test_long_public_token_finishes_without_changing_text(self):
  src=str(Path(__file__).resolve().parents[1]/'src')
  code='import sys;sys.path.insert(0,sys.argv[1]);from process_observability.redaction import redact_text;s="a"*100000;assert redact_text(s)==s'
  subprocess.run([sys.executable,'-B','-c',code,src],check=True,timeout=3,capture_output=True)
 def test_prefixed_credential_keys_still_mask_values_and_keep_lines(self):
  for key in ('password','db_password','dbPassword','123secret','ключPassword','prefix-client-secret','someApiKey','domain.password','some-access-token'):
   with self.subTest(key=key):
    for quote in ('"',"'",''):
     raw=key+' = '+quote+'SYNTHETIC_CANARY'+quote+'\nordinary = 1\n';safe=redact_text(raw)
     self.assertNotIn('SYNTHETIC_CANARY',safe);self.assertEqual(safe.count('\n'),raw.count('\n'));self.assertIn(key,safe)
 def test_ordinary_identifiers_and_log_text_remain_unchanged(self):
  for text in ('myPasswordHint = "public"','counter = 42','logger.info("done")','"public": "'+('word '*1000)+'"'):
   self.assertEqual(redact_text(text),text)

if __name__=='__main__':unittest.main()
