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
        with self.assertRaises(json.JSONDecodeError):
            parse_n8n_response(input_data)

    def test_empty_list(self):
        input_data = "[]"
        expected = []
        self.assertEqual(parse_n8n_response(input_data), expected)

if __name__ == '__main__':
    unittest.main()
