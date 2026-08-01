"""Default-off Paseo Runtime Adapter V1 control-plane subject.

This module deliberately has no Paseo client or delivery path.  It reuses the
existing reference JSON-RPC peer and stdio serving boundary until a later slice
proves exact Paseo lifecycle and delivery semantics.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping

from llm_collab.runtime_adapter_reference import (
    AdapterIdentity,
    ReferenceAdapter,
    _load_json_frame,
    serve,
)
from llm_collab.runtime_adapter_requests import METHOD_CANCEL, METHOD_DELIVER, METHOD_RECONCILE


@dataclass(frozen=True)
class PaseoAdapterIdentity(AdapterIdentity):
    """Stable test/default identity; deployment facts remain manifest-owned."""

    adapter_id: str = "paseo_cli_v1"
    adapter_revision: str = "paseo_cli_v1_rev1"
    manifest_id: str = "paseo_manifest_v1"
    manifest_revision: str = "paseo_manifest_v1_rev1"
    endpoint_id: str = "endpoint_paseo_v1"
    workspace_id: str = "ws_paseo_v1"
    capability_set_id: str = "caps_paseo_v1"
    capability_set_revision: str = "paseo_caps_v1"

    def endpoint(self) -> Mapping[str, Any]:
        endpoint = dict(super().endpoint())
        endpoint["agent_id"] = "agent_paseo_managed"
        return endpoint

    def initialize_result(self) -> Mapping[str, Any]:
        result = dict(super().initialize_result())
        result["capability_set"] = {
            **result["capability_set"],
            "capabilities": tuple(
                {"capability": capability, "quality": "unsupported"}
                for capability in (
                    "runtime.deliver",
                    "runtime.cancel",
                    "runtime.reconcile",
                )
            ),
        }
        return result


class PaseoAdapter(ReferenceAdapter):
    """JSON-RPC control plane with every mutating runtime method disabled."""

    def __init__(self, *, identity: PaseoAdapterIdentity | None = None) -> None:
        super().__init__(identity=identity or PaseoAdapterIdentity())

    def handle_text(self, raw: str) -> str | bytes | None:
        """Reject a valid cancel request before the reference subject validates it."""

        try:
            frame = _load_json_frame(raw)
        except Exception:
            return super().handle_text(raw)
        if (
            self._initialized
            and not self._shutdown
            and isinstance(frame, dict)
            and set(frame) == {"jsonrpc", "id", "method", "params"}
            and frame.get("method") == METHOD_CANCEL
        ):
            return self._error(frame["id"], "CAPABILITY_NOT_DECLARED")
        return super().handle_text(raw)

    def _handle_deliver(self, request_id: Any, params: Mapping[str, Any]) -> str:
        return self._error(request_id, "CAPABILITY_NOT_DECLARED")

    def _handle_reconcile(self, request_id: Any, params: Mapping[str, Any]) -> str:
        return self._error(request_id, "CAPABILITY_NOT_DECLARED")


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Default-off Paseo Runtime Adapter V1")
    parser.parse_args(argv)
    return serve(
        adapter=PaseoAdapter(),
        stdin=stdin if stdin is not None else sys.stdin.buffer,
        stdout=stdout if stdout is not None else sys.stdout.buffer,
        stderr=stderr if stderr is not None else sys.stderr.buffer,
    )


if __name__ == "__main__":
    raise SystemExit(main())
