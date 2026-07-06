# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic>=0.40"]
# ///
import json
import os
from pathlib import Path

import anthropic

inputs_dir = Path(json.loads(os.environ["TRAP_MANIFEST"])["inputs_dir"])
question = (inputs_dir / "question.txt").read_text().strip()

client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=256,
    messages=[{"role": "user", "content": question}],
)
print(message.content[0].text)
