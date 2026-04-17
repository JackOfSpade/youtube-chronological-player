import json

def test_json_parse():
    text = "```json\n{\n  \"video_ids\": [\"abc\", \"def\"]\n}\n```"
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    print(json.loads(text))

test_json_parse()
