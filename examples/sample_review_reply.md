I read the diff and the review comment on it. The comment asks for a behaviour the
module already has, but the change itself leaves the negative-exponent case unhandled.

```json
{
  "summary": "The change is small and readable, but power() still returns 1.0 for negative exponents, and the reviewer's request is already covered by the existing code.",
  "findings": [
    {
      "kind": "verdict",
      "verdict": "disagree",
      "path": "calc/core.py",
      "line": 1,
      "author": "alice",
      "quote": "Please add a module docstring here.",
      "title": "The module already has a docstring",
      "body": "calc/core.py opens with a docstring on line 1, so the requested change would duplicate it."
    },
    {
      "kind": "finding",
      "severity": "high",
      "path": "calc/core.py",
      "line": 2,
      "title": "Negative exponents return 1.0",
      "body": "power() loops `exponent` times, so a negative exponent never enters the loop and the function returns 1.0. Recurse on the positive exponent and take the reciprocal."
    }
  ]
}
```

Nothing else in the diff needs attention.
