#!/usr/bin/env python3
"""
deploydna_server.py — Thread-safe HTTP & Background Webhook Bridge for DeployDNA.
Supports background Flow webhooks (POST /webhook/deploydna) and a synchronous API (GET /api/audit).
The report lives in Salesforce only: Risk_Score__c plus the PDF attached to the story. Nothing is cached
or exported to disk.

Verdict contract (enforced by ddr.audit_story):
  * Risk_Score__c = 'ANALYZING' once at the start
  * Agentia failure / SOQL fallback run silently: NO intermediate verdict is written
  * ONE final verdict write, verified by read-back
"""
import os
import re
import time
import threading

from flask import Flask, request, jsonify
from flask_cors import CORS

import deploydna as ddr

app = Flask(__name__)
CORS(app)

DEFAULT_ORG      = ddr.DEFAULT_ORG_ALIAS
LOOKBACK         = int(os.getenv("LOOKBACK_DAYS", "365"))
EVIDENCE_MODE    = os.getenv("DEPLOYDNA_EVIDENCE", "auto")
STORY_RE         = re.compile(r"^[A-Za-z0-9_\-]+$")

# Per-story state. A story already being audited is never started twice, and the verdict write
# itself re-triggers the Flow, so triggers are also ignored for COOLDOWN_SECONDS after a run FINISHES.
_STATE_LOCK      = threading.Lock()
IN_FLIGHT        = set()
FINISHED_AT      = {}
COOLDOWN_SECONDS = int(os.getenv("DEPLOYDNA_COOLDOWN", "60"))


def _bool(v):
    return str(v).lower() in ("1", "true", "yes", "on")


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _claim(story, force):
    """Returns None if the audit may start, else the reason it must be skipped."""
    with _STATE_LOCK:
        if story in IN_FLIGHT:
            return "already running"
        since = time.time() - FINISHED_AT.get(story, 0)
        if since < COOLDOWN_SECONDS and not force:
            return f"audited {int(since)}s ago"
        IN_FLIGHT.add(story)
        return None


def _release(story):
    with _STATE_LOCK:
        IN_FLIGHT.discard(story)
        FINISHED_AT[story] = time.time()


def execute_audit_pipeline(story, org, mock=False, force=False, lookback=LOOKBACK, writeback=True):
    """
    Runs one audit. Returns the report, or None if skipped (duplicate / already running).
    Any exception propagates; ddr.audit_story has already replaced ANALYZING with INCOMPLETE.
    """
    org = ddr.sanitize_org_alias(org)
    skip = _claim(story, force)
    if skip:
        print(f"[*] [SKIP] {story}: {skip}. Skipping duplicate trigger.")
        return None
    try:
        print(f"\n[*] [AUDIT START] {story} on org '{org}' (evidence={EVIDENCE_MODE})")
        report = ddr.audit_story(
            story, org, lookback_days=lookback, use_mock=mock,
            quiet=True, evidence_mode=EVIDENCE_MODE, writeback=writeback,
        )
        print(f"[*] [AUDIT COMPLETE] {story} -> Final verdict: {report.get('riskLevel')}")
        return report
    finally:
        _release(story)


def run_audit_background(story, org, mock, force, lookback):
    try:
        execute_audit_pipeline(story, org, mock=mock, force=force, lookback=lookback, writeback=True)
    except Exception as e:
        import traceback
        print(f"[!] [THREAD ERROR] {story}: {e}\n{traceback.format_exc()}")


@app.route("/health", methods=["GET"])
def health():
    with _STATE_LOCK:
        running = sorted(IN_FLIGHT)
    return jsonify({"status": "ok", "org": DEFAULT_ORG, "running": running})


@app.route("/webhook/deploydna", methods=["POST", "GET"])
def api_audit_webhook():
    """Receives background webhook from Salesforce Flow / Apex callout."""
    data  = request.get_json(silent=True) or {}
    story = (data.get("storyName") or request.args.get("story") or "").strip()
    if not story or not STORY_RE.match(story):
        return jsonify({"status": "error", "message": "Missing or invalid storyName"}), 400

    try:
        org = ddr.sanitize_org_alias(data.get("orgAlias") or request.args.get("org"))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    mock     = _bool(data.get("mock")    or request.args.get("mock", "0"))
    force    = _bool(data.get("force") or request.args.get("force", "0"))
    lookback = _int(data.get("lookback") or request.args.get("lookback"), LOOKBACK)

    # Decide here so the caller gets an honest answer; the thread would otherwise skip silently.
    with _STATE_LOCK:
        busy = story in IN_FLIGHT
        recent = time.time() - FINISHED_AT.get(story, 0) < COOLDOWN_SECONDS and not force
    if busy or recent:
        return jsonify({"status": "skipped", "story": story,
                        "message": "audit already running" if busy else "audited moments ago"}), 200

    threading.Thread(target=run_audit_background, args=(story, org, mock, force, lookback),
                     daemon=True).start()
    return jsonify({"status": "queued", "story": story, "org": org,
                    "message": f"DeployDNA background audit started for {story}"}), 200


@app.route("/api/audit", methods=["GET"])
def api_audit():
    story = request.args.get("story", "").strip()
    if not story or not STORY_RE.match(story):
        return jsonify({"ok": False, "error": "Missing or invalid ?story=US-XXXXXXX"}), 400

    try:
        org = ddr.sanitize_org_alias(request.args.get("org"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    mock      = _bool(request.args.get("mock", "0"))
    writeback = _bool(request.args.get("writeback", "1"))
    force     = _bool(request.args.get("force", "0"))
    lookback  = _int(request.args.get("lookback"), LOOKBACK)

    try:
        report = execute_audit_pipeline(story, org, mock=mock, force=force,
                                        lookback=lookback, writeback=writeback)
        if report is None:
            return jsonify({"ok": False, "status": "skipped",
                            "error": "Audit already running or just finished; pass force=1 to re-run."}), 202
        return jsonify({
            "ok": True,
            "story":          story,
            "riskLevel":      report.get("riskLevel"),
            "riskSummary":    report.get("riskSummary"),
            "componentCount": report.get("targetStory", {}).get("componentCount", 0),
            "matchCount":     len(report.get("matches", [])),
            "collisionCount": len(report.get("collisions", [])),
            "generatedAt":    report.get("generatedAt"),
        })
    except ddr.StoryNotFound as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": f"{e}\n\n{traceback.format_exc()}"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print("====================================================")
    print(f"  DeployDNA Headless Server : http://127.0.0.1:{port}")
    print( "  Webhook Endpoint          : POST /webhook/deploydna")
    print(f"  Default Salesforce Org    : {DEFAULT_ORG}")
    print(f"  Evidence mode             : {EVIDENCE_MODE}")
    print("====================================================")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)