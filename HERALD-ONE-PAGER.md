# Herald: Automated Upstream Intelligence for Confidential Computing

## Executive Summary

Herald is a lightweight automation tool that monitors upstream open-source repositories and delivers AI-curated intelligence digests directly to Microsoft Teams. It eliminates manual tracking effort, reduces the risk of missing critical upstream changes, and ensures engineering teams maintain continuous situational awareness of the open-source ecosystem they depend on.

Beyond simple aggregation, Herald applies AI analysis that surfaces insights a human reviewer scanning commit logs would be unlikely to catch -- cross-cutting patterns across repositories, non-obvious relevance of dependency changes to AMD's platform, and emerging competitive dynamics in the upstream contributor landscape.

---

## The Business Problem

AMD's confidential computing platform depends on a growing ecosystem of upstream open-source projects -- container runtimes, attestation frameworks, guest components, and kernel subsystems. These projects produce hundreds of commits, pull requests, and issues per week across multiple repositories.

Today, staying current requires engineers to manually review GitHub activity, identify what is relevant to AMD's platform, and relay findings to the broader team. This approach has four significant costs:

1. **Engineer time** -- Senior engineers spend cycles on monitoring instead of development

2. **Latency** -- Critical changes (security patches, breaking API changes, deprecations) may go unnoticed for days or weeks

3. **Knowledge silos** -- Upstream awareness concentrates in whoever happened to check recently, rather than being distributed across the team

4. **Missed influence windows** -- Upstream decisions that directly affect AMD's platform are made in pull request discussions, issue threads, and design proposals. These conversations have a limited window of activity before decisions are finalized and code is merged. Without systematic monitoring, AMD misses opportunities to:
   - **Shape API and architecture decisions** in attestation frameworks, key management interfaces, and guest component designs while they are still open for input
   - **Advocate for SEV-SNP optimizations** in performance-sensitive code paths before alternative approaches are locked in
   - **Provide review feedback on PRs** that affect AMD hardware compatibility, ensuring correctness before changes reach production consumers
   - **Surface AMD-specific use cases** during requirements discussions, preventing designs that inadvertently exclude or disadvantage AMD's platform
   - **Build upstream credibility** through consistent, timely engagement -- contributor reputation compounds over time, and sporadic participation carries less weight than sustained presence

   The cost of missing these windows is not just the engineering effort to work around unfavorable upstream decisions after the fact. It is the strategic cost of ceding influence over the platform AMD's confidential computing story depends on.

---

## The Solution

Herald automates the full upstream monitoring workflow:

**Collect** -- Fetches commits, pull requests, issues, and releases from configured GitHub repositories via the GitHub REST API.

**Analyze** -- Structures the raw activity data and submits it to Claude with AMD-specific team context, priorities, and focus areas embedded in the prompt.

**Prioritize** -- The AI model ranks and summarizes findings by relevance to AMD SEV-SNP, attestation workflows, key management, and other configured priority areas. Items with no relevance to the team are excluded.

**Deliver** -- Produces a concise digest (3-7 bullet executive summary with recommended actions per repository) and posts it as an Adaptive Card to the team's Microsoft Teams channel. A detailed raw report is archived alongside every run for reference and audit purposes.

---

## What AI Finds That Humans Miss

The core value of Herald is not just saving time on reading -- it is surfacing insights that a manual review of commit logs and PR titles would not reliably produce. Examples from actual Herald runs:

- **Hidden AMD relevance in generic changes** -- A PR titled "Use Key-Value Storage and Policy Engine for RVPS/AS/KBS Configuration" (#1190) appears to be routine infrastructure cleanup. Herald identified that this change directly alters how attestation policies -- including those governing AMD SEV-SNP verification -- are configured and managed, and flagged it as high priority for review.

- **Competitive positioning signals** -- Herald detected that NVIDIA PPCIE attestation support (#1234) was merged into the Trustee project, expanding the hardware ecosystem Trustee supports alongside SEV-SNP. A human scanning commit subjects ("nvidia: add PPCIE support") may not register the competitive implication. Herald flagged it as ecosystem context that AMD should monitor.

- **Cross-concern pattern recognition** -- Herald correlated a default policy revert (#1264) with the broader unified configuration overhaul (#1190) and identified that the upcoming discussion about default resource policy represents a window where AMD should ensure SEV-SNP attestation requirements are represented -- before a new default is locked in.

- **Downstream risk in dependency updates** -- A routine Dependabot PR bumping `sha2` from 0.10.9 to 0.11.0 was flagged because the cryptographic hashing library change could affect attestation evidence verification paths. A human triaging Dependabot noise would likely approve without considering AMD-specific implications.

- **Security supply chain connections** -- Herald linked a `jsonwebtoken` vulnerability fix (#1257) to its impact on `kbs_protocol` and `kms` components, identifying that the security exposure extended to the key brokering stack used in confidential container attestation -- not just the token library itself.

These are not hypothetical scenarios. They are drawn from Herald's analysis of the `confidential-containers/trustee` repository over a 14-day window. The tool consistently identifies connections and implications that require domain expertise to recognize -- expertise that is encoded in the team context configuration and applied uniformly on every run.

### Additional AI-Driven Capabilities

| Capability | Value to AMD |
|---|---|
| **Feature opportunity detection** | Identifies open issues and design discussions where AMD could contribute SEV-SNP-specific enhancements, attestation optimizations, or hardware-aware implementations -- opportunities that may not reference AMD explicitly but are directly relevant to AMD's platform |
| **Customer interest signal correlation** | When issues reference performance requirements, enterprise deployment patterns, or security certifications, Herald can surface these as indicators of what downstream consumers and partners value most -- informing AMD's upstream contribution priorities |
| **Contributor pattern analysis** | Detects shifts in who is driving commits and design decisions. If a competitor increases their contribution velocity in a strategically important subsystem, Herald flags it -- a signal that would be invisible to anyone not tracking commit authorship trends manually |
| **Breaking change impact assessment** | Evaluates dependency bumps and API changes against the team's technology stack, distinguishing between benign version increments and changes that will require adaptation in AMD's integration or testing pipelines |
| **Cross-repository correlation** | When monitoring multiple upstream projects, Herald can identify when a change in one repository (e.g., guest-components) has implications for another (e.g., trustee), surfacing integration risks that single-repo monitoring would miss |

---

## Cost and ROI

### Operating Cost

Herald's operating cost has been validated against production runs. Measured token usage from actual digests:

| Run | Input (prompt) | Output (summary) | Estimated Cost |
|---|---|---|---|
| OCC-team, trustee, 14 days | ~3,000 tokens | ~1,500 tokens | ~$0.03 |
| CLI-repos, guest-components, 14 days | ~1,500 tokens | ~1,000 tokens | ~$0.02 |

*Costs based on Claude Sonnet at $3/MTok input, $15/MTok output.*

| Component | Cost |
|---|---|
| Infrastructure | None -- runs as a single Python script via cron job or manual invocation |
| GitHub API | Free (public repositories; token required for higher rate limits) |
| Claude CLI usage | $0.02-0.05 per repository per run (measured) |
| Maintenance | Minimal -- single-file application, no external service dependencies |

**Estimated annual operating cost: $50-150** (daily runs across 5-10 repositories). Even at aggressive scale (50 repositories, daily runs), annual cost remains under $500.

### Value Recovery

| Category | Conservative Estimate |
|---|---|
| Engineer time recovered | 2-4 hours/week currently spent on manual upstream review, per engineer actively tracking repositories |
| Incident risk reduction | Early detection of breaking changes and security patches reduces likelihood of reactive firefighting and production incidents |
| Knowledge distribution | Shifts upstream awareness from individual contributors to the full team via automated Teams delivery |
| Response time improvement | Reduces mean time from upstream change to team awareness from days/weeks to < 24 hours |

### Opportunity Cost Avoided

| Scenario | Cost of Inaction |
|---|---|
| Missed architecture input | An upstream attestation API ships with a design that requires AMD to maintain an out-of-tree workaround indefinitely. Carrying a fork or compatibility shim costs ongoing engineering effort and creates fragility with every upstream release. |
| Missed feature opportunity | An upstream project opens an RFC for a performance-sensitive attestation path. AMD could contribute an SEV-SNP-optimized implementation, positioning AMD hardware as the reference platform. Without visibility, the window closes and a generic implementation becomes the default. |
| Late security response | A vulnerability affecting SEV-SNP guests is patched upstream but not surfaced to the team for weeks. Customers and partners discover the gap before AMD does. |
| Ceded design influence | A competitor contributes the reference implementation for a key interface. Their hardware assumptions become the default. AMD's platform requires additional integration effort on every subsequent release. |
| Missed customer signal | Downstream consumers open issues requesting features or performance characteristics that align with SEV-SNP capabilities. Without systematic monitoring, AMD does not learn what customers are asking for until the request is either fulfilled by a competitor or abandoned. |
| Foregone contributor standing | Inconsistent upstream engagement means AMD's input carries less weight in design discussions. Teams that show up consistently get more influence over project direction. |

Each of these scenarios has occurred in open-source ecosystems where platform vendors did not maintain systematic upstream monitoring. The question is not whether these windows exist, but whether the team is reliably aware of them while they are still open.

At a fully loaded engineering cost of $150/hour, recovering even 2 hours/week across 3 engineers yields approximately **$45,000/year** in redirected engineering capacity -- against an operating cost under $150. The opportunity cost of a single missed upstream design decision that requires a sustained workaround can exceed this figure on its own.

---

## Operational Characteristics

- **Resilience** -- Graceful degradation at every layer. GitHub rate limits trigger stale-cache fallback. Claude CLI failures fall back to formatted raw data. The tool never fails silently.
- **Configurability** -- Repositories, time windows, activity types, team context, and delivery targets are all defined in a single JSON configuration file.
- **Security** -- No credentials stored in code. GitHub token passed via environment variable. No data leaves the network beyond GitHub API calls and Claude CLI invocations.
- **Auditability** -- Every run produces a timestamped detailed report preserving the full raw activity data that informed the AI summary.
- **Zero infrastructure footprint** -- No servers, databases, or SaaS subscriptions. Runs from any workstation or CI/CD pipeline with Python and Claude CLI installed.

---

## Strategic Value

Herald positions the team to:

- **Find what humans would miss** -- AI analysis consistently surfaces non-obvious relevance, cross-repository implications, and competitive signals that manual commit-log scanning does not reliably catch
- **Detect AMD feature opportunities** -- Automatically identifies open design discussions, RFCs, and implementation gaps where AMD could contribute SEV-SNP-optimized solutions and establish AMD hardware as the upstream reference platform
- **Track what customers care about** -- Surfaces downstream issue reports, feature requests, and deployment patterns that indicate market demand, informing where AMD's upstream contributions will have the highest customer impact
- **Close the influence gap** -- Systematic visibility into upstream discussions enables AMD to participate in design decisions while they are still being made, rather than reacting to finalized implementations
- **Monitor competitive dynamics** -- Detects shifts in contributor activity and design ownership that signal where competitors are investing their upstream engineering effort
- **Respond faster to upstream security disclosures** affecting AMD confidential computing components
- **Identify contribution opportunities** before competitors shape project direction -- the team that shows up first with a well-informed review or proposal sets the terms of the conversation
- **Convert monitoring into engagement** -- Each digest's recommended actions section surfaces specific PRs, issues, and discussions where AMD input would be valuable, lowering the barrier from "we should contribute more" to "here is where to contribute today"
- **Track ecosystem momentum** around SEV-SNP adoption, attestation standards, and confidential container frameworks
- **Scale monitoring** to additional repositories as the confidential computing ecosystem grows, without proportional increase in engineer effort

---

## Current Status and Next Steps

Herald is operational and currently configured to monitor the `confidential-containers/trustee` repository with automated delivery to the OCC Team's Microsoft Teams channel. Expanding coverage to additional repositories requires only a configuration change.

**Potential enhancements under consideration:**
- Scheduled daily/weekly runs via CI/CD pipeline automation
- Expanded repository coverage across the full confidential computing stack
- Contribution opportunity scoring -- flagging open PRs and issues where AMD input would have the highest strategic impact
- Customer signal extraction -- identifying downstream adoption patterns and feature demand from issue activity
- Competitor contribution tracking -- trend analysis of commit authorship across strategically important subsystems
- Historical trend analysis across digest runs
- Customizable alert thresholds for high-priority upstream activity
