package cli

// judgeApplyUsage is the --help block for `ams-store judge-apply`, blueprint section
// 1.1 and section 13 Q1. Hub-only: the Codex call itself stays in the Python chain, and
// this verb consumes the plan file that call produces.
const judgeApplyUsage = `usage: ams-store judge-apply --plan <file> --store <dir> [--dry-run]
                            [--max-migrations 5]

Apply a judge plan to one store under every apply-guard: strict decrease, anchor
retention, the seal, the line round-trip, migration write-then-verify, the blast cap
and protected-set overflow. Hub-only.

  --plan <file>          the plan file to apply
  --store <dir>          the store (memory directory) to apply it to
  --dry-run              report what would be applied; write nothing
  --max-migrations <n>   cap the migrations one run may perform (default 5)

Exit: 0 applied or nothing to do, 2 bad invocation, 4 lock held.`

func judgeApplyCommand() command {
	return command{
		Name:    "judge-apply",
		Summary: "apply a judge plan under every apply-guard (hub-only)",
		Usage:   judgeApplyUsage,
		Run:     notImplemented("judge-apply"),
	}
}
