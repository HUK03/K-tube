import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.video_id = 'abc123def45'
        self.folder = None

    def fake_download(self, command, **kwargs):
        self.command = command
        self.folder = Path(command[command.index('--output') + 1]).parent
        (self.folder / 'video.mp4').write_bytes(b'\x00\x00\x00\x18ftypmp42test')
        return subprocess.CompletedProcess(command, 0, '', '')

    def post(self):
        return self.client.post('/api/download', json={'videoId': self.video_id}, headers={'Origin': 'http://localhost:5000'})

    def test_streams_file_and_cleans_up_when_closed(self):
        with patch.object(server.subprocess, 'run', side_effect=self.fake_download), patch.object(server.shutil, 'which', return_value='/usr/bin/ffmpeg'):
            response = self.post()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'\x00\x00\x00\x18ftypmp42test')
            self.assertIn('youtube-abc123def45.mp4', response.headers['Content-Disposition'])
            self.assertIn('Content-Disposition', response.headers['Access-Control-Expose-Headers'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b', self.command)
            self.assertEqual(self.command[-1], 'https://www.youtube.com/watch?v=abc123def45')
            self.assertIn('--ignore-config', self.command)
            response.close()
            self.assertFalse(self.folder.exists())

    def test_without_ffmpeg_uses_combined_stream(self):
        with patch.object(server.subprocess, 'run', side_effect=self.fake_download), patch.object(server.shutil, 'which', return_value=None):
            response = self.post()
            self.assertEqual(response.status_code, 200)
            self.assertIn('b[ext=mp4]/b', self.command)
            response.close()

    def test_invalid_ids_cannot_launch_downloader(self):
        with patch.object(server.subprocess, 'run') as run:
            for value in ['', '../secret', 'https://example.com', 'a' * 12, None, 123]:
                response = self.client.post('/api/download', json={'videoId': value})
                self.assertEqual(response.status_code, 400)
            self.assertEqual(self.client.post('/api/download', json=[]).status_code, 400)
            run.assert_not_called()

    def test_timeout_returns_actionable_error_and_removes_files(self):
        def timeout(command, **kwargs):
            self.fake_download(command)
            raise subprocess.TimeoutExpired(command, 180)
        with patch.object(server.subprocess, 'run', side_effect=timeout):
            response = self.post()
            self.assertEqual(response.status_code, 504)
            self.assertIn('3분', response.json['error'])
            self.assertFalse(self.folder.exists())

    def test_upstream_failure_does_not_send_partial_video(self):
        def fail(command, **kwargs):
            self.fake_download(command)
            return subprocess.CompletedProcess(command, 1, '', 'Video unavailable')
        with patch.object(server.subprocess, 'run', side_effect=fail):
            response = self.post()
            self.assertEqual(response.status_code, 502)
            self.assertIn('Video unavailable', response.json['error'])
            self.assertFalse(self.folder.exists())

    def test_skipped_download_is_not_a_success(self):
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(self.post().status_code, 422)

    def test_large_file_is_rejected_and_cleaned(self):
        with patch.object(server.subprocess, 'run', side_effect=self.fake_download), patch.object(server, 'DOWNLOAD_LIMIT', 1):
            self.assertEqual(self.post().status_code, 413)
            self.assertFalse(self.folder.exists())

    def test_second_request_is_rejected_while_busy(self):
        server.download_slot.acquire()
        try:
            with patch.object(server.subprocess, 'run') as run:
                self.assertEqual(self.post().status_code, 409)
                run.assert_not_called()
        finally:
            server.download_slot.release()

    def test_health_advertises_download_support(self):
        self.assertIs(self.client.get('/api/health').json['video_download'], True)


if __name__ == '__main__':
    unittest.main()
