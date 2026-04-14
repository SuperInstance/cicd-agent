"""
CI/CD Report Generator
========================
Generate CI/CD reports in JSON, text, and Markdown formats.
Includes trend analysis and summary statistics.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("fleet.cicd.reporter")


class CIReporter:
    """Generate CI/CD reports in multiple formats.

    Supported formats:
    - **json**: Structured data for programmatic consumption
    - **text**: Human-readable plain-text summary
    - **markdown**: For wiki / documentation / PR comments

    Usage::

        reporter = CIReporter()
        json_report = reporter.generate_json(pipeline_run)
        text_report = reporter.generate_text(pipeline_run)
        md_report = reporter.generate_markdown(pipeline_run)
    """

    def generate_json(self, run: Any) -> dict:
        """Generate a structured JSON report from a PipelineRun.

        Accepts any object with ``to_dict()`` or falls back to ``__dict__``.
        """
        if isinstance(run, dict):
            data = dict(run)
        elif hasattr(run, "to_dict"):
            data = run.to_dict()
        elif hasattr(run, "__dict__"):
            data = run.__dict__
        else:
            data = {"raw": str(run)}

        # Enrich with computed summary
        data["_report_meta"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "format": "json",
            "generator": "fleet-cicd-agent",
        }
        return data

    def generate_text(self, run: Any) -> str:
        """Generate a human-readable plain-text report."""
        if isinstance(run, dict):
            data = run
        elif hasattr(run, "to_dict"):
            data = run.to_dict()
        elif hasattr(run, "__dict__"):
            data = run.__dict__
        else:
            return str(run)

        lines: list[str] = []
        sep = "=" * 60
        thin = "-" * 40

        lines.append(sep)
        lines.append(f"  FLEET CI/CD REPORT")
        lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(sep)
        lines.append("")
        lines.append(f"  Run ID:      {data.get('run_id', 'N/A')}")
        lines.append(f"  Repo:        {data.get('repo_name', 'N/A')}")
        lines.append(f"  Status:      {self._status_emoji(data.get('status', ''))} {data.get('status', 'N/A').upper()}")
        lines.append(f"  Trigger:     {data.get('trigger', 'N/A')}")
        lines.append(f"  Commit:      {data.get('commit_sha', 'N/A')[:12]}")
        lines.append(f"  Message:     {data.get('commit_message', 'N/A')[:60]}")
        lines.append(f"  Branch:      {data.get('branch', 'N/A')}")
        lines.append(f"  Duration:    {data.get('total_duration', 0):.1f}s")
        lines.append(f"  Started:     {data.get('started_at', 'N/A')}")
        lines.append(f"  Finished:    {data.get('finished_at', 'N/A')}")
        lines.append("")

        # Stages
        stages = data.get("stages", [])
        if stages:
            lines.append(thin)
            lines.append("  PIPELINE STAGES")
            lines.append(thin)
            for stage in stages:
                name = stage.get("stage", "unknown")
                status = stage.get("status", "unknown")
                duration = stage.get("duration", 0)
                msg = stage.get("message", "")
                emoji = self._status_emoji(status)
                lines.append(f"  {emoji} {name:<12} {status:<10} ({duration:.1f}s)")
                if msg:
                    lines.append(f"      {msg[:70]}")
            lines.append("")

        # Test details
        test_stage = self._find_stage(stages, "test")
        if test_stage:
            td = test_stage.get("data", {})
            lines.append(thin)
            lines.append("  TEST SUMMARY")
            lines.append(thin)
            lines.append(f"  Passed:  {td.get('passed', 0)}")
            lines.append(f"  Failed:  {td.get('failed', 0)}")
            lines.append(f"  Skipped: {td.get('skipped', 0)}")
            lines.append(f"  Errors:  {td.get('errors', 0)}")
            lines.append(f"  Total:   {td.get('total', 0)}")
            lines.append("")

            # Individual tests
            tests = td.get("tests", [])
            if tests:
                lines.append("  Individual Tests:")
                for t in tests:
                    name = t.get("name", "?")
                    status = t.get("status", "?")
                    emoji = self._status_emoji(status)
                    lines.append(f"    {emoji} {name}")
                lines.append("")

        lines.append(sep)
        return "\n".join(lines)

    def generate_markdown(self, run: Any) -> str:
        """Generate a Markdown report (for wikis / PR comments)."""
        if isinstance(run, dict):
            data = run
        elif hasattr(run, "to_dict"):
            data = run.to_dict()
        elif hasattr(run, "__dict__"):
            data = run.__dict__
        else:
            return f"```\n{run}\n```"

        status = data.get("status", "unknown")
        status_badge = (
            "![Passed](https://img.shields.io/badge/passed-brightgreen)"
            if status == "passed"
            else "![Failed](https://img.shields.io/badge/failed-red)"
        )

        lines: list[str] = []
        lines.append(f"# CI/CD Report — `{data.get('repo_name', 'N/A')}`")
        lines.append(f"{status_badge}")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        lines.append(f"| **Run ID** | `{data.get('run_id', 'N/A')}` |")
        lines.append(f"| **Status** | **{status.upper()}** |")
        lines.append(f"| **Trigger** | {data.get('trigger', 'N/A')} |")
        lines.append(f"| **Commit** | `{data.get('commit_sha', 'N/A')[:12]}` |")
        lines.append(f"| **Branch** | `{data.get('branch', 'N/A')}` |")
        lines.append(f"| **Duration** | {data.get('total_duration', 0):.1f}s |")
        lines.append(f"| **Started** | {data.get('started_at', 'N/A')} |")
        lines.append(f"| **Finished** | {data.get('finished_at', 'N/A')} |")
        lines.append("")

        # Commit message
        commit_msg = data.get("commit_message", "")
        if commit_msg:
            lines.append(f"> _{commit_msg[:100]}_")
            lines.append("")

        # Stages table
        stages = data.get("stages", [])
        if stages:
            lines.append("## Pipeline Stages")
            lines.append("")
            lines.append("| Stage | Status | Duration | Message |")
            lines.append("|-------|--------|----------|---------|")
            for stage in stages:
                name = stage.get("stage", "")
                st = stage.get("status", "")
                dur = f"{stage.get('duration', 0):.1f}s"
                msg = stage.get("message", "")[:50]
                lines.append(f"| {name} | {st} | {dur} | {msg} |")
            lines.append("")

        # Test summary
        test_stage = self._find_stage(stages, "test")
        if test_stage:
            td = test_stage.get("data", {})
            lines.append("## Test Results")
            lines.append("")
            lines.append(f"- **Passed:** {td.get('passed', 0)}")
            lines.append(f"- **Failed:** {td.get('failed', 0)}")
            lines.append(f"- **Skipped:** {td.get('skipped', 0)}")
            lines.append(f"- **Errors:** {td.get('errors', 0)}")
            lines.append(f"- **Total:** {td.get('total', 0)}")
            lines.append("")

            tests = td.get("tests", [])
            if tests:
                lines.append("<details><summary>Individual Tests</summary>")
                lines.append("")
                lines.append("| Test | Status |")
                lines.append("|------|--------|")
                for t in tests:
                    lines.append(f"| `{t.get('name', '?')}` | {t.get('status', '?')} |")
                lines.append("")
                lines.append("</details>")
                lines.append("")

        lines.append("---")
        lines.append(f"_Generated by Fleet CI/CD Agent at "
                      f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_")
        return "\n".join(lines)

    def generate_trend(self, runs: list[Any], limit: int = 20) -> dict:
        """Generate a trend report comparing multiple pipeline runs.

        Args:
            runs: List of PipelineRun objects (or dicts).
            limit: Maximum number of runs to analyse.

        Returns:
            A dict with pass rate, average duration, and per-run summaries.
        """
        data_runs = []
        for r in runs:
            if hasattr(r, "to_dict"):
                data_runs.append(r.to_dict())
            elif isinstance(r, dict):
                data_runs.append(r)

        recent = data_runs[-limit:]
        total = len(recent)
        if total == 0:
            return {"total_runs": 0, "message": "No runs to analyse"}

        passed = sum(1 for r in recent if r.get("status") == "passed")
        failed = sum(1 for r in recent if r.get("status") == "failed")
        durations = [r.get("total_duration", 0) for r in recent if r.get("total_duration")]

        avg_duration = sum(durations) / len(durations) if durations else 0
        max_duration = max(durations) if durations else 0
        min_duration = min(durations) if durations else 0

        # Per-run summary
        run_summaries = []
        for r in recent:
            run_summaries.append({
                "run_id": r.get("run_id", ""),
                "repo": r.get("repo_name", ""),
                "status": r.get("status", ""),
                "duration": r.get("total_duration", 0),
                "commit": r.get("commit_sha", "")[:8],
            })

        return {
            "total_runs": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{(passed / total * 100):.1f}%",
            "avg_duration": round(avg_duration, 2),
            "max_duration": round(max_duration, 2),
            "min_duration": round(min_duration, 2),
            "runs": run_summaries,
        }

    # -- Helpers --

    @staticmethod
    def _status_emoji(status: str) -> str:
        mapping = {
            "passed": "✅",
            "failed": "❌",
            "skipped": "⏭️ ",
            "running": "🔄",
            "pending": "⏳",
            "cancelled": "🚫",
            "retrying": "🔁",
        }
        return mapping.get(status, "  ")

    @staticmethod
    def _find_stage(stages: list[dict], name: str) -> Optional[dict]:
        """Find a stage by name in a list of stage dicts."""
        for s in stages:
            if isinstance(s, dict) and s.get("stage") == name:
                return s
            elif hasattr(s, "to_dict") and s.to_dict().get("stage") == name:
                return s.to_dict()
        return None
