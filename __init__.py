"""Native Hermes user plugin; registration never performs network I/O."""
from .schemas import CONSEQUENTIAL, MUTATIONS, SCHEMAS, TOOLSETS
from .handlers import handler


def register(ctx):
    for name, schema in SCHEMAS.items():
        kwargs = {"check_fn": lambda: ctx.get_config("enable_mutations", default=False) is True} if name in MUTATIONS else {}
        ctx.register_tool(name=name, toolset=TOOLSETS[name], schema=schema, handler=handler(ctx, name), **kwargs)
    ctx.register_hook("pre_tool_call", approval_hook)


def approval_hook(tool_name="", args=None, **kwargs):
    if tool_name in CONSEQUENTIAL:
        # Use static wording: descriptions, recipients, identifiers and remote text
        # never become approval prompt instructions. Hermes presents the actual call.
        messages = {"jmap_send_draft": "Submit the selected draft for email delivery?",
                    "jmap_delete_email": "Permanently delete the selected email from every mailbox? This cannot be undone through this plugin.",
                    "jmap_create_event": "Create the requested calendar event?",
                    "jmap_update_event": "Change the selected calendar event or occurrence?",
                    "jmap_delete_event": "Delete the selected calendar event or occurrence?"}
        scheduling = isinstance(args, dict) and args.get("send_scheduling_messages") is True
        return {"action": "approve", "message": messages[tool_name] + (" This also requests scheduling messages to participants." if scheduling else ""), "rule_key": tool_name}
