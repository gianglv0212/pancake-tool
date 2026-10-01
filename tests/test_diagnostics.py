import io
import logging
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from bot import send
from diagnostics import SafeFormatter, register_secret, log_http_error


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.handler = logging.StreamHandler(self.output)
        self.handler.setFormatter(SafeFormatter('%(levelname)s %(message)s'))
        self.logger = logging.getLogger('pancake')
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def test_http_error_body_visible_token_hidden(self):
        token = 'secret-test-123/abc'
        register_secret(token)
        error = HTTPError('https://pages.fm/?page_access_token=' + token, 400, 'Bad request', {},
                          io.BytesIO(b'{"error":"invalid content_ids","page_access_token":"secret-test-123/abc"}'))
        log_http_error(self.logger, 'photo=2/3 upload', error)
        log = self.output.getvalue()
        self.assertIn('HTTP=400', log)
        self.assertIn('invalid content_ids', log)
        self.assertIn('photo=2/3', log)
        self.assertNotIn(token, log)

    def test_send_network_traceback_redacted(self):
        token = 'credential-long-456'
        with patch('bot.urlopen', side_effect=OSError('network failed token=' + token)):
            self.assertEqual(send('p1', 'c1', token, {'action':'reply_inbox','content_ids':['img1']}), 'unknown')
        log = self.output.getvalue()
        self.assertIn('Traceback', log)
        self.assertIn('img1', log)
        self.assertIn('conversation=c1', log)
        self.assertNotIn(token, log)

    def test_rotating_utf8_file(self):
        from logging.handlers import RotatingFileHandler
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bot.log'
            handler = RotatingFileHandler(path, maxBytes=5000, backupCount=1, encoding='utf-8')
            handler.setFormatter(SafeFormatter('%(message)s'))
            handler.emit(logging.LogRecord('test', logging.INFO, '', 1, 'Lỗi ảnh access_token=abc123', (), None))
            handler.close()
            saved = path.read_text(encoding='utf-8')
            self.assertIn('Lỗi ảnh', saved)
            self.assertNotIn('abc123', saved)
