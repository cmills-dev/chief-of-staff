"""A small permission policy: two hard denies, a draft-only rule and a git role table.

The only hard denies by default are payments and file deletion. Outbound messages are
draft-only: an assistant may save a draft and may never send. Git rules give one role
(the Chief by default) the right to commit and merge with --no-ff into a configured main
branch. Other roles keep a review-first path. Push stays denied for every role.

The policy reads an optional `.chief/policy.json`. Every key is optional.
"""

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from . import atomic

PAYMENT = "payment"
DELETION = "file-deletion"

DEFAULT_PATTERNS = {
    PAYMENT: [
        r"\b(pay|payment|purchase|buy|checkout|subscribe|subscription|invoice|refund|charge|top[- ]?up)\b",
        r"\btransfer\s+(money|funds)\b",
        r"\bsk_live_[A-Za-z0-9]+",
    ],
    DELETION: [
        r"(^|[\s;&|])(rm|rmdir|unlink|shred|srm|trash)(\s|$)",
        r"\bgit\s+(rm|clean)\b",
        r"\bgit\s+branch\s+-D\b",
        r"\bgit\s+worktree\s+remove\b",
        r"\bfind\b.*\s-delete\b",
        r"\bfind\b.*-exec\s+rm\b",
        r"\bcurl\b.*-X\s*DELETE\b",
    ],
}

OUTBOUND_VERBS = ("send", "reply", "forward", "post", "publish")
READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "blame", "ls-files"}
CHIEF_GIT = {"add", "commit", "merge", "branch", "worktree", "apply", "am"}
ALWAYS_DENIED_GIT = ("push", "rebase", "reset", "clean", "filter-branch", "update-ref")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    category: str = ""
    reason: str = ""

    def as_dict(self):
        return dict(allowed=self.allowed, category=self.category, reason=self.reason)


class Policy:
    def __init__(
        self,
        patterns=None,
        extra_patterns=None,
        outbound="draft-only",
        main_branch="main",
        merge_roles=("chief",),
    ):
        if outbound not in ("draft-only", "allow"):
            raise ValueError("outbound must be 'draft-only' or 'allow'")
        base = patterns if patterns is not None else DEFAULT_PATTERNS
        merged = {k: list(v) for k, v in base.items()}
        for key, extra in (extra_patterns or {}).items():
            merged.setdefault(key, []).extend(extra)
        self.patterns = {
            k: [re.compile(p, re.I) for p in v] for k, v in merged.items()
        }
        self.outbound = outbound
        self.main_branch = main_branch
        self.merge_roles = tuple(merge_roles)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("policy must be a JSON object")
        return cls(
            patterns=data.get("patterns"),
            extra_patterns=data.get("extra_patterns"),
            outbound=data.get("outbound", "draft-only"),
            main_branch=data.get("main_branch", "main"),
            merge_roles=data.get("merge_roles", ("chief",)),
        )

    @classmethod
    def load(cls, root):
        path = atomic.state_dir(root) / "policy.json"
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(atomic.read_text(path)))

    def check_text(self, text):
        """Deny a command or request that matches a hard-deny category."""
        for category, regexes in self.patterns.items():
            for rx in regexes:
                if rx.search(text):
                    return Verdict(
                        False, category, "Hard deny: " + category + " is not permitted"
                    )
        return Verdict(True)

    def check_outbound(self, verb):
        """Messages leave as drafts. Sending is denied while outbound is draft-only."""
        verb = verb.lower()
        if verb == "draft":
            return Verdict(True)
        if self.outbound == "draft-only" and verb in OUTBOUND_VERBS:
            return Verdict(
                False, "draft-only", "Outbound messages are saved as drafts, never sent"
            )
        return Verdict(True)

    def check_git(self, role, command, branch=""):
        """Decide one git command line for a role.

        `branch` is the branch the command runs on. Push is denied for every role.
        """
        words = shlex.split(command) if isinstance(command, str) else list(command)
        if not words or words[0] != "git":
            return Verdict(True)
        args, i = words[1:], 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-c", "-C") else 1
        args = args[i:]
        if not args:
            return Verdict(True)
        sub, rest = args[0], args[1:]
        if sub == "push":
            return Verdict(False, "push", "Push is denied for every role")
        if sub in ALWAYS_DENIED_GIT:
            return Verdict(False, "git-" + sub, "git " + sub + " is denied for every role")
        if sub == "commit" and any(a.startswith("--amend") for a in rest):
            return Verdict(False, "git-amend", "Amending a commit is denied for every role")
        if sub == "merge" and "--squash" in rest:
            return Verdict(False, "git-squash", "Squash merges are denied for every role")
        if sub == "rm" or (sub == "branch" and {"-d", "-D", "--delete"} & set(rest)):
            return Verdict(False, DELETION, "Hard deny: file deletion is not permitted")
        if sub == "worktree":
            if rest[:1] == ["remove"]:
                return Verdict(False, DELETION, "Hard deny: file deletion is not permitted")
            if rest[:1] not in (["add"], ["list"]):
                return Verdict(False, "git-worktree", "Only worktree add and list are permitted")
        if sub in READ_ONLY_GIT:
            return Verdict(True)
        on_main = branch == self.main_branch
        if role in self.merge_roles:
            if sub not in CHIEF_GIT:
                return Verdict(False, "git-scope", "git " + sub + " is outside this role")
            if sub == "merge" and "--no-ff" not in rest:
                return Verdict(False, "git-merge", "Merges use --no-ff")
            return Verdict(True)
        if sub in ("merge", "am", "apply", "commit") and on_main:
            return Verdict(
                False,
                "review-first",
                "Changes reach " + self.main_branch + " through review for this role",
            )
        return Verdict(True)
