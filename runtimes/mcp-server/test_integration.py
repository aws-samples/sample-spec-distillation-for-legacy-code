"""
test_integration.py — Integration tests for the bedrock-distill MCP server.

Validates core functionality using mocked boto3 clients (no real Bedrock calls).
Run with:
    .venv/bin/python -m pytest test_integration.py -v
"""

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bedrock_client
import jobs
import tools
from server import _shutdown_handler


# ── Fixtures & Helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_job_registry():
    """Clear the jobs registry before each test to avoid cross-contamination."""
    jobs._registry.clear()
    yield
    jobs._registry.clear()


def make_mock_client(delay: float = 0):
    """Create a mock bedrock-runtime client returning canned ConverseResult.

    Args:
        delay: Seconds to sleep before returning (simulates latency).

    Returns:
        A MagicMock with a .converse method that returns a valid response.
    """
    client = MagicMock()

    def mock_converse(**kwargs):
        if delay:
            time.sleep(delay)
        return {
            "output": {
                "message": {
                    "content": [
                        {"text": "### RULE-001\nREQUIRE x > 0\nMocked extraction output"}
                    ]
                }
            },
            "usage": {"inputTokens": 5000, "outputTokens": 500},
            "stopReason": "end_turn",
        }

    client.converse = mock_converse
    return client


def make_temp_pages(n: int, tmpdir: str) -> list[str]:
    """Create n temporary page files with synthetic content.

    Args:
        n: Number of pages to create.
        tmpdir: Directory to create pages in.

    Returns:
        List of absolute file paths to the created pages.
    """
    pages = []
    for i in range(n):
        page_path = os.path.join(tmpdir, f"page_{i:03d}.md")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(f"# Page {i}\n\nThis is synthetic page content for page {i}.\n")
        pages.append(page_path)
    return pages


def make_submit_params(pages: list[str], output_dir: str, **overrides) -> dict:
    """Build a standard set of submit parameters.

    Args:
        pages: List of page file paths.
        output_dir: Output directory path.
        **overrides: Override any default parameter.

    Returns:
        Parameters dict ready for handle_submit_distillation.
    """
    params = {
        "pages": pages,
        "still_prompt": "Extract all business rules from this page.",
        "still_name": "business_rules",
        "output_dir": output_dir,
        "mode": "realtime",
        "concurrency": 5,
        "max_tokens": 4096,
        "model_id": "us.anthropic.claude-sonnet-4-6",
    }
    params.update(overrides)
    return params


# ── Tests ────────────────────────────────────────────────────────────────────


class TestSubmitRealtime:
    """test_submit_realtime — Submit 9 pages, verify completion and output files."""

    def test_submit_realtime(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(9, tmpdir)
            mock_client = make_mock_client(delay=0)

            params = make_submit_params(pages, output_dir)
            result = tools.handle_submit_distillation(params, mock_client)

            # Verify job_id returned and no error
            assert "error" not in result
            assert "job_id" in result
            job_id = result["job_id"]
            assert result["status"] == "running"
            assert result["pages_submitted"] == 9

            # Wait for completion (should be fast with no delay)
            deadline = time.time() + 10
            while time.time() < deadline:
                job = jobs.get_job(job_id)
                if job and job.status == "complete":
                    break
                time.sleep(0.1)

            job = jobs.get_job(job_id)
            assert job is not None
            assert job.status == "complete"

            # Verify all 9 result files written
            result_files = []
            for i in range(9):
                path = Path(output_dir) / f"page_{i:03d}" / "business_rules_extract.md"
                assert path.exists(), f"Missing result file: {path}"
                result_files.append(path)

            assert len(result_files) == 9

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestCancelMidRun:
    """test_cancel_mid_run — Submit 50 pages with slow mock, cancel after 1s."""

    def test_cancel_mid_run(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(50, tmpdir)
            # Slow mock: 0.5s per call. With concurrency 5, ~10 calls/s theoretical.
            mock_client = make_mock_client(delay=0.5)

            params = make_submit_params(pages, output_dir, concurrency=5)
            result = tools.handle_submit_distillation(params, mock_client)

            assert "error" not in result
            job_id = result["job_id"]

            # Wait 1 second then cancel
            time.sleep(1.0)

            cancel_result = tools.handle_cancel_distillation(
                {"job_id": job_id}
            )

            assert cancel_result["status"] == "cancelled"

            # Wait for threads to wind down
            time.sleep(1.0)

            job = jobs.get_job(job_id)
            assert job is not None
            assert job.status == "cancelled"

            # Verify partial results preserved on disk
            completed_files = list(Path(output_dir).glob("page_*/business_rules_extract.md"))
            assert len(completed_files) > 0, "Expected at least some partial results on disk"
            assert len(completed_files) < 50, "Expected fewer than 50 results (job was cancelled)"

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestResume:
    """test_resume — Pre-populate 5 results, submit 9 pages, verify only 4 processed."""

    def test_resume(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(9, tmpdir)

            # Pre-populate output_dir with 5 existing result files (page_000 through page_004)
            for i in range(5):
                page_dir = Path(output_dir) / f"page_{i:03d}"
                page_dir.mkdir(parents=True, exist_ok=True)
                result_file = page_dir / "business_rules_extract.md"
                result_file.write_text(
                    f"# Pre-existing result for page {i}\nPrevious extraction.",
                    encoding="utf-8",
                )

            # Track actual calls to call_converse_with_retry
            call_count = {"value": 0}
            original_call = bedrock_client.call_converse_with_retry

            def counting_call(client, model_id, system_prompt, user_content, max_tokens, **kwargs):
                call_count["value"] += 1
                return bedrock_client.ConverseResult(
                    output_text="### RULE-001\nMocked resume output",
                    input_tokens=5000,
                    output_tokens=500,
                    stop_reason="end_turn",
                )

            with patch("bedrock_client.call_converse_with_retry", side_effect=counting_call):
                mock_client = make_mock_client(delay=0)
                params = make_submit_params(pages, output_dir)
                result = tools.handle_submit_distillation(params, mock_client)

                assert "error" not in result
                job_id = result["job_id"]

                # pages_submitted should be 4 (9 total - 5 already done)
                assert result["pages_submitted"] == 4

                # Wait for completion
                deadline = time.time() + 10
                while time.time() < deadline:
                    job = jobs.get_job(job_id)
                    if job and job.status == "complete":
                        break
                    time.sleep(0.1)

                job = jobs.get_job(job_id)
                assert job is not None
                assert job.status == "complete"
                # Only 4 pages should have been processed (5 skipped via resume)
                assert call_count["value"] == 4

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestCheckProgress:
    """test_check_progress — Submit 20 pages with slow mock, poll progress increases."""

    def test_check_progress(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(20, tmpdir)
            # ~0.2s per call with concurrency 3 => slow enough to observe progress
            mock_client = make_mock_client(delay=0.2)

            params = make_submit_params(pages, output_dir, concurrency=3)
            result = tools.handle_submit_distillation(params, mock_client)

            assert "error" not in result
            job_id = result["job_id"]

            # Poll multiple times to observe progress increasing
            progress_values = []
            for _ in range(6):
                time.sleep(0.4)
                check = tools.handle_check_distillation(
                    {"job_id": job_id}
                )
                progress_values.append(check["pages_complete"])

            # Verify pages_complete increases over time
            # At minimum, later polls should show more progress than earlier ones
            assert progress_values[-1] > progress_values[0], (
                f"Expected progress to increase. Got: {progress_values}"
            )

            # Wait for completion to avoid dangling threads
            deadline = time.time() + 15
            while time.time() < deadline:
                job = jobs.get_job(job_id)
                if job and job.status in ("complete", "failed"):
                    break
                time.sleep(0.2)

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestGetResultsMetrics:
    """test_get_results_metrics — Submit 5 pages, verify metrics after completion."""

    def test_get_results_metrics(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(5, tmpdir)
            mock_client = make_mock_client(delay=0.05)  # Small delay for realistic timing

            params = make_submit_params(pages, output_dir)
            result = tools.handle_submit_distillation(params, mock_client)

            assert "error" not in result
            job_id = result["job_id"]

            # Wait for completion
            deadline = time.time() + 10
            while time.time() < deadline:
                job = jobs.get_job(job_id)
                if job and job.status == "complete":
                    break
                time.sleep(0.1)

            job = jobs.get_job(job_id)
            assert job is not None
            assert job.status == "complete"

            # Get results
            results = tools.handle_get_distillation_results({"job_id": job_id})

            assert "error" not in results
            assert results["files_written"] == 5
            assert results["wall_time_s"] > 0
            assert results["pages_per_minute"] > 0
            assert "cost_estimate_usd" not in results  # dollar figures are not this system's fact to assert
            assert results["total_input_tokens"] == 5 * 5000  # 5 pages * 5000 input tokens
            assert results["total_output_tokens"] == 5 * 500  # 5 pages * 500 output tokens

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestInvalidParams:
    """test_invalid_params — Verify error responses for various invalid inputs."""

    def test_empty_pages(self):
        output_dir = tempfile.mkdtemp()
        try:
            params = make_submit_params([], output_dir)
            result = tools.handle_submit_distillation(params, make_mock_client())
            assert result["error"] is True
            assert "pages" in result["message"].lower()
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_nonexistent_file(self):
        output_dir = tempfile.mkdtemp()
        try:
            params = make_submit_params(
                ["/nonexistent/path/fake_page.md"], output_dir
            )
            result = tools.handle_submit_distillation(params, make_mock_client())
            assert result["error"] is True
            assert "does not exist" in result["message"]
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_empty_still_prompt(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(1, tmpdir)
            params = make_submit_params(pages, output_dir, still_prompt="")
            result = tools.handle_submit_distillation(params, make_mock_client())
            assert result["error"] is True
            assert "still_prompt" in result["message"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_invalid_mode(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(1, tmpdir)
            params = make_submit_params(pages, output_dir, mode="invalid_mode")
            result = tools.handle_submit_distillation(params, make_mock_client())
            assert result["error"] is True
            assert "mode" in result["message"].lower()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_unknown_job_id(self):
        result = tools.handle_check_distillation(
            {"job_id": "nonexistent-job-id"}
        )
        assert result["error"] is True
        assert "not found" in result["message"].lower()

    def test_unknown_job_id_cancel(self):
        result = tools.handle_cancel_distillation(
            {"job_id": "nonexistent-job-id"}
        )
        assert result["error"] is True
        assert "not found" in result["message"].lower()

    def test_unknown_job_id_results(self):
        result = tools.handle_get_distillation_results(
            {"job_id": "nonexistent-job-id"}
        )
        assert result["error"] is True
        assert "not found" in result["message"].lower()


class TestGracefulShutdown:
    """test_graceful_shutdown — Verify _shutdown_handler cancels all running jobs."""

    def test_graceful_shutdown(self):
        tmpdir = tempfile.mkdtemp()
        output_dir = tempfile.mkdtemp()
        try:
            pages = make_temp_pages(30, tmpdir)
            mock_client = make_mock_client(delay=0.5)

            params = make_submit_params(pages, output_dir, concurrency=3)
            result = tools.handle_submit_distillation(params, mock_client)

            assert "error" not in result
            job_id = result["job_id"]

            # Give it a moment to start processing
            time.sleep(0.5)

            # Verify the job is running
            job = jobs.get_job(job_id)
            assert job is not None
            assert job.status == "running"

            # Call the shutdown handler (patch sys.exit to avoid killing test process)
            with patch("sys.exit") as mock_exit:
                _shutdown_handler(None, None)

            # Verify all jobs are cancelled
            job = jobs.get_job(job_id)
            assert job is not None
            assert job.status == "cancelled"

            # sys.exit(0) should have been called
            mock_exit.assert_called_once_with(0)

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)


class TestModelRejectsTemperature:
    """Newer models (sonnet-5, opus-5) deprecate the `temperature` param and return HTTP 400
    if it is sent; the Converse call sites must omit it for those families."""

    def test_rejects_for_newer_families(self):
        assert bedrock_client._model_rejects_temperature("anthropic.claude-sonnet-5") is True
        assert bedrock_client._model_rejects_temperature("anthropic.claude-opus-5-5") is True
        assert bedrock_client._model_rejects_temperature("anthropic.claude-opus-5") is True

    def test_allows_for_sonnet_4_6(self):
        # sonnet-4-6 is the current working default; it still accepts temperature.
        assert bedrock_client._model_rejects_temperature("anthropic.claude-sonnet-4-6") is False
