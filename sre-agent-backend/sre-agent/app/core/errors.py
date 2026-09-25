"""Fatal execution signals shared without coupling Workflow to persistence."""


class TaskOwnershipLostError(RuntimeError):
    """Stop the agent; this error must never become an ordinary tool observation."""
