# Handling Linear tickets in this project

Read the ticket's labels first. If it has none, treat it as **feature**.

## Scope of a change

Not every ticket ends in a code change. Those that do are one pull request,
which may have several commits. In bootstrap mode nothing is submitted through
GitHub, but scope the work as if it were: one pull request's worth, split into
commits that each capture one logical change and make review easier.

## Labels

* **bug** — assume it needs triage. Reproduce it, and write a test that
  demonstrates it. If testing it would take substantial new infrastructure, say
  so on the ticket and in the commit message, then land the fix anyway. If you
  cannot reproduce it, comment with what you tried, cancel the ticket, and
  report the task complete.
* **feature** — plan it and post the plan as a comment. If it is too large for
  one pull request, split it into sub-issues, say so in a comment, and report
  the task complete without writing code; foregent dispatches the sub-issues
  separately.
* **refactor** — cleanup, performance, quality, or preparing the ground for a
  feature. Handle it as a feature.
* **design** — may be combined with feature or refactor, and **takes precedence
  over both**: produce the design, implement nothing. The project manager wants
  to agree on the approach first. Think as an architect — the big-picture scope
  of the project, the alternatives, the tradeoffs. Post the design, move the
  ticket to In Review, and report blocked on `human:design-review`.
* **investigation** — the project manager wants information, not a pull
  request, though you may write and run code to get it. Post your findings,
  move the ticket to In Review, and report blocked on `human:findings-review`.
