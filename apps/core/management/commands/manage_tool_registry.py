from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.services.tool_registry import ToolRegistryService


class Command(BaseCommand):
    help = "Inspect tool definitions and tool execution requests."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tool", type=str, help="Show one tool definition by name.")
        parser.add_argument("--requests", action="store_true", help="List recent tool execution requests.")
        parser.add_argument("--status", type=str, help="Optional request status filter.")
        parser.add_argument("--submit", type=str, help="Submit a tool request by tool name.")
        parser.add_argument("--inputs", type=str, help="JSON object of requested tool inputs.")
        parser.add_argument("--confirm", type=int, help="Confirm a pending tool request by ID.")
        parser.add_argument("--reject", type=int, help="Reject a pending tool request by ID.")
        parser.add_argument("--execute", type=int, help="Execute an approved tool request by ID.")
        parser.add_argument("--reason", type=str, help="Optional rejection reason.")

    def handle(self, *args, **options) -> None:
        service = ToolRegistryService()

        if options["confirm"]:
            request = service.confirm_request(options["confirm"])
            self.stdout.write(self.style.SUCCESS(f"Confirmed tool request #{request.id} for '{request.tool.name}'."))
            return

        if options["reject"]:
            request = service.reject_request(options["reject"], reason=options.get("reason"))
            self.stdout.write(self.style.WARNING(f"Rejected tool request #{request.id} for '{request.tool.name}'."))
            return

        if options["execute"]:
            request = service.execute_request(options["execute"])
            self.stdout.write(self.style.SUCCESS(f"Executed tool request #{request.id} for '{request.tool.name}'."))
            self.stdout.write(json.dumps(request.metadata.get("execution_result", {}), indent=2, sort_keys=True))
            return

        if options["submit"]:
            requested_inputs = self._parse_inputs(options.get("inputs"))
            request = service.submit_request(tool_name=options["submit"], requested_inputs=requested_inputs)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Submitted tool request #{request.id} for '{request.tool.name}' with status '{request.status}'."
                )
            )
            self.stdout.write(f"Decision: {request.decision_message}")
            return

        if options["requests"]:
            for request in service.list_requests(status=options.get("status")):
                self.stdout.write(
                    f"#{request.id} {request.status} | tool={request.tool.name} | confirm={request.requires_confirmation}"
                )
            return

        if options["tool"]:
            tool = service.list_enabled_tools()
            matching = [item for item in tool if item.name == options["tool"]]
            if not matching:
                raise CommandError(f"Enabled tool not found: {options['tool']}")
            self.stdout.write(self._format_tool_detail(matching[0]))
            return

        for tool in ToolRegistryService().list_enabled_tools():
            self.stdout.write(
                f"{tool.name} | confirm={tool.requires_confirmation} | destructive={tool.is_destructive} | external={tool.is_external}"
            )

    def _parse_inputs(self, raw_inputs: str | None) -> dict:
        if not raw_inputs:
            return {}
        try:
            payload = json.loads(raw_inputs)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON for --inputs: {exc}") from exc
        if not isinstance(payload, dict):
            raise CommandError("--inputs must decode to a JSON object.")
        return payload

    def _format_tool_detail(self, tool) -> str:
        return "\n".join(
            [
                f"name: {tool.name}",
                f"enabled: {tool.is_enabled}",
                f"requires_confirmation: {tool.requires_confirmation}",
                f"is_destructive: {tool.is_destructive}",
                f"is_external: {tool.is_external}",
                f"allowed_inputs: {tool.allowed_inputs}",
                f"forbidden_inputs: {tool.forbidden_inputs}",
                f"audit_requirements: {tool.audit_requirements}",
                f"safe_roots: {tool.safe_roots}",
            ]
        )
