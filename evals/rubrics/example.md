# Rubric: example

Use this format for any LLM-as-judge scorer.

**Task:** Judge whether the OUTPUT correctly answers the INPUT.

**Score 1 (pass)** if all of:
- The output addresses what was asked.
- Every factual claim is supported by the provided context.

**Score 0 (fail)** otherwise.

**Output format:** first a short `reasoning` paragraph, then `score: 0` or `score: 1`.

Reasoning is required so judge failures are debuggable.
