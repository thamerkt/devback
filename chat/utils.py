import json

def parse_n8n_response(content):
    """
    Parses n8n raw response into a standardized list of message dicts.
    Supports:
    - Simple text messages
    - Nested content (products, pages, categories)
    - Non-JSON or 'json ' prefixed strings
    Returns a list of dicts: [{"message": "...", "type": "...", "content": [...]}, ...]
    """
    # Remove leading 'json ' if present
    if isinstance(content, str) and content.lower().startswith("json "):
        content = content[5:]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Non-JSON content -> wrap as a single written message
        return [{"message": str(content), "type": "written"}]

    items = data if isinstance(data, list) else [data]

    def process_item(item):
        if isinstance(item, list):
            for sub in item:
                yield from process_item(sub)
        elif isinstance(item, dict):
            msg_type = item.get("type", "written")
            msg_text = item.get("message") or item.get("text") or ""
            if not isinstance(msg_text, str):
                msg_text = str(msg_text)

            result = {"message": msg_text, "type": msg_type}

            # Handle nested content if present
            nested = item.get("content") or item.get("output")
            if nested:
                if isinstance(nested, list):
                    processed_content = []
                    for c in nested:
                        # Preserve structured objects
                        if isinstance(c, dict) and c.get("type") in ["product", "page", "category"]:
                            processed_content.append(c)
                        else:
                            # Recursively process any text messages
                            processed_content.extend(list(process_item(c)))
                    result["content"] = processed_content
                else:
                    # Wrap single non-list content
                    result["content"] = list(process_item([nested]))

            yield result
        else:
            # If item is not a dict or list, wrap as text message
            yield {"message": str(item), "type": "written"}

    return list(process_item(items))
