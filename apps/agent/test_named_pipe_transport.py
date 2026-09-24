from __future__ import annotations

import os
import threading
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from packages.agent_protocol import SessionIpcRequest, SessionIpcResponse, SessionPeer
from session_worker import SessionWorkerBroker
from named_pipe_transport import (
    FrameError,
    JsonFrameCodec,
    NamedPipeClient,
    NamedPipeError,
    NamedPipeUnavailable,
    NamedPipeServer,
    PipePeerPolicy,
    current_user_sid,
    process_session_id,
)


class JsonFrameCodecTests(unittest.TestCase):
    def test_round_trip_and_rejects_malformed_frames(self) -> None:
        payload = {"message": "测试", "sequence": 1}
        encoded = JsonFrameCodec.encode(payload)
        self.assertEqual(JsonFrameCodec.decode(encoded), payload)

        with self.assertRaisesRegex(FrameError, "pipe_frame_size_invalid"):
            JsonFrameCodec.decode((2**32 - 1).to_bytes(4, "little"))
        with self.assertRaisesRegex(FrameError, "pipe_frame_length_mismatch"):
            JsonFrameCodec.decode(encoded[:-1])
        with self.assertRaisesRegex(FrameError, "pipe_frame_json_invalid"):
            JsonFrameCodec.decode((3).to_bytes(4, "little") + b"bad")

    def test_frame_limit_is_enforced(self) -> None:
        with self.assertRaisesRegex(FrameError, "pipe_frame_size_invalid"):
            JsonFrameCodec.encode({"value": "x" * 20}, max_frame_bytes=16)

    def test_client_rejects_non_positive_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "pipe_connect_timeout_invalid"):
            NamedPipeClient("MathAgentPlatform-test").connect(timeout_seconds=0)


@unittest.skipUnless(os.name == "nt", "Windows Named Pipe tests require Windows")
class NamedPipeWindowsTests(unittest.TestCase):
    def _request(self, message_type: str, peer: SessionPeer, *, request_id: str, idempotency_key: str, payload: dict | None = None) -> SessionIpcRequest:
        return SessionIpcRequest(
            request_id=request_id,
            idempotency_key=idempotency_key,
            message_type=message_type,
            peer=peer,
            worker_id="worker-pipe-001",
            user_session_id=None,
            sent_at=datetime.now(UTC),
            payload=payload or {},
        )

    def test_named_pipe_round_trip_authenticates_process_and_supports_multiple_requests(self) -> None:
        sid = current_user_sid()
        pipe_name = f"MathAgentPlatform-{uuid4().hex}"
        policy = PipePeerPolicy(
            peer_id="worker-pipe-001",
            peer_kind="user_session_worker",
            allowed_sid=sid,
            allowed_session_ids=(process_session_id(os.getpid()),),
        )
        server = NamedPipeServer(pipe_name, policy)
        broker = SessionWorkerBroker()
        broker.register_worker(
            "worker-pipe-001",
            "session-pipe-001",
            sid,
            supported_execution_modes=("USER_SESSION",),
            capabilities=("session.run",),
        )
        errors: list[BaseException] = []

        def handler(request: SessionIpcRequest, peer: SessionPeer) -> SessionIpcResponse:
            self.assertEqual(peer.user_sid.casefold(), sid.casefold())
            self.assertEqual(peer.process_id, os.getpid())
            self.assertEqual(peer.windows_session_id, process_session_id(os.getpid()))
            return broker.handle(request, peer)

        def serve() -> None:
            try:
                server.serve_once(handler)
            except BaseException as error:  # Re-raise in the test thread below.
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = NamedPipeClient(pipe_name)
        connection = client.connect(timeout_seconds=5)
        try:
            peer = SessionPeer(
                peer_id="worker-pipe-001",
                peer_kind="user_session_worker",
                process_id=os.getpid(),
                windows_session_id=process_session_id(os.getpid()),
                user_sid=sid,
            )
            first = connection
            first.send_model(self._request("session.hello", peer, request_id="pipe-request-001", idempotency_key="pipe-idempotency-001"))
            response = first.receive_response()
            self.assertEqual(response.status, "COMPLETED")
            first.send_model(self._request("session.status", peer, request_id="pipe-request-002", idempotency_key="pipe-idempotency-002", payload={"session_state": "logged_in"}))
            response = first.receive_response()
            self.assertEqual(response.status, "COMPLETED")
        finally:
            connection.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_pipe_policy_accepts_multiple_trusted_sids(self) -> None:
        policy = PipePeerPolicy(
            peer_id="service-pipe-001",
            peer_kind="machine_service",
            allowed_sids=("S-1-5-18", current_user_sid()),
            allowed_session_ids=(0, process_session_id(os.getpid())),
        )
        self.assertEqual(len(policy.trusted_sids), 2)
        self.assertEqual(len(policy.allowed_session_ids), 2)

    def test_named_pipe_rejects_client_outside_allowed_session(self) -> None:
        sid = current_user_sid()
        actual_session = process_session_id(os.getpid())
        disallowed_session = actual_session + 1
        pipe_name = f"MathAgentPlatform-{uuid4().hex}"
        server = NamedPipeServer(
            pipe_name,
            PipePeerPolicy(
                peer_id="worker-pipe-002",
                peer_kind="user_session_worker",
                allowed_sid=sid,
                allowed_session_ids=(disallowed_session,),
            ),
        )
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                server.serve_once(lambda _request, _peer: self.fail("unauthorized_peer_reached_handler"))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            connection = NamedPipeClient(pipe_name).connect(timeout_seconds=5)
        except NamedPipeError as error:
            self.assertIn("win32_error_233", str(error))
        else:
            connection.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn(
            str(errors[0]),
            {"pipe_peer_windows_session_mismatch", "ConnectNamedPipe: win32_error_232"},
        )


@unittest.skipUnless(os.name != "nt", "Non-Windows guard is covered on non-Windows hosts")
class NamedPipeNonWindowsTests(unittest.TestCase):
    def test_transport_is_explicitly_unavailable(self) -> None:
        with self.assertRaises(NamedPipeUnavailable):
            NamedPipeClient("MathAgentPlatform-test")


if __name__ == "__main__":
    unittest.main()
