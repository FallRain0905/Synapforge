from __future__ import annotations

import json
import unittest
from pathlib import Path

from packages.agent_protocol import GATEWAY_COMMAND_TYPES, SESSION_IPC_MESSAGE_TYPES


class ProtocolSchemaTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def read_schema(self, relative_path: str) -> dict[str, object]:
        path = self.ROOT / relative_path
        return json.loads(path.read_text(encoding="utf-8"))

    def test_shared_protocol_schemas_are_valid_json_and_have_expected_roots(self) -> None:
        session = self.read_schema("packages/agent_protocol/session.schema.json")
        gateway = self.read_schema("packages/agent_protocol/gateway.schema.json")
        domain = self.read_schema("packages/contracts/domain.schema.json")

        self.assertEqual(session["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(gateway["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(domain["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            session["$defs"]["SessionIpcRequest"]["properties"]["message_type"]["enum"],
            list(SESSION_IPC_MESSAGE_TYPES),
        )
        self.assertEqual(gateway["$defs"]["GatewayEnvelope"]["properties"]["message_type"]["type"], "string")
        self.assertTrue(GATEWAY_COMMAND_TYPES)
        for name in ("SessionPeer", "SessionRunContext", "SessionRunStartPayload", "TerminalSize", "SessionLifecycleTransition", "SessionIpcRequest", "SessionIpcResponse", "SessionIpcEvent"):
            self.assertIn(name, session["$defs"])
        for name in ("GatewayEnvelope", "AgentHeartbeat", "GatewayEventAck", "GatewayReplayRequest", "GatewayCommandResult", "AgentEventPayload"):
            self.assertIn(name, gateway["$defs"])
        for name in ("Task", "Handoff", "Artifact", "Run", "Event", "GatewayEnvelope"):
            self.assertIn(name, domain["$defs"])


if __name__ == "__main__":
    unittest.main()
