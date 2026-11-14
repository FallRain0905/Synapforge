"""Studio 控制面：127.0.0.1 上的 HTTP + 单页控制台。

只绑回环地址，启动时生成一次性 token；写接口校验 token 与 Origin。
所有业务调用 `studio/commands.py`，handler 不写 SQL。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import commands as base
from ..db import open_store
from ..paths import state_dir
from . import agents as studio_agents
from . import backends
from . import capabilities as studio_capabilities
from . import commands as studio_commands
from . import providers as studio_providers
from . import plugins as studio_plugins
from . import profiles as studio_profiles
from . import routing as studio_routing
from . import sessions as studio_sessions
from . import store as studio_store
from .agent_runtime import AgentRuntimeManager
from .commands import StudioError
from .supervisor import SupervisorManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "synapforge-studio/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 — 安静
        return

    # ---- 基础 --------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _fail(self, code: str, errors: list[str] | None = None, status: int = 400) -> None:
        self._json({"ok": False, "code": code, "errors": errors or []}, status)

    def _token(self) -> str:
        header = (self.headers.get("X-Studio-Token") or "").strip()
        if header:
            return header
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0].strip()

    def _authorized(self) -> bool:
        expected = getattr(self.server, "token", "")
        given = self._token()
        return bool(expected) and bool(given) and hmac.compare_digest(expected, given)

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        port = getattr(self.server, "server_port", 0)
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        return origin in allowed

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StudioError("invalid_json", [str(error)]) from error
        if not isinstance(value, dict):
            raise StudioError("invalid_json", ["body 必须是对象"])
        return value

    def _store(self) -> Any:
        return open_store(getattr(self.server, "state_path", None))

    # ---- GET ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html", "/workbench"):
            index = STATIC_DIR / "workbench.html"
            if not index.exists():
                self._send(500, b"studio asset missing", "text/plain; charset=utf-8")
                return
            self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/clusters", "/studio"):
            index = STATIC_DIR / "studio.html"
            if not index.exists():
                self._send(500, b"studio asset missing", "text/plain; charset=utf-8")
                return
            self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if not path.startswith("/api/studio/"):
            self._fail("not_found", status=404)
            return
        if not self._authorized():
            self._fail("unauthorized", status=401)
            return
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 5 and parts[2] == "runs" and parts[4] == "stream":
            self._stream_run(parts[3])
            return
        try:
            self._route_get(path, parse_qs(parsed.query))
        except StudioError as error:
            self._fail(error.code, error.errors)
        except Exception as error:  # noqa: BLE001
            self._fail("studio_error", [f"{type(error).__name__}: {error}"], status=500)

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        # /api/studio/...
        tail = parts[2:]
        if tail == ["state"]:
            cluster_id = (query.get("cluster_id") or [None])[0]
            store = self._store()
            try:
                if cluster_id:
                    detail = studio_commands.cluster_detail(store, cluster_id)
                    self._json({"ok": True, "cluster": detail,
                                "agents": [asdict(p) for p in backends.probe_agents()],
                                "supervisor": getattr(self.server, "manager").active()})
                else:
                    self._json({"ok": True, "clusters": studio_commands.list_clusters(store),
                                "cluster": None,
                                "agents": [asdict(p) for p in backends.probe_agents()],
                                "supervisor": getattr(self.server, "manager").active()})
            finally:
                store.close()
            return
        if tail == ["clusters"]:
            store = self._store()
            try:
                self._json({"ok": True, "clusters": studio_commands.list_clusters(store)})
            finally:
                store.close()
            return
        if len(tail) == 2 and tail[0] == "clusters":
            store = self._store()
            try:
                self._json({"ok": True, "cluster": studio_commands.cluster_detail(store, tail[1])})
            finally:
                store.close()
            return
        if len(tail) == 3 and tail[0] == "roles" and tail[2] == "logs":
            lines = int((query.get("lines") or ["200"])[0] or 200)
            text = getattr(self.server, "manager").log_tail(tail[1], lines)
            self._json({"ok": True, "role_id": tail[1], "log": text})
            return
        if tail == ["events"]:
            cluster_id = (query.get("cluster_id") or [None])[0]
            if not cluster_id:
                self._fail("cluster_required", ["cluster_id: 必填"])
                return
            store = self._store()
            try:
                cluster = studio_store.get_cluster(store, cluster_id)
                if cluster is None:
                    raise StudioError("cluster_not_found", [cluster_id])
                limit = int((query.get("limit") or ["100"])[0] or 100)
                after = (query.get("after") or [None])[0]
                events = base.recent_events(store, project_id=cluster["project_id"],
                                            limit=limit,
                                            after_sequence=int(after) if after else None)
                self._json({"ok": True, "events": events})
            finally:
                store.close()
            return
        if tail == ["executors"]:
            self._json({"ok": True, "agents": [asdict(p) for p in backends.probe_agents()]})
            return
        if tail == ["models"]:
            store = self._store()
            try:
                self._json({"ok": True, **studio_providers.catalog(store)})
            finally:
                store.close()
            return
        if tail == ["providers"]:
            store = self._store()
            try:
                self._json({"ok": True, "providers": studio_providers.list_providers(store)})
            finally:
                store.close()
            return
        if len(tail) == 3 and tail[0] == "providers" and tail[2] == "keys":
            store = self._store()
            try:
                provider = studio_store.get_provider(store, tail[1])
                if provider is None:
                    raise StudioError("provider_not_found", [tail[1]])
                self._json({"ok": True, **studio_routing.route_summary(store, provider)})
            finally:
                store.close()
            return
        if tail == ["routes"]:
            include_remote = (query.get("remote") or ["1"])[0] not in ("0", "false", "no")
            store = self._store()
            try:
                routes = studio_providers.scan_local_routes(store=store,
                                                            include_remote=include_remote)
                self._json({"ok": True, "routes": routes})
            finally:
                store.close()
            return
        if tail == ["library"]:
            store = self._store()
            try:
                agents = studio_store.list_agents(store)
                plugins = studio_plugins.list_plugins(store)
            finally:
                store.close()
            skills = studio_capabilities.find_skills(Path.home())
            mcp = studio_capabilities.find_mcp(Path.home(), agents)
            skill_users: dict[str, list[str]] = {}
            mcp_users: dict[str, list[str]] = {}
            plugin_users: dict[str, list[str]] = {}
            for agent in agents:
                for item in agent.get("skills") or []:
                    skill_users.setdefault(str(item), []).append(agent["name"])
                for item in agent.get("mcp") or []:
                    mcp_users.setdefault(str(item), []).append(agent["name"])
                for item in agent.get("plugins") or []:
                    plugin_users.setdefault(str(item), []).append(agent["name"])
            self._json({
                "ok": True,
                "skills": [{**item, "agents": skill_users.get(item["id"], [])}
                           for item in skills],
                "mcp": [{**item, "agents": mcp_users.get(item["id"], [])} for item in mcp],
                "plugins": [{**item, "agents": plugin_users.get(item["id"], [])}
                            for item in plugins],
                "agents": [{"id": a["id"], "name": a["name"]} for a in agents],
                "counts": {"skills": len(skills), "mcp": len(mcp),
                           "plugins": len(plugins), "agents": len(agents)},
            })
            return
        if tail == ["capabilities"]:
            store = self._store()
            try:
                agents = studio_store.list_agents(store)
            finally:
                store.close()
            self._json({"ok": True, **studio_capabilities.inventory(agents=agents)})
            return
        if tail == ["overview"]:
            store = self._store()
            try:
                payload = studio_agents.overview(store)
                payload["executors"] = [asdict(p) for p in backends.probe_agents()]
                self._json({"ok": True, **payload})
            finally:
                store.close()
            return
        if tail == ["profiles"]:
            store = self._store()
            try:
                self._json({"ok": True, "profiles": studio_profiles.list_profiles(store)})
            finally:
                store.close()
            return
        if tail == ["plugins"]:
            store = self._store()
            try:
                self._json({"ok": True, "plugins": studio_plugins.list_plugins(store)})
            finally:
                store.close()
            return
        if tail == ["sessions"]:
            agent_id = (query.get("agent_id") or [None])[0]
            text = (query.get("q") or [""])[0]
            include_archived = (query.get("archived") or ["0"])[0] not in ("0", "false", "no")
            store = self._store()
            try:
                self._json({"ok": True, "sessions": studio_sessions.list_sessions(
                    store, agent_id=agent_id or None, query=text,
                    include_archived=include_archived)})
            finally:
                store.close()
            return
        if len(tail) == 3 and tail[0] == "sessions" and tail[2] == "export":
            store = self._store()
            try:
                filename, markdown = studio_sessions.export_markdown(store, tail[1])
                self._json({"ok": True, "filename": filename, "markdown": markdown})
            finally:
                store.close()
            return
        if len(tail) == 2 and tail[0] == "sessions":
            store = self._store()
            try:
                self._json({"ok": True, **studio_sessions.session_detail(store, tail[1])})
            finally:
                store.close()
            return
        if tail == ["runs"]:
            store = self._store()
            try:
                self._json({"ok": True, "runs": studio_agents.all_runs(store)})
            finally:
                store.close()
            return
        if len(tail) == 2 and tail[0] == "runs":
            store = self._store()
            try:
                run = studio_store.get_agent_run(store, tail[1])
                if run is None:
                    raise StudioError("run_not_found", [tail[1]])
                agent = studio_store.get_agent(store, run["agent_id"])
                provider = (studio_store.get_provider(store, run["provider_id"])
                            if run.get("provider_id") else None)
                run["provider_name"] = provider["name"] if provider else "本机默认"
                steps = studio_store.list_agent_steps(store, run["id"])
                self._json({"ok": True, "run": run, "steps": steps,
                            "agent": agent})
            finally:
                store.close()
            return
        if tail == ["artifacts"]:
            store = self._store()
            try:
                self._json({"ok": True, "artifacts": studio_agents.all_artifacts(store)})
            finally:
                store.close()
            return
        if tail == ["usage"]:
            store = self._store()
            try:
                self._json({"ok": True, "usage": studio_agents.usage_summary(store)})
            finally:
                store.close()
            return
        if tail == ["settings"]:
            raw_state = getattr(self.server, "state_path", None)
            state_dir_path = (Path(raw_state).expanduser().resolve().parent
                              if raw_state else state_dir())
            store = self._store()
            try:
                catalog = studio_providers.catalog(store)
                providers = studio_providers.list_providers(store)
            finally:
                store.close()
            self._json({"ok": True,
                        "models": catalog,
                        "providers": providers,
                        "executors": [asdict(p) for p in backends.probe_agents()],
                        "state_dir": str(state_dir_path),
                        "claude_settings": str(Path.home() / ".claude" / "settings.json"),
                        "default_model": catalog["default"]})
            return
        if tail == ["agents"]:
            store = self._store()
            try:
                catalog = studio_providers.catalog(store)
                self._json({"ok": True, "agents": studio_agents.list_agents(store),
                            "models": catalog, "providers": studio_providers.list_providers(store)})
            finally:
                store.close()
            return
        if len(tail) == 3 and tail[0] == "agents" and tail[2] == "extensions":
            store = self._store()
            try:
                self._json({"ok": True,
                           **studio_agents.agent_extensions(store, tail[1])})
            finally:
                store.close()
            return
        if len(tail) == 3 and tail[0] == "agents" and tail[2] == "memory":
            store = self._store()
            try:
                self._json({"ok": True,
                           **studio_agents.agent_memory(store, tail[1])})
            finally:
                store.close()
            return
        if len(tail) == 2 and tail[0] == "agents":
            after = int((query.get("after_seq") or ["0"])[0] or 0)
            store = self._store()
            try:
                self._json({"ok": True, "agent": studio_agents.agent_state(
                    store, tail[1], after_seq=after)})
            finally:
                store.close()
            return
        if len(tail) == 2 and tail[0] == "artifacts":
            store = self._store()
            try:
                payload = base.read_artifact(store, artifact_id=tail[1])
                text = payload.get("text")
                self._json({"ok": True, **payload,
                            "text": (text[:12000] if isinstance(text, str) else None),
                            "truncated": bool(isinstance(text, str) and len(text) > 12000)})
            finally:
                store.close()
            return
        self._fail("not_found", status=404)

    def _stream_run(self, run_id: str) -> None:
        store = self._store()
        try:
            run = studio_store.get_agent_run(store, run_id)
            steps = studio_store.list_agent_steps(store, run_id)
        finally:
            store.close()
        if run is None:
            self._fail("run_not_found", [run_id], status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(event: str, data: dict[str, Any]) -> None:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        manager: AgentRuntimeManager = getattr(self.server, "agent_manager")
        box = manager.subscribe(run_id)
        try:
            for step in steps:
                send("step", {"type": "step", "step": step})
            send("run", {"type": "run", "run": run})
            # QUEUED 也要保持连接：worker 起来后事件会从同一个 broker 推过来
            if run["status"] not in ("RUNNING", "QUEUED"):
                send("done", {"type": "done"})
                return
            while True:
                try:
                    event = box.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event.get("type") == "done":
                    send("done", event)
                    break
                send(str(event.get("type") or "message"), event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            manager.unsubscribe(run_id, box)

    # ---- POST / PATCH ------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        self._mutate("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._mutate("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._mutate("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._mutate("DELETE")

    def _mutate(self, method: str) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/studio/"):
            self._fail("not_found", status=404)
            return
        if not self._authorized():
            self._fail("unauthorized", status=401)
            return
        if not self._origin_ok():
            self._fail("origin_rejected", status=403)
            return
        try:
            body = self._read_json()
            self._route_mutate(method, parsed.path, body)
        except StudioError as error:
            self._fail(error.code, error.errors)
        except Exception as error:  # noqa: BLE001
            self._fail("studio_error", [f"{type(error).__name__}: {error}"], status=500)

    def _route_mutate(self, method: str, path: str, body: dict[str, Any]) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        tail = parts[2:]
        manager: SupervisorManager = getattr(self.server, "manager")
        agent_manager: AgentRuntimeManager = getattr(self.server, "agent_manager")

        if (method == "POST" and tail == ["skills", "import"]):
            store = self._store()
            try:
                result = studio_capabilities.import_plugin_folder(
                    str(body.get("path") or ""),
                    home=getattr(self.server, "skills_home", None))
                self._json({"ok": True, **result}, status=201)
            except ValueError as error:
                self._fail("skill_import_failed", [str(error)])
            finally:
                store.close()
            return

        if method == "POST" and tail == ["profiles"]:
            store = self._store()
            try:
                profile = studio_profiles.save_profile(
                    store, name=str(body.get("name") or ""),
                    note=str(body.get("note") or ""),
                    profile_id=str(body.get("id") or ""))
                self._json({"ok": True, "profile": profile}, status=201)
            finally:
                store.close()
            return

        if method == "PATCH" and len(tail) == 2 and tail[0] == "profiles":
            store = self._store()
            try:
                profile = studio_profiles.update_profile_meta(
                    store, tail[1], name=body.get("name"), note=body.get("note"))
                self._json({"ok": True, "profile": profile})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "profiles"
                and tail[2] == "apply"):
            store = self._store()
            try:
                self._json({"ok": True, **studio_profiles.apply_profile(store, tail[1])})
            finally:
                store.close()
            return

        if method == "DELETE" and len(tail) == 2 and tail[0] == "profiles":
            store = self._store()
            try:
                self._json({"ok": True, **studio_profiles.delete_profile(store, tail[1])})
            finally:
                store.close()
            return

        if method == "POST" and tail == ["plugins"]:
            store = self._store()
            try:
                plugin = studio_plugins.install_plugin(
                    store, str(body.get("path") or ""),
                    state_path=getattr(self.server, "state_path", None))
                self._json({"ok": True, "plugin": plugin}, status=201)
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "plugins"
                and tail[2] == "update"):
            store = self._store()
            try:
                plugin = studio_plugins.update_plugin(
                    store, tail[1],
                    state_path=getattr(self.server, "state_path", None))
                self._json({"ok": True, "plugin": plugin})
            finally:
                store.close()
            return

        if method == "DELETE" and len(tail) == 2 and tail[0] == "plugins":
            store = self._store()
            try:
                result = studio_plugins.delete_plugin(
                    store, tail[1],
                    state_path=getattr(self.server, "state_path", None))
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if method == "PATCH" and len(tail) == 2 and tail[0] == "sessions":
            store = self._store()
            try:
                session = studio_sessions.rename_session(
                    store, tail[1], str(body.get("title") or ""))
                self._json({"ok": True, "session": session})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "sessions"
                and tail[2] == "activate"):
            store = self._store()
            try:
                agent = studio_sessions.activate_session(store, tail[1])
                self._json({"ok": True, "agent": agent})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "sessions"
                and tail[2] == "archive"):
            store = self._store()
            try:
                archived = body.get("archived")
                session = studio_sessions.toggle_archive(
                    store, tail[1],
                    archived=None if archived is None else bool(archived))
                self._json({"ok": True, "session": session})
            finally:
                store.close()
            return

        if method == "POST" and tail == ["agents"]:
            store = self._store()
            try:
                agent = studio_agents.create_agent(
                    store, name=str(body.get("name") or ""),
                    model=str(body.get("model") or ""),
                    system_prompt=str(body.get("system_prompt") or ""),
                    workspace=str(body.get("workspace") or ""),
                    allow_bash=bool(body.get("allow_bash")),
                    allow_web=bool(body.get("allow_web", True)),
                    backend=str(body.get("backend") or "claude"),
                    provider_id=str(body.get("provider_id") or ""),
                    command=str(body.get("command") or ""))
                self._json({"ok": True, "agent": agent}, status=201)
            finally:
                store.close()
            return

        if method == "POST" and tail == ["providers"]:
            store = self._store()
            try:
                provider = studio_providers.create_provider(
                    store, name=str(body.get("name") or ""),
                    kind=str(body.get("kind") or "anthropic"),
                    base_url=str(body.get("base_url") or ""),
                    api_key=str(body.get("api_key") or ""),
                    models=body.get("models") or [],
                    note=str(body.get("note") or ""),
                    enabled=bool(body.get("enabled", True)),
                    is_default=bool(body.get("is_default")))
                self._json({"ok": True, "provider": provider}, status=201)
            finally:
                store.close()
            return

        if method == "POST" and tail == ["providers", "import"]:
            store = self._store()
            try:
                provider = studio_providers.import_claude_settings(store)
                self._json({"ok": True, "provider": provider}, status=201)
            finally:
                store.close()
            return

        if method == "POST" and tail == ["providers", "import-route"]:
            store = self._store()
            try:
                provider = studio_providers.import_route(
                    store, url=str(body.get("url") or ""),
                    kind=str(body.get("kind") or "anthropic"),
                    name=str(body.get("name") or ""))
                self._json({"ok": True, "provider": provider}, status=201)
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "providers"
                and tail[2] == "test"):
            store = self._store()
            try:
                result = studio_providers.test_provider(
                    store, tail[1], deep=bool(body.get("deep")),
                    model=str(body.get("model") or ""))
                provider = studio_store.get_provider(store, tail[1])
                self._json({"ok": True, "check": result,
                            "route": studio_routing.route_summary(store, provider)
                            if provider else None})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 4 and tail[0] == "providers"
                and tail[2] == "keys" and tail[3] == "test"):
            store = self._store()
            try:
                result = studio_routing.test_key(store, tail[1], str(body.get("key_id") or ""))
                self._json({"ok": True, "check": result,
                            **studio_routing.route_summary(
                                store, studio_store.get_provider(store, tail[1]))})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "providers" and tail[2] == "keys":
            store = self._store()
            try:
                pool = studio_routing.add_key(
                    store, tail[1], api_key=str(body.get("api_key") or ""),
                    label=str(body.get("label") or ""),
                    enabled=bool(body.get("enabled", True)))
                self._json({"ok": True, **pool}, status=201)
            finally:
                store.close()
            return

        if (method in ("PATCH", "DELETE") and len(tail) == 4
                and tail[0] == "providers" and tail[2] == "keys"):
            store = self._store()
            try:
                if method == "DELETE":
                    pool = studio_routing.delete_key(store, tail[1], tail[3])
                else:
                    pool = studio_routing.update_key(
                        store, tail[1], tail[3],
                        enabled=body.get("enabled"), label=body.get("label"))
                self._json({"ok": True, **pool})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 4 and tail[0] == "providers"
                and tail[2] == "keys" and tail[3] == "restore"):
            store = self._store()
            try:
                pool = studio_routing.restore_key(store, tail[1], str(body.get("key_id") or ""))
                self._json({"ok": True, **pool})
            finally:
                store.close()
            return

        if (method in ("PATCH", "DELETE") and len(tail) == 2
                and tail[0] == "providers"):
            store = self._store()
            try:
                if method == "DELETE":
                    result = studio_providers.delete_provider(store, tail[1])
                    self._json({"ok": True, **result})
                else:
                    provider = studio_providers.update_provider(store, tail[1], **body)
                    self._json({"ok": True, "provider": provider})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "agents" and tail[2] == "test":
            store = self._store()
            try:
                result = studio_providers.test_agent(
                    store, tail[1], deep=bool(body.get("deep")),
                    model=str(body.get("model") or ""))
                self._json({"ok": True, "check": result})
            finally:
                store.close()
            return

        if (method == "PUT" and len(tail) == 3 and tail[0] == "agents"
                and tail[2] == "extensions"):
            store = self._store()
            try:
                result = studio_agents.update_agent_extensions(
                    store, tail[1], skills=body.get("skills"),
                    mcp=body.get("mcp"), mcp_managed=body.get("mcp_managed"),
                    plugins=body.get("plugins"))
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if (method == "PUT" and len(tail) == 3 and tail[0] == "agents"
                and tail[2] == "memory"):
            store = self._store()
            try:
                result = studio_agents.save_agent_memory(
                    store, tail[1], name=str(body.get("name") or ""),
                    content=str(body.get("content") or ""))
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if method in ("PATCH", "DELETE") and len(tail) == 2 and tail[0] == "agents":
            store = self._store()
            try:
                if method == "DELETE":
                    result = studio_agents.delete_agent(store, tail[1])
                    self._json({"ok": True, **result})
                else:
                    agent = studio_agents.update_agent(store, tail[1], **body)
                    self._json({"ok": True, "agent": agent})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "agents" and tail[2] == "tasks":
            store = self._store()
            try:
                result = studio_agents.start_task(
                    store, tail[1], str(body.get("task") or ""))
                agent_manager.start_agent(tail[1])
                self._json({"ok": True, **result}, status=201)
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "agents" and tail[2] == "new-session":
            store = self._store()
            try:
                agent = studio_agents.new_session(store, tail[1])
                self._json({"ok": True, "agent": agent})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "runs" and tail[2] == "retry":
            store = self._store()
            try:
                result = studio_agents.retry_run(store, tail[1])
                agent_manager.start_agent(result["agent"]["id"])
                self._json({"ok": True, **result}, status=201)
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "runs" and tail[2] == "stop":
            agent_manager.stop_run(tail[1])
            store = self._store()
            try:
                self._json({"ok": True, "run": studio_store.get_agent_run(store, tail[1])})
            finally:
                store.close()
            return

        if method == "POST" and tail == ["clusters"]:
            name = str(body.get("name") or "").strip()
            workspace = str(body.get("workspace_root") or "").strip()
            if not workspace:
                workspace = str(state_dir() / "workspace" / f"cluster-{secrets.token_hex(3)}")
            store = self._store()
            try:
                detail = studio_commands.create_cluster(
                    store, name=name, objective=str(body.get("objective") or ""),
                    workspace_root=workspace, lead=dict(body.get("lead") or {}),
                    roles=list(body.get("roles") or []),
                    policy=dict(body.get("policy") or {}))
                self._json({"ok": True, "cluster": detail}, status=201)
            finally:
                store.close()
            return

        if method == "PATCH" and len(tail) == 2 and tail[0] == "roles":
            store = self._store()
            try:
                role = studio_commands.update_role(store, tail[1], **body)
                self._json({"ok": True, "role": role})
            finally:
                store.close()
            return

        if (method == "POST" and len(tail) == 3 and tail[0] == "clusters"
                and tail[2] == "roles"):
            store = self._store()
            try:
                role = studio_commands.add_role(
                    store, tail[1], agent_id=str(body.get("agent_id") or ""),
                    name=str(body.get("name") or ""),
                    role_prompt=str(body.get("role_prompt") or ""),
                    **{k: v for k, v in body.items()
                       if k not in ("agent_id", "name", "role_prompt")})
                self._json({"ok": True, "role": role}, status=201)
            finally:
                store.close()
            return

        if method == "DELETE" and len(tail) == 2 and tail[0] == "roles":
            store = self._store()
            try:
                result = studio_commands.delete_role(store, tail[1])
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "clusters":
            cluster_id, action = tail[1], tail[2]
            if action == "start":
                self._json({"ok": True, **manager.start(cluster_id)})
                return
            if action == "pause":
                self._json({"ok": True, **manager.pause(
                    cluster_id, stop_processes=bool(body.get("stop_processes")))})
                return
            if action == "resume":
                self._json({"ok": True, **manager.resume(cluster_id)})
                return
            if action == "stop":
                self._json({"ok": True, **manager.stop(cluster_id)})
                return
            self._fail("not_found", status=404)
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "roles":
            role_id, action = tail[1], tail[2]
            if action == "start":
                self._json({"ok": True, **manager.nudge(role_id)})
                return
            if action == "stop":
                self._json({"ok": True, **manager.stop_role(role_id)})
                return
            self._fail("not_found", status=404)
            return

        if method == "POST" and tail == ["instructions"]:
            store = self._store()
            try:
                result = studio_commands.send_instruction(
                    store, str(body.get("cluster_id") or ""),
                    str(body.get("text") or ""),
                    role_id=body.get("role_id") or None)
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if method == "POST" and tail == ["messages"]:
            store = self._store()
            try:
                cluster_id = str(body.get("cluster_id") or "")
                cluster = studio_store.get_cluster(store, cluster_id)
                if cluster is None:
                    raise StudioError("cluster_not_found", [cluster_id])
                message = base.post_message(
                    store, project_id=cluster["project_id"], sender="local-operator",
                    text=str(body.get("text") or ""), sender_kind="member")
                self._json({"ok": True, "message": message})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "tasks" and tail[2] == "review":
            store = self._store()
            try:
                result = studio_commands.review_task(
                    store, tail[1], str(body.get("verdict") or "APPROVED"),
                    str(body.get("summary") or "本地控制台复核"))
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        if method == "POST" and len(tail) == 3 and tail[0] == "artifacts" and tail[2] == "review":
            store = self._store()
            try:
                result = studio_commands.review_artifact(
                    store, tail[1], str(body.get("verdict") or "APPROVED"),
                    str(body.get("summary") or "本地控制台复核"))
                self._json({"ok": True, **result})
            finally:
                store.close()
            return

        self._fail("not_found", status=404)


def build_server(*, port: int = 0, state_path: str | Path | None = None,
                 token: str | None = None,
                 skills_home: str | Path | None = None,
                 ) -> tuple[ThreadingHTTPServer, SupervisorManager, str, int]:
    """构造但不启动；测试与 `serve` 共用，避免两套初始化。"""

    store = open_store(state_path)
    try:
        studio_store.ensure_schema(store)
    finally:
        store.close()

    manager = SupervisorManager(state_path)
    actual_token = token or secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), _Handler)
    actual = httpd.server_address[1]
    httpd.state_path = str(state_path) if state_path else None  # type: ignore[attr-defined]
    httpd.skills_home = str(skills_home) if skills_home else None  # type: ignore[attr-defined]
    httpd.token = actual_token  # type: ignore[attr-defined]
    httpd.manager = manager  # type: ignore[attr-defined]
    httpd.agent_manager = AgentRuntimeManager(state_path)  # type: ignore[attr-defined]
    httpd.server_port = actual  # type: ignore[attr-defined]
    return httpd, manager, actual_token, actual


def serve(*, port: int = 0, open_browser: bool = True,
          state_path: str | Path | None = None) -> None:
    info_path = (Path(state_path).expanduser().resolve().parent if state_path else state_dir()) / "studio.json"
    existing_token = ""
    try:
        existing = json.loads(info_path.read_text(encoding="utf-8"))
        if existing.get("contract") == "studio-v1":
            existing_token = str(existing.get("token") or "")
    except (OSError, ValueError):
        existing_token = ""
    httpd, manager, token, actual = build_server(
        port=port, state_path=state_path, token=existing_token or None)

    url = f"http://127.0.0.1:{actual}/?token={token}"
    info = {"contract": "studio-v1", "port": actual, "url": url, "token": token,
            "pid": os.getpid(), "state_path": str(state_path) if state_path else str(state_dir() / "state.db")}
    try:
        info_path.parent.mkdir(parents=True, exist_ok=True)
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(info_path, 0o600)
    except OSError:
        pass
    print("synapforge studio（本地控制台）", flush=True)
    print(f"  {url}", flush=True)
    print(f"  状态库：{state_path or state_dir() / 'state.db'}", flush=True)
    print("  关闭：Ctrl-C", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        manager.shutdown()
        getattr(httpd, "agent_manager").shutdown()
        httpd.server_close()
        try:
            if info_path.exists() and json.loads(info_path.read_text(encoding="utf-8")).get("token") == token:
                info_path.unlink()
        except (OSError, ValueError):
            pass
