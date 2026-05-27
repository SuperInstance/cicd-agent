"""
Tests for cicd_agent package
=============================
Comprehensive tests covering stages, pipelines, artifacts, triggers,
and deployment strategies. Uses only unittest + dataclasses.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from cicd_agent.stage import Stage, StageName, StageResult, StageStatus
from cicd_agent.pipeline import Pipeline, PipelineConfig, PipelineRun, PipelineStatus
from cicd_agent.artifact import Artifact, ArtifactManager
from cicd_agent.trigger import TriggerCallback, TriggerEvent, TriggerManager, TriggerType
from cicd_agent.deploy import Deployer, DeployResult, DeployStrategy


# ===================================================================
# Stage Tests
# ===================================================================

class TestStageStatus(unittest.TestCase):
    def test_all_values(self):
        expected = {"pending", "running", "passed", "failed", "skipped", "cancelled", "warning"}
        self.assertEqual({s.value for s in StageStatus}, expected)


class TestStageResult(unittest.TestCase):
    def test_to_dict(self):
        sr = StageResult(name="test", status=StageStatus.PASSED, duration=1.5, message="ok")
        d = sr.to_dict()
        self.assertEqual(d["name"], "test")
        self.assertEqual(d["status"], "passed")
        self.assertEqual(d["duration"], 1.5)

    def test_ok_property(self):
        self.assertTrue(StageResult(name="x", status=StageStatus.PASSED).ok)
        self.assertTrue(StageResult(name="x", status=StageStatus.WARNING).ok)
        self.assertTrue(StageResult(name="x", status=StageStatus.SKIPPED).ok)
        self.assertFalse(StageResult(name="x", status=StageStatus.FAILED).ok)
        self.assertFalse(StageResult(name="x", status=StageStatus.RUNNING).ok)

    def test_default_fields(self):
        sr = StageResult(name="x", status=StageStatus.PENDING)
        self.assertEqual(sr.artifacts, [])
        self.assertEqual(sr.warnings, [])
        self.assertEqual(sr.data, {})


class TestStage(unittest.TestCase):
    def _pass_action(self, ctx):
        return StageResult(name="test", status=StageStatus.PASSED, message="done")

    def _fail_action(self, ctx):
        return StageResult(name="test", status=StageStatus.FAILED, message="oops")

    def _exception_action(self, ctx):
        raise RuntimeError("boom")

    def test_execute_pass(self):
        s = Stage(name="test", action=self._pass_action)
        result = s.execute({})
        self.assertEqual(result.status, StageStatus.PASSED)
        self.assertEqual(result.name, "test")
        self.assertIsNotNone(result.started_at)
        self.assertIsNotNone(result.finished_at)
        self.assertGreater(result.duration, -0.01)

    def test_execute_fail(self):
        s = Stage(name="test", action=self._fail_action)
        result = s.execute({})
        self.assertEqual(result.status, StageStatus.FAILED)

    def test_execute_exception(self):
        s = Stage(name="test", action=self._exception_action)
        result = s.execute({})
        self.assertEqual(result.status, StageStatus.FAILED)
        self.assertIn("boom", result.message)

    def test_retry_on_fail(self):
        attempts = {"n": 0}
        def retry_action(ctx):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return StageResult(name="r", status=StageStatus.FAILED)
            return StageResult(name="r", status=StageStatus.PASSED)

        s = Stage(name="retry-test", action=retry_action, retry_count=3, retry_delay=0.01)
        result = s.execute({})
        self.assertEqual(result.status, StageStatus.PASSED)
        self.assertEqual(attempts["n"], 3)

    def test_to_dict(self):
        s = Stage(name="build", action=self._pass_action, depends_on=["init"], gate=True)
        d = s.to_dict()
        self.assertEqual(d["name"], "build")
        self.assertEqual(d["depends_on"], ["init"])
        self.assertTrue(d["gate"])

    def test_action_returns_bool(self):
        """Action returning True/False instead of StageResult."""
        s = Stage(name="bool-test", action=lambda ctx: True)
        result = s.execute({})
        self.assertEqual(result.status, StageStatus.PASSED)

        s2 = Stage(name="bool-fail", action=lambda ctx: False)
        result2 = s2.execute({})
        self.assertEqual(result2.status, StageStatus.FAILED)


# ===================================================================
# Pipeline Tests
# ===================================================================

class TestPipelineConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = PipelineConfig()
        self.assertEqual(cfg.name, "default")
        self.assertEqual(cfg.max_workers, 4)
        self.assertFalse(cfg.fail_fast)

    def test_should_skip(self):
        cfg = PipelineConfig()
        self.assertTrue(cfg.should_skip("[skip-ci] quick fix"))
        self.assertFalse(cfg.should_skip("fix: normal fix"))
        self.assertFalse(cfg.should_skip(""))


class TestPipelineRun(unittest.TestCase):
    def test_auto_run_id(self):
        run = PipelineRun(pipeline_name="test")
        self.assertIn("test", run.run_id)

    def test_to_dict(self):
        run = PipelineRun(
            pipeline_name="p",
            status=PipelineStatus.PASSED,
            stage_results=[StageResult(name="s", status=StageStatus.PASSED)],
        )
        d = run.to_dict()
        self.assertEqual(d["pipeline_name"], "p")
        self.assertEqual(d["status"], "passed")
        self.assertEqual(len(d["stage_results"]), 1)

    def test_passed_property(self):
        self.assertTrue(PipelineRun(status=PipelineStatus.PASSED).passed)
        self.assertTrue(PipelineRun(status=PipelineStatus.PARTIAL).passed)
        self.assertFalse(PipelineRun(status=PipelineStatus.FAILED).passed)


class TestPipeline(unittest.TestCase):
    def _make_pass(self, name):
        return Stage(name=name, action=lambda ctx: StageResult(name=name, status=StageStatus.PASSED))

    def _make_fail(self, name):
        return Stage(name=name, action=lambda ctx: StageResult(name=name, status=StageStatus.FAILED))

    def test_add_remove_stage(self):
        p = Pipeline()
        p.add_stage(self._make_pass("build"))
        self.assertIn("build", p.list_stages())
        p.remove_stage("build")
        self.assertNotIn("build", p.list_stages())

    def test_simple_pipeline(self):
        p = Pipeline()
        p.add_stage(self._make_pass("build"))
        p.add_stage(self._make_pass("test"))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.PASSED)
        self.assertEqual(len(run.stage_results), 2)

    def test_pipeline_with_deps(self):
        results = {}
        def build_action(ctx):
            results["build"] = True
            return StageResult(name="build", status=StageStatus.PASSED)

        def test_action(ctx):
            results["test"] = True
            self.assertIn("build", ctx)
            return StageResult(name="test", status=StageStatus.PASSED)

        p = Pipeline()
        p.add_stage(Stage(name="build", action=build_action))
        p.add_stage(Stage(name="test", action=test_action, depends_on=["build"]))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.PASSED)
        self.assertIn("build", results)
        self.assertIn("test", results)

    def test_pipeline_failure(self):
        p = Pipeline()
        p.add_stage(self._make_fail("build"))
        p.add_stage(self._make_pass("test"))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.FAILED)

    def test_gate_blocks_downstream(self):
        """Gate failure should cancel downstream stages."""
        p = Pipeline()
        p.add_stage(Stage(name="build", action=lambda ctx: StageResult(name="build", status=StageStatus.FAILED), gate=True))
        p.add_stage(Stage(name="test", action=lambda ctx: StageResult(name="test", status=StageStatus.PASSED), depends_on=["build"]))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.FAILED)
        # test stage should be cancelled
        cancelled = [s for s in run.stage_results if s.status == StageStatus.CANCELLED]
        self.assertTrue(len(cancelled) > 0)

    def test_optional_failure(self):
        """Optional stage failure should result in PARTIAL."""
        p = Pipeline()
        p.add_stage(self._make_pass("build"))
        p.add_stage(Stage(name="lint", action=lambda ctx: StageResult(name="lint", status=StageStatus.FAILED), optional=True, depends_on=["build"]))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.PARTIAL)

    def test_skip_pipeline(self):
        p = Pipeline()
        p.add_stage(self._make_pass("build"))
        run = p.execute(commit_message="[skip-ci] docs only")
        self.assertEqual(run.status, PipelineStatus.SKIPPED)

    def test_parallel_execution(self):
        """Independent stages should run in parallel."""
        order = []
        lock = threading.Lock()
        def slow_action(name, duration):
            def action(ctx):
                time.sleep(duration)
                with lock:
                    order.append(name)
                return StageResult(name=name, status=StageStatus.PASSED)
            return action

        p = Pipeline(config=PipelineConfig(max_workers=4))
        p.add_stage(Stage(name="a", action=slow_action("a", 0.05)))
        p.add_stage(Stage(name="b", action=slow_action("b", 0.05)))
        p.add_stage(Stage(name="c", action=slow_action("c", 0.05)))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.PASSED)
        self.assertEqual(len(run.stage_results), 3)

    def test_cycle_detection(self):
        p = Pipeline()
        p.add_stage(Stage(name="a", action=lambda c: StageResult(name="a", status=StageStatus.PASSED), depends_on=["b"]))
        p.add_stage(Stage(name="b", action=lambda c: StageResult(name="b", status=StageStatus.PASSED), depends_on=["a"]))
        with self.assertRaises(ValueError):
            p.execute()

    def test_timeout(self):
        def slow_action(ctx):
            time.sleep(10)
            return StageResult(name="slow", status=StageStatus.PASSED)

        p = Pipeline(config=PipelineConfig(timeout=0.1))
        p.add_stage(Stage(name="slow", action=slow_action))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.TIMEOUT)

    def test_summary(self):
        p = Pipeline(config=PipelineConfig(name="test"))
        p.add_stage(self._make_pass("build"))
        p.execute()
        p.execute()
        s = p.summary()
        self.assertEqual(s["total_runs"], 2)
        self.assertEqual(s["passed"], 2)
        self.assertEqual(s["pipeline"], "test")

    def test_get_runs(self):
        p = Pipeline()
        p.add_stage(self._make_pass("x"))
        p.execute()
        runs = p.get_runs()
        self.assertEqual(len(runs), 1)

    def test_get_last_run(self):
        p = Pipeline()
        self.assertIsNone(p.get_last_run())
        p.add_stage(self._make_pass("x"))
        p.execute()
        self.assertIsNotNone(p.get_last_run())


# ===================================================================
# Artifact Tests
# ===================================================================

class TestArtifact(unittest.TestCase):
    def test_auto_checksum(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            path = f.name
        try:
            art = Artifact(name="test.txt", path=path)
            self.assertTrue(art.checksum)
            self.assertGreater(art.size_bytes, 0)
            self.assertTrue(art.verify())
        finally:
            os.unlink(path)

    def test_verify_tampered(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original")
            path = f.name
        try:
            art = Artifact(name="t.txt", path=path)
            with open(path, "w") as f:
                f.write("tampered")
            self.assertFalse(art.verify())
        finally:
            os.unlink(path)

    def test_verify_missing_file(self):
        art = Artifact(name="missing", path="/nonexistent/file.txt", checksum="abc123")
        self.assertFalse(art.verify())

    def test_to_dict(self):
        art = Artifact(name="x", path="/tmp/x", checksum="abc", size_bytes=10, version="1.0")
        d = art.to_dict()
        self.assertEqual(d["name"], "x")
        self.assertEqual(d["version"], "1.0")

    def test_copy_to(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.txt"
            src.write_text("content")
            art = Artifact(name="src.txt", path=str(src))
            dest = Path(td) / "dest"
            copied = art.copy_to(str(dest))
            self.assertTrue(Path(copied.path).exists())
            self.assertEqual(Path(copied.path).read_text(), "content")
            self.assertTrue(copied.verify())

    def test_sha256(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test")
            path = f.name
        try:
            h = Artifact.sha256(Path(path))
            self.assertEqual(len(h), 64)
        finally:
            os.unlink(path)


class TestArtifactManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = ArtifactManager(base_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_register_and_get(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("data")
            path = f.name
        try:
            art = self.mgr.register("myfile", path, version="1.0", artifact_type="build")
            self.assertIsNotNone(art.checksum)
            fetched = self.mgr.get("myfile", version="1.0")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.version, "1.0")
        finally:
            os.unlink(path)

    def test_find_by_type(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.mgr.register("a", path, artifact_type="build")
            self.mgr.register("b", path, artifact_type="test-report")
            found = self.mgr.find(artifact_type="build")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].name, "a")
        finally:
            os.unlink(path)

    def test_find_by_tag(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.mgr.register("a", path, tags=["production", "v2"])
            self.mgr.register("b", path, tags=["staging"])
            found = self.mgr.find(tag="production")
            self.assertEqual(len(found), 1)
        finally:
            os.unlink(path)

    def test_verify_all(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("ok")
            path = f.name
        try:
            self.mgr.register("a", path)
            results = self.mgr.verify_all()
            self.assertTrue(all(results.values()))
        finally:
            os.unlink(path)

    def test_cleanup(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            for i in range(10):
                self.mgr.register(f"art-{i}", path)
            removed = self.mgr.cleanup(keep=5)
            self.assertEqual(removed, 5)
        finally:
            os.unlink(path)

    def test_remove(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.mgr.register("x", path)
            self.assertTrue(self.mgr.remove("x"))
            self.assertFalse(self.mgr.remove("x"))
        finally:
            os.unlink(path)

    def test_list_all(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.mgr.register("a", path)
            self.mgr.register("b", path)
            all_arts = self.mgr.list_all()
            self.assertEqual(len(all_arts), 2)
        finally:
            os.unlink(path)

    def test_persistence(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("persist")
            path = f.name
        try:
            self.mgr.register("persist-test", path, version="1.0")
            # Create new manager pointing to same dir
            mgr2 = ArtifactManager(base_dir=self.tmpdir)
            art = mgr2.get("persist-test", version="1.0")
            self.assertIsNotNone(art)
            self.assertEqual(art.version, "1.0")
        finally:
            os.unlink(path)


# ===================================================================
# Trigger Tests
# ===================================================================

class TestTriggerEvent(unittest.TestCase):
    def test_auto_timestamp(self):
        e = TriggerEvent(trigger_type=TriggerType.MANUAL)
        self.assertTrue(e.timestamp)

    def test_to_dict(self):
        e = TriggerEvent(trigger_type=TriggerType.WEBHOOK, source="repo")
        d = e.to_dict()
        self.assertEqual(d["trigger_type"], "webhook")
        self.assertEqual(d["source"], "repo")


class TestTriggerManager(unittest.TestCase):
    def setUp(self):
        self.mgr = TriggerManager()

    def test_register_and_fire(self):
        fired = []
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: fired.append(e))
        triggered = self.mgr.fire(TriggerType.MANUAL)
        self.assertIn("r1", triggered)
        self.assertEqual(len(fired), 1)

    def test_fire_no_match(self):
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: None)
        triggered = self.mgr.fire(TriggerType.WEBHOOK)
        self.assertEqual(triggered, [])

    def test_commit_pattern(self):
        fired = []
        self.mgr.register(
            "deploy-tag",
            TriggerType.COMMIT_PATTERN,
            pattern=r"\[deploy\]",
            callback=lambda e: fired.append(e),
        )
        triggered = self.mgr.fire(
            TriggerType.COMMIT_PATTERN,
            commit_message="fix: bug [deploy]",
        )
        self.assertIn("deploy-tag", triggered)
        self.assertEqual(len(fired), 1)

    def test_commit_pattern_no_match(self):
        self.mgr.register(
            "deploy-tag",
            TriggerType.COMMIT_PATTERN,
            pattern=r"\[deploy\]",
            callback=lambda e: None,
        )
        triggered = self.mgr.fire(
            TriggerType.COMMIT_PATTERN,
            commit_message="fix: bug",
        )
        self.assertEqual(triggered, [])

    def test_enable_disable(self):
        fired = []
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: fired.append(1))
        self.mgr.disable("r1")
        triggered = self.mgr.fire(TriggerType.MANUAL)
        self.assertEqual(triggered, [])
        self.assertEqual(len(fired), 0)
        self.mgr.enable("r1")
        triggered = self.mgr.fire(TriggerType.MANUAL)
        self.assertIn("r1", triggered)

    def test_unregister(self):
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: None)
        self.assertTrue(self.mgr.unregister("r1"))
        self.assertFalse(self.mgr.unregister("r1"))

    def test_history(self):
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: None)
        self.mgr.fire(TriggerType.MANUAL)
        self.mgr.fire(TriggerType.MANUAL)
        history = self.mgr.get_history()
        self.assertEqual(len(history), 2)

    def test_list_rules(self):
        self.mgr.register("r1", TriggerType.MANUAL, callback=lambda e: None, pattern="test")
        rules = self.mgr.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "r1")
        self.assertTrue(rules[0]["enabled"])


# ===================================================================
# Deploy Tests
# ===================================================================

class TestDeployResult(unittest.TestCase):
    def test_to_dict(self):
        r = DeployResult(
            strategy=DeployStrategy.BLUE_GREEN,
            target="prod",
            version="2.0",
            success=True,
            message="ok",
        )
        d = r.to_dict()
        self.assertEqual(d["strategy"], "blue_green")
        self.assertTrue(d["success"])

    def test_defaults(self):
        r = DeployResult(strategy=DeployStrategy.DIRECT)
        self.assertFalse(r.success)
        self.assertFalse(r.rollback_performed)


class TestDeployer(unittest.TestCase):
    def test_direct_deploy(self):
        d = Deployer()
        result = d.deploy(DeployStrategy.DIRECT, "prod", "1.0")
        self.assertTrue(result.success)
        self.assertEqual(d.get_active_version("prod"), "1.0")

    def test_direct_deploy_failure(self):
        d = Deployer(deploy_action=lambda t, v, c: False)
        result = d.deploy(DeployStrategy.DIRECT, "prod", "1.0")
        self.assertFalse(result.success)

    def test_blue_green(self):
        d = Deployer()
        result = d.deploy(DeployStrategy.BLUE_GREEN, "prod", "2.0", previous_version="1.0")
        self.assertTrue(result.success)
        self.assertEqual(d.get_active_version("prod"), "2.0")

    def test_blue_green_green_fails(self):
        d = Deployer(deploy_action=lambda t, v, c: t == "prod-green")
        result = d.deploy(DeployStrategy.BLUE_GREEN, "prod", "2.0")
        self.assertFalse(result.success)

    def test_blue_green_unhealthy(self):
        actions = {"count": 0}
        def deploy_fn(target, version, config):
            actions["count"] += 1
            return True

        def health_fn(target):
            return "green" not in target  # green is unhealthy

        d = Deployer(deploy_action=deploy_fn, health_check=health_fn)
        result = d.deploy(DeployStrategy.BLUE_GREEN, "prod", "2.0")
        self.assertFalse(result.success)

    def test_canary(self):
        steps_hit = []
        def deploy_fn(target, version, config):
            steps_hit.append(config.get("canary_percent"))
            return True

        d = Deployer(deploy_action=deploy_fn)
        result = d.deploy(
            DeployStrategy.CANARY, "prod", "3.0",
            canary_steps=[25.0, 50.0, 100.0],
        )
        self.assertTrue(result.success)
        self.assertEqual(steps_hit, [25.0, 50.0, 100.0])

    def test_canary_failure_rollback(self):
        steps_hit = []
        def deploy_fn(target, version, config):
            steps_hit.append(config.get("canary_percent"))
            # Fail at 50%
            return config.get("canary_percent", 0) <= 25.0

        d = Deployer(deploy_action=deploy_fn)
        result = d.deploy(
            DeployStrategy.CANARY, "prod", "3.0",
            previous_version="2.0",
            canary_steps=[25.0, 50.0, 100.0],
        )
        self.assertFalse(result.success)
        self.assertTrue(result.rollback_performed)

    def test_rolling(self):
        batches = []
        def deploy_fn(target, version, config):
            batches.append(config.get("batch"))
            return True

        d = Deployer(deploy_action=deploy_fn)
        result = d.deploy(
            DeployStrategy.ROLLING, "prod", "4.0",
            deploy_config={"instances": 6, "batch_size": 2},
        )
        self.assertTrue(result.success)
        self.assertEqual(batches, [1, 2, 3])

    def test_rolling_failure(self):
        batches = []
        def deploy_fn(target, version, config):
            batches.append(config.get("batch"))
            return config.get("batch", 0) < 2

        d = Deployer(deploy_action=deploy_fn)
        result = d.deploy(
            DeployStrategy.ROLLING, "prod", "4.0",
            deploy_config={"instances": 3, "batch_size": 1},
        )
        self.assertFalse(result.success)

    def test_rollback(self):
        d = Deployer()
        d.deploy(DeployStrategy.DIRECT, "prod", "2.0")
        result = d.rollback("prod", "1.0")
        self.assertTrue(result.success)
        self.assertTrue(result.rollback_performed)
        self.assertEqual(d.get_active_version("prod"), "1.0")

    def test_unknown_strategy(self):
        d = Deployer()
        result = d.deploy(DeployStrategy.ROLLBACK, "prod", "1.0")
        self.assertFalse(result.success)

    def test_get_history(self):
        d = Deployer()
        d.deploy(DeployStrategy.DIRECT, "prod", "1.0")
        d.deploy(DeployStrategy.DIRECT, "prod", "2.0")
        history = d.get_history()
        self.assertEqual(len(history), 2)

    def test_active_version_unknown(self):
        d = Deployer()
        self.assertEqual(d.get_active_version("nonexistent"), "unknown")


# ===================================================================
# Integration Tests
# ===================================================================

class TestPipelineWithArtifacts(unittest.TestCase):
    """Test pipeline producing artifacts via ArtifactManager."""

    def test_pipeline_registers_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ArtifactManager(base_dir=os.path.join(td, "arts"))

            def build_action(ctx):
                # Simulate creating a build artifact
                art_path = os.path.join(td, "build.zip")
                with open(art_path, "w") as f:
                    f.write("build-output")
                mgr.register("build.zip", art_path, version="1.0", artifact_type="build")
                return StageResult(name="build", status=StageStatus.PASSED)

            p = Pipeline(config=PipelineConfig(name="with-artifacts"))
            p.add_stage(Stage(name="build", action=build_action))
            run = p.execute()
            self.assertEqual(run.status, PipelineStatus.PASSED)
            art = mgr.get("build.zip", version="1.0")
            self.assertIsNotNone(art)
            self.assertTrue(art.verify())


class TestPipelineWithTriggers(unittest.TestCase):
    """Test trigger manager firing pipeline execution."""

    def test_trigger_starts_pipeline(self):
        runs = []
        def run_pipeline(event):
            p = Pipeline(config=PipelineConfig(name="triggered"))
            p.add_stage(Stage(name="build", action=lambda ctx: StageResult(
                name="build", status=StageStatus.PASSED,
            )))
            run = p.execute(trigger=event.trigger_type.value)
            runs.append(run)

        triggers = TriggerManager()
        triggers.register("on-push", TriggerType.WEBHOOK, callback=run_pipeline)
        triggered = triggers.fire(TriggerType.WEBHOOK, source="my-repo", commit_sha="abc123")
        self.assertIn("on-push", triggered)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, PipelineStatus.PASSED)


class TestPipelineWithDeploy(unittest.TestCase):
    """Test pipeline integrating with Deployer."""

    def test_pipeline_deploys_on_pass(self):
        deploys = []
        def deploy_action(target, version, config):
            deploys.append((target, version))
            return True

        deployer = Deployer(deploy_action=deploy_action)

        def deploy_stage(ctx):
            result = deployer.deploy(DeployStrategy.DIRECT, "prod", "1.0")
            return StageResult(
                name="deploy",
                status=StageStatus.PASSED if result.success else StageStatus.FAILED,
                message=result.message,
            )

        p = Pipeline(config=PipelineConfig(name="deploy-test"))
        p.add_stage(Stage(name="build", action=lambda ctx: StageResult(name="build", status=StageStatus.PASSED)))
        p.add_stage(Stage(name="deploy", action=deploy_stage, depends_on=["build"]))
        run = p.execute()
        self.assertEqual(run.status, PipelineStatus.PASSED)
        self.assertEqual(deploys, [("prod", "1.0")])


class TestEndToEnd(unittest.TestCase):
    """Full end-to-end pipeline: stage → artifact → trigger → deploy."""

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as td:
            art_mgr = ArtifactManager(base_dir=os.path.join(td, "arts"))
            deployer = Deployer()

            def build(ctx):
                p = os.path.join(td, "app.tar.gz")
                with open(p, "w") as f:
                    f.write("app-binary")
                art_mgr.register("app.tar.gz", p, version="2.0", artifact_type="build")
                return StageResult(name="build", status=StageStatus.PASSED, artifacts=[p])

            def test(ctx):
                return StageResult(name="test", status=StageStatus.PASSED, message="5 passed")

            def deploy(ctx):
                r = deployer.deploy(DeployStrategy.BLUE_GREEN, "prod", "2.0")
                return StageResult(
                    name="deploy", status=StageStatus.PASSED if r.success else StageStatus.FAILED,
                    message=r.message,
                )

            p = Pipeline(config=PipelineConfig(name="e2e"))
            p.add_stage(Stage(name="build", action=build, gate=True))
            p.add_stage(Stage(name="test", action=test, depends_on=["build"]))
            p.add_stage(Stage(name="deploy", action=deploy, depends_on=["test"]))

            run = p.execute(trigger="manual", commit_sha="deadbeef")
            self.assertEqual(run.status, PipelineStatus.PASSED)
            self.assertEqual(len(run.stage_results), 3)
            self.assertIsNotNone(art_mgr.get("app.tar.gz", version="2.0"))
            self.assertEqual(deployer.get_active_version("prod"), "2.0")


# ===================================================================
# Main
# ===================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
