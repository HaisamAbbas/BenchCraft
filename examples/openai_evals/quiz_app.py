"""A tiny quiz application for the openai/evals bridge examples (Prompt 17).

CLI protocol: one JSON request on stdin (`{"case_id", "input"}`), one JSON answer on
stdout. The input is an openai/evals prompt (a string or chat messages); the app answers
the last user message from a fixed table, and deliberately gets the planet question wrong.
Set QUIZ_LOG to a file to record every request it receives.
"""

import json
import os
import sys

ANSWERS = {
    "What is 2+2?": "4",
    "What is the capital of France?": "Paris is the capital of France.",
    "What is the largest planet?": "Saturn",  # wrong on purpose
    "What is the chemical symbol for water?": "H2O",
}

request = json.load(sys.stdin)
prompt = request["input"]
last = prompt[-1]["content"] if isinstance(prompt, list) else str(prompt)
if os.environ.get("QUIZ_LOG"):
    with open(os.environ["QUIZ_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"case_id": request.get("case_id"), "input": prompt}) + "\n")
print(json.dumps({"output": ANSWERS.get(last, "I don't know.")}))
