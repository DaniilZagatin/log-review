"""End-to-end CLI checks. All HTTP calls are replaced by an in-memory transport."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
import os
import ssl
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from process_observability.cli import UserInputError, _outside_repository, main
from process_observability.demo import RecordedModel
from process_observability.llm import TransportResponse
from process_observability.source_tools import snapshot_repository

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/synthetic"
# Response bodies of the synthetic replay, selected by task. The demo keeps its strict fingerprint lookup;
# live analyze uses the new review protocol, so its requests can never match those fingerprints.
_RECORDS = list(json.loads((EXAMPLE / "model-responses.json").read_text(encoding="utf-8"))["records"].values())
DEMO = {task: next(record for record in _RECORDS if key in record)
        for task, key in (("code", "findings"), ("TASK: map", "stages"), ("TASK: instrumentation", "cards"),
                          ("TASK: metrics", "metrics"))}


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(map(str, args)))
    return code, out.getvalue(), err.getvalue()


def review_reply(brief):
    """New-protocol code reply: the demo's function-level findings that lie inside the owned intervals, plus an
    empty relation array whenever the brief asks for technical relations (atomic default)."""
    owned = brief["owned_intervals"]
    findings = [{"id": item["id"], "kind": "logging", "description": item["description"], "refs": item["refs"]}
                for item in DEMO["code"]["findings"]
                if any(part["path"] == ref["path"] and part["start_line"] <= ref["start_line"]
                       and ref["end_line"] <= part["end_line"] for part in owned for ref in item["refs"][:1])]
    reply = {"findings": findings, "requests": [],
             "interval_status": [{"interval_id": part["id"], "status": "reviewed", "note": ""} for part in owned],
             "unknowns": DEMO["code"]["unknowns"] if any(part["path"] == "app.py" for part in owned) else []}
    return {**reply, "relations": []} if "relation_kinds" in brief else reply


def envelope(response):
    return TransportResponse(200, json.dumps({"choices": [{"finish_reason": "stop",
                                                           "message": {"content": json.dumps(response)}}]}).encode())


class Transport:
    """Thread-safe mock HTTP: review replies for code tasks, demo bodies for business tasks unless overridden.

    An override may be a list of replies used in order; its last reply then repeats.
    """

    def __init__(self, business=None):
        self.business, self.requests, self.lock = business or {}, [], threading.Lock()

    def __call__(self, url, payload, headers, context):
        with self.lock:
            self.requests.append((url, payload, headers, context))
            system, user = (message["content"] for message in payload["messages"])
            task = system.split("\n", 1)[0]
            answer = self.business.get(task)
            if type(answer) is list:
                answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if answer is not None:
            return answer if isinstance(answer, TransportResponse) else envelope(answer)
        if task == "TASK: code":
            return envelope(review_reply(json.loads(user)))
        if task == "TASK: code-reduce":
            items = json.loads(user)["items"]
            return envelope({"summaries": [{"description": "Приём, проверка, исполнение и отмена заявки.",
                                            "source_ids": [item["id"] for item in items]}]})
        return envelope(DEMO[task])

    def tasks(self):
        return [payload["messages"][0]["content"].split("\n", 1)[0] for _, payload, _, _ in self.requests]


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        # TLS file loading has dedicated adapter checks; CLI tests need no real credentials.
        tls=patch('process_observability.llm._build_ssl_context',
                  side_effect=lambda config:ssl.create_default_context() if config.base_url.startswith('https:') else None)
        tls.start();self.addCleanup(tls.stop)

    def test_demo_writes_all_outputs_and_keeps_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            code, out, err = invoke("demo", "--out", output)
            self.assertEqual(code, 0, err)
            self.assertEqual(Path(json.loads(out)["report"]), output / "report.html")
            self.assertEqual(set(p.name for p in output.iterdir()), {
                "manifest.json", "process_map.json", "log_coverage.json", "instrumentation_plan.json",
                "metric_catalog.json", "evidence.jsonl", "bundle.json", "report.html", "report.md", "diagnostics.json", "trace.jsonl"})
            bundle = json.loads((output / "bundle.json").read_text())
            self.assertEqual((bundle["manifest"]["data_origin"], bundle["manifest"]["model_mode"]), ("synthetic", "fake"))
            facts = {f["id"]: f for f in bundle["log_coverage"]["facts"]}
            self.assertEqual(facts["cancel.result"]["delivery"], "unknown")
            self.assertEqual(facts["fulfil.result"]["event_keys"], ["fulfil.completed"])
            self.assertTrue(all(m["value"] is None for m in bundle["metric_catalog"]["metrics"]))
            page = (output / "report.html").read_text()
            for text in ("<svg", "Отмена заявки", "Место в коде", "Сценарии и операции"):
                self.assertIn(text, page)
            self.assertEqual(bundle["logging_inventory"]["scope"]["sites_found"],7)
            self.assertEqual(page.count("<script>"),1)

            self.assertNotIn("ground_truth", (output / "bundle.json").read_text())
            events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
            # The demo stays the strict legacy four-call replay, never the review path.
            self.assertEqual([row["data"]["task"] for row in events if row["event"] == "model.request"],
                             ["code", "map", "instrumentation", "metrics"])
            self.assertEqual(bundle["validation"]["policy"]["business_max_attempts"], 1)
            self.assertEqual({stage: record["outcome"] for stage, record in bundle["validation"]["stages"].items()},
                             dict.fromkeys(("map", "coverage", "instrumentation", "metrics"), "accepted"))

    def test_invalid_digest_budget_is_rejected_before_reading_a_model_config(self):
        for value in ("999", "240000", "240001"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                with patch("process_observability.cli._model_configs") as configs:
                    code, _, err = invoke("analyze", "--repo", EXAMPLE, "--out", Path(directory) / "run",
                                          "--digest-chars", value)
                self.assertEqual(code, 2)
                self.assertIn("--digest-chars must be at least 1000", err)
                configs.assert_not_called()

    def test_demo_is_reproducible_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "left", Path(directory) / "right"
            for output in (left, right):
                self.assertEqual(invoke("demo", "--out", output)[0], 0)
            for file in left.iterdir():
                if file.name == "trace.jsonl":continue
                self.assertEqual(file.read_bytes(), (right / file.name).read_bytes(), file.name)
            sentinel = left / "keep.txt"
            sentinel.write_text("preserve")
            code, _, err = invoke("demo", "--out", left)
            self.assertEqual(code, 2)
            self.assertIn("already exists", err)
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_live_review_pipeline_with_mock_http_and_corporate_config(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.urllib_transport", side_effect=transport):
            output = Path(directory) / "live-mocked"
            code, out, err = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--business-file", EXAMPLE / "business.md",
                                    "--out", output, "--reduction-workers", "10", "--split-invalid-reduction-batches",
                                    "--map-mode", "coarse-refine")
            self.assertEqual(code, 0, err)
            self.assertFalse(json.loads(out)["partial_input"])
            bundle = json.loads((output / "bundle.json").read_text())
            self.assertEqual(bundle["manifest"]["model_mode"], "live")
            self.assertEqual(json.loads((output / "code_reduction.json").read_text())["accounting"]["workers"], 10)
            self.assertTrue(json.loads((output / "code_reduction.json").read_text())["accounting"]["split_invalid_batches"])
            self.assertEqual(bundle["code_reduction"]["accounting"]["digest_budget"], 180000)
            self.assertTrue(all(stage["status"] == "completed" for stage in bundle["manifest"]["stages"].values()))
            self.assertEqual(transport.tasks(), ["TASK: code", "TASK: map", "TASK: instrumentation", "TASK: metrics"])
            config = json.loads((ROOT / "config/model.corporate.json").read_text())
            for index,(url, payload, headers, _) in enumerate(transport.requests):
                role='code' if index==0 else 'business'
                profile=config[role]
                self.assertEqual(url, profile["base_url"].rstrip("/") + "/chat/completions")
                self.assertEqual(payload["model"], profile["model"])
                self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": profile['thinking']})
                self.assertEqual(payload["max_tokens"], profile["max_output_tokens"])
                self.assertNotIn("Authorization", headers)
                system, user = (message["content"] for message in payload["messages"])
                if role=='code':
                    brief = json.loads(user)
                    self.assertIn("FACT-SOURCE-CATALOG", system)
                    self.assertNotIn("TASK: map", system)
                    self.assertLessEqual(len(system) + len(user), 32000)
                    owned = sorted(line for item in brief["owned_intervals"]
                                   for line in range(item["start_line"], item["end_line"] + 1))
                    self.assertEqual(owned, list(range(1, 37)))
                else:
                    context = json.loads(user)
                    self.assertNotIn('source', context)
                    self.assertNotIn('emit("request.received", case_id=case_id', user)
                    self.assertLessEqual(len(system) + len(user), 240000)
            mapping = json.loads(transport.requests[1][1]["messages"][1]["content"])
            self.assertEqual(len(mapping["code_digest"]), 6)
            self.assertIn("logging_event_keys", mapping["context_omissions"])
            for name in ("code_graph.json", "code_review.json", "code_reduction.json", "logging_inventory.json"):
                self.assertTrue((output / name).is_file(), name)
            self.assertTrue((output / "review" / "run.json").is_file())
            review = json.loads((output / "code_review.json").read_text())
            self.assertTrue(review["complete"])
            self.assertEqual(len(review["findings"]), 4)
            residual = bundle["code_reduction"]["residual"]
            self.assertEqual(residual["unknowns"], 2)
            self.assertTrue(all(any(question in text for text in bundle["process_map"]["unknowns"])
                                for question in DEMO["code"]["unknowns"]))
            trace=[json.loads(line) for line in (output/'trace.jsonl').read_text().splitlines()]
            requests = [row['data'] for row in trace if row['event']=='model.request']
            self.assertEqual([row['role'] for row in requests], ['code','business','business','business'])
            self.assertEqual({row['data']['api_request_sha256'] for row in trace if row['event']=='api.response'},
                             {row['api_request_sha256'] for row in requests})

    def test_live_http_failure_keeps_incomplete_report_and_safe_diagnostics(self):
        secret_body = b'UPSTREAM-PRIVATE-RESPONSE token=NEVER-SAVE'
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.urllib_transport", return_value=TransportResponse(503, secret_body)) as http:
            output = Path(directory) / "failed"
            code, _, _ = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--out", output)
            self.assertEqual(code, 1)
            self.assertEqual(http.call_count, 1)  # the failed code task is not retried and no business call follows
            diagnostics = json.loads((output / "diagnostics.json").read_text())
            self.assertEqual(diagnostics["model_calls"][0]["failure"], {"category": "http", "status": 503})
            self.assertTrue(all(s["status"] == "incomplete" for s in diagnostics["stage_states"].values()))
            self.assertIn("no accepted task", diagnostics["stage_states"]["map"]["blockers"][0])
            review = json.loads((output / "code_review.json").read_text())
            self.assertEqual([(error["category"], error["status_code"]) for error in review["errors"]], [("http", 503)])
            files = [file for file in output.rglob("*") if file.is_file()]
            self.assertTrue(any(file.parent.name == "tasks" for file in files))
            for file in files:
                self.assertNotIn("UPSTREAM-PRIVATE-RESPONSE", file.read_text())
                self.assertNotIn("NEVER-SAVE", file.read_text())

    def test_check_model_sends_no_repository_content(self):
        response = TransportResponse(200, b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}')
        with patch("process_observability.llm.urllib_transport", return_value=response) as http:
            code, out, err = invoke("check-model")
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["ok"])
            self.assertEqual(http.call_count,2)
            payload = http.call_args.args[1]
            self.assertNotIn("source", json.dumps(payload))
            self.assertIn("no repository content", payload["messages"][1]["content"])

    def test_business_role_failure_never_falls_back_to_the_code_model(self):
        transport=Transport({'TASK: map':TransportResponse(503,b'PRIVATE-ERROR')})
        with tempfile.TemporaryDirectory() as directory,patch('process_observability.llm.urllib_transport',side_effect=transport):
            output=Path(directory)/'failed-business'
            code,_,_=invoke('analyze','--repo',EXAMPLE,'--include','app.py','--business-file',EXAMPLE/'business.md','--out',output,
                            '--map-mode','coarse-refine')
            self.assertEqual(code,1)
            self.assertEqual([payload['model'] for _,payload,_,_ in transport.requests],['GLM-5.3','DeepSeek-V4-Flash'])
            self.assertTrue((output/'report.html').exists())
            diagnostics=json.loads((output/'diagnostics.json').read_text())
            self.assertEqual(diagnostics['model_calls'][1]['role'],'business')
            self.assertEqual(diagnostics['model_calls'][1]['failure']['status'],503)
            # HTTP failures are transport errors: recorded, never sent again as a validation repair.
            [attempt]=diagnostics['validation']['stages']['map']['attempts']
            self.assertEqual((attempt['layer'],attempt['code'],attempt['status'],attempt['retry']),
                             ('transport','http',503,'skipped_not_repairable'))

    def test_live_business_stage_gets_one_traced_repair_without_rerunning_code_review(self):
        rejected = json.loads(json.dumps(DEMO["TASK: map"]))
        rejected["event_mappings"][0]["event_key"] = None
        transport = Transport({"TASK: map": [rejected, DEMO["TASK: map"]]})
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.urllib_transport", side_effect=transport):
            output = Path(directory) / "repaired"
            code, _, err = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--business-file",
                                  EXAMPLE / "business.md", "--out", output, "--map-mode", "coarse-refine")
            self.assertEqual(code, 0, err)
            self.assertEqual(transport.tasks(), ["TASK: code", "TASK: map", "TASK: map", "TASK: instrumentation",
                                                 "TASK: metrics"])
            bundle = json.loads((output / "bundle.json").read_text())
            self.assertTrue(all(stage["status"] == "completed" for stage in bundle["manifest"]["stages"].values()))
            first, repair = (json.loads(payload["messages"][1]["content"]) for _, payload, _, _ in transport.requests[1:3])
            feedback = repair.pop("validation_feedback")
            self.assertEqual(repair, first)
            [error] = feedback["errors"]
            self.assertEqual((error["layer"], error["code"], error["path"], error["expected"], error["actual_type"]),
                             ("schema", "null_not_allowed", "event_mappings[0].event_key", "str", "null"))
            diagnostics = json.loads((output / "diagnostics.json").read_text())
            self.assertEqual(diagnostics["validation"], bundle["validation"])
            attempts = diagnostics["validation"]["stages"]["map"]["attempts"]
            self.assertEqual([(item["attempt"], item["outcome"]) for item in attempts], [(1, "rejected"), (2, "accepted")])
            self.assertEqual((attempts[0]["code"], attempts[0]["path"], attempts[0]["retry"]),
                             ("null_not_allowed", "event_mappings[0].event_key", "scheduled"))
            trace = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
            responses = {row["data"]["api_request_sha256"] for row in trace if row["event"] == "api.response"}
            self.assertLessEqual({item["api_request_sha256"] for item in attempts}, responses)
            [issue] = [row["data"] for row in trace if row["event"] == "validation.issue"]
            self.assertEqual((issue["stage"], issue["attempt"], issue["code"], issue["api_request_sha256"]),
                             ("map", 1, "null_not_allowed", attempts[0]["api_request_sha256"]))
            review = json.loads((output / "code_review.json").read_text())
            self.assertEqual((review["ledger"]["summary"]["model_calls"], review["ledger"]["summary"]["repairs"]), (1, 0))

    def test_tls_paths_resolve_from_the_config_directory(self):
        from process_observability.cli import _model_configs
        business=_model_configs(ROOT/'config/model.corporate.json')['business']
        self.assertTrue(business.base_url.startswith('https://'))
        self.assertEqual(Path(business.client_key),ROOT/'certs/deepseek/client.key.txt')
        self.assertEqual(Path(business.ca_bundle),ROOT/'certs/deepseek/ca.crt.txt')

    def test_certificate_key_cannot_be_used_as_business_input(self):
        with tempfile.TemporaryDirectory() as directory:
            key=Path(directory)/'client.key.txt';key.write_text('PRIVATE-INPUT-DO-NOT-READ')
            output=Path(directory)/'rejected'
            code,_,_=invoke('analyze','--repo',EXAMPLE,'--include','app.py','--business-file',key,'--out',output)
            self.assertEqual(code,2)
            self.assertNotIn('PRIVATE-INPUT-DO-NOT-READ',(output/'trace.jsonl').read_text())

    def test_changed_demo_request_fails_without_fallback(self):
        replay = RecordedModel(EXAMPLE / "model-responses.json")
        with self.assertRaisesRegex(ValueError, "replay miss"):
            replay.complete_json("TASK: map", "changed input")

    def test_interrupt_preserves_prompt_trace_and_feedback(self):
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.urllib_transport",side_effect=KeyboardInterrupt):
            output=Path(directory)/"interrupted"
            code,_,_=invoke("analyze","--repo",EXAMPLE,"--include","app.py","--out",output)
            self.assertEqual(code,130)
            events=[json.loads(line)["event"] for line in (output/"trace.jsonl").read_text().splitlines()]
            self.assertIn("model.request",events)
            self.assertEqual(events[-1],"run.interrupted")
            archive=Path(directory)/"feedback.zip"
            feedback_code,_,notice=invoke("feedback","--run",output,"--out",archive)
            self.assertEqual(feedback_code,0)
            self.assertIn('selected source code and business text',notice)
            self.assertTrue(archive.is_file())

    def test_discovery_runs_without_model_or_runtime_logs(self):
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.OpenAICompatibleModel",side_effect=AssertionError("No model expected")):
            output=Path(directory)/"discovery"
            code,out,err=invoke("discover-logging","--repo",EXAMPLE,"--include","app.py","--out",output)
            self.assertEqual(code,0,err)
            self.assertEqual(json.loads(out)["sites_found"],7)
            self.assertTrue((output/"logging_inventory.json").is_file())

    @staticmethod
    def make_repo(directory, files):
        root = Path(directory) / "repo"
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def test_discovery_is_uncapped_by_default(self):
        files = {f"m{index:02d}.py": "".join(f'log.info("evt.{index}.{step}")\n' for step in range(45)) for index in range(10)}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "discovery"
            code, out, err = invoke("discover-logging", "--repo", self.make_repo(directory, files), "--out", output)
            self.assertEqual(code, 0, err)
            scope = json.loads(out)
            # The legacy library caps (300 sites, 40 per file) would have truncated this inventory.
            self.assertEqual((scope["sites_found"], scope["sites_returned"], scope["truncated"]), (450, 450, False))

    def test_discovery_of_a_partial_scope_keeps_the_inventory_and_exits_one(self):
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.OpenAICompatibleModel",
                                                               side_effect=AssertionError("No model expected")):
            root = self.make_repo(directory, {"app.py": 'log.info("order.submitted")\n'})
            (root / "binary.py").write_bytes(b"value = 1\x00\n")
            refused = Path(directory) / "refused"
            code, out, err = invoke("discover-logging", "--repo", root, "--out", refused)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("binary.py", err)
            self.assertFalse(refused.exists())
            output = Path(directory) / "partial"
            code, out, err = invoke("discover-logging", "--repo", root, "--out", output, "--allow-partial")
            self.assertEqual(code, 1, err)
            self.assertTrue(json.loads(out)["partial_input"])
            inventory = json.loads((output / "logging_inventory.json").read_text(encoding="utf-8"))
            self.assertTrue(inventory["source_scope"]["partial_input"])
            self.assertEqual(list(inventory["input_fingerprints"]), ["app.py"])
            self.assertEqual(inventory["scope"]["sites_found"], 1)

    def test_output_inside_the_target_repository_is_refused_before_any_write_or_call(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("process_observability.cli._whole_snapshot", side_effect=AssertionError("no snapshot")), \
                patch("process_observability.cli._model_configs", side_effect=AssertionError("no model config")), \
                patch("process_observability.llm.OpenAICompatibleModel", side_effect=AssertionError("no model client")), \
                patch("process_observability.llm.urllib_transport", side_effect=AssertionError("no model call")):
            base = Path(directory)
            root = self.make_repo(directory, {"app.py": 'log.info("order.submitted")\n'})
            before = sorted(path.relative_to(base).as_posix() for path in base.rglob("*"))
            cases = {"direct": (root, root / "audit"), "nested": (root, root / "a" / "b"),
                     "repository itself": (root, root),
                     "dot-dot": (root, base / "elsewhere" / ".." / "repo" / "audit")}
            try:
                os.symlink(root, base / "alias", target_is_directory=True)
            except (OSError, NotImplementedError):
                pass  # symlink aliases are unavailable here; the other paths still apply
            else:
                before = sorted(path.relative_to(base).as_posix() for path in base.rglob("*"))
                cases.update({"symlinked output parent": (root, base / "alias" / "audit"),
                              "symlinked repository": (base / "alias", root / "audit")})
            upper = base / "REPO"
            if upper.exists() and os.path.samefile(upper, root):
                cases["case variant"] = (root, upper / "audit")  # case-insensitive file system
            for label, (repo, out) in cases.items():
                for command in (("analyze", "--plan-only"), ("analyze",), ("discover-logging",)):
                    with self.subTest(label=label, command=command[0], options=command[1:]):
                        code, stdout, err = invoke(command[0], "--repo", repo, "--out", out, *command[1:])
                        self.assertEqual((code, stdout), (2, ""))
                        self.assertIn("choose an output directory outside the repository", err)
                        self.assertEqual(sorted(path.relative_to(base).as_posix() for path in base.rglob("*")), before)

    def test_output_outside_the_target_repository_stays_valid_and_out_of_later_snapshots(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("process_observability.llm.urllib_transport", side_effect=AssertionError("no model call")):
            root = self.make_repo(directory, {"app.py": 'log.info("order.submitted")\n'})
            before = snapshot_repository(root, max_files=None, max_chars=None).fingerprints
            # A sibling whose name extends the repository name is still outside it.
            plan, inventory = Path(directory) / "repo-audit", Path(directory) / "repo-inventory"
            code, _, err = invoke("analyze", "--repo", root, "--out", plan, "--plan-only")
            self.assertEqual(code, 0, err)
            code, _, err = invoke("discover-logging", "--repo", root, "--out", inventory)
            self.assertEqual(code, 0, err)
            self.assertTrue((plan / "code_review.json").is_file())
            self.assertTrue((inventory / "logging_inventory.json").is_file())
            self.assertEqual(snapshot_repository(root, max_files=None, max_chars=None).fingerprints, before)
            # The documented runs/ location of this workspace is valid for another target repository.
            _outside_repository(root, ROOT / "runs" / "pilot")
            with self.assertRaises(UserInputError):
                _outside_repository(root, root / "runs" / "pilot")

    def test_plan_only_covers_whole_scope_without_model_client_or_call(self):
        files = {f"pkg{index % 5}/mod_{index:03d}.py": "".join(
            f"def handler_{index}_{step}(value):\n    return value + {step}  # {'p' * 60}\n\n" for step in range(18))
            for index in range(70)}
        with tempfile.TemporaryDirectory() as directory, \
                patch("process_observability.cli._model_configs", side_effect=AssertionError("no model config")), \
                patch("process_observability.llm.OpenAICompatibleModel", side_effect=AssertionError("no model client")), \
                patch("process_observability.llm.urllib_transport", side_effect=AssertionError("no model call")) as http:
            root = self.make_repo(directory, files)
            output = Path(directory) / "plan"
            code, out, err = invoke("analyze", "--repo", root, "--out", output, "--plan-only")
            self.assertEqual(code, 0, err)
            self.assertEqual(http.call_count, 0)
            self.assertGreater(json.loads(out)["tasks"], 1)
            self.assertEqual({path.name for path in output.iterdir()},
                             {"trace.jsonl", "code_graph.json", "code_review.json", "logging_inventory.json"})
            plan = json.loads((output / "code_review.json").read_text())
            self.assertEqual(plan["mode"], "plan_only")
            self.assertEqual(plan["summary"]["snapshot_files"], 70)
            self.assertGreater(plan["summary"]["request_chars_total"], 120000)
            self.assertTrue(plan["summary"]["planned_coverage_complete"])
            lines = {}
            for interval in plan["intervals"]:
                lines.setdefault(interval["path"], []).extend(range(interval["start_line"], interval["end_line"] + 1))
            self.assertEqual({path: sorted(found) for path, found in lines.items()},
                             {path: list(range(1, text.count("\n") + 1)) for path, text in files.items()})
            for task in plan["tasks"]:
                self.assertLessEqual(task["request_chars"], 32000)
                self.assertNotIn("user", task)
                self.assertNotIn("system", task)
            events = [json.loads(line)["event"] for line in (output / "trace.jsonl").read_text().splitlines()]
            self.assertIn("review.plan_only", events)
            self.assertNotIn("model.request", events)

    def test_resume_reuses_review_checkpoints_and_refuses_changed_options(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second, third = (Path(directory) / name for name in ("first", "second", "third"))
            failing = Transport({"TASK: map": TransportResponse(503, b"down")})
            with patch("process_observability.llm.urllib_transport", side_effect=failing):
                code, _, _ = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--out", first,
                                    "--map-mode", "coarse-refine")
            self.assertEqual(code, 1)
            self.assertEqual(failing.tasks(), ["TASK: code", "TASK: map"])
            self.assertTrue((first / "code_review.json").is_file())
            before = {path: path.read_bytes() for path in (first / "review").rglob("*") if path.is_file()}
            resumed = Transport()
            with patch("process_observability.llm.urllib_transport", side_effect=resumed):
                code, _, err = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--out", second,
                                      "--resume-from", first, "--map-mode", "coarse-refine")
            self.assertEqual(code, 0, err)
            self.assertEqual(resumed.tasks(), ["TASK: map", "TASK: instrumentation", "TASK: metrics"])
            review = json.loads((second / "code_review.json").read_text())
            self.assertEqual((review["ledger"]["summary"]["cache_hits"], review["ledger"]["summary"]["model_calls"]), (1, 0))
            self.assertEqual({path: path.read_bytes() for path in (first / "review").rglob("*") if path.is_file()}, before)
            refused = Transport()
            with patch("process_observability.llm.urllib_transport", side_effect=refused):
                code, _, err = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--out", third,
                                      "--resume-from", first, "--review-request-chars", "30000")
            self.assertEqual(code, 2)
            self.assertIn("Resume refused", err)
            self.assertIn("options", err)
            self.assertEqual(refused.requests, [])

    def test_partial_scope_aborts_unless_allowed_and_then_exits_one_with_completed_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(directory, {"app.py": (EXAMPLE / "app.py").read_text(encoding="utf-8"),
                                              "big.py": "# big\n" * 1000})
            transport = Transport()
            with patch("process_observability.llm.urllib_transport", side_effect=transport):
                code, _, err = invoke("analyze", "--repo", root, "--out", Path(directory) / "strict", "--max-file-bytes", "3000")
                self.assertEqual(code, 2)
                self.assertIn("big.py", err)
                self.assertEqual(transport.requests, [])
                output = Path(directory) / "partial"
                code, out, err = invoke("analyze", "--repo", root, "--out", output, "--max-file-bytes", "3000",
                                        "--allow-partial", "--map-mode", "coarse-refine")
            self.assertEqual(code, 1, err)
            self.assertTrue(json.loads(out)["partial_input"])
            bundle = json.loads((output / "bundle.json").read_text())
            self.assertTrue(all(stage["status"] == "completed" for stage in bundle["manifest"]["stages"].values()))
            self.assertIn("resource_unsupported_inputs", bundle["code_review"]["incomplete_reasons"])
            self.assertTrue(bundle["scope"]["partial_input"])

    def test_business_budget_is_checked_before_the_call_and_review_artifacts_remain(self):
        transport = Transport()
        with tempfile.TemporaryDirectory() as directory, patch("process_observability.llm.urllib_transport", side_effect=transport):
            output = Path(directory) / "budget"
            code, _, _ = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py", "--out", output,
                                "--business-request-chars", "5000", "--digest-chars", "1250")
            self.assertEqual(code, 1)
            self.assertEqual(transport.tasks(), ["TASK: code", "TASK: code-reduce"])
            bundle = json.loads((output / "bundle.json").read_text())
            self.assertIn("explicit budget", bundle["manifest"]["stages"]["map"]["blockers"][0])
            reduction = json.loads((output / "code_reduction.json").read_text())
            self.assertEqual((len(reduction["digest"]), reduction["accounting"]["leaf_count"]), (1, 6))
            self.assertEqual(len(reduction["residual"]["leaf_ids"]), reduction["accounting"]["salient_leaf_count"])

    def test_minimum_business_budget_is_rejected_before_any_client_is_created(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "process_observability.llm.OpenAICompatibleModel", side_effect=AssertionError("No client expected")):
            code, _, error = invoke("analyze", "--repo", EXAMPLE, "--include", "app.py",
                                    "--out", Path(directory) / "run", "--business-request-chars", "1000")
            self.assertEqual(code, 2)
            self.assertIn("--business-request-chars must be greater than 1000", error)

    def test_export_onboarding_writes_a_new_packet_without_model_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("process_observability.llm.OpenAICompatibleModel", side_effect=AssertionError("no model client")), \
                patch("process_observability.llm.urllib_transport", side_effect=AssertionError("no model call")):
            run, packet = Path(directory) / "demo", Path(directory) / "packet"
            self.assertEqual(invoke("demo", "--out", run)[0], 0)
            code, out, err = invoke("export-onboarding", "--run", run, "--out", packet)
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual((result["status"], result["run_id"]), ("all_stages_completed", "synthetic-demo"))
            self.assertEqual(sorted(path.name for path in packet.iterdir()), result["files"])
            before = {path.name: path.read_bytes() for path in packet.iterdir()}
            code, _, err = invoke("export-onboarding", "--run", run, "--out", packet)
            self.assertEqual(code, 2)
            self.assertIn("already exists", err)
            self.assertEqual({path.name: path.read_bytes() for path in packet.iterdir()}, before)
            code, _, err = invoke("export-onboarding", "--run", Path(directory) / "missing", "--out", Path(directory) / "other")
            self.assertEqual(code, 2)
            self.assertFalse((Path(directory) / "other").exists())

    def test_export_onboarding_of_a_partial_scope_run_exits_one_with_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(directory, {"app.py": (EXAMPLE / "app.py").read_text(encoding="utf-8"),
                                              "big.py": "# big\n" * 1000})
            run = Path(directory) / "partial"
            with patch("process_observability.llm.urllib_transport", side_effect=Transport()):
                code, _, err = invoke("analyze", "--repo", root, "--out", run, "--max-file-bytes", "3000", "--allow-partial",
                                      "--map-mode", "coarse-refine")
            self.assertEqual(code, 1, err)
            code, out, err = invoke("export-onboarding", "--run", run, "--out", Path(directory) / "packet")
            self.assertEqual(code, 1, err)
            result = json.loads(out)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["files"], ["SHA256SUMS", "events.csv", "logging-changes.csv", "metrics.csv",
                                               "onboarding.json", "process.md"])
            document = json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))
            self.assertTrue(document["scope"]["partial_input"])
            self.assertTrue(any(reason.startswith("Code review incomplete: ") and "resource_unsupported_inputs" in reason
                                for reason in document["partial_reasons"]))
            self.assertIn("resource_unsupported_inputs", document["technical_review"]["incomplete_reasons"])
            self.assertNotIn("exclusions", document["scope"]["source"])
            self.assertIn("Область исходников: неполная", (Path(result["export"]) / "process.md").read_text(encoding="utf-8"))


CART = ("import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
        "def add_item(cart, item):\n    cart.items.append(item)\n    cart.save()\n"
        "    logger.info(\"cart.item_added\", cart_id=cart.pk)\n    return cart\n\n\n"
        "def remove_item(cart, item):\n    cart.items.remove(item)\n    cart.save()\n"
        "    logger.info(\"cart.item_removed\", cart_id=cart.pk)\n    return cart\n")
CART_FINDINGS = [{"id": "add", "kind": "entry_point", "description": "Добавление позиции в корзину и сохранение.",
                  "refs": [{"path": "shop/cart.py", "start_line": 6, "end_line": 10, "symbol": "add_item"}]},
                 {"id": "remove", "kind": "entry_point", "description": "Удаление позиции из корзины и сохранение.",
                  "refs": [{"path": "shop/cart.py", "start_line": 13, "end_line": 17, "symbol": "remove_item"}]}]
CART_MAP = {"process": {"purpose": "Покупка", "boundaries": None, "case_definition": "Одна корзина"},
            "stages": [{"id": "cart", "name": "Корзина", "roles": [], "systems": [], "refs": []}], "transitions": [],
            "operations": [{"id": "cart-step", "name": "Работа с корзиной", "stage_id": "cart", "kind": "operation",
                            "description": None, "actor": None, "event_keys": [], "refs": []}],
            "operation_transitions": [], "event_mappings": [], "facts": [], "unknowns": [],
            "scenarios": [{"id": "buy", "name": "Покупка", "description": None, "operation_ids": ["cart-step"],
                           "transition_ids": []}]}


def cart_code(brief):
    owned = brief["owned_intervals"]
    return {"findings": [item for item in CART_FINDINGS if any(
                part["path"] == item["refs"][0]["path"] and part["start_line"] <= item["refs"][0]["start_line"]
                and item["refs"][0]["end_line"] <= part["end_line"] for part in owned)],
            "interval_status": [{"interval_id": part["id"], "status": "reviewed", "note": ""} for part in owned],
            "requests": [], "unknowns": []}


def cart_scope(brief):
    return {"areas": [{"area": item["number"], "disposition": "stages", "stages": [1], "reason": None,
                       "question": None} for item in brief["areas"]], "unknowns": []}


def cart_refine(brief):
    operations = [{"name": "Изменить корзину: " + item["symbol"], "description": None, "actor": "Покупатель",
                   "kind": "operation", "initiation": "api_call", "initiation_basis": None, "parent_operation": 1,
                   "refs": []} for item in brief["candidates"]]
    accounting = [{"candidate": item["number"], "disposition": "realized", "new_operations": [index],
                   "known_operations": [], "exclusion": None, "reason": None, "question": None}
                  for index, item in enumerate(brief["candidates"], 1)]
    mappings = [{"site": site["number"], "new_operation": site["candidates"][0], "known_operation": None,
                 "lifecycle_role": "result", "role_basis": "Запись после сохранения корзины",
                 "occurrence_rule": "Одна запись на успешное изменение", "ambiguity": None,
                 "refs": [{"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"]}]}
                for site in brief["sites"]]
    return {"operations": operations, "candidate_accounting": accounting, "event_mappings": mappings,
            "operation_transitions": [], "unknowns": []}


class RefinementTransport:
    """Mock HTTP for the catalog-driven map stage: handlers by task line, each given the request JSON."""

    HANDLERS = {"TASK: code": cart_code, "TASK: map": lambda brief: CART_MAP, "TASK: map-scope": cart_scope,
                "TASK: map-refine": cart_refine, "TASK: instrumentation": lambda brief: {"cards": []},
                "TASK: metrics": lambda brief: {"metrics": []}}

    def __init__(self):
        self.tasks, self.lock = [], threading.Lock()

    def __call__(self, url, payload, headers, context):
        system, user = (message["content"] for message in payload["messages"])
        task = system.split("\n", 1)[0]
        with self.lock:
            self.tasks.append(task)
        return envelope(self.HANDLERS[task](json.loads(user)))


class ActionRefinementCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.repo = CliIntegrationTests.make_repo(directory.name, {"shop/cart.py": CART})
        # A local placeholder profile: HTTP is mocked, nothing reads private configuration or credentials.
        self.config = self.base / "model.json"
        self.config.write_text(json.dumps({"code": {"base_url": "http://code.invalid/v1", "model": "code-model"},
                                           "business": {"base_url": "http://business.invalid/v1",
                                                        "model": "business-model"}}), encoding="utf-8")

    def analyze(self, name, *extra):
        # These are the legacy coarse-refine checks: the mode is selected explicitly since atomic is the default.
        transport = RefinementTransport()
        with patch("process_observability.llm.urllib_transport", side_effect=transport):
            code, out, err = invoke("analyze", "--repo", self.repo, "--out", self.base / name, "--config", self.config,
                                    "--map-mode", "coarse-refine", *extra)
        return code, out, err, transport

    def test_analyze_refines_actions_and_export_carries_the_accounting(self):
        code, out, err, transport = self.analyze("run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["action_refinement"], "complete")
        self.assertEqual(transport.tasks, ["TASK: code", "TASK: map", "TASK: map-scope", "TASK: map-refine",
                                           "TASK: instrumentation", "TASK: metrics"])
        run = self.base / "run"
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        record = bundle["action_refinement"]
        self.assertEqual(json.loads((run / "action_refinement.json").read_text(encoding="utf-8")), record)
        self.assertTrue((run / "action_catalog.json").is_file())
        refined = [op for op in bundle["process_map"]["operations"] if op["id"] != "cart-step"]
        self.assertEqual(sorted(op["name"] for op in refined),
                         ["Изменить корзину: add_item", "Изменить корзину: remove_item"])
        self.assertEqual(bundle["process_map"]["scenarios"][0]["operation_ids"], ["cart-step", *[op["id"] for op in refined]])
        diagnostics = json.loads((run / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostics["review"]["action_refinement"]["status"], "complete")
        self.assertEqual(diagnostics["review"]["action_catalog"]["candidates"], 2)
        code, out, err = invoke("export-onboarding", "--run", run, "--out", self.base / "packet")
        self.assertEqual(code, 0, err)
        packet = Path(json.loads(out)["export"])
        document = json.loads((packet / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(document["action_accounting"], record)
        with (packet / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = {row["event_key"]: row for row in csv.DictReader(handle)}
        by_name = {op["name"]: op["id"] for op in refined}
        self.assertEqual(rows["cart.item_removed"]["operation_ids"], by_name["Изменить корзину: remove_item"])
        self.assertIn("Учёт кандидатов действий", (packet / "process.md").read_text(encoding="utf-8"))

    def test_request_limit_defers_by_id_and_reports_partial_through_exit_code_and_export(self):
        code, out, err, transport = self.analyze("limited", "--refine-max-requests", "1")
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["action_refinement"], "partial")
        self.assertNotIn("TASK: map-refine", transport.tasks)
        bundle = json.loads((self.base / "limited" / "bundle.json").read_text(encoding="utf-8"))
        self.assertTrue(all(state["status"] == "completed" for state in bundle["manifest"]["stages"].values()))
        states = [state for head in bundle["action_refinement"]["candidates"].values() for state in head["stages"]]
        self.assertEqual({(state["state"], state["reason"]) for state in states}, {("deferred", "request_limit_reached")})
        code, out, err = invoke("export-onboarding", "--run", self.base / "limited", "--out", self.base / "packet")
        self.assertEqual(code, 1, err)
        document = json.loads((Path(json.loads(out)["export"]) / "onboarding.json").read_text(encoding="utf-8"))
        self.assertIn("Action refinement partial: request_limit_reached: 2", document["partial_reasons"])

    def test_refinement_can_be_switched_off_and_invalid_limits_stop_before_any_client(self):
        code, out, err, transport = self.analyze("off", "--action-refinement", "off")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["action_refinement"], "disabled")
        self.assertEqual(transport.tasks, ["TASK: code", "TASK: map", "TASK: instrumentation", "TASK: metrics"])
        self.assertTrue((self.base / "off" / "action_catalog.json").is_file())
        for value in ("0", "1001"):
            with self.subTest(value=value), patch("process_observability.cli._model_configs",
                                                  side_effect=AssertionError("no model config")):
                code, _, err = invoke("analyze", "--repo", self.repo, "--out", self.base / f"bad{value}",
                                      "--refine-max-requests", value)
                self.assertEqual(code, 2)
                self.assertIn("--refine-max-requests must be between 1 and 1000", err)


def cart_atomic(brief):
    """One terminal operation per supplied unit, in page order, in one working group proposed on the first page;
    each emit site is a result of its unit's operation. Facts are not page output: composition derives them."""
    known = [item["number"] for item in brief["working_groups"]]
    group = known[0] if known else 1
    operations = [{"name": "Изменить корзину: " + unit["symbol"], "description": None, "actor": "Покупатель",
                   "kind": "operation", "group": group, "initiation": "api_call", "initiation_basis": None,
                   "exit": "terminal", "exit_question": None, "refs": []}
                  for unit in brief["units"]]
    accounting = [{"unit": unit["number"], "disposition": "realized", "new_operations": [index], "known_operations": [],
                   "serves": [], "exclusion": None, "reason": None, "question": None}
                  for index, unit in enumerate(brief["units"], 1)]
    positions = {unit["number"]: index for index, unit in enumerate(brief["units"], 1)}
    mappings = [{"site": site["number"], "new_operation": positions[site["units"][0]], "known_operation": None,
                 "lifecycle_role": "result", "role_basis": "Запись после сохранения корзины",
                 "occurrence_rule": "Одна запись на успешное изменение", "ambiguity": None,
                 "refs": [{"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"]}]}
                for site in brief["sites"] if site["units"]]
    return {"operations": operations,
            "new_groups": [] if known else [{"name": "Корзина", "contract": "Покупатель меняет состав корзины."}],
            "unit_accounting": accounting, "event_mappings": mappings, "operation_transitions": [], "unknowns": []}


def cart_compose(brief):
    """One stage over every accepted action and one result fact per action boundary."""
    numbers = [op["number"] for op in brief["operations"]]
    return {"process": {"purpose": "Покупка", "boundaries": None, "case_definition": "Одна корзина"},
            "stages": [{"name": "Корзина", "contract": "Покупатель добавляет и удаляет позиции корзины.",
                        "operations": numbers}],
            "facts": [{"stage": 1, "operations": [op["number"]], "obligation": None,
                       "meaning": op["name"] + ": состав корзины изменён", "rationale": "Учёт изменений корзины",
                       "scope": "Все корзины", "condition": None, "metric_ids": [], "evidence_alternatives": [],
                       "guidance_ids": ["FACT-QUESTION"], "lifecycle_roles": ["result"]}
                      for op in brief["operations"]],
            "obligations": [], "hypotheses": [], "scenarios": [], "groups": [], "unknowns": []}


def cart_code_with_relations(brief):
    """The code reply plus the relation array whenever the brief asks for technical relations."""
    reply = cart_code(brief)
    return {**reply, "relations": []} if "relation_kinds" in brief else reply


class AtomicTransport(RefinementTransport):
    """Mock HTTP for both map modes: the legacy coarse/scope/refine handlers plus the atomic pages and composition."""

    HANDLERS = {**RefinementTransport.HANDLERS, "TASK: code": cart_code_with_relations,
                "TASK: map-atomic": cart_atomic, "TASK: map-compose": cart_compose}

    def __init__(self):
        super().__init__()
        self.requests = []

    def __call__(self, url, payload, headers, context):
        with self.lock:
            self.requests.append(payload)
        return super().__call__(url, payload, headers, context)


def collapsed(tasks):
    """Task lines with consecutive repeats folded: page counts depend on the measured request size."""
    return [task for index, task in enumerate(tasks) if index == 0 or tasks[index - 1] != task]


def local_config(path):
    # A local placeholder profile: HTTP is mocked, nothing reads private configuration or credentials.
    path.write_text(json.dumps({"code": {"base_url": "http://code.invalid/v1", "model": "code-model"},
                                "business": {"base_url": "http://business.invalid/v1", "model": "business-model"}}),
                    encoding="utf-8")
    return path


def atomic_run(base, name, files=None, *extra):
    """A saved atomic analyze run through the real CLI with mocked HTTP; returns (run directory, transport)."""
    base = Path(base).resolve()
    repo = CliIntegrationTests.make_repo(base / f"{name}-source", files or {"shop/cart.py": CART})
    transport = AtomicTransport()
    with patch("process_observability.llm.urllib_transport", side_effect=transport):
        code, _, err = invoke("analyze", "--repo", repo, "--out", base / name,
                              "--config", local_config(base / f"{name}-model.json"), "--map-mode", "atomic", *extra)
    if code != 0:
        raise AssertionError(err)
    return base / name, transport


class AtomicCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.repo = CliIntegrationTests.make_repo(directory.name, {"shop/cart.py": CART})
        self.config = local_config(self.base / "model.json")

    def analyze(self, name, *extra):
        transport = AtomicTransport()
        with patch("process_observability.llm.urllib_transport", side_effect=transport):
            code, out, err = invoke("analyze", "--repo", self.repo, "--out", self.base / name, "--config", self.config,
                                    *extra)
        return code, out, err, transport

    def test_atomic_analyze_sends_no_coarse_request_and_export_carries_the_accounting(self):
        code, out, err, transport = self.analyze("run", "--map-mode", "atomic")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["atomic_map"], "complete")
        self.assertEqual(collapsed(transport.tasks), ["TASK: code", "TASK: map-atomic", "TASK: map-compose",
                                                      "TASK: instrumentation", "TASK: metrics"])
        run = self.base / "run"
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        record = bundle["atomic_map"]
        self.assertEqual(json.loads((run / "atomic_map.json").read_text(encoding="utf-8")), record)
        for name in ("action_catalog.json", "declaration_index.json", "command_units.json", "report.html", "report.md"):
            self.assertTrue((run / name).is_file(), name)
        self.assertIn("report_sources", bundle)  # the post-model report source collection is unchanged
        operations = {op["name"]: op["id"] for op in bundle["process_map"]["operations"]}
        self.assertEqual(sorted(operations), ["Изменить корзину: add_item", "Изменить корзину: remove_item"])
        self.assertEqual(bundle["process_map"]["scenarios"], [])
        self.assertEqual([stage["name"] for stage in bundle["process_map"]["stages"]], ["Корзина"])
        facts = {fact["meaning"]: fact for fact in bundle["log_coverage"]["facts"]}
        self.assertEqual(sorted(facts), ["Изменить корзину: add_item: состав корзины изменён",
                                         "Изменить корзину: remove_item: состав корзины изменён"])
        for symbol, key in (("add_item", "cart.item_added"), ("remove_item", "cart.item_removed")):
            fact = facts[f"Изменить корзину: {symbol}: состав корзины изменён"]
            self.assertEqual((fact["instrumentation"], fact["lifecycle_check"], fact["event_keys"]),
                             ("found", "compatible", [key]))
        # the code stage was asked for technical relations by default in atomic mode and they are bound to the run
        self.assertEqual(bundle["technical_relations"]["status"], "collected")
        diagnostics = json.loads((run / "diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostics["review"]["atomic_map"]["status"], "complete")
        self.assertEqual(diagnostics["review"]["action_catalog"]["candidates"], 2)
        code, out, err = invoke("export-onboarding", "--run", run, "--out", self.base / "packet")
        self.assertEqual(code, 0, err)
        packet = Path(json.loads(out)["export"])
        document = json.loads((packet / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(document["atomic_accounting"], record)
        self.assertIsNone(document["action_accounting"])
        with (packet / "events.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = {row["event_key"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows["cart.item_removed"]["operation_ids"], operations["Изменить корзину: remove_item"])
        self.assertIn("## Учёт атомарных действий", (packet / "process.md").read_text(encoding="utf-8"))

    def test_normal_analyze_is_atomic_and_coarse_refine_is_an_explicit_legacy_choice(self):
        code, out, err, transport = self.analyze("default")
        self.assertEqual(code, 0, err)
        self.assertIn("TASK: map-compose", transport.tasks)
        self.assertFalse({"TASK: map", "TASK: map-scope", "TASK: map-refine", "TASK: map-skeleton"}
                         & set(transport.tasks))
        code, out, err, transport = self.analyze("legacy", "--map-mode", "coarse-refine")
        self.assertEqual(code, 0, err)
        self.assertEqual(transport.tasks, ["TASK: code", "TASK: map", "TASK: map-scope", "TASK: map-refine",
                                           "TASK: instrumentation", "TASK: metrics"])
        bundle = json.loads((self.base / "legacy" / "bundle.json").read_text(encoding="utf-8"))
        self.assertNotIn("atomic_map", bundle)
        self.assertEqual(bundle["action_refinement"]["status"], "complete")

    def test_atomic_options_are_checked_before_any_model_client(self):
        for value in ("0", "129"):
            with self.subTest(value=value), patch("process_observability.cli._model_configs",
                                                  side_effect=AssertionError("no model config")):
                code, _, err = invoke("analyze", "--repo", self.repo, "--out", self.base / f"bad{value}",
                                      "--map-mode", "atomic", "--atomic-page-units", value)
                self.assertEqual(code, 2)
                self.assertIn("--atomic-page-units must be between 1 and 128", err)
        for option, value in (("--map-mode", "fast"), ("--triage", "maybe"), ("--compose", "maybe")):
            with self.subTest(option=option), patch("process_observability.cli._model_configs",
                                                    side_effect=AssertionError("no model config")):
                with self.assertRaises(SystemExit) as caught:
                    invoke("analyze", "--repo", self.repo, "--out", self.base / "bad-choice", option, value)
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.base / "bad-choice").exists())

    def test_a_partial_atomic_map_exits_one(self):
        code, out, err, transport = self.analyze("limited", "--map-mode", "atomic", "--refine-max-requests", "1",
                                                 "--atomic-page-units", "1")
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["atomic_map"], "partial")
        self.assertEqual(transport.tasks.count("TASK: map-atomic"), 1)
        bundle = json.loads((self.base / "limited" / "bundle.json").read_text(encoding="utf-8"))
        reasons = {state.get("reason") for state in bundle["atomic_map"]["unit_states"].values()
                   if state["state"] == "deferred"}
        self.assertEqual(reasons, {"request_limit_reached"})
        code, out, err = invoke("export-onboarding", "--run", self.base / "limited", "--out", self.base / "packet")
        self.assertEqual(code, 1, err)
        document = json.loads((Path(json.loads(out)["export"]) / "onboarding.json").read_text(encoding="utf-8"))
        self.assertIn("Atomic map partial: request_limit_reached: 1", document["partial_reasons"])

    def test_strict_demo_still_replays_only_its_original_recorded_requests(self):
        from process_observability.demo import request_fingerprint
        used, original = [], RecordedModel.complete_json

        def watch(model, system, user):
            used.append(request_fingerprint(system, user))
            return original(model, system, user)
        with patch.object(RecordedModel, "complete_json", watch):
            code, _, err = invoke("demo", "--out", self.base / "demo")
        self.assertEqual(code, 0, err)
        records = json.loads((EXAMPLE / "model-responses.json").read_text(encoding="utf-8"))["records"]
        self.assertEqual(len(used), 4)
        self.assertLessEqual(set(used), set(records))
        bundle = json.loads((self.base / "demo" / "bundle.json").read_text(encoding="utf-8"))
        for key in ("atomic_map", "command_units"):
            self.assertNotIn(key, bundle)
        code, _, err = invoke("export-onboarding", "--run", self.base / "demo", "--out", self.base / "demo-packet")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
