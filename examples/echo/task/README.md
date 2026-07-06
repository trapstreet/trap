# Echo Task

Your solution receives a JSON object via stdin and must echo the `message` field to stdout.

## Input

A JSON object delivered via stdin:

```json
{"message": "Hello, World!"}
```

## Output

The value of `message`, printed to stdout (with a trailing newline).

```
Hello, World!
```

## Error handling

If the `message` field is missing, print an error to stderr and exit with code 1.

## Evaluation

Each case declares one or more assertions in its `expected.json` file.
The judge (`judge.py`) checks each declared assertion against the solution's stdout and exit code:


| Assertion     | Passes when                                          |
|---------------|------------------------------------------------------|
| `contains`    | stdout contains the substring (case-insensitive)     |
| `not_contains`| stdout does not contain the substring                |
| `exact`       | stdout matches exactly (after strip)                 |
| `regex`       | stdout matches the regex pattern                     |
| `exit_code`   | process exit code equals the value                   |

All declared assertions must pass for a case to pass.
