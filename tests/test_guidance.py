from pathlib import Path
import tempfile
import unittest

from process_observability.guidance import ROOT, load_guidance


class GuidanceTests(unittest.TestCase):
    def test_default_pack_loads_business_method_without_requiring_delivery(self):
        pack=load_guidance()
        self.assertEqual(pack.provenance["selection_status"],"not_declared")
        self.assertIn("FACT-QUESTION",pack.ids["map"])
        self.assertIn("FACT-SOURCE-CATALOG",pack.ids["map"])
        self.assertIn("FACT-METRIC-BASELINE",pack.ids["map"])
        for field in ('guid','date_time','сase_id','case_description','product_instance_id','product',
                      'operation','event','event_status','event_status_description','event_error_type',
                      'is_case_complete','is_case_success','employee_id','process_id','role','client_id',
                      'client_id_internal','message','channel','details'):
            for phase in ('map','instrumentation'):
                self.assertIn('`'+field+'`',pack.texts[phase])
        self.assertIn("MAP-ALTERNATIVES",pack.ids["map"])
        self.assertIn("INST-REUSE",pack.ids["instrumentation"])
        self.assertIn("METRIC-DEFINITION",pack.ids["metrics"])
        self.assertIn("METRIC-KGB",pack.ids["metrics"])
        self.assertIn("METRIC-RECONCILIATION",pack.ids["metrics"])
        self.assertIn("METRIC-FORMULAS",pack.ids["metrics"])
        self.assertIn("N_success(C_closed)",pack.texts["metrics"])
        self.assertIn("сопоставленных последовательных",pack.texts["metrics"])
        self.assertIn("формула сверки двух значений",pack.texts["metrics"])
        self.assertIn("общебанковский CSI",pack.texts["metrics"])
        self.assertIn("не готовая формула обязательного",pack.texts["metrics"])
        self.assertNotIn("DELIVERY-KAFKA",pack.ids["instrumentation"])
        self.assertFalse(any(name.endswith("kafka.md") for name in pack.fingerprints))
        self.assertLess(max(map(len,pack.texts.values())),14000)
        self.assertNotIn("snapshot_sha256",pack.texts["map"])

    def test_format_and_delivery_are_independent_explicit_selections(self):
        pack=load_guidance(event_formats=["logging"],deliveries=["kafka"])
        self.assertIn("FORMAT-LOGGING",pack.ids["map"])
        self.assertIn("DELIVERY-KAFKA",pack.ids["instrumentation"])
        self.assertNotIn("DELIVERY-KAFKA",pack.ids["map"])
        self.assertNotEqual(pack.provenance["pack_sha256"],load_guidance().provenance["pack_sha256"])
        self.assertEqual(pack.provenance["pack_sha256"],load_guidance(event_formats=["logging"],deliveries=["kafka"]).provenance["pack_sha256"])

    def test_missing_unknown_or_oversized_guidance_fails_explicitly(self):
        with self.assertRaises(ValueError):load_guidance(event_formats=["../../other"])
        with self.assertRaises(ValueError):load_guidance(max_phase_chars=1)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):load_guidance(root=directory)

    def test_business_only_edit_changes_the_pack_hash_but_not_the_technical_excerpt(self):
        import shutil
        from process_observability.workflow import technical_guidance
        original = load_guidance()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pack"
            shutil.copytree(ROOT, root)
            path = root / "references/map.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn("Подписи делай короткими", text)
            path.write_text(text.replace("Подписи делай короткими", "Подписи короткие"), encoding="utf-8")
            edited = load_guidance(root=root)
        self.assertNotEqual(edited.provenance["pack_sha256"], original.provenance["pack_sha256"])
        self.assertNotEqual(edited.texts["map"], original.texts["map"])  # business prompts do change
        self.assertEqual(technical_guidance(edited)[0], technical_guidance(original)[0])
        self.assertEqual(technical_guidance(edited)[1]["sha256"], technical_guidance(original)[1]["sha256"])

    def test_selected_references_cannot_escape_via_symlink(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"pack"
            shutil.copytree(ROOT,root)
            (root/"references/map.md").unlink()
            (root/"references/map.md").symlink_to(ROOT/"references/map.md")
            with self.assertRaises(ValueError):load_guidance(root=root)


if __name__ == "__main__":unittest.main()
