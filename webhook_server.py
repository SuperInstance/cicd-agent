"""
GitHub Webhook Receiver
========================
HTTP server that receives GitHub push webhooks and triggers CI/CD pipelines.
Uses only stdlib (http.server, hmac, json, hashlib).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet.cicd.webhook")


class WebhookServer:
    """Receive GitHub push webhooks and trigger pipelines.

    Endpoints:
    - ``POST /webhook/github`` — receive push events
    - ``GET /status``          — pipeline status
    - ``GET /history``         — recent pipeline runs

    Usage::

        def handle_push(repo_name, commit_sha, message, branch):
            print(f"Push: {repo_name} {commit_sha[:8]}")

        server = WebhookServer(port=9000, secret="my-secret", on_push=handle_push)
        server.serve_forever()
    """

    def __init__(
        self,
        port: int = 9000,
        secret: str = "",
        on_push: Optional[Callable] = None,
        status_provider: Optional[Callable] = None,
        history_provider: Optional[Callable] = None,
    ):
        self.port = port
        self.secret = secret
        self.on_push = on_push or (lambda *a: None)
        self.status_provider = status_provider or (lambda: {"status": "ok"})
        self.history_provider = history_provider or (lambda: [])
        self.received_events: list[dict] = []
        self._lock = threading.Lock()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def serve_forever(self):
        """Start the HTTP server (blocks until shutdown)."""
        handler = _make_handler(self)
        self._server = HTTPServer(("0.0.0.0", self.port), handler)
        logger.info("Webhook server listening on port %d", self.port)
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Webhook server interrupted")
        finally:
            self._server.server_close()

    def serve_in_thread(self):
        """Start the HTTP server in a background thread."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Webhook server started in background on port %d", self.port)

    def shutdown(self):
        """Gracefully stop the server."""
        if self._server:
            self._server.shutdown()
            logger.info("Webhook server shut down")

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify the HMAC-SHA256 signature from a GitHub webhook.

        Args:
            payload: Raw request body bytes.
            signature: Value of the ``X-Hub-Signature-256`` header
                       (e.g. ``sha256=abcdef...``).

        Returns:
            True if the signature is valid or if no secret is configured.
        """
        if not self.secret:
            return True

        if not signature or not signature.startswith("sha256="):
            return False

        expected = "sha256=" + hmac.new(
            self.secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def record_event(self, event: dict) -> None:
        """Record a received webhook event for history."""
        with self._lock:
            self.received_events.append({
                **event,
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
            # Keep only the last 200 events
            if len(self.received_events) > 200:
                self.received_events = self.received_events[-200:]


# ---------------------------------------------------------------------------
# Request Handler Factory
# ---------------------------------------------------------------------------

def _make_handler(server: WebhookServer):
    """Create a BaseHTTPRequestHandler class that closes over the WebhookServer."""

    class _Handler(BaseHTTPRequestHandler):
        """Handles incoming HTTP requests for the webhook server."""

        def log_message(self, format, *args):
            logger.debug("Webhook HTTP: %s", format % args)

        def do_GET(self):
            """Handle GET requests."""
            if self.path == "/status":
                self._handle_status()
            elif self.path == "/history":
                self._handle_history()
            elif self.path == "/health":
                self._send_json(200, {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()})
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            """Handle POST requests."""
            if self.path == "/webhook/github":
                self._handle_github_webhook()
            elif self.path == "/webhook":
                self._handle_github_webhook()
            else:
                self._send_json(404, {"error": "Not found"})

        # -- Handlers --

        def _handle_github_webhook(self):
            """Process a GitHub webhook push event."""
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json(400, {"error": "Empty payload"})
                return

            payload = self.rfile.read(content_length)

            # Verify HMAC signature
            signature = self.headers.get("X-Hub-Signature-256", "")
            if not server.verify_signature(payload, signature):
                logger.warning("Webhook signature verification failed")
                self._send_json(403, {"error": "Invalid signature"})
                return

            # Parse payload
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"Invalid JSON: {e}"})
                return

            # Determine event type
            event_type = self.headers.get("X-GitHub-Event", "push")

            if event_type == "ping":
                logger.info("Received ping event")
                self._send_json(200, {"msg": "pong"})
                return

            if event_type == "push":
                self._process_push_event(data)
            elif event_type == "pull_request":
                self._process_pull_request_event(data)
            else:
                logger.info("Unsupported event type: %s", event_type)
                self._send_json(200, {"msg": f"Event {event_type} acknowledged"})

        def _process_push_event(self, data: dict):
            """Parse a push event and trigger the pipeline."""
            try:
                repo_name = self._extract_repo_name(data)
                commits = data.get("commits", [])
                branch = self._extract_branch(data)
                after_sha = data.get("after", "")

                if not commits:
                    self._send_json(200, {"msg": "No commits in push"})
                    return

                latest = commits[-1]
                commit_sha = latest.get("id", after_sha)
                message = latest.get("message", "")
                added = latest.get("added", [])
                modified = latest.get("modified", [])
                removed = latest.get("removed", [])

                event = {
                    "event": "push",
                    "repo": repo_name,
                    "commit_sha": commit_sha,
                    "message": message,
                    "branch": branch,
                    "files": {
                        "added": added,
                        "modified": modified,
                        "removed": removed,
                    },
                }
                server.record_event(event)

                logger.info(
                    "Push event: repo=%s sha=%s branch=%s msg=%s",
                    repo_name, commit_sha[:8], branch, message[:60],
                )

                # Trigger the pipeline callback
                try:
                    server.on_push(repo_name, commit_sha, message, branch)
                except Exception as e:
                    logger.error("Pipeline callback error: %s", e)

                self._send_json(200, {
                    "status": "accepted",
                    "run_id": f"webhook-{commit_sha[:8]}",
                    "repo": repo_name,
                })

            except Exception as e:
                logger.error("Error processing push event: %s", e)
                self._send_json(500, {"error": str(e)})

        def _process_pull_request_event(self, data: dict):
            """Parse a pull request event."""
            try:
                action = data.get("action", "unknown")
                pr = data.get("pull_request", {})
                repo_name = self._extract_repo_name(data)
                head = pr.get("head", {})
                branch = head.get("ref", "")
                sha = head.get("sha", "")

                event = {
                    "event": "pull_request",
                    "action": action,
                    "repo": repo_name,
                    "branch": branch,
                    "commit_sha": sha,
                }
                server.record_event(event)

                logger.info(
                    "PR event: repo=%s action=%s branch=%s sha=%s",
                    repo_name, action, branch, sha[:8],
                )

                self._send_json(200, {
                    "status": "accepted",
                    "event": "pull_request",
                    "action": action,
                })
            except Exception as e:
                logger.error("Error processing PR event: %s", e)
                self._send_json(500, {"error": str(e)})

        def _handle_status(self):
            """Return current pipeline status."""
            status = server.status_provider()
            self._send_json(200, status)

        def _handle_history(self):
            """Return recent pipeline runs and webhook events."""
            history = server.history_provider()
            with server._lock:
                events = list(server.received_events[-20:])
            self._send_json(200, {"runs": history, "events": events})

        # -- Utilities --

        def _send_json(self, code: int, data: Any):
            """Send a JSON response."""
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _extract_repo_name(data: dict) -> str:
            """Extract the repository name from a webhook payload."""
            repo = data.get("repository", {})
            full_name = repo.get("full_name", "")
            name = repo.get("name", "")
            return full_name or name or "unknown"

        @staticmethod
        def _extract_branch(data: dict) -> str:
            """Extract the branch name from a push webhook payload."""
            ref = data.get("ref", "")
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
            return ref

    _Handler.__name__ = "WebhookHandler"
    return _Handler


# ---------------------------------------------------------------------------
# Webhook Parsing Utilities (for testing)
# ---------------------------------------------------------------------------

def parse_github_push_event(payload: dict) -> dict:
    """Parse a GitHub push webhook payload into a structured event.

    Returns a dict with: repo, branch, commits, changed_files.
    """
    repo = payload.get("repository", {})
    ref = payload.get("ref", "")
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    commits = payload.get("commits", [])

    all_changed = set()
    for c in commits:
        all_changed.update(c.get("added", []))
        all_changed.update(c.get("modified", []))
        all_changed.update(c.get("removed", []))

    return {
        "repo": repo.get("full_name", repo.get("name", "unknown")),
        "branch": branch,
        "after": payload.get("after", ""),
        "commits": [
            {
                "sha": c.get("id", ""),
                "author": c.get("author", {}).get("name", ""),
                "message": c.get("message", ""),
                "added": c.get("added", []),
                "modified": c.get("modified", []),
                "removed": c.get("removed", []),
            }
            for c in commits
        ],
        "changed_files": sorted(all_changed),
    }
