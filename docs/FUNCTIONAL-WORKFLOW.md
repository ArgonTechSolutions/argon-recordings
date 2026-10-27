# Speakers → video → agent summary

This is the local functional workflow. Deployment and private client pairing remain separate. Model optimization is deferred; automatic speaker labels are experimental and can be corrected without rerunning transcription.

## In the recording

1. Open a processed recording and expand **Review speakers**. Add a name with **Add speaker**, select a time range and use **Assign range**. Rename, reset a range or undo an edit as needed. This works without a diarization model or automatic result. If an analysis worker is configured, **Analyze speakers** is optional. Speaker edits preserve transcript text.
2. Mark a useful point/range. Extract a readable still or a short motion clip under visual evidence; use source resolution for small text and higher source-supported frame rate for motion. Zoom a screenshot or play a clip against the recording audio. Automatic visual suggestions require explicit acceptance; manual marks win.
3. Open **Agent access** in the account controls. Select the project and expiry. Enable screenshot/clip requests if the agent should create missing evidence. Create access and save the one-time token using your client's private credential mechanism. Tokens expire and can be revoked here. This step is performed by the owner; development does not provision real credentials.

## Connect the agent

Build the private package from the repository:

```sh
python3 scripts/agent-integration/build_plugin.py /tmp/argon-recordings-plugin.zip
```

Unpack it in your chosen private directory, create a Python 3.11+ virtual environment and install its pinned `requirements.txt`. Configure your client's stdio MCP server with that environment's Python and the unpacked `server.py`. Supply `ARGON_RECORDINGS_API_ORIGIN` and `ARGON_AGENT_TOKEN_FILE` privately. The token file must be a regular non-symlink file with mode `0600`. Use HTTPS for connections beyond loopback. The app's **Connection setup** shows its origin; the agent host must be able to reach it. No remote/cloud transport or public ingress is configured by this package.

Load the packaged `skills/recording-summary/SKILL.md` in the client, or install the private plugin if the client supports its manifest. Exact credential-free configuration and package layout: [adapter README](../scripts/agent-integration/README.md). Client registration and secret-file creation are interactive owner setup, not a background development step.

Suggested request (replace the project/recording and language):

> Use recording-summary to summarize this recording in Spanish. Read all transcript pages and the reviewed speaker labels/marks. Inspect useful screenshots; request missing evidence if permitted. Cite source times, distinguish explicit statements from inference, and leave unsupported owners or dates unknown. Return a readable summary and an importable report draft JSON. Refresh source/evidence revisions before finalizing.

The agent runs synthesis; the app does not need a hosted LLM account. A missing derive permission permits reading retained evidence but prevents requesting new images/clips. Local stdio integration does not establish reachability from another host.

## Review and export

Import the generated JSON under **Reports**, save the draft, inspect its citations/images and mark it reviewed. **Export ZIP** includes the report, selected images, transcript/speaker snapshot, source provenance and checksums. A later edit makes the report stale rather than silently rewriting the reviewed snapshot; regenerate against current evidence when needed.

The skill's report-draft command/schema and revision checks are described in [SUMMARY-WORKFLOW.md](SUMMARY-WORKFLOW.md) and [REPORTS.md](REPORTS.md). A structurally valid draft still needs your review for factual accuracy. Automatic model quality, Spanish/private-recording acceptance, installed Philemon behavior and actual owner-client pairing remain separate from this local workflow.
