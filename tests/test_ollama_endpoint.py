"""A private model instance never changes other projects' Ollama settings."""
import io
import json
import os
import unittest
from unittest.mock import patch
from scripts.equivalency.engine import ollama_api_url, post_ollama, local_model


class LocalModelEndpointTests(unittest.TestCase):
    def test_default_runtime_remains_compatible(self):
        with patch.dict(os.environ):
            os.environ.pop('COURSEMATCH_OLLAMA_URL', None)
            self.assertEqual(ollama_api_url('chat'), 'http://127.0.0.1:11434/api/chat')

    def test_project_url_is_used_for_chat_and_model_identity(self):
        def response(value):
            return io.BytesIO(json.dumps(value).encode())
        with patch.dict(os.environ, {'COURSEMATCH_OLLAMA_URL':'http://127.0.0.1:11437'}):
            with patch('urllib.request.urlopen', return_value=response({'done':True})) as opener:
                self.assertTrue(post_ollama('chat', {'model':'local'}, timeout=7)['done'])
                self.assertEqual(opener.call_args.args[0].full_url, 'http://127.0.0.1:11437/api/chat')
                self.assertEqual(opener.call_args.kwargs['timeout'], 7)
            with patch('urllib.request.urlopen', return_value=response({'models':[{'name':'local','size':1,'digest':'local-digest'}]})) as opener:
                self.assertEqual(local_model('local'), 'local-digest')
                self.assertEqual(opener.call_args.args[0], 'http://127.0.0.1:11437/api/tags')

    def test_remote_credentials_queries_paths_and_invalid_ports_fail_before_network(self):
        for base in ['https://example.com','http://localhost:11437','http://127.0.0.1:80','http://127.0.0.1:65536',
                     'http://user@127.0.0.1:11437','http://127.0.0.1:11437/path','http://127.0.0.1:11437?query=1','http://127.0.0.1:11437\n']:
            with self.subTest(base=base), patch.dict(os.environ, {'COURSEMATCH_OLLAMA_URL':base}), patch('urllib.request.urlopen') as opener:
                with self.assertRaises(ValueError):
                    post_ollama('chat',{})
                opener.assert_not_called()
        with self.assertRaises(ValueError):
            ollama_api_url('../private')
