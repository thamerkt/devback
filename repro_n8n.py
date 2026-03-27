import json
import ast

def parse_n8n_response(content):
    if not isinstance(content, str):
        content = str(content)
    if content.lower().startswith("json "):
        content = content[5:]
    content = content.strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            return [{"message": content, "type": "written"}]

    items = data if isinstance(data, list) else [data]

    def process_item(item):
        if isinstance(item, list):
            for sub in item:
                yield from process_item(sub)
        elif isinstance(item, dict):
            # NEW: If item ONLY has 'output' and it's a dict, promote it
            if "output" in item and len(item) == 1 and isinstance(item["output"], dict):
                yield from process_item(item["output"])
                return

            msg_type = item.get("type", "written")
            msg_text = item.get("message") or item.get("text") or ""
            
            nested = item.get("content") or item.get("output")
            
            if not msg_text and isinstance(nested, str):
                msg_text = nested
                nested = None

            if not isinstance(msg_text, str):
                msg_text = str(msg_text)

            result = {"message": msg_text, "type": msg_type}

            if nested:
                if isinstance(nested, list):
                    processed_content = []
                    for c in nested:
                        if isinstance(c, dict) and c.get("type") in ["product", "page", "category"]:
                            processed_content.append(c)
                        else:
                            processed_content.extend(list(process_item(c)))
                    result["content"] = processed_content
                elif isinstance(nested, dict):
                    if nested.get("type") in ["product", "page", "category"]:
                        result["content"] = [nested]
                    else:
                        sub_messages = list(process_item(nested))
                        if sub_messages:
                            if not result["message"] and sub_messages[0].get("message"):
                                result["message"] = sub_messages[0]["message"]
                                if len(sub_messages) > 1:
                                    result["content"] = sub_messages[1:]
                            else:
                                result["content"] = sub_messages
            yield result
        else:
            yield {"message": str(item), "type": "written"}

    return list(process_item(items))

# Test with user's input
user_input = """
[
  {
    "output": {
      "message": "Hey there! 👋 Browsing our collection?",
      "type": "written"
    }
  }
]
"""
print("Result:", parse_n8n_response(user_input))
