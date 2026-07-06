import json
import sys

data = json.load(sys.stdin)

if "message" not in data:
    print("error: missing 'message' field", file=sys.stderr)
    sys.exit(1)

print(data["message"])
