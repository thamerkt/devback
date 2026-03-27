import json
import unittest
from chat.utils import parse_n8n_response

class TestN8nParsing(unittest.TestCase):
    def test_list_format(self):
        input_data = json.dumps([{"message": "Hello", "type": "written"}])
        expected = [{"message": "Hello", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_single_dict_format(self):
        input_data = json.dumps({"message": "Hello", "type": "written"})
        expected = [{"message": "Hello", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_missing_type(self):
        input_data = json.dumps({"message": "Hello"})
        expected = [{"message": "Hello", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_alternative_keys(self):
        input_data = json.dumps({"text": "Hello"})
        expected = [{"message": "Hello", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)
        
        input_data = json.dumps({"content": "Hello"})
        self.assertEqual(parse_n8n_response(input_data), expected)

        input_data = json.dumps({"output": "Hello"})
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_plain_text(self):
        input_data = "Just a string"
        expected = [{"message": "Just a string", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_python_repr(self):
        # Test single-quoted string (Python representation)
        input_data = "[{'message': 'Hello there!', 'type': 'written'}]"
        expected = [{"message": "Hello there!", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_empty_list(self):
        input_data = "[]"
        expected = []
        self.assertEqual(parse_n8n_response(input_data), expected)

    def test_nested_output_format(self):
        # The format reported by the user
        input_data = json.dumps([
            {
                "output": {
                    "message": "Hey there! 👋 Browsing our collection?",
                    "type": "written"
                }
            }
        ])
        expected = [{"message": "Hey there! 👋 Browsing our collection?", "type": "written"}]
        self.assertEqual(parse_n8n_response(input_data), expected)

if __name__ == '__main__':
    unittest.main()
