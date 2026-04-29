"""Hermes Web UI chat-jobs sidecar.

Serves cursor-resumable SSE replays of chat event logs written by the
WebUI's streaming engine. Read-only; the WebUI owns the writes.

Wire format and protocol live in `log_reader.py`. HTTP entry point in
`server.py`.
"""
