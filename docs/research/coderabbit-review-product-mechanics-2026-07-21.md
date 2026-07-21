# CodeRabbit review product mechanics: first-party evidence brief

Date: 2026-07-21

Scope: current pull-request review mechanics, evidence grounding, comment and summary structure, issue workflows, notifications, interaction, learning, evaluation, and large-PR reviewability. This brief uses only CodeRabbit's official documentation and blog, plus public GitHub output from the official CodeRabbit app on a CodeRabbit-owned repository.

## Executive finding

The product is not one large reviewer prompt. Its documented advantage is a review system around several models: repository sandboxing, semantic and graph-based context selection, tool findings, issue and PR history, candidate generation, verification, incremental updates, feedback memory, and multiple human-facing surfaces. The most important product lesson is to make every finding traceable and actionable while keeping the full review navigable.

CodeRabbit's current "cohorts" are primarily semantic units for human comprehension, not token-sized batches. It groups related files and hunks into independent cohorts, orders foundational layers before dependants, and anchors each layer to exact diff ranges. It does not document that poorly structured code must already align with cohort boundaries. Treating excessive cross-cohort coupling or an incoherent PR as a finding would be a useful Grug extension, but that is an inference, not a copied behavior.

## 1. Review engine and context selection

### Verified behavior

- Each review runs with a full repository clone in an isolated sandbox. The documented architecture combines 50+ static analyzers, linters, and SAST tools with agentic repository exploration, specialized review and verification agents, memory, and external integrations. [Architecture](https://docs.coderabbit.ai/overview/architecture)
- The context engine constructs a code graph across files and, where configured, repositories. It draws from linked issues, architecture standards, custom instructions, conventions, past PRs, learnings, MCP sources, tool findings, and current web documentation, then filters that material for the change. [Explainable reviews](https://www.coderabbit.ai/blog/explainable-reviews-coderabbit-review-context-engine)
- CodeRabbit says it targets an approximately 1:1 ratio of reviewed code to supporting context. It rebuilds a dependency graph per review, uses symbol definitions to enrich comments, indexes PR titles/descriptions/commit ranges and issues for intent, and runs generated verification scripts to suppress low-value or hallucinated comments. [Context engineering](https://www.coderabbit.ai/blog/context-engineering-ai-code-reviews)
- Tasks are routed by complexity to different model classes. A review agent proposes comments; verification agents filter them against guidelines, configuration, and team preferences. Model, prompt, and context changes are evaluated against recall, precision, latency, and cost. [Explainable reviews](https://www.coderabbit.ai/blog/explainable-reviews-coderabbit-review-context-engine)
- Reviews are automatic and incremental. A new PR gets a full analysis; later pushes focus on new commits and retain prior comment context. The default auto-pause after five reviewed commits prevents repeated near-duplicate work on active branches. A user can explicitly request incremental or fresh full review. [Automatic review controls](https://docs.coderabbit.ai/configuration/auto-review), [review commands](https://docs.coderabbit.ai/guides/commands)

### Inference for Grug

The reusable pattern is a retrieval-and-verification pipeline:

1. Build a semantic change map before asking for findings.
2. Retrieve context per change unit and per candidate finding, not once for the whole PR.
3. Preserve provenance for every retrieved item.
4. Generate candidates with specialist reviewers.
5. Verify candidates with tools or an independent reasoner.
6. Publish only grounded, deduplicated findings.

The first-party sources do not disclose the ranking algorithm, exact agent count, prompts, confidence threshold, or model allocation. Those details should not be represented as known.

## 2. Evidence and source grounding

### Verified behavior

CodeRabbit exposes several kinds of provenance rather than relying on an unexplained assertion:

- Findings are native inline comments anchored to a specific changed line. In Change Stack, reviewers can also comment against the exact range covered by a range summary. [Change Stack](https://docs.coderabbit.ai/pr-reviews/change-stack)
- Code definitions selected from the dependency graph can be shown in a review comment. Tool-originated findings identify the contributing linter or analyzer. [Context engineering](https://www.coderabbit.ai/blog/context-engineering-ai-code-reviews)
- Web queries may fetch current public documentation and release information. A configured Review details section states when web search was used. The docs do not promise that every external factual sentence carries a conventional footnote URL. [Web Search](https://docs.coderabbit.ai/knowledge-base/web-search)
- Related issues and past PRs are surfaced as links. Linked issue requirements are compared with the PR and can produce an inline finding when a requirement is absent. [PR walkthroughs](https://docs.coderabbit.ai/pr-reviews/walkthroughs), [linked issue validation](https://docs.coderabbit.ai/issues/pr-validation)
- Live official app output shows a collapsible "Analysis chain" containing shell commands used to investigate a finding, a named tool section, and an exact path/line explanation. The review-level details disclose configuration, run ID, commit range, selected files, linked repositories, CI failures, and additional tool context. [Official app review on `coderabbitai/skills#20`](https://github.com/coderabbitai/skills/pull/20#pullrequestreview-4658822863), [its inline finding](https://github.com/coderabbitai/skills/pull/20#discussion_r3548051480)

### Recommended finding evidence contract for Grug (inference)

Every published finding should retain machine-readable provenance even if the GitHub rendering is concise:

```text
finding_id
head_sha
path + start_line + end_line
category + severity + effort + confidence
claim
failure scenario / consequence
recommended action
code evidence[]        # changed range, definition, call site, test, config
intent evidence[]      # issue criterion, PR description, prior decision
tool evidence[]        # command/tool, version, result digest
external sources[]     # title, canonical URL, retrieval time, supporting claim
verification[]         # verifier, result, counterexample attempted
```

The rendered comment should show the claim and consequence immediately, while provenance, analysis, and agent instructions can be collapsed. This produces auditability without turning the default view into a transcript.

## 3. Comment anatomy and interaction design

### Verified behavior

- Comments are categorized by domain, including functional correctness, security/privacy, stability/availability, data integrity/integration, performance/scalability, and maintainability/code quality. They also carry severity from Critical through Info. [Review overview](https://docs.coderabbit.ai/guides/code-review-overview)
- The current Change Stack interface adds orthogonal labels: finding type (potential issue, refactor, or nitpick) and estimated fix effort (such as quick win or heavy lift). [Code Peek and Chat Agent release](https://www.coderabbit.ai/blog/code-search-peek-in-coderabbit-review)
- A live official inline comment leads with category, severity, and effort; provides a collapsed analysis chain; states a concise imperative title and explains the concrete conflict; exposes contributing tools; and includes a collapsed, copyable "Prompt for AI Agents" that instructs an agent to verify first, fix only valid issues, keep the change minimal, and validate. [Official inline finding](https://github.com/coderabbitai/skills/pull/20#discussion_r3548051480)
- The review body counts actionable comments, aggregates all fix prompts into one agent prompt, offers Autofix actions, and keeps run metadata and additional/non-actionable comments collapsed. [Official app review](https://github.com/coderabbitai/skills/pull/20#pullrequestreview-4658822863)
- Autofix collects structured instructions only from unresolved CodeRabbit threads, applies changes with a coding agent, performs repository setup/build verification, and delivers either a commit on the branch or a stacked PR. [Autofix](https://docs.coderabbit.ai/finishing-touches/autofix)
- Any review comment can start a conversation. Users can challenge it, ask for evidence or alternatives, request code, change review behavior, pause/resume, resolve, approve, or request incremental/full review. The chat retains PR-wide context. [Interactive chat](https://docs.coderabbit.ai/guide/chat), [review commands](https://docs.coderabbit.ai/guides/commands)

### Comment pattern worth adopting

The live artifact suggests a strong default order:

1. Category, severity, and effort chips.
2. One-sentence imperative headline.
3. Two or three sentences connecting code to a concrete failure or violated intent.
4. A minimal correction, preferably with an applicable suggestion.
5. Collapsed evidence and analysis provenance.
6. Collapsed agent-ready prompt with verify-before-fix language.
7. Stable fingerprint for deduplication across pushes.

This is partly an inference from one official public artifact; the hidden fingerprint format and internal schema are not documented as a public contract.

## 4. Summaries, walkthroughs, and complex PRs

### Verified behavior

- A plain-language summary is written into the PR description and regenerated on incremental pushes. It groups changes by type and accepts free-form formatting instructions. It may instead be placed at the top of the walkthrough. [PR summaries](https://docs.coderabbit.ai/pr-reviews/summaries)
- A separate, collapsible walkthrough comment can contain a grouped changed-files summary, sequence diagrams, review-effort estimate, related issues, linked-issue assessment, related PRs, suggested labels and reviewers, status, and optional personality content. Related file changes are consolidated instead of listed mechanically. [PR walkthroughs](https://docs.coderabbit.ai/pr-reviews/walkthroughs)
- Change Stack reorganizes a PR into a small number of independent semantic cohorts. Each cohort contains ordered layers: foundational data shapes and contracts precede consumers, call sites, and tests. Every layer maps to specific ranges and has a range summary; diagrams appear only when a visual model is useful. [Change Stack](https://docs.coderabbit.ai/pr-reviews/change-stack)
- The interface presents cohort/layer navigation, scoped diff, per-range context/comments/chat, semantic diff, symbol definition/usage lookup, reviewer progress, prior snapshots, and stale-view protection. It disables commenting while a new snapshot is generated so comments do not attach to obsolete code. [Change Stack](https://docs.coderabbit.ai/pr-reviews/change-stack)
- CodeRabbit separately detects suspected low-quality AI-generated PRs on public GitHub repositories, reports that in the walkthrough, and can label them. It does not currently block or close them automatically. [Slop Detection](https://docs.coderabbit.ai/pr-reviews/slop-detection)

### Reviewability as a Grug product feature (inference)

The user's proposed behavior goes beyond the documented competitor feature and is sound: if a PR cannot be decomposed into coherent semantic cohorts without repeatedly duplicating context, Grug should say so. The complaint should target review risk, not the model's token limit.

Candidate reviewability signals:

- many unrelated cohorts under one PR intent;
- one hunk or file participating in several independent concerns;
- circular dependencies between layers;
- contracts, implementation, and tests that cannot be ordered coherently;
- broad changes with no linked objective or acceptance criteria;
- generated or mechanical churn dominating semantic changes;
- a cohort too large because a module has mixed responsibilities;
- high cross-cohort symbol fan-out that makes isolated verification unreliable.

The output should distinguish two levels:

- PR hygiene: "Split this PR into these independent changes" with suggested cut lines.
- Design maintainability: "This module prevents bounded review because it owns unrelated responsibilities" with the dependency evidence that proves the coupling.

Do not automatically equate a large cohort with bad code. A cohesive schema migration or protocol change may legitimately be large. The finding should require semantic evidence of mixed responsibilities or unsafe coupling.

## 5. Issues and planning

### Verified behavior

- Users can ask the bot inside a PR discussion to create a GitHub, GitLab, Jira, or Linear issue. It carries code context and discussion history into a structured issue and supports assignee, priority, and timing instructions. [Create issues](https://docs.coderabbit.ai/issues/creation)
- Issue Enrichment posts an updateable comment containing possible duplicates, related issues, related PRs, suggested assignees, and labels. Similarity is based on vectorized issue/PR representations; editing an issue re-runs enrichment. [Issue Enrichment](https://docs.coderabbit.ai/issues/enrichment)
- Linked issue validation extracts requirements from the issue title and description, compares them with the PR, and reports Addressed, Not addressed, or Unclear. Comments in the issue thread are not currently considered. It recommends a problem statement, expected solution, affected components, and explicit acceptance criteria. [Linked issue validation](https://docs.coderabbit.ai/issues/pr-validation)
- The walkthrough can show linked-issue gaps and feed that result into a pre-merge check. Built-in and natural-language custom checks have warning/error enforcement, display objective/status/explanation/resolution, and may block when request-changes workflow is enabled. [Pre-Merge Checks](https://docs.coderabbit.ai/pr-reviews/pre-merge-checks)

### Inference for Grug

A review finding should be promotable into an issue without regenerating its meaning. The issue should preserve:

- the original finding and evidence fingerprint;
- affected paths/symbols and current head SHA;
- observed behavior and impact;
- proposed acceptance criteria;
- suggested owner from history/ownership;
- relationship to the originating PR and any precedent;
- whether it is intentionally deferred or blocks merge.

The reverse path matters equally: reviews should validate the PR against issue acceptance criteria and flag unplanned scope. This turns findings, issues, implementation, and later regression checks into one traceable lifecycle.

## 6. Learnings and customization

### Verified behavior

- Natural-language feedback in PR and issue threads can become scoped learnings for a file, repository, or organization. Applicable learnings are loaded before a PR or issue comment is produced. Formal standards should remain review instructions or repository coding-guideline files. [Learnings](https://docs.coderabbit.ai/knowledge-base/learnings)
- When chat creates a learning, the response explicitly shows a collapsed "Learnings Added" section. A learning records provenance such as PR, path, and user. Teams can require admin review with delayed auto-approval. [Learnings](https://docs.coderabbit.ai/knowledge-base/learnings)
- Guidance says to learn patterns rather than one-off exceptions, store the reason as well as the preference, and prefer replies to exact inline comments because they produce more specific context. Learnings expose usage count, last-used time, creator, repository, and editable text; unused or stale records can be audited. [Learnings](https://docs.coderabbit.ai/knowledge-base/learnings)
- Scope can be local, global, or automatic. The documentation explicitly warns that global learning across diverse stacks can contaminate reviews. [Learnings](https://docs.coderabbit.ai/knowledge-base/learnings)
- Repository standards in files such as `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, and Copilot instructions are auto-detected as code guidelines. [Knowledge Base](https://docs.coderabbit.ai/knowledge-base/index)

### Inference for Grug

Feedback should not directly mutate an opaque prompt. Store a proposed learning with source thread, scope, rationale, examples, owner, confidence, approval state, usage, and measured effect. Retrieve it only when path/language/domain similarity justifies it. Periodically retire learnings that are unused, contradicted, or associated with rejected comments.

## 7. Email, digests, and ecosystem notifications

### Verified behavior

The first-party documentation found does not describe a special per-finding email format. It does document scheduled recurring reports delivered by email, Slack, Discord, or Teams. Reports can be filtered by repository, label, user, and team; include PR state, dates, reviewers, discussions, repository, and author; use daily standup, sprint, release-note, or custom prompted templates; and group by repository, label, team, or user. [Scheduled reports](https://docs.coderabbit.ai/guides/scheduled-reports)

This should not be overstated as transactional PR notification behavior. It is a configurable reporting/digest surface.

### Inference for Grug

Use the same canonical review data for GitHub, Slack/Discord, and email, but tailor density:

- PR: complete inline evidence and interactive actions.
- Slack/Discord: blockers, changes since last review, owner mentions, and commands.
- Email/digest: trend and queue view, grouped by team/repository, with deep links instead of duplicated detail.
- Issue note: durable problem statement, acceptance criteria, provenance, and ownership.

Avoid generating independent prose for each channel; otherwise verdicts and severity drift.

## 8. Proving quality and improving continuously

### Verified behavior

- CodeRabbit says its internal evaluation loop measures recall, precision, latency, and cost for model, prompt, and context changes. [Explainable reviews](https://www.coderabbit.ai/blog/explainable-reviews-coderabbit-review-context-engine)
- Its analytics treat comment acceptance as a quality signal and break acceptance down by severity and category. It also tracks review iterations, tool findings, time to first human review, time to last commit, time to merge, and per-PR complexity/comment details. The company itself cautions that throughput alone does not prove better software. [Measuring what matters](https://www.coderabbit.ai/blog/measuring-what-matters-in-the-age-of-ai-assisted-development)

### Recommended Grug scorecard (inference)

No deployment should be called "better" from a passing smoke test. Prove improvement with:

- offline recall on known human-confirmed findings;
- precision from blinded human adjudication;
- accepted/fixed/rejected rates by rule, severity, model, and evidence source;
- false-positive reason taxonomy;
- escaped-defect linkage after merge;
- incremental-review duplicate rate;
- stale or invalid line-anchor rate;
- time to first useful finding and total review latency;
- coverage completeness, including cohorts skipped by budget;
- reviewability metrics: cohort count, cross-cohort dependency density, and reviewer navigation burden;
- randomized or staged prompt/model/context experiments with versioned run IDs.

Acceptance is useful but not sufficient: authors may comply with noisy advice or ignore valid findings. Pair behavioral metrics with blinded labels and post-merge outcomes.

## 9. Practical build order for Grug

1. Make finding provenance a stable data model and expose it in collapsed GitHub sections.
2. Replace directory/size cohorts with semantic cohorts plus dependency-ordered layers; retain size budgets only as an execution constraint.
3. Add a reviewability assessor that can recommend PR splits and flag responsibility/coupling problems with evidence.
4. Generate a living summary and walkthrough from the same change graph used for review.
5. Make issue requirements first-class inputs and make every deferred finding promotable to a structured issue.
6. Add feedback-derived proposed learnings with scope, rationale, approval, usage, and effectiveness metrics.
7. Add incremental snapshots, finding fingerprints, stale-anchor protection, and explicit coverage reporting.
8. Add actionable notifications and digests from the canonical review record.
9. Continuously run the historical corpus plus live acceptance/escape analysis; gate reviewer changes on statistically credible improvement.

## Source and certainty notes

- Product mechanics labeled "verified" are supported by linked first-party material.
- Public GitHub examples are official app output in a CodeRabbit-owned repository, but one artifact is not proof that every plan, platform, profile, or review has identical rendering.
- Marketing statements about scale or quality were used only to describe the claimed evaluation system, not as independent proof of superior accuracy.
- Exact prompts, retrieval scores, verification thresholds, model names, and internal schemas remain undisclosed.
