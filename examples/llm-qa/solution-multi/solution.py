# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.0"]
# ///
import json
import os
import subprocess
from pathlib import Path

from openai import OpenAI

inputs_dir = Path(json.loads(os.environ["TRAP_MANIFEST"])["inputs_dir"])
question = (inputs_dir / "question.txt").read_text().strip()

client = OpenAI()
response = client.chat.completions.create(
    model="gpt-4o-mini",
    max_tokens=256,
    messages=[{"role": "user", "content": question}],
)
answer_openai = response.choices[0].message.content.strip()

result = subprocess.run(["claude", "-p", question], capture_output=True, text=True, check=True)
answer_cc = result.stdout.strip()

print(f"{answer_openai}\n\n---\n\n{answer_cc}")
