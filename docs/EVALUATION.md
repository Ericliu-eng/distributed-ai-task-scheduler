# Routing cost-quality evaluation

The repository includes a fixed 50-case dataset (25 easy, 25 hard) with arithmetic, structured extraction, classification, and executable code graders. Generated code runs in a separate isolated Python process with restricted builtins and a 5-second timeout; that contains hangs and crashes but is not a security sandbox. Every prompt is self-contained: hard cases carry their own incident timelines, contract histories, ledgers, and specifications, so prompt length reflects real content. The evaluation compares an all-large baseline with multiple routing thresholds.

## Claude results

```bash
python -m pip install -r requirements-dev.txt
ANTHROPIC_API_KEY=... python -m bench.evaluate --provider anthropic \
  --output bench/results/routing-evaluation-claude.json
```

The small tier is `claude-haiku-4-5` and the large tier is `claude-sonnet-5-5`. Each response, with its token usage and cost, is appended to [`bench/results/claude-responses.jsonl`](../bench/results/claude-responses.jsonl) as it arrives. Interrupted runs resume, and rerunning the command re-grades the recorded responses without new API calls. The full run made 100 calls and cost **$0.05**.

| Strategy / difficulty threshold | Correct | Quality retention | Cost saving | Small / large |
| --- | ---: | ---: | ---: | ---: |
| All large (Sonnet 5.5) | 50 / 50 | 100% | — | 0 / 50 |
| All small (Haiku 4.5) | 49 / 50 | 98% | 44.14% | 50 / 0 |
| 0.25 | 50 / 50 | 100% | 11.99% | 27 / 23 |
| 0.40 | 50 / 50 | 100% | 24.47% | 35 / 15 |
| **0.55 (default)** | **50 / 50** | **100%** | **35.00%** | 48 / 2 |
| 0.70 | 49 / 50 | 98% | 44.14% | 50 / 0 |

At the default threshold, only the two longest prompts reach Sonnet. One of them is the multi-step inventory ledger (`numeric-009`), the only case Haiku answered incorrectly, so routing keeps full baseline quality at 35% lower measured cost. Cost savings are capped at about 44% because Sonnet 5.5's list price is only 2x Haiku 4.5's. The full report is [`bench/results/routing-evaluation-claude.json`](../bench/results/routing-evaluation-claude.json).

## Limits

- One recorded sample per case and tier at default sampling settings. A rerun can differ.
- The dataset separates the tiers on only one case. It shows the routing and cost mechanics, not a general quality benchmark; harder cases are needed to measure a real quality gap.
- Graders extract answers instead of requiring exact formatting. String fields compare case-insensitively, a classification label counts on its own first or last line, and fenced code or JSON is unwrapped. These rules were finalized after reviewing the first run, where both models lost points on formatting and on one ambiguous answer key (`keyboard` vs. the source text's `keyboards`). The rules apply equally to both tiers, and the raw responses are committed for review.

## Deterministic fixture adapter

```bash
python -m bench.evaluate --output bench/results/routing-evaluation.json
```

Without an API key, the evaluation uses canned responses stored in the dataset. It keeps routing, grading, and cost accounting deterministic for CI. It is not evidence of model quality. Results are stored in [`bench/results/routing-evaluation.json`](../bench/results/routing-evaluation.json).
