"""Who performed an action — web operator, CLI operator, or the system itself.

An audit row is only worth keeping if it says *who*. Until now the dashboard let
the caller name itself: a POST body field decided what the audit trail recorded.
That is not an identity, it is a suggestion.

This module defines the one shape every operator reference takes, so an auditor
reading the ledger months later can tell a logged-in operator from a command on
the box from an automatic decision. The prefix is deliberately part of the
stored string rather than a separate column: the references are already written
by a dozen call sites into an existing text column, and a value that carries its
own origin cannot be separated from it by a later migration or an export.

The separator is a colon, which the rights-record grammar in
``core/anchor_rights.py`` rejects. That is intentional and not an oversight: a
rights approval is a legal statement about a named person and must never be
satisfiable by a dashboard session.
"""

KIND_WEB = "web"
KIND_CLI = "cli"
KIND_SYSTEM = "system"

_KINDS = (KIND_WEB, KIND_CLI, KIND_SYSTEM)

# The audit columns truncate at 64 characters; do it here so the stored value is
# the value this module promises, not a silently shortened one.
_MAX_LABEL = 48


def _label(raw: str, fallback: str) -> str:
    label = " ".join(str(raw or "").split())[:_MAX_LABEL].strip()
    return label or fallback


def web_operator(username: str) -> str:
    """The stable reference for an authenticated dashboard session."""
    return f"{KIND_WEB}:{_label(username, 'unknown')}"


def cli_operator(label: str) -> str:
    """The reference for a human running a command on the machine."""
    return f"{KIND_CLI}:{_label(label, 'unknown')}"


def system_operator(label: str) -> str:
    """The reference for an automatic decision with no human behind it."""
    return f"{KIND_SYSTEM}:{_label(label, 'automatic')}"


def identity_kind(operator_ref: str) -> str:
    """``web``, ``cli``, ``system`` — or ``unknown`` for a legacy row.

    Rows written before this module existed carry a bare label. They are not
    retro-fitted: pretending to know the origin of an old decision would be
    worse than admitting it is unknown.
    """
    ref = str(operator_ref or "")
    prefix, sep, rest = ref.partition(":")
    if sep and prefix in _KINDS and rest.strip():
        return prefix
    return "unknown"


def identity_label(operator_ref: str) -> str:
    """The part a human reads, without the origin prefix."""
    ref = str(operator_ref or "")
    prefix, sep, rest = ref.partition(":")
    if sep and prefix in _KINDS:
        return rest
    return ref
