"""Direct call to the pre-1.0 OpenAI SDK's ChatCompletion resource --
removed in openai>=1.0 (Nov 2023 rewrite). Expected NOT to be found by the
scanner: openai.ChatCompletion.create is a two-level attribute chain
(module.Resource.method), and impact_analysis.py's matcher only resolves
one-level module.symbol(...) calls -- see the real-world battery report's
documented finding on this.
"""

import openai


def ask(prompt: str) -> str:
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message["content"]
