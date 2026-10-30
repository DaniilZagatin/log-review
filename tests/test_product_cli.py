"""Public commands expose one map producer and one complete product flow."""
import contextlib
import io
from pathlib import Path
import unittest
from process_observability.cli import build_parser, status_report

class ProductCommands(unittest.TestCase):
    def test_complete_product_modes_share_one_entry(self):
        for flag,value in [('--scenario-text','Scenario'),('--process-file','brief.txt'),('--map','map.json'),('--resume-from','previous')]:
            args=build_parser().parse_args(['onboard',flag,value,'--out','next'])
            self.assertEqual(args.command,'onboard')
            self.assertEqual(getattr(args,flag[2:].replace('-','_')),value)
            self.assertEqual(args.source,[])

    def test_source_list_is_repeatable(self):
        args=build_parser().parse_args(['onboard','--scenario-text','S','--out','next','--source','a','b.zip','--source','c'])
        self.assertEqual(args.source,['a','b.zip','c'])

    def test_removed_builder_is_neither_importable_nor_a_command(self):
        root=Path(__file__).resolve().parents[1]/'src/process_observability'
        for module in ('workflow','direct_atomic','action_refinement','code_review','review_reduction'):
            self.assertFalse((root/(module+'.py')).exists(),module)
        for command in ('analyze','demo','export-onboarding'):
            with self.subTest(command=command),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                build_parser().parse_args([command])
        self.assertEqual(status_report()['map_producer'],'belka')

    def test_product_does_not_advertise_ignored_scope_flags(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            build_parser().parse_args(['onboard','--scenario-text','S','--out','next','--include','*.py'])

    def test_offline_export_requires_only_saved_run_and_destination(self):
        args=build_parser().parse_args(['export','--run','previous','--out','html'])
        self.assertEqual(args.command,'export')
        self.assertFalse(hasattr(args,'config'))
